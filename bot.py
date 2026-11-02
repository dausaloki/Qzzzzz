"""Telegram Quiz Bot — entry point.

Exactly ONE Application and ONE polling loop are ever created (``main()``).
A file lock prevents a second copy from polling inside the same container,
and ``Conflict: terminated by other getUpdates request`` (another deployment
still running) is logged and retried by PTB instead of crashing the bot.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import signal
import sys
import threading
import time

from telegram import (
    BotCommand, ChatMember, InlineKeyboardButton as B, InlineKeyboardMarkup as M, InlineQueryResultArticle,
    InlineQueryResultsButton, InputTextMessageContent, Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    AIORateLimiter, Application, ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler,
    ChatMemberHandler, CommandHandler, ContextTypes, InlineQueryHandler, MessageHandler, PollAnswerHandler,
    TypeHandler, filters,
)

import access_control
import admin_tools
import ai_gen
import backup
import config
import database as db
import group_runner
import html_report
import keyboards as kb
import membership
import pdf_parser
import quiz_creator as creator
import quiz_engine as engine
import quiz_modes
import quiz_runner as runner
import review_pdf
import stats as st
from keyboards import esc

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)          # request URLs contain the bot token
logging.getLogger("telegram").setLevel(logging.INFO)          # PTB DEBUG logs the API URL (with token)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("quizbot")

_bot_username_cache: dict[str, object] = {}
_IST = timezone(timedelta(hours=5, minutes=30))
_notify_inflight: set[int] = set()


def _now_ist_str() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def format_first_start_notification(user, first_start_at: str) -> str:
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    full_name = " ".join(p for p in (first, last) if p) or "User"
    raw_uname = (getattr(user, "username", None) or "").strip().lstrip("@")
    uname_display = f"@{esc(raw_uname)}" if raw_uname else "No username"
    profile_link = f'<a href="tg://user?id={user.id}">{esc(full_name)}</a>'
    return (
        "🆕 <b>NEW USER STARTED THE QUIZ BOT</b>\n\n"
        f"👤 Name: {profile_link}\n\n"
        f"🔗 Username: {uname_display}\n\n"
        f"🆔 User ID: <code>{user.id}</code>\n\n"
        f"🕒 First Start: {esc(first_start_at)}"
    )


async def _retry_first_start_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    uid = data.get("user_id")
    text = data.get("text")
    ts = data.get("ts", "")
    tries = int(data.get("tries", 1))
    target = (config.SUPPORT_NOTIFY_CHAT or "").strip()
    if not uid or not text or not target:
        return
    u_row = db.get_user(uid)
    if u_row and int(u_row.get("first_start_notified") or 0):
        return
    try:
        await context.bot.send_message(
            chat_id=target,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        db.mark_first_start_notified(uid, ts)
    except Exception as exc:  # noqa: BLE001
        log.warning("Support notification retry %d to %s for user %s failed: %s", tries, target, uid, exc)
        if tries < 3 and context.application and context.application.job_queue:
            context.application.job_queue.run_once(
                _retry_first_start_job,
                when=min(30.0 * tries, 120.0),
                data={"user_id": uid, "text": text, "ts": ts, "tries": tries + 1},
                name=f"first_start_notify:{uid}",
            )


async def notify_first_start(context, user) -> bool:
    """Send a one-time notification to ``config.SUPPORT_NOTIFY_CHAT`` on a user's first ``/start``."""
    target = (config.SUPPORT_NOTIFY_CHAT or "").strip()
    if not target or user is None or getattr(user, "is_bot", False):
        return False
    uid = int(user.id)
    if uid in _notify_inflight:
        return False
    now_str = _now_ist_str()
    try:
        should_notify, first_start_at = db.prepare_first_start_notification(user, now_str)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not prepare first-start notification for user %s: %s", uid, exc)
        return False
    if not should_notify:
        return False
    text = format_first_start_notification(user, first_start_at)
    _notify_inflight.add(uid)
    try:
        await context.bot.send_message(
            chat_id=target,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        db.mark_first_start_notified(uid, first_start_at)
        app = getattr(context, "application", None)
        if app and app.job_queue:
            for job in app.job_queue.get_jobs_by_name(f"first_start_notify:{uid}"):
                job.schedule_removal()
        return True
    except Exception as exc:  # noqa: BLE001 — never crash or block /start
        log.warning("Failed to send first-start notification to %s for user %s: %s", target, uid, exc)
        app = getattr(context, "application", None)
        if app and app.job_queue and not app.job_queue.get_jobs_by_name(f"first_start_notify:{uid}"):
            app.job_queue.run_once(
                _retry_first_start_job,
                when=5.0,
                data={"user_id": uid, "text": text, "ts": first_start_at, "tries": 1},
                name=f"first_start_notify:{uid}",
            )
        return False
    finally:
        _notify_inflight.discard(uid)


async def _cache_me(bot) -> None:
    me = await bot.get_me()
    _bot_username_cache["u"] = me.username
    # inline mode is a BotFather setting; only offer inline sharing when it is on
    _bot_username_cache["inline"] = bool(getattr(me, "supports_inline_queries", False))


async def bot_username(context) -> str:
    if "u" not in _bot_username_cache:
        await _cache_me(context.bot)
    return _bot_username_cache["u"]


async def inline_enabled(context) -> bool:
    if "inline" not in _bot_username_cache:
        await _cache_me(context.bot)
    return bool(_bot_username_cache["inline"])


def is_private(update: Update) -> bool:
    return bool(update.effective_chat) and update.effective_chat.type == ChatType.PRIVATE


async def group_redirect(update: Update, context, qid: str | None = None) -> None:
    """In groups: the quiz card (▶️ play it in this group with native quiz polls,
    or 👤 start privately); without a quiz — a link to the bot's private chat."""
    username = await bot_username(context)
    quiz = db.get_quiz(qid) if qid else None
    if quiz and quiz.get("status") == "ready" and quiz.get("question_count"):
        link = engine.deep_link(username, qid)
        await update.effective_chat.send_message(group_runner.ready_text(quiz), parse_mode=ParseMode.HTML,
                                                 reply_markup=kb.group_card(qid, link))
        return
    if quiz:
        link = engine.deep_link(username, qid)
        text = f"🎯 <b>{esc(quiz['title'])}</b>\n\n⚠️ यह quiz अभी तैयार नहीं है (draft / कोई प्रश्न नहीं)।"
    else:
        link = f"https://t.me/{username}"
        text = "🤖 Quiz बनाने/खेलने के लिए bot को private chat में खोलें 👇"
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb.start_privately(link))


