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

from telegram import BotCommand, InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, TimedOut
from telegram.ext import (
    AIORateLimiter, Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, PollAnswerHandler, filters,
)

import config
import database as db
import keyboards as kb
import quiz_creator as creator
import quiz_engine as engine
import quiz_runner as runner
import stats as st
from keyboards import esc

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("quizbot")

_bot_username_cache: dict[str, str] = {}


async def bot_username(context) -> str:
    if "u" not in _bot_username_cache:
        me = await context.bot.get_me()
        _bot_username_cache["u"] = me.username
    return _bot_username_cache["u"]


def is_private(update: Update) -> bool:
    return bool(update.effective_chat) and update.effective_chat.type == ChatType.PRIVATE


async def group_redirect(update: Update, context, qid: str | None = None) -> None:
    """In groups, never run a shared sequential session — send a private deep link."""
    username = await bot_username(context)
    if qid and db.get_quiz(qid):
        quiz = db.get_quiz(qid)
        link = engine.deep_link(username, qid)
        text = (f"🎯 <b>{esc(quiz['title'])}</b>\n📝 {quiz['question_count']} प्रश्न • "
                f"⏱ {config.timer_label(quiz['timer'])}\n\n"
                "हर participant नीचे button दबाकर अपना <b>अलग private session</b> शुरू करे 👇")
    else:
        link = f"https://t.me/{username}"
        text = "🤖 Quiz बनाने/खेलने के लिए bot को private chat में खोलें 👇"
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb.start_privately(link))


# ------------------------------------------------------------ commands
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_user(user)
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
        await runner.start_quiz(context.application, user, update.effective_chat.id, qid)
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


async def stop_cmd(update: Update, context) -> None:
    if not is_private(update):
        return
    done = await runner.stop_quiz(context.application, update.effective_user.id)
    if not done:
        await update.effective_chat.send_message("ℹ️ अभी कोई quiz नहीं चल रहा।", reply_markup=kb.main_menu())


async def skip_cmd(update: Update, context) -> None:
    if not is_private(update):
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
    await update.effective_chat.send_message(text[:config.MESSAGE_MAX], parse_mode=ParseMode.HTML, reply_markup=markup)


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
    await runner.start_quiz(context.application, update.effective_user, update.effective_chat.id, qid)


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


async def show_quiz_card(update: Update, qid: str) -> None:
    quiz = db.get_quiz(qid)
    if not quiz:
        await creator.respond(update, "❌ Quiz नहीं मिला।", kb.main_menu())
        return
    await creator.respond(update, creator.quiz_summary(quiz),
                          kb.quiz_card(qid, quiz["owner_id"] == update.effective_user.id))


async def show_settings(update: Update, edit: bool = True) -> None:
    user = db.ensure_user_row(update.effective_user.id)
    await creator.respond(update, "⚙️ <b>Settings</b>\n\nये defaults आपके <b>नए</b> quizzes पर लागू होंगे। "
                                  "किसी मौजूदा quiz की settings: My Quizzes → Quiz → ✏️ Edit.",
                          kb.settings_menu(user), edit=edit)


async def share_quiz(update: Update, context, qid: str) -> None:
    quiz = db.get_quiz(qid)
    link = engine.deep_link(await bot_username(context), qid)
    await update.effective_chat.send_message(
        f"🔗 <b>Share: {esc(quiz['title'])}</b>\n\n{link}\n\n"
        "Link खोलने वाला हर user bot की private chat में अपना अलग quiz session शुरू करेगा। "
        "Group में भी यह link डाल सकते हैं।",
        parse_mode=ParseMode.HTML, reply_markup=kb.share_markup(link, quiz["title"]),
        disable_web_page_preview=True)


# ------------------------------------------------------------ callbacks
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    user = update.effective_user
    db.upsert_user(user)
    prefix = data.split(":", 1)[0]

    if not is_private(update):
        parts = data.split(":")
        await q.answer()
        await group_redirect(update, context, parts[2] if prefix == "q" and len(parts) > 2 else None)
        return

    if prefix == "c":
        await creator.handle_creator_callback(update, context)
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
            await creator.respond(update, text[:config.MESSAGE_MAX], markup)
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
        if action in ("edit", "del", "delok") and not owner:
            await q.answer("⛔ यह quiz आपका नहीं है।", show_alert=True)
            return
        await q.answer()
        if action == "view":
            await show_quiz_card(update, qid)
        elif action == "run":
            await runner.start_quiz(context.application, user, update.effective_chat.id, qid)
        elif action == "share":
            await share_quiz(update, context, qid)
        elif action == "edit":
            await creator.show_edit_menu(update, qid)
        elif action == "stats":
            await creator.respond(update, st.quiz_stats_text(qid, user.id)[:config.MESSAGE_MAX],
                                  st.quiz_stats_markup(qid), edit=False)
        elif action == "del":
            await creator.respond(update, f"🗑 <b>{esc(quiz['title'])}</b> delete करें? "
                                          "इसके सभी प्रश्न और stats हट जाएँगे।", kb.confirm_delete(qid))
        elif action == "delok":
            db.delete_quiz(qid, user.id)
            await creator.respond(update, "🗑 Quiz delete हो गया।", kb.main_menu())
        return

    await q.answer("⌛ यह button अब valid नहीं है।")


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
    if msg.poll:
        await msg.reply_text("ℹ️ Poll forward करने के बजाय ➕ New Quiz या 📥 Import का उपयोग करें।",
                             reply_markup=kb.main_menu())
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
    BotCommand("skip", "प्रश्न skip करें"), BotCommand("stop", "Quiz रोकें"),
    BotCommand("cancel", "रद्द करें"), BotCommand("help", "Help"),
]


async def post_init(app: Application) -> None:
    me = await app.bot.get_me()
    _bot_username_cache["u"] = me.username
    log.info("Logged in as @%s (id %s)", me.username, me.id)
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("set_my_commands failed: %s", exc)
    rec = await runner.recover_sessions(app)
    log.info("Session recovery: %s", rec)


def build_application(token: str, request=None, get_updates_request=None, rate_limit: bool = True) -> Application:
    db.init_db()
    builder: ApplicationBuilder = Application.builder().token(token).post_init(post_init)
    if rate_limit:
        builder = builder.rate_limiter(AIORateLimiter(max_retries=3))
    if request is not None:
        builder = builder.request(request)
    if get_updates_request is not None:
        builder = builder.get_updates_request(get_updates_request)
    app = builder.build()
    if app.job_queue is None:
        raise RuntimeError("JobQueue missing: install python-telegram-bot[job-queue]")

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("newquiz", newquiz_cmd))
    app.add_handler(CommandHandler(["myquizzes", "my"], myquizzes_cmd))
    app.add_handler(CommandHandler("import", import_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(PollAnswerHandler(runner.handle_poll_answer))
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
    log.info("Starting polling (single instance)…")
    drop = os.getenv("DROP_PENDING_UPDATES", "false").lower() in ("1", "true", "yes")
    # run_polling owns the event loop; it is called exactly once.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=drop)


if __name__ == "__main__":
    main()
