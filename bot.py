import asyncio
import json
import os
import tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import PollType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, PollAnswerHandler,
    CallbackQueryHandler, ContextTypes, filters
)

from pdf_parser import parse_pdf, parse_source_text, save_questions
from quiz_engine import validate_questions, result_text

TOKEN = os.getenv("BOT_TOKEN")
QUESTIONS_FILE = "questions.json"
TIMEOUT = 30

sessions = {}          # user_id -> session
poll_to_session = {}  # poll_id -> (user_id, question_index)

def load_bank():
    if not Path(QUESTIONS_FILE).exists():
        return []
    try:
        data = json.loads(Path(QUESTIONS_FILE).read_text(encoding="utf-8"))
        validate_questions(data)
        return data
    except Exception:
        return []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Quiz Bot तैयार है।\n\n"
        "/quiz — quiz शुरू करें\n"
        "/stop — quiz बंद करें\n\n"
        "Admin PDF या text source भेजकर question bank बदल सकता है।"
    )

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bank = load_bank()
    if not bank:
        await update.message.reply_text("❌ अभी कोई valid question bank उपलब्ध नहीं है।")
        return

    sessions[user.id] = {
        "chat_id": update.effective_chat.id,
        "index": 0,
        "correct": 0,
        "wrong": 0,
        "skipped": 0,
        "total": len(bank),
        "questions": bank,
        "poll_id": None,
        "timeout_task": None,
    }
    await send_next(user.id, context)

async def send_next(user_id, context):
    session = sessions.get(user_id)
    if not session:
        return

    if session["index"] >= session["total"]:
        await finish_quiz(user_id, context)
        return

    q = session["questions"][session["index"]]

    # Full original question is always sent first so no source text is lost.
    full_question = (
        f"प्रश्न {q['number']}.\n\n{q['question']}\n\n"
        f"A) {q['options'][0]['text']}\n\n"
        f"B) {q['options'][1]['text']}\n\n"
        f"C) {q['options'][2]['text']}\n\n"
        f"D) {q['options'][3]['text']}"
    )
    chat_id = session["chat_id"]

    # Telegram messages have a 4096-character limit.
    if len(full_question) <= 4000:
        await context.bot.send_message(chat_id=chat_id, text=full_question)

    labels = [x["text"] for x in q["options"]]
    correct_id = "ABCD".index(q["correct"])

    poll = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"प्रश्न {q['number']} — अपना उत्तर चुनें",
        options=labels,
        type=PollType.QUIZ,
        correct_option_id=correct_id,
        is_anonymous=False,
        allows_multiple_answers=False,
    )

    session["poll_id"] = poll.poll.id
    poll_to_session[poll.poll.id] = (user_id, session["index"])

    async def timeout():
        await asyncio.sleep(TIMEOUT)
        current = sessions.get(user_id)
        if not current:
            return
        if current["index"] == session["index"] and current.get("poll_id") == poll.poll.id:
            current["skipped"] += 1
            current["index"] += 1
            current["poll_id"] = None
            poll_to_session.pop(poll.poll.id, None)
            await context.bot.send_message(chat_id=chat_id, text="⏱️ समय समाप्त — प्रश्न छोड़ा गया।")
            await send_next(user_id, context)

    session["timeout_task"] = asyncio.create_task(timeout())

async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    mapping = poll_to_session.get(answer.poll_id)
    if not mapping:
        return

    user_id, idx = mapping
    session = sessions.get(user_id)
    if not session or session["index"] != idx:
        return

    if session["timeout_task"]:
        session["timeout_task"].cancel()

    q = session["questions"][idx]
    selected = answer.option_ids[0] if answer.option_ids else None
    correct_id = "ABCD".index(q["correct"])

    if selected == correct_id:
        session["correct"] += 1
        msg = "✅ सही उत्तर!"
    else:
        session["wrong"] += 1
        msg = f"❌ गलत उत्तर! सही उत्तर: {q['correct']}"

    session["index"] += 1
    session["poll_id"] = None
    poll_to_session.pop(answer.poll_id, None)

    await context.bot.send_message(chat_id=session["chat_id"], text=msg)
    await send_next(user_id, context)

async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in sessions:
        await update.message.reply_text("कोई active quiz नहीं है।")
        return
    await finish_quiz(user_id, context)

async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id in sessions:
        await finish_quiz(user_id, context)

async def finish_quiz(user_id, context):
    session = sessions.pop(user_id, None)
    if not session:
        return
    if session.get("timeout_task"):
        session["timeout_task"].cancel()
    if session.get("poll_id"):
        poll_to_session.pop(session["poll_id"], None)

    text = result_text(
        session["total"],
        session["correct"],
        session["wrong"],
        session["skipped"],
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 फिर से Quiz", callback_data="restart_quiz")]
    ])
    await context.bot.send_message(
        chat_id=session["chat_id"],
        text=text,
        reply_markup=keyboard
    )

async def restart_callback(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    bank = load_bank()
    if not bank:
        await query.message.reply_text("Question bank उपलब्ध नहीं है।")
        return
    sessions[user_id] = {
        "chat_id": query.message.chat_id,
        "index": 0, "correct": 0, "wrong": 0, "skipped": 0,
        "total": len(bank), "questions": bank,
        "poll_id": None, "timeout_task": None
    }
    await send_next(user_id, context)

def admin_allowed(update):
    admin_ids = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
    return not admin_ids or update.effective_user.id in admin_ids

async def source_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_allowed(update):
        return

    document = update.message.document
    if not document:
        return

    if not document.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ केवल PDF भेजें।")
        return

    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(path)

        questions = parse_pdf(path, max_questions=100)
        validate_questions(questions)

        # Atomic replacement: old bank remains if parsing/validation fails.
        tmp = QUESTIONS_FILE + ".tmp"
        save_questions(questions, tmp)
        os.replace(tmp, QUESTIONS_FILE)

        await update.message.reply_text(
            f"✅ Question bank update हो गया।\n"
            f"कुल प्रश्न: {len(questions)}\n"
            f"अब /quiz से एक ही quiz में सभी प्रश्न चलेंगे।"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ PDF process नहीं हुआ:\n{e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

async def source_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_allowed(update):
        return
    text = update.message.text or ""
    if not text.strip():
        return
    try:
        questions = parse_source_text(text, max_questions=100)
        validate_questions(questions)
        tmp = QUESTIONS_FILE + ".tmp"
        save_questions(questions, tmp)
        os.replace(tmp, QUESTIONS_FILE)
        await update.message.reply_text(f"✅ Text source से {len(questions)} प्रश्न तैयार हो गए।")
    except Exception as e:
        await update.message.reply_text(f"❌ Text parse नहीं हुआ:\n{e}")

async def error_handler(update, context):
    print("ERROR:", context.error)

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", start_quiz))
    app.add_handler(CommandHandler("stop", stop_quiz))
    app.add_handler(MessageHandler(filters.Document.PDF, source_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, source_text))
    app.add_handler(PollAnswerHandler(poll_answer))
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^restart_quiz$"))
    app.add_error_handler(error_handler)

    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