# ------------------------------------------------------------ join gate
async def membership_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler (group -1).  Private chats and inline queries
    are blocked until membership of the required group is VERIFIED with
    getChatMember.  Not gated: poll answers and chat_member updates (group
    quiz participants are never forced into a private chat), group messages
    (group actions check the acting user themselves) and the j:check button."""
    if not membership.enabled():
        return
    if update.poll_answer or update.chat_member or update.my_chat_member or update.poll:
        return
    user = update.effective_user
    if user is None or user.is_bot:
        return
    if update.inline_query:
        if not await membership.has_access(context.bot, user.id):
            await update.inline_query.answer(
                [], cache_time=0, is_personal=True,
                button=InlineQueryResultsButton(text="📢 पहले Group Join करें", start_parameter="join"))
            raise ApplicationHandlerStop
        return
    chat = update.effective_chat
    if chat is None or chat.type != ChatType.PRIVATE:
        return
    cq = update.callback_query
    if cq is not None and (cq.data or "").startswith("j:"):
        return
    msg = update.effective_message
    force, payload = False, ""
    is_start_cmd = bool(
        cq is None and msg is not None and msg.text and msg.text.split()[0].split("@")[0] == "/start"
    )
    if is_start_cmd:
        # every /start asks Telegram again — a user who left must join again
        force = True
        parts = msg.text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
    if await membership.has_access(context.bot, user.id, force=force):
        return
    db.upsert_user(user)
    if is_start_cmd:
        await notify_first_start(context, user)
    if payload == "join":
        payload = ""
    if cq is not None:
        await cq.answer("🔒 पहले Telegram Group Join करें।")
    await chat.send_message(membership.WELCOME_TEXT, reply_markup=membership.join_markup(payload))
    raise ApplicationHandlerStop


async def join_check(update: Update, context) -> None:
    """✅/🔄 I've Joined — the click itself proves nothing; getChatMember decides."""
    q = update.callback_query
    user = update.effective_user
    db.upsert_user(user)
    parts = (q.data or "").split(":", 2)
    payload = parts[2] if len(parts) > 2 else ""
    ok = await membership.verify_now(context.bot, user.id) if membership.enabled() else True
    if ok is True:
        await notify_first_start(context, user)
        await q.answer("✅ Verified")
        await creator.respond(update, membership.VERIFIED_TEXT, None)
        qid = engine.parse_start_payload(payload) if payload else None
        if qid and db.get_quiz(qid):
            await runner.send_ready(context.application, user, update.effective_chat.id, qid)
        else:
            await show_main_menu(update, context, edit=False)
        return
    await q.answer("❌ Membership verify नहीं हुई" if ok is False else "⚠️ अभी verify नहीं हो सका")
    text = membership.NOT_JOINED_TEXT if ok is False else membership.CHECK_FAILED_TEXT
    try:
        await creator.respond(update, text, membership.join_markup(payload, retry=True))
    except BadRequest as exc:              # same text again → "message is not modified"
        if "not modified" not in str(exc).lower():
            raise


