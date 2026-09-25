import os
import asyncio
import time
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    PollAnswerHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from quiz_engine import load_questions, score_result
from pdf_parser import parse_pdf, parse_txt, parse_text_to_questions, save_questions

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
QUIZ_FILE = DATA_DIR / "questions.json"

TIME_LIMIT = 30

# (chat_id, quiz_starter_user_id) -> session
SESSIONS = {}

LOCK = asyncio.Lock()


def is_admin(user_id):
    return bool(ADMIN_IDS) and user_id in ADMIN_IDS


def start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Quiz Start", callback_data="start_quiz")]
    ])


def stop_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Quiz बंद करें", callback_data="stop_quiz")]
    ])


def key(chat_id, user_id):
    return (chat_id, user_id)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Quiz Bot\n\n"
        "Native Telegram Quiz mode में प्रश्न आएगा।\n"
        "एक समय में एक प्रश्न होगा।\n"
        "हर प्रश्न के लिए 30 सेकंड होंगे।",
        reply_markup=start_keyboard(),
    )


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_quiz(
        context.bot,
        update.effective_chat.id,
        update.effective_user.id,
    )


async def start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await start_quiz(
        context.bot,
        query.message.chat_id,
        query.from_user.id,
    )


async def start_quiz(bot, chat_id, user_id):
    questions = load_questions(QUIZ_FILE)[:100]

    if not questions:
        await bot.send_message(
            chat_id,
            "❌ अभी कोई Quiz उपलब्ध नहीं है।\n"
            "Admin पहले PDF/Text से Quiz तैयार करे।"
        )
        return

    session_key = key(chat_id, user_id)

    async with LOCK:
        old = SESSIONS.get(session_key)

        if old and not old["finished"]:
            await bot.send_message(
                chat_id,
                "⚠️ आपकी Quiz पहले से चल रही है।"
            )
            return

        SESSIONS[session_key] = {
            "questions": questions,
            "index": 0,
            "correct": 0,
            "wrong": 0,
            "skipped": 0,
            "finished": False,
            "poll_id": None,
            "poll_message_id": None,
            "timer_task": None,
            "started": time.time(),
        }

    await bot.send_message(
        chat_id,
        f"🚀 Quiz शुरू!\n\nकुल प्रश्न: {len(questions)}"
    )

    await send_next_poll(bot, chat_id, user_id)


