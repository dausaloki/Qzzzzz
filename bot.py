import os
import asyncio
import time
import json
import uuid
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from quiz_engine import load_questions, score_result
from pdf_parser import parse_pdf, parse_txt, parse_text_to_questions, save_questions

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Example Railway variable:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

QUIZ_FILE = DATA_DIR / "questions.json"

TIME_LIMIT = 30

# Each user in each chat has a separate session.
SESSIONS = {}

# Prevent race conditions when a timeout and a button press happen together.
LOCK = asyncio.Lock()


def is_admin(user_id: int) -> bool:
    # Security: if ADMIN_IDS is not configured, nobody can replace the quiz source.
    return bool(ADMIN_IDS) and user_id in ADMIN_IDS


def start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Quiz Start", callback_data="quiz:start")],
    ])


def question_keyboard(session):
    buttons = []

    for i, option in enumerate(session["question"]["options"]):
        buttons.append([
            InlineKeyboardButton(
                f"{chr(65 + i)}. {option}",
                callback_data=f"quiz:answer:{session['token']}:{i}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🛑 Quiz बंद करें",
            callback_data=f"quiz:stop:{session['token']}"
        )
    ])

    return InlineKeyboardMarkup(buttons)


def session_key(chat_id, user_id):
    return (chat_id, user_id)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 <b>Quiz Bot</b>\n\n"
        "▶️ Quiz Start दबाएँ।\n"
        "• एक समय में केवल 1 प्रश्न\n"
        "• हर प्रश्न के लिए 30 सेकंड\n"
        "• उत्तर देते ही अगला प्रश्न\n"
        "• समय समाप्त होने पर प्रश्न skip\n"
        "• बीच में बंद करने पर तुरंत result",
        parse_mode=ParseMode.HTML,
        reply_markup=start_keyboard(),
    )


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_quiz(
        context.bot,
        update.effective_chat.id,
        update.effective_user.id,
    )


async def start_quiz(bot, chat_id, user_id):
    questions = load_questions(QUIZ_FILE)

    if not questions:
        await bot.send_message(
            chat_id,
            "❌ अभी कोई Quiz उपलब्ध नहीं है।\n"
            "Admin पहले PDF/Text भेजकर Quiz तैयार करे।"
        )
        return

    key = session_key(chat_id, user_id)

    async with LOCK:
        old = SESSIONS.get(key)

        if old and not old["finished"]:
            await bot.send_message(
                chat_id,
                "⚠️ आपकी Quiz पहले से चल रही है।\n"
                "पहले उसे पूरा करें या 🛑 Quiz बंद करें।"
            )
            return

        SESSIONS[key] = {
            "questions": questions[:100],
            "index": 0,
            "correct": 0,
            "wrong": 0,
            "skipped": 0,
            "finished": False,
            "token": "",
            "question": None,
            "question_started": 0,
            "timer_task": None,
            "question_message_id": None,
        }

    await bot.send_message(
        chat_id,
        f"🚀 <b>Quiz शुरू!</b>\n\n"
        f"कुल प्रश्न: {len(questions[:100])}",
        parse_mode=ParseMode.HTML,
    )

    await send_next_question(bot, chat_id, user_id)


async def send_next_question(bot, chat_id, user_id):
    key = session_key(chat_id, user_id)

    async with LOCK:
        session = SESSIONS.get(key)

        if not session or session["finished"]:
            return

        if session["index"] >= len(session["questions"]):
            session["finished"] = True
            result = score_result(session)

            await bot.send_message(
                chat_id,
                result,
                parse_mode=ParseMode.HTML,
                reply_markup=start_keyboard(),
            )
            return

        old_timer = session.get("timer_task")

        if old_timer and not old_timer.done():
            old_timer.cancel()

        question = session["questions"][session["index"]]

        session["question"] = question
        session["token"] = uuid.uuid4().hex[:16]
        session["question_started"] = time.monotonic()

        text = (
            f"📝 <b>प्रश्न {session['index'] + 1}/"
            f"{len(session['questions'])}</b>\n\n"
            f"{question['question']}\n\n"
            f"⏱️ <b>समय: {TIME_LIMIT} सेकंड</b>"
        )

        message = await bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=question_keyboard(session),
        )

        session["question_message_id"] = message.message_id

        session["timer_task"] = asyncio.create_task(
            question_timeout(
                bot,
                chat_id,
                user_id,
                session["token"],
            )
        )