# ------------------------------------------------------------ commands
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_user(user)
    if is_private(update):
        await notify_first_start(context, user)
    if context.args and context.args[0] == "join":      # from the inline "join first" button
        context.args = []
    qid = engine.parse_start_payload(context.args[0]) if context.args else None
    if not is_private(update):
        await group_redirect(update, context, qid)
        return
    if context.args and qid is None:
        await update.effective_chat.send_message("⚠️ Link invalid है।")
    if qid:
        if not db.get_quiz(qid):
            await update.effective_chat.send_message("❌ यह quiz मौजूद नहीं है (शायद delete हो गया)।",
                                                     reply_markup=kb.main_menu())
            return
        await runner.send_ready(context.application, user, update.effective_chat.id, qid)
        return
    await show_main_menu(update, context, edit=False)


async def show_main_menu(update: Update, context, edit: bool = True) -> None:
    user = update.effective_user
    state, _ = db.get_state(user.id)
    extra = ""
    if state and state.startswith("c_"):
        extra = "\n\n📝 <i>आपका quiz creation चालू है — जारी रखें, /done या /cancel भेजें।</i>"
    elif state and state.startswith("imp_"):
        extra = "\n\n📥 <i>Import चालू है — file/text भेजें या /cancel।</i>"
    text = (f"👋 नमस्ते <b>{esc(user.first_name or 'there')}</b>!\n\n"
            "🎯 <b>Quiz Bot</b> — Telegram के native quiz polls से quiz बनाइए, import कीजिए और खेलिए।"
            + extra)
    await creator.respond(update, text, kb.main_menu(), edit=edit)


async def help_cmd(update: Update, context) -> None:
    await update.effective_chat.send_message(kb.HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb.back_home())


async def newquiz_cmd(update: Update, context) -> None:
    if not is_private(update):
        await group_redirect(update, context)
        return
    await creator.begin_new_quiz(update, context)


async def import_cmd(update: Update, context) -> None:
    if not is_private(update):
        await group_redirect(update, context)
        return
    await creator.begin_import(update, context)


async def done_cmd(update: Update, context) -> None:
    if is_private(update):
        await creator.finalize(update, context)


async def cancel_cmd(update: Update, context) -> None:
    if is_private(update):
        await creator.cancel(update, context)


async def undo_cmd(update: Update, context) -> None:
    if is_private(update):
        await creator.undo(update, context)


async def is_group_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in (ChatMember.OWNER, ChatMember.ADMINISTRATOR)


async def stop_cmd(update: Update, context) -> None:
    if not is_private(update):
        chat = update.effective_chat
        s = db.get_active_group_session(chat.id)
        if not s:
            await chat.send_message("ℹ️ इस group में अभी कोई quiz नहीं चल रहा।")
            return
        uid = update.effective_user.id
        if s["started_by"] != uid and not await is_group_admin(context.bot, chat.id, uid):
            await chat.send_message("⛔ Quiz केवल उसे शुरू करने वाला या group admin रोक सकता है।")
            return
        if not await group_runner.stop_group_quiz(context.application, chat.id):
            await chat.send_message("ℹ️ इस group में अभी कोई quiz नहीं चल रहा।")
        return
    done = await runner.stop_quiz(context.application, update.effective_user.id)
    if not done:
        await update.effective_chat.send_message("ℹ️ अभी कोई quiz नहीं चल रहा।", reply_markup=kb.main_menu())