async def send_next_poll(bot, chat_id, user_id):
    session_key = key(chat_id, user_id)

    async with LOCK:
        session = SESSIONS.get(session_key)

        if not session or session["finished"]:
            return

        if session["index"] >= len(session["questions"]):
            session["finished"] = True
            result = score_result(session)

            await bot.send_message(
                chat_id,
                result,
                reply_markup=start_keyboard(),
            )
            return

        old_timer = session.get("timer_task")
        if old_timer and not old_timer.done():
            old_timer.cancel()

        q = session["questions"][session["index"]]

        poll = await bot.send_poll(
            chat_id=chat_id,
            question=f"प्रश्न {session['index'] + 1}/{len(session['questions'])}\n\n{q['question']}",
            options=q["options"],
            type="quiz",
            correct_option_id=q["correct"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )

        session["poll_id"] = poll.poll.id
        session["poll_message_id"] = poll.message_id
        session["poll_started"] = time.monotonic()

        session["timer_task"] = asyncio.create_task(
            poll_timeout(bot, chat_id, user_id, poll.poll.id)
        )

    # Stop button is separate so the question itself remains Telegram's
    # native Quiz UI.
    await bot.send_message(
        chat_id,
        f"📝 प्रश्न {session['index'] + 1}/{len(session['questions'])} चल रहा है।",
        reply_markup=stop_keyboard(),
    )


async def poll_timeout(bot, chat_id, user_id, poll_id):
    try:
        await asyncio.sleep(TIME_LIMIT)
    except asyncio.CancelledError:
        return

    session_key = key(chat_id, user_id)

    async with LOCK:
        session = SESSIONS.get(session_key)

        if not session or session["finished"]:
            return

        if session.get("poll_id") != poll_id:
            return

        session["skipped"] += 1
        session["index"] += 1

    try:
        await bot.stop_poll(chat_id=chat_id, message_id=session["poll_message_id"])
    except Exception:
        pass

    await bot.send_message(
        chat_id,
        "⏰ 30 सेकंड पूरे — प्रश्न skip हो गया।"
    )

    await send_next_poll(bot, chat_id, user_id)


async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer

    # Find the session whose current poll this is.
    target = None

    async with LOCK:
        for session_key, session in SESSIONS.items():
            if (
                not session["finished"]
                and session.get("poll_id") == answer.poll_id
            ):
                target = (session_key, session)
                break

        if not target:
            return

        (chat_id, starter_user_id), session = target

        # Only the user who started the quiz advances it.
        # Other group members can see the native poll but do not control
        # the quiz sequence.
        if answer.user.id != starter_user_id:
            return

        elapsed = time.monotonic() - session["poll_started"]

        if elapsed >= TIME_LIMIT:
            return

        selected = answer.option_ids[0] if answer.option_ids else None

        if selected is None:
            return

        q = session["questions"][session["index"]]

        if selected == q["correct"]:
            session["correct"] += 1
        else:
            session["wrong"] += 1

        session["index"] += 1

        timer = session.get("timer_task")
        if timer and not timer.done():
            timer.cancel()

        message_id = session.get("poll_message_id")

    # Close the current native Telegram quiz poll.
    try:
        await context.bot.stop_poll(
            chat_id=chat_id,
            message_id=message_id
        )
    except Exception:
        pass

    await send_next_poll(
        context.bot,
        chat_id,
        starter_user_id,
    )


async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    user_id = query.from_user.id
    session_key = key(chat_id, user_id)

    async with LOCK:
        session = SESSIONS.get(session_key)

        if not session or session["finished"]:
            await query.message.reply_text("कोई active Quiz नहीं है।")
            return

        session["finished"] = True

        timer = session.get("timer_task")
        if timer and not timer.done():
            timer.cancel()

        result = score_result(session, stopped=True)

        poll_message_id = session.get("poll_message_id")

    if poll_message_id:
        try:
            await context.bot.stop_poll(
                chat_id=chat_id,
                message_id=poll_message_id
            )
        except Exception:
            pass

    await query.message.reply_text(
        result,
        reply_markup=start_keyboard(),
    )


async def source_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin sends PDF/TXT/plain MCQ text.
    The new source REPLACES the previous question bank.
    Maximum 100 questions.
    """

    if not is_admin(update.effective_user.id):
        return

    questions = []

    if update.message.document:
        document = update.message.document
        filename = (document.file_name or "").lower()

        if not (filename.endswith(".pdf") or filename.endswith(".txt")):
            await update.message.reply_text(
                "❌ केवल PDF या TXT file भेजें।"
            )
            return

        suffix = ".pdf" if filename.endswith(".pdf") else ".txt"
        path = DATA_DIR / f"source_{update.effective_user.id}{suffix}"

        tg_file = await document.get_file()
        await tg_file.download_to_drive(path)

        try:
            questions = (
                parse_pdf(path)
                if suffix == ".pdf"
                else parse_txt(path)
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Source पढ़ने में समस्या:\n{e}"
            )
            return

    elif update.message.text:
        try:
            questions = parse_text_to_questions(update.message.text)
        except Exception as e:
            await update.message.reply_text(
                f"❌ Text process नहीं हुआ:\n{e}"
            )
            return

    if not questions:
        await update.message.reply_text(
            "❌ Valid MCQ नहीं मिले।\n\n"
            "हर प्रश्न में A/B/C/D options और स्पष्ट सही उत्तर/answer key होना चाहिए।\n"
            "Bot सही उत्तर अनुमान से नहीं बनाएगा।"
        )
        return

    questions = questions[:100]
    save_questions(questions, QUIZ_FILE)

    # End currently active sessions because the source changed.
    async with LOCK:
        for session in SESSIONS.values():
            if not session["finished"]:
                session["finished"] = True
                timer = session.get("timer_task")
                if timer and not timer.done():
                    timer.cancel()

    await update.message.reply_text(
        f"✅ नई Quiz तैयार है!\n\n"
        f"📝 कुल प्रश्न: {len(questions)}\n"
        f"📌 अधिकतम: 100\n\n"
        f"अब /quiz भेजें।"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Quiz menu\n"
        "/quiz - Quiz शुरू करें\n"
        "/help - Help\n\n"
        "Admin PDF/TXT/MCQ text भेजकर नई Quiz बना सकता है।"
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Railway Variables में BOT_TOKEN सेट करें।"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(
        CallbackQueryHandler(
            start_button,
            pattern=r"^start_quiz$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            stop_quiz,
            pattern=r"^stop_quiz$"
        )
    )

    # Native Telegram Quiz/Poll answers.
    app.add_handler(PollAnswerHandler(poll_answer))

    # Admin-only source upload / text.
    app.add_handler(
        MessageHandler(
            (
                filters.Document.PDF
                | filters.Document.FileExtension("txt")
                | filters.TEXT
            ) & ~filters.COMMAND,
            source_message,
        )
    )

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
    
