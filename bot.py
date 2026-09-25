"""Telegram Quiz Bot — entry point.

Exactly ONE Application and ONE polling loop are ever created (``main()``).
A file lock prevents a second copy from polling inside the same container,
and ``Conflict: terminated by other getUpdates request`` (another deployment
still running) is logged and retried by PTB instead of crashing the bot.
"""
from __future__ import annotations

import logging
import os
import sys
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

import config
import database as db
import group_runner
import keyboards as kb
import membership
import quiz_creator as creator
import quiz_engine as engine
import quiz_runner as runner
import stats as st
from keyboards import esc

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)          # request URLs contain the bot token
logging.getLogger("telegram").setLevel(logging.INFO)          # PTB DEBUG logs the API URL (with token)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("quizbot")

_bot_username_cache: dict[str, object] = {}


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
    if cq is None and msg is not None and msg.text and msg.text.split()[0].split("@")[0] == "/start":
        # every /start asks Telegram again — a user who left must join again
        force = True
        parts = msg.text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
    if await membership.has_access(context.bot, user.id, force=force):
        return
    db.upsert_user(user)
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


async def quiz_cmd(update: Update, context) -> None:
    """/quiz <id> — in groups shows a Start Privately card; in private starts it."""
    qid = context.args[0] if context.args else None
    if qid and qid.startswith("quiz_"):
        qid = qid[5:]
    if not is_private(update):
        await group_redirect(update, context, qid)
        return
    if not qid or not db.get_quiz(qid):
        await update.effective_chat.send_message("Usage: /quiz QUIZ_ID")
        return
    await runner.send_ready(context.application, update.effective_user, update.effective_chat.id, qid)


# ------------------------------------------------------------ menus
async def show_my_quizzes(update: Update, context, edit: bool = True) -> None:
    rows_db = db.get_owner_quizzes(update.effective_user.id)
    if not rows_db:
        await creator.respond(update, "📚 अभी आपका कोई quiz नहीं है।",
                              M([[B("➕ New Quiz", callback_data="m:new"), B("📥 Import", callback_data="m:imp")],
                                 kb.home_button()]), edit=edit)
        return
    rows = []
    for r in rows_db[:40]:
        draft = " 📝" if r.get("status") == "draft" else ""
        rows.append([B(f"📘 {r['title'][:40]} ({r['question_count']}){draft}", callback_data=f"q:view:{r['id']}")])
    rows.append([B("➕ New Quiz", callback_data="m:new"), *kb.home_button()])
    await creator.respond(update, f"📚 <b>My Quizzes</b> ({len(rows_db)})", M(rows), edit=edit)


async def show_start_menu(update: Update, context) -> None:
    uid = update.effective_user.id
    own = db.get_owner_quizzes(uid, include_drafts=False)
    recent = db.get_recent_played_quizzes(uid)
    rows = [[B(f"▶ {q['title'][:40]} ({q['question_count']})", callback_data=f"q:run:{q['id']}")] for q in own[:20]]
    rows += [[B(f"🔁 {q['title'][:40]} ({q['question_count']})", callback_data=f"q:run:{q['id']}")] for q in recent]
    if not rows:
        await creator.respond(update, "ℹ️ शुरू करने के लिए कोई quiz नहीं है। पहले quiz बनाएँ/import करें, "
                                      "या किसी का share link खोलें।",
                              M([[B("➕ New Quiz", callback_data="m:new"), B("📥 Import", callback_data="m:imp")],
                                 kb.home_button()]))
        return
    rows.append(kb.home_button())
    await creator.respond(update, "▶️ <b>कौन-सा quiz शुरू करें?</b>", M(rows))


async def show_settings(update: Update, edit: bool = True) -> None:
    user = db.ensure_user_row(update.effective_user.id)
    await creator.respond(update, "⚙️ <b>Settings</b>\n\nये defaults आपके <b>नए</b> quizzes पर लागू होंगे। "
                                  "किसी मौजूदा quiz की settings: My Quizzes → Quiz → ⚙️ Settings.",
                          kb.settings_menu(user), edit=edit)