async def skip_cmd(update: Update, context) -> None:
    if not is_private(update):
        return
    # during quiz creation /skip skips the optional step (description / explanation)
    if await creator.skip(update, context):
        return
    res = await runner.skip_current(context.application, update.effective_user.id)
    if res == "none":
        await update.effective_chat.send_message("ℹ️ अभी कोई quiz नहीं चल रहा।")
    elif res == "busy":
        await update.effective_chat.send_message("⏳ अगला प्रश्न आ रहा है…")


async def myquizzes_cmd(update: Update, context) -> None:
    if not is_private(update):
        await group_redirect(update, context)
        return
    await show_my_quizzes(update, context, edit=False)


async def stats_cmd(update: Update, context) -> None:
    if not is_private(update):
        await group_redirect(update, context)
        return
    text, markup = st.user_overview(update.effective_user.id)
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def settings_cmd(update: Update, context) -> None:
    if not is_private(update):
        return
    await show_settings(update, edit=False)


# ------------------------------------------------------------ backup (admin)
BACKUP_HELP = (
    "🗄️ <b>Backup</b> (केवल administrator)\n"
    "/backup — status (file, records, integrity, pending changes)\n"
    "/backup now — अभी backup बनाओ\n"
    "/backup verify — file + database मिलाकर जाँच\n"
    "/backup sync — GitHub पर अभी भेजो (token हो तो)\n"
    "/backup download — quiz_backup.json file यहीं भेजो\n"
    "/backup restore — restore का dry-run (कुछ नहीं लिखा जाता)\n\n"
    "असली restore: <code>python scripts/backup_tool.py restore --apply</code>"
)


def _backup_report(res: dict) -> str:
    ok = bool(res.get("ok")) and not res.get("skipped")
    lines = ["✅ Backup पूरा" if ok else ("ℹ️ बदलाव नहीं — file वैसी ही है" if res.get("skipped") else
                                       "❌ Backup असफल")]
    if res.get("path"):
        lines.append(f"File: <code>{res['path']}</code>")
    if res.get("bytes"):
        lines.append(f"Size: {res['bytes']:,} bytes")
    counts = res.get("counts") or {}
    if counts:
        lines.append("Records: " + ", ".join(f"{k}={counts.get(k, 0)}" for k in
                                            ("quizzes", "questions", "attempts", "answers", "users")))
    if res.get("commit"):
        lines.append(f"GitHub commit: <code>{res['commit']}</code>")
    if res.get("attempts") and res["attempts"] > 1:
        lines.append(f"कोशिशें: {res['attempts']}")
    if res.get("errors"):
        lines.append("Error: " + "; ".join(str(e) for e in res["errors"][:3]))
    return "\n".join(lines)


