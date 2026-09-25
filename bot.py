import os
import asyncio
import time
import json
import uuid
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

from quiz_engine import load_questions, score_result
from pdf_parser import parse_pdf

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
QUIZ_FILE = DATA_DIR / "questions.json"

SESSIONS = {}
LOCK = asyncio.Lock()
TIME_LIMIT = 30

def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS

def keyboard_start():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Quiz Start", callback_data="start")],
        [InlineKeyboardButton("📊 Leaderboard", callback_data="leaderboard")]
    ])

def keyboard_stop():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Quiz बंद करें", callback_data="stop")]
    ])

def keyboard_question(session):
    rows = []
    for i, opt in enumerate(session["question"]["options"]):
        rows.append([InlineKeyboardButton(
            f"{chr(65+i)}. {opt}", callback_data=f"ans:{session['id']}:{i}"
        )])
    rows.append([InlineKeyboardButton("🛑 Quiz बंद करें", callback_data=f"stop:{session['id']}")])
    return InlineKeyboardMarkup(rows)

async def send_question(bot, chat_id, user_id):
    key = (chat_id, user_id)
    async with LOCK:
        session = SESSIONS.get(key)
        if not session or session["finished"]:
            return
        idx = session["index"]
        questions = session["questions"]
        if idx >= len(questions):
            session["finished"] = True
            result = score_result(session)
            await bot.send_message(chat_id, result, reply_markup=keyboard_start())
            return

        old_task = session.get("timer_task")
        if old_task and not old_task.done():
            old_task.cancel()

        q = questions[idx]
        session["question"] = q
        session["question_started"] = time.monotonic()
        session["id"] = uuid.uuid4().hex[:12]

        text = (
            f"📝 <b>प्रश्न {idx+1}/{len(questions)}</b>\n\n"
            f"{q['question']}\n\n"
            f"⏱️ समय: {TIME_LIMIT} सेकंड"
        )
        await bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML,
            reply_markup=keyboard_question(session)
        )
        session["timer_task"] = asyncio.create_task(
            timeout_question(bot, chat_id, user_id, session["id"])
        )

async def timeout_question(bot, chat_id, user_id, question_id):
    await asyncio.sleep(TIME_LIMIT)
    key = (chat_id, user_id)
    async with LOCK:
        session = SESSIONS.get(key)
        if not session or session["finished"] or session.get("id") != question_id:
            return
        session["skipped"] += 1
        session["index"] += 1
    await bot.send_message(chat_id, "⏰ <b>समय समाप्त</b> — प्रश्न छोड़ दिया गया।",
                           parse_mode=ParseMode.HTML)
    await send_question(bot, chat_id, user_id)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 <b>Telegram Quiz Bot</b>\n\n"
        "एक समय में एक प्रश्न आएगा।\n"
        "हर प्रश्न के लिए 30 सेकंड मिलेंगे।\n"
        "बीच में Quiz बंद करके भी result देख सकते हैं।",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_start()
    )

async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_quiz(update.effective_chat.id, update.effective_user.id, context)

async def start_quiz(chat_id, user_id, context):
    questions = load_questions(QUIZ_FILE)
    if not questions:
        await context.bot.send_message(chat_id, "❌ अभी कोई Quiz उपलब्ध नहीं है।")
        return

    key = (chat_id, user_id)
    old = SESSIONS.get(key)
    if old and not old["finished"]:
        await context.bot.send_message(
            chat_id, "⚠️ आपकी एक Quiz पहले से चल रही है। पहले उसे पूरा करें या Quiz बंद करें."
        )
        return

    SESSIONS[key] = {
        "questions": questions[:100],
        "index": 0,
        "correct": 0,
        "wrong": 0,
        "skipped": 0,
        "finished": False,
        "timer_task": None,
        "id": ""
    }
    await context.bot.send_message(chat_id, "🚀 <b>Quiz शुरू हो गई!</b>",
                                   parse_mode=ParseMode.HTML)
    await send_question(context.bot, chat_id, user_id)

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    key = (chat_id, user_id)

    if data == "start":
        await start_quiz(chat_id, user_id, context)
        return

    if data == "leaderboard":
        await query.message.reply_text("📊 इस version में leaderboard session के दौरान तैयार किया जाता है।")
        return

    if data.startswith("stop:"):
        session_id = data.split(":", 1)[1]
        async with LOCK:
            session = SESSIONS.get(key)
            if not session or session["finished"] or session.get("id") != session_id:
                return
            session["finished"] = True
            task = session.get("timer_task")
            if task and not task.done():
                task.cancel()
            result = score_result(session, stopped=True)
        await query.message.reply_text(result, parse_mode=ParseMode.HTML,
                                       reply_markup=keyboard_start())
        return

    if data.startswith("ans:"):
        _, session_id, answer = data.split(":")
        answer = int(answer)
        async with LOCK:
            session = SESSIONS.get(key)
            if not session or session["finished"] or session.get("id") != session_id:
                return
            if time.monotonic() - session["question_started"] > TIME_LIMIT:
                return
            q = session["question"]
            if answer == q["correct"]:
                session["correct"] += 1
                msg = "✅ सही उत्तर!"
            else:
                session["wrong"] += 1
                correct_text = q["options"][q["correct"]]
                msg = f"❌ गलत उत्तर!\nसही उत्तर: {correct_text}"
            session["index"] += 1
            task = session.get("timer_task")
            if task and not task.done():
                task.cancel()
        await query.message.reply_text(msg)
        await send_question(context.bot, chat_id, user_id)

async def pdf_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    document = update.message.document
    if not document or not document.file_name.lower().endswith(".pdf"):
        return

    path = DATA_DIR / f"upload_{update.effective_user.id}.pdf"
    tg_file = await document.get_file()
    await tg_file.download_to_drive(path)

    try:
        questions = parse_pdf(path)
        questions = questions[:100]
        if not questions:
            raise ValueError("कोई valid MCQ नहीं मिला।")
        QUIZ_FILE.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8")
        await update.message.reply_text(
            f"✅ PDF से {len(questions)} प्रश्न तैयार हुए।\n"
            f"अब group में /quiz चलाकर Quiz शुरू कर सकते हैं।"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ PDF process नहीं हो सकी: {e}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Bot menu\n"
        "/quiz - Quiz शुरू करें\n\n"
        "Admin: PDF भेजकर questions update कर सकता है।"
    )

def main():
    if not BOT_TOKEN:
        raise SystemExit("Railway Variables में BOT_TOKEN सेट करें।")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.Document.PDF, pdf_message))

    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