async def share_quiz(update: Update, context, qid: str) -> None:
    quiz = db.get_quiz(qid)
    username = await bot_username(context)
    link = engine.deep_link(username, qid)
    ready = quiz.get("status") == "ready" and quiz.get("question_count")
    await update.effective_chat.send_message(
        f"🔗 <b>Share: {esc(quiz['title'])}</b>\n\n{link}\n\n"
        "Link खोलने वाला हर user bot की private chat में अपना अलग quiz session शुरू करेगा।\n"
        "👥 <b>Group में Quiz चलाएँ</b> → group चुनें → वहाँ ▶️ दबाएँ: quiz उसी group में native quiz "
        "polls से चलेगा और अंत में leaderboard आएगा।",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.share_markup(link, quiz["title"], qid, inline=await inline_enabled(context),
                                     group_url=engine.group_link(username, qid) if ready else ""),
        disable_web_page_preview=True)


# ------------------------------------------------------------ inline mode
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline sharing (works only if inline mode is enabled for the bot in BotFather).

    ``quiz_<id>`` → that quiz's card; any other query → the user's own quizzes
    whose title matches.  The card contains a Start button (deep link) — every
    user who taps it gets an independent private session."""
    iq = update.inline_query
    if iq is None:
        return
    username = await bot_username(context)
    query = (iq.query or "").strip()
    quizzes = []
    if query.startswith("quiz_"):
        quiz = db.get_quiz(query[5:])
        if quiz and quiz.get("status") == "ready" and quiz.get("question_count"):
            quizzes = [quiz]
    else:
        needle = query.lower()
        quizzes = [db.get_quiz(q["id"]) for q in db.get_owner_quizzes(iq.from_user.id, include_drafts=False)
                   if q.get("question_count") and needle in q["title"].lower()][:20]
        quizzes = [q for q in quizzes if q]
    results = []
    for quiz in quizzes:
        link = engine.deep_link(username, quiz["id"])
        n = quiz["question_count"]
        text = (f"🎲 <b>{esc(quiz['title'])}</b>\n🖊 {n} {'question' if n == 1 else 'questions'} · "
                f"⏱ {config.timer_label(quiz['timer'])}\n\nनीचे button दबाकर private chat में quiz शुरू करें 👇")
        results.append(InlineQueryResultArticle(
            id=quiz["id"][:64], title=quiz["title"][:100],
            description=f"{n} questions · ⏱ {config.timer_label(quiz['timer'])}",
            input_message_content=InputTextMessageContent(text, parse_mode=ParseMode.HTML),
            reply_markup=kb.start_privately(link)))
    await iq.answer(results, cache_time=5, is_personal=True)


# ------------------------------------------------------------ callbacks
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    user = update.effective_user
    db.upsert_user(user)
    prefix = data.split(":", 1)[0]

    if not is_private(update):
        parts = data.split(":")
        if prefix == "g" and len(parts) > 2 and parts[1] == "go":
            await group_go(update, context, parts[2])
            return
        await q.answer()
        target = parts[2] if prefix in ("q", "r") and len(parts) > 2 else None
        await group_redirect(update, context, target)
        return

    if prefix == "j":
        await join_check(update, context)
        return
    if prefix == "c":
        await creator.handle_creator_callback(update, context)
        return
    if prefix == "f":
        await creator.handle_forward_callback(update, context)
        return
    if prefix == "e":
        await creator.handle_edit_callback(update, context)
        return
    if prefix == "i":
        await creator.handle_import_callback(update, context)
        return

    if prefix == "m":
        await q.answer()
        action = data[2:]
        if action == "home":
            await show_main_menu(update, context)
        elif action == "new":
            await creator.begin_new_quiz(update, context)
        elif action == "my":
            await show_my_quizzes(update, context)
        elif action == "imp":
            await creator.begin_import(update, context)
        elif action == "start":
            await show_start_menu(update, context)
        elif action == "stats":
            text, markup = st.user_overview(user.id)
            await creator.respond(update, text, markup)
        elif action == "set":
            await show_settings(update)
        elif action == "help":
            await creator.respond(update, kb.HELP_TEXT, kb.back_home())
        return

    if prefix == "s":
        await q.answer()
        parts = data.split(":")
        u = db.ensure_user_row(user.id)
        if parts[1] == "timer":
            await creator.respond(update, "⏱ नए quizzes के लिए default timer:",
                                  kb.timer_menu("s:settimer", u["default_timer"], "m:set"))
            return
        if parts[1] == "settimer":
            v = int(parts[2])
            if v in config.TIMER_CHOICES:
                db.update_user_settings(user.id, default_timer=v)
        elif parts[1] == "sq":
            db.update_user_settings(user.id, default_shuffle_questions=0 if u["default_shuffle_questions"] else 1)
        elif parts[1] == "so":
            db.update_user_settings(user.id, default_shuffle_options=0 if u["default_shuffle_options"] else 1)
        await show_settings(update)
        return

    if prefix == "r":
        action = data[2:]
        if action.startswith("go:"):
            qid = action[3:]
            if not db.get_quiz(qid):
                await q.answer("❌ Quiz नहीं मिला (delete हो चुका है)।", show_alert=True)
                return
            await q.answer("🚀")
            await runner.start_quiz(context.application, user, update.effective_chat.id, qid,
                                    start_msg_id=q.message.message_id if q.message else None)
            return
        if action == "stop":
            await q.answer("⏹ Stopping…")
            done = await runner.stop_quiz(context.application, user.id)
            if not done:
                await update.effective_chat.send_message("ℹ️ कोई quiz नहीं चल रहा।")
        elif action == "skip":
            res = await runner.skip_current(context.application, user.id, q.message.message_id if q.message else None)
            await q.answer({"ok": "⏭ Skipped", "none": "कोई quiz नहीं चल रहा",
                            "busy": "अगला प्रश्न आ रहा है…", "expired": "यह प्रश्न पहले ही पूरा हो चुका"}[res])
        return

    if prefix == "q":
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
        await q.answer(f"🔒 Quiz शुरू करने के लिए पहले {config.REQUIRED_CHAT} Join करें।", show_alert=True)
        return
    res = await group_runner.start_group_quiz(context.application, update.effective_chat.id, qid, user)
    if res == "ok":
        await q.answer("🚀 Quiz शुरू!")
        return
    await q.answer({"busy": "⏳ इस group में पहले से एक quiz चल रहा है। पहले वह पूरा होने दें या /stop करें।",
                    "missing": "❌ Quiz नहीं मिला (delete हो चुका है)।",
                    "empty": "⚠️ इस quiz में कोई प्रश्न नहीं है।",
                    "draft": "⚠️ यह quiz अभी draft है।"}.get(res, "⚠️ Quiz शुरू नहीं हो सका।"), show_alert=True)


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
    BotCommand("stats", "मेरे results"), BotCommand("done", "Quiz creation पूरा करें"),
    BotCommand("undo", "आख़िरी प्रश्न हटाएँ (creation)"),
    BotCommand("skip", "Skip (प्रश्न / optional step)"), BotCommand("stop", "Quiz रोकें"),
    BotCommand("cancel", "रद्द करें"), BotCommand("help", "Help"),
]


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
    builder: ApplicationBuilder = Application.builder().token(token).post_init(post_init)
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


def main() -> None:
    try:
        token = config.get_token()
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)
    global _INSTANCE_LOCK
    _INSTANCE_LOCK = acquire_single_instance_lock()  # kept open for the process lifetime
    app = build_application(token)
    log.info("Database: %s", db.get_db_path())
    on_railway = any(os.getenv(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID"))
    if on_railway and not os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        log.warning("NO RAILWAY VOLUME ATTACHED — %s is on ephemeral disk and will be WIPED on every "
                    "redeploy. Add a Volume mounted at /data (service → Settings → Volumes).",
                    db.get_db_path())
    log.info("Starting polling (single instance)…")
    drop = os.getenv("DROP_PENDING_UPDATES", "false").lower() in ("1", "true", "yes")
    # run_polling owns the event loop; it is called exactly once.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=drop)


if __name__ == "__main__":
    main()