async def backup_cmd(update: Update, context) -> None:
    """/backup [now|verify|sync|restore] — restricted to BACKUP_ADMIN_IDS."""
    chat = update.effective_chat
    if not is_private(update):
        await chat.send_message("🔒 /backup केवल private chat में (bot से personal chat) चलता है।")
        return
    uid = update.effective_user.id if update.effective_user else 0
    if not config.BACKUP_ADMIN_IDS:
        await chat.send_message(
            "🔒 Backup commands locked: Railway → Variables में <code>BACKUP_ADMIN_IDS</code> "
            f"सेट करें (आपका Telegram user id: <code>{uid}</code>).", parse_mode=ParseMode.HTML)
        return
    if uid not in config.BACKUP_ADMIN_IDS:
        log.warning("Unauthorized /backup attempt by user %s", uid)
        await chat.send_message("⛔ यह command केवल administrator के लिए है।")
        return

    action = (context.args[0].lower() if context.args else "status")
    if action in ("help", "h"):
        await chat.send_message(BACKUP_HELP, parse_mode=ParseMode.HTML)
        return
    if action in ("status", "s"):
        await chat.send_message(backup.status_text(), parse_mode=ParseMode.HTML)
        return
    if action == "now":
        await chat.send_message("⏳ Backup बन रहा है…")
        res = await asyncio.to_thread(backup.export_now, "admin", True)
        await chat.send_message(_backup_report(res), parse_mode=ParseMode.HTML)
        return
    if action == "verify":
        ok, problems, info = await asyncio.to_thread(backup.verify_file, None, True)
        text = [f"🔎 <b>Verify</b> — {'✅ ठीक' if ok else '❌ समस्या'}",
                f"File: <code>{info.get('path')}</code> ({info.get('bytes', 0):,} bytes)",
                f"Generated: {info.get('generated_at') or '—'}"]
        if info.get("quizzes") is not None:
            text.append(f"Quizzes: {info['quizzes']}")
        if problems:
            text.append("Problems: " + "; ".join(problems[:5]))
        parts = data.split(":")
        if len(parts) < 3:
            await q.answer()
            return
        action, qid = parts[1], parts[2]
        quiz = db.get_quiz(qid)
        if not quiz:
            await q.answer("❌ Quiz नहीं मिला (delete हो चुका है)।", show_alert=True)
            return
        owner = quiz["owner_id"] == user.id
        if action in ("edit", "del", "delok", "set") and not owner:
            await q.answer("⛔ यह quiz आपका नहीं है।", show_alert=True)
            return
        await q.answer()
        if action == "view":
            await creator.send_quiz_card(update, qid, edit=True)
        elif action == "run":
            await runner.send_ready(context.application, user, update.effective_chat.id, qid)
        elif action == "set":
            await creator.show_quiz_settings(update, qid)
        elif action == "share":
            await share_quiz(update, context, qid)
        elif action == "edit":
            await creator.show_edit_menu(update, qid)
        elif action == "stats":
            await creator.respond(update, st.quiz_stats_text(qid, user.id),
                                  st.quiz_stats_markup(qid), edit=False)
        elif action == "html":
            built = html_report.build_latest_html_report(user.id, quiz_id=qid, user=user)
            if not built:
                await update.effective_chat.send_message(
                    "ℹ️ आपने अभी यह quiz पूरा नहीं किया है — पहले एक attempt पूरा करें।")
                return
            html_bytes, filename, rep = built
            await update.effective_chat.send_document(
                document=html_bytes,
                filename=filename,
                caption=(
                    f"🌐 <b>Interactive HTML Result Report</b> — {esc(rep.get('quiz_title') or 'Quiz')}\n"
                    f"✅ {rep.get('correct', 0)}/{rep.get('total', 0)} · 🎯 {rep.get('accuracy', 0):.1f}%"
                ),
                parse_mode=ParseMode.HTML,
            )
        elif action == "del":
            await creator.respond(update, f"🗑 <b>{esc(quiz['title'])}</b> delete करें? "
                                          "इसके सभी प्रश्न और stats हट जाएँगे।", kb.confirm_delete(qid))
        elif action == "delok":
            db.delete_quiz(qid, user.id)
            await creator.respond(update, "🗑 Quiz delete हो गया।", kb.main_menu())
        return

    await q.answer("⌛ यह button अब valid नहीं है।")


async def group_go(update: Update, context, qid: str) -> None:
    """▶️ Start Quiz in this Group — one session per group at a time."""
    q = update.callback_query
    user = update.effective_user
    if not await membership.has_access(context.bot, user.id, allow_unverified=True):
        req_chat = membership.effective_required_chat()
        await q.answer(f"🔒 Quiz शुरू करने के लिए पहले {req_chat} Join करें।", show_alert=True)
        return
    res = await group_runner.start_group_quiz(context.application, update.effective_chat.id, qid, user)
    if res == "ok":
        await q.answer("🚀 Quiz शुरू!")
        return
    await q.answer({"busy": "⏳ इस group में पहले से एक quiz चल रहा है। पहले वह पूरा होने दें या /stop करें।",
                    "missing": "❌ Quiz नहीं मिला (delete हो चुका है)।",
                    "empty": "⚠️ इस quiz में कोई प्रश्न नहीं है।",
                    "draft": "⚠️ यह quiz अभी draft है।",
                    "denied": "⛔ इस quiz के लिए अनुमति (access) आवश्यक है।"}.get(res, "⚠️ Quiz शुरू नहीं हो सका।"), show_alert=True)