async def question_timeout(bot, chat_id, user_id, token):
    try:
        await asyncio.sleep(TIME_LIMIT)
    except asyncio.CancelledError:
        return

    key = session_key(chat_id, user_id)

    async with LOCK:
        session = SESSIONS.get(key)

        if not session or session["finished"]:
            return

        # Ignore timeout belonging to an older question.
        if session.get("token") != token:
            return

        session["skipped"] += 1
        session["index"] += 1

        message_id = session.get("question_message_id")

    if message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
        except Exception:
            pass

    await bot.send_message(
        chat_id,
        "⏰ <b>समय समाप्त</b>\nप्रश्न skip हो गया।",
        parse_mode=ParseMode.HTML,
    )

    await send_next_question(bot, chat_id, user_id)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data == "quiz:start":
        await start_quiz(
            context.bot,
            query.message.chat_id,
            query.from_user.id,
        )
        return

    if data.startswith("quiz:stop:"):
        token = data.split(":")[2]

        chat_id = query.message.chat_id
        user_id = query.from_user.id
        key = session_key(chat_id, user_id)

        async with LOCK:
            session = SESSIONS.get(key)

            if (
                not session
                or session["finished"]
                or session.get("token") != token
            ):
                return

            session["finished"] = True

            timer = session.get("timer_task")
            if timer and not timer.done():
                timer.cancel()

            result = score_result(session, stopped=True)

        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await query.message.reply_text(
            result,
            parse_mode=ParseMode.HTML,
            reply_markup=start_keyboard(),
        )
        return

    if data.startswith("quiz:answer:"):
        parts = data.split(":")

        if len(parts) != 4:
            return

        token = parts[2]

        try:
            selected = int(parts[3])
        except ValueError:
            return

        chat_id = query.message.chat_id
        user_id = query.from_user.id
        key = session_key(chat_id, user_id)

        async with LOCK:
            session = SESSIONS.get(key)

            if (
                not session
                or session["finished"]
                or session.get("token") != token
            ):
                return

            elapsed = time.monotonic() - session["question_started"]

            if elapsed >= TIME_LIMIT:
                return

            question = session["question"]

            if not 0 <= selected < len(question["options"]):
                return

            if selected == question["correct"]:
                session["correct"] += 1
                result_text = "✅ सही उत्तर!"
            else:
                session["wrong"] += 1
                correct_text = question["options"][question["correct"]]
                result_text = (
                    "❌ गलत उत्तर!\n"
                    f"सही उत्तर: {correct_text}"
                )

            session["index"] += 1

            timer = session.get("timer_task")
            if timer and not timer.done():
                timer.cancel()

            old_message_id = session.get("question_message_id")

        # Remove buttons from the answered question.
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=old_message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        await query.message.reply_text(result_text)

        # Next question is shown only after the answer.
        await send_next_question(
            context.bot,
            chat_id,
            user_id,
        )


async def source_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only source importer.

    Accepted:
    - PDF document
    - TXT document
    - plain text containing MCQs + explicit answers

    Every new source REPLACES the previous question bank.
    Maximum 100 questions.
    """

    user_id = update.effective_user.id

    if not is_admin(user_id):
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
        path = DATA_DIR / f"source_{user_id}{suffix}"

        tg_file = await document.get_file()
        await tg_file.download_to_drive(path)

        try:
            if suffix == ".pdf":
                questions = parse_pdf(path)
            else:
                questions = parse_txt(path)

        except Exception as e:
            await update.message.reply_text(
                f"❌ Source पढ़ने में समस्या:\n{e}"
            )
            return

    elif update.message.text:
        try:
            questions = parse_text_to_questions(
                update.message.text
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Text process नहीं हुआ:\n{e}"
            )
            return

    if not questions:
        await update.message.reply_text(
            "❌ Valid MCQ नहीं मिले।\n\n"
            "हर प्रश्न में 4 options और स्पष्ट सही उत्तर/answer key होना चाहिए।\n"
            "Bot सही उत्तर का अनुमान नहीं लगाएगा।\n\n"
            "उदाहरण:\n\n"
            "1. भारत की राजधानी क्या है?\n"
            "A. मुंबई\n"
            "B. नई दिल्ली\n"
            "C. जयपुर\n"
            "D. भोपाल\n"
            "उत्तर: B"
        )
        return

    questions = questions[:100]

    save_questions(questions, QUIZ_FILE)

    # Stop currently running sessions because the source has changed.
    async with LOCK:
        for session in SESSIONS.values():
            if not session["finished"]:
                session["finished"] = True
                timer = session.get("timer_task")
                if timer and not timer.done():
                    timer.cancel()

    await update.message.reply_text(
        "✅ <b>नई Quiz तैयार हो गई!</b>\n\n"
        f"📝 कुल प्रश्न: {len(questions)}\n"
        "📌 अधिकतम सीमा: 100\n\n"
        "अब Group में /quiz भेजें।",
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Quiz Bot Commands\n\n"
        "/start - Quiz menu\n"
        "/quiz - Quiz शुरू करें\n"
        "/help - Help\n\n"
        "Admin PDF/TXT या MCQ text भेजकर नई Quiz बना सकता है।"
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ Railway Variables में BOT_TOKEN सेट करें।"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    # Admin-only function internally checks ADMIN_IDS.
    app.add_handler(
        MessageHandler(
            (
                filters.Document.PDF
                | filters.Document.FileExtension("txt")
                | filters.TEXT
            )
            & ~filters.COMMAND,
            source_message,
        )
    )

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
    