# ------------------------------------------------------------ messages
async def private_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    db.upsert_user(user)
    state, data = db.get_state(user.id)
    if state:
        if state.startswith("c_"):
            await creator.handle_creator_message(update, context, state, data)
            return
        if state.startswith("fw_"):
            await creator.handle_forward_message(update, context, state, data)
            return
        if state.startswith("e_"):
            await creator.handle_edit_message(update, context, state, data)
            return
        if state.startswith("imp_"):
            await creator.handle_import_text(update, context, state, data)
            return
        if state == "ai_wait":
            db.clear_state(user.id)
            await _run_ai_generation(update, user, msg.text or "")
            return
        if state == "st_timer":
            try:
                v = config.parse_timer(msg.text or "")
            except ValueError as exc:
                await msg.reply_text(f"⚠️ {exc}")
                return
            db.update_user_settings(user.id, default_timer=v)
            db.clear_state(user.id)
            await msg.reply_text(f"✅ Default timer: {config.timer_label(v)}")
            await show_settings(update, edit=False)
            return
        db.clear_state(user.id)
    if msg.document and creator.is_import_document(msg.document):
        db.set_state(user.id, "imp_wait", {"buffer": ""})
        await creator.handle_import_document(update, context)
        return
    item = creator.item_from_message(msg)
    if item and creator.is_question_item(item):
        # a (forwarded) quiz poll / MCQ text with no quiz open → ask which quiz
        await creator.begin_forward_import(update, item)
        return
    await msg.reply_text("👇 नीचे menu से विकल्प चुनें (PDF/Text import के लिए 📥 दबाएँ)।",
                         reply_markup=kb.main_menu())


# ------------------------------------------------------------ errors
_last_conflict_log = [0.0]


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        now = time.time()
        if now - _last_conflict_log[0] > 60:
            _last_conflict_log[0] = now
            log.warning("Telegram Conflict: another bot instance is polling with the same token. "
                        "Stop other deployments/replicas (Railway: 1 replica, no local copy). "
                        "This instance keeps retrying and will take over automatically.")
        return
    if isinstance(err, (TimedOut, NetworkError)) and not isinstance(err, BadRequest):
        log.warning("Network problem: %s", err)
        return
    if isinstance(err, Forbidden):
        log.info("Forbidden (user blocked bot?): %s", err)
        return
    if isinstance(err, BadRequest) and ("query is too old" in str(err).lower()
                                        or "message is not modified" in str(err).lower()):
        return
    log.error("Unhandled error while processing update", exc_info=err)
    if isinstance(err, Exception) and admin_tools.effective_log_group():
        try:
            await admin_tools.notify_log_group_error(context.bot, err, update)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        text = ("⚠️ Database में अस्थायी समस्या आई, कृपया दोबारा कोशिश करें।"
                if isinstance(err, db.DatabaseError) else
                "⚠️ कुछ गड़बड़ हुई। कृपया दोबारा कोशिश करें या /start भेजें।")
        try:
            await context.bot.send_message(update.effective_chat.id, text)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------ app
BOT_COMMANDS = [
    BotCommand("start", "Main menu"), BotCommand("newquiz", "नया quiz बनाएँ"),
    BotCommand("myquizzes", "मेरे quizzes"), BotCommand("import", "PDF/Text से quiz"),
    BotCommand("aiquiz", "AI से नया quiz बनाएँ"),
    BotCommand("share", "Quiz share करें"),
    BotCommand("stats", "मेरे results"), BotCommand("done", "Quiz creation पूरा करें"),
    BotCommand("undo", "आख़िरी प्रश्न हटाएँ (creation)"),
    BotCommand("skip", "Skip (प्रश्न / optional step)"), BotCommand("stop", "Quiz रोकें"),
    BotCommand("cancel", "रद्द करें"), BotCommand("help", "Help"),
    BotCommand("review", "Review PDF (पिछला quiz)"),
    BotCommand("htmlreport", "Interactive HTML Report"),
    BotCommand("backup", "Backup status (admin)"),
]


async def post_shutdown(app: Application) -> None:
    """SIGTERM / Railway redeploy: write and upload whatever is still pending."""
    try:
        res = await asyncio.to_thread(backup.shutdown_export)
        log.info("Shutdown backup: %s", {k: res.get(k) for k in ("ok", "bytes", "skipped", "errors")})
    except Exception as exc:  # noqa: BLE001 — shutdown must never fail
        log.error("Shutdown backup failed: %s", exc)


async def post_init(app: Application) -> None:
    await _cache_me(app.bot)
    log.info("Logged in as @%s (inline mode: %s)", _bot_username_cache["u"],
             "on" if _bot_username_cache["inline"] else "off")
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("set_my_commands failed: %s", exc)
    rec = await runner.recover_sessions(app)
    log.info("Session recovery: %s", rec)
    grec = await group_runner.recover_group_sessions(app)
    log.info("Group session recovery: %s", grec)
    if membership.enabled():
        log.info("Join gate: users must be members of %s (the bot must be a member/admin there)",
                 config.REQUIRED_CHAT)
    else:
        log.info("Join gate disabled (REQUIRED_CHAT empty)")
    for name, why in config.unsupported_env_settings():
        log.warning("Environment variable %s is set but IGNORED: %s", name, why)
    try:
        import pdf_extract
        ok, info = pdf_extract.ocr_status()
        if ok:
            log.info("OCR ready (%s) — tessdata: %s", pdf_extract.OCR_LANGUAGES, info)
        else:
            log.warning("OCR disabled: %s — scanned PDFs will be reported, not imported", info)
    except Exception as exc:  # noqa: BLE001 — never block startup on OCR probing
        log.warning("OCR status check failed: %s", exc)


def build_application(token: str, request=None, get_updates_request=None, rate_limit: bool = True) -> Application:
    db.init_db()
    builder: ApplicationBuilder = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown)
    if rate_limit:
        builder = builder.rate_limiter(AIORateLimiter(max_retries=3))
    if config.TELEGRAM_API_BASE:
        # e.g. a self-hosted telegram-bot-api server (or the fake server used by tests)
        builder = builder.base_url(f"{config.TELEGRAM_API_BASE}/bot").base_file_url(
            f"{config.TELEGRAM_API_BASE}/file/bot")
    if request is not None:
        builder = builder.request(request)
    if get_updates_request is not None:
        builder = builder.get_updates_request(get_updates_request)
    app = builder.build()
    if app.job_queue is None:
        raise RuntimeError("JobQueue missing: install python-telegram-bot[job-queue]")

    app.add_handler(TypeHandler(Update, membership_gate), group=-1)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("newquiz", newquiz_cmd))
    app.add_handler(CommandHandler(["myquizzes", "my"], myquizzes_cmd))
    app.add_handler(CommandHandler("import", import_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("share", share_cmd))
    app.add_handler(CommandHandler("review", review_cmd))
    app.add_handler(CommandHandler(["htmlreport", "report"], htmlreport_cmd))
    app.add_handler(CommandHandler("aiquiz", aiquiz_cmd))
    app.add_handler(CommandHandler("quizmode", quizmode_cmd))
    app.add_handler(CommandHandler("section", section_cmd))
    app.add_handler(CommandHandler("access", access_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("batch", batch_cmd))
    app.add_handler(CommandHandler("authchat", authchat_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(PollAnswerHandler(runner.handle_poll_answer))
    app.add_handler(ChatMemberHandler(membership.on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & (
            filters.TEXT | filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.ANIMATION
            | filters.AUDIO | filters.VOICE | filters.POLL),
        private_message_router))
    app.add_error_handler(error_handler)
    return app


def acquire_single_instance_lock():
    """Refuse to start a second polling process in the same container/volume."""
    try:
        import fcntl
    except ImportError:  # Windows — no lock available
        return None
    fh = open(config.LOCK_PATH, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another bot process already holds %s — refusing to start a second poller.", config.LOCK_PATH)
        sys.exit(1)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


_INSTANCE_LOCK = None

# ---------------------------------------------------------------- start-up SIGTERM
# python-telegram-bot installs its SIGTERM handler only once polling starts.
# Before that (migration, start-up backup + verify, backup thread start) a
# SIGTERM — e.g. Railway stopping/redeploying right after a start — hit Python's
# default action and killed the process instantly (exit -15, no shutdown
# backup).  This handler covers exactly that window; PTB's polling start replaces it
# with PTB's own handler, so behaviour once polling runs is unchanged.
_EARLY_STOP: dict = {"signal": None, "armed": False}


def _early_stop_handler(signum, _frame) -> None:
    _EARLY_STOP["signal"] = signum
    if _EARLY_STOP["armed"]:              # all start-up work done: abort the polling start
        raise SystemExit(0)
    # otherwise: only remember it; main() stops cleanly at the next checkpoint


def install_early_stop_handler() -> bool:
    if threading.current_thread() is not threading.main_thread():
        return False
    try:
        signal.signal(signal.SIGTERM, _early_stop_handler)
        return True
    except (ValueError, OSError, AttributeError):   # pragma: no cover - exotic platforms
        return False


def _stop_if_signalled(where: str) -> bool:
    """True (after a final backup) when SIGTERM arrived during start-up."""
    if _EARLY_STOP["signal"] is None:
        return False
    log.info("Stop signal %s received during start-up (%s) — exiting cleanly without polling.",
             _EARLY_STOP["signal"], where)
    try:
        res = backup.shutdown_export()
        log.info("Shutdown backup: %s", {k: res.get(k) for k in ("ok", "bytes", "skipped", "errors")})
    except Exception as exc:  # noqa: BLE001 — shutdown must never fail
        log.error("Shutdown backup failed: %s", exc)
    return True


def main() -> None:
    install_early_stop_handler()
    try:
        token = config.get_token()
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)
    global _INSTANCE_LOCK
    _INSTANCE_LOCK = acquire_single_instance_lock()  # kept open for the process lifetime
    app = build_application(token)
    log.info("Database: %s", db.get_db_path())
    # Numeric variables that were set to something that is not a number: the
    # default is used (see envutil).  Only the NAME is logged, never the value.
    import envutil
    for name, default in envutil.invalid_settings():
        log.warning("Environment variable %s is not a valid number — using the default %s instead.",
                    name, default)
    try:  # Hindi Review PDF fonts: report what the PDFs will actually use; never block start-up
        fonts_ok, fonts_info = review_pdf.pdf_font_status()
        if fonts_ok:
            log.info("PDF fonts ready: %s", fonts_info)
        else:
            log.warning("PDF fonts DEGRADED (Hindi text may not render): %s", fonts_info)
    except Exception as exc:  # noqa: BLE001
        log.warning("PDF font status check failed: %s", exc)
    on_railway = any(os.getenv(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID"))
    if on_railway and not os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        log.warning("NO RAILWAY VOLUME ATTACHED — %s and %s are on ephemeral disk and will be WIPED on "
                    "every redeploy. Add a Volume mounted at /data (service → Settings → Volumes).",
                    db.get_db_path(), config.BACKUP_PATH)
    elif on_railway:
        volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
        if volume and not str(config.BACKUP_PATH).startswith(volume):
            log.warning("Backup file %s is OUTSIDE the Railway volume %s — it will be lost on redeploy.",
                        config.BACKUP_PATH, volume)
    # ---- automatic backup: export + verify on every start, then keep syncing
    try:
        info = backup.install()
        st = info.get("startup") or {}
        if st.get("ok"):
            log.info("Backup file: %s (%s bytes, %s quizzes, %s questions, %s attempts)",
                     st.get("path"), f"{st.get('bytes', 0):,}", (st.get("counts") or {}).get("quizzes", 0),
                     (st.get("counts") or {}).get("questions", 0), (st.get("counts") or {}).get("attempts", 0))
        else:
            log.error("Startup backup FAILED: %s", st.get("errors"))
        ok, problems, vinfo = backup.verify_file()
        if ok:
            log.info("Backup verified: %s (%s, %s)", vinfo.get("path"), vinfo.get("generated_at"),
                     vinfo.get("counts"))
        else:
            log.error("Backup verification FAILED: %s", "; ".join(problems[:5]))
        backup.start_background()
    except Exception as exc:  # noqa: BLE001 — the bot must start even if backups cannot
        log.error("Backup system could not start: %s", exc)
    if _stop_if_signalled("before polling"):
        return
    drop = os.getenv("DROP_PENDING_UPDATES", "false").lower() in ("1", "true", "yes")
    log.info("Starting polling (single instance)…")
    _EARLY_STOP["armed"] = True
    try:
        # run_polling owns the event loop; it is called exactly once.  Its first
        # step replaces _early_stop_handler with PTB's own stop-signal handler.
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=drop)
    except SystemExit:
        if _EARLY_STOP["signal"] is None:
            raise
        _stop_if_signalled("while starting polling")


if __name__ == "__main__":
    main()
