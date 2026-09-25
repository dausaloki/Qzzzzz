import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PollAnswerHandler, ConversationHandler, ContextTypes, filters
)

import database as db
from pdf_parser import parse_pdf, parse_text
from quiz_engine import build_order, poll_data, short_text

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("quizbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing")

MAX_QUESTIONS = 100
DEFAULT_TIMER = 30

# Creation conversation states
TITLE, DESCRIPTION, PRETEXT, QTEXT, OPTIONS, CORRECT, EXPLANATION = range(7)

# Per-user in-memory runner. Quiz data itself is persisted in SQLite.
RUNNERS = {}
POLL_MAP = {}
CREATORS = {}

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Quiz", callback_data="new")],
        [InlineKeyboardButton("📚 My Quizzes", callback_data="my")],
        [InlineKeyboardButton("📥 Import PDF/Text", callback_data="import")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ])

def quiz_menu(qid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start Quiz", callback_data=f"start:{qid}")],
        [InlineKeyboardButton("🔗 Share Quiz", callback_data=f"share:{qid}")],
        [InlineKeyboardButton("✏️ Edit Quiz", callback_data=f"edit:{qid}")],
        [InlineKeyboardButton("📊 Quiz Stats", callback_data=f"stats:{qid}")],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"del:{qid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="my")],
    ])

def edit_menu(qid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Edit Title", callback_data=f"etitle:{qid}")],
        [InlineKeyboardButton("📄 Edit Description", callback_data=f"edesc:{qid}")],
        [InlineKeyboardButton("➕ Add Question", callback_data=f"addq:{qid}")],
        [InlineKeyboardButton("🗑 Delete Question", callback_data=f"delqmenu:{qid}")],
        [InlineKeyboardButton("⏱ Timer", callback_data=f"timer:{qid}")],
        [InlineKeyboardButton("🔀 Shuffle Questions", callback_data=f"sq:{qid}")],
        [InlineKeyboardButton("🔀 Shuffle Options", callback_data=f"so:{qid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"quiz:{qid}")],
    ])

def timer_menu(qid):
    vals = [(10,"10s"),(15,"15s"),(30,"30s"),(60,"60s"),
            (90,"90s"),(120,"2m"),(180,"3m"),(300,"5m"),(0,"No timer")]
    rows = []
    row = []
    for value,label in vals:
        row.append(InlineKeyboardButton(label, callback_data=f"settimer:{qid}:{value}"))
        if len(row)==3:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"edit:{qid}")])
    return InlineKeyboardMarkup(rows)

def help_text():
    return (
        "🤖 <b>QuizBot Pro</b>\n\n"
        "• Native Telegram Quiz Poll\n"
        "• Manual quiz creator\n"
        "• PDF/Text import\n"
        "• 10s–5m timer या no timer\n"
        "• Shuffle questions/options\n"
        "• Explanation after answer\n"
        "• My Quizzes / Edit / Delete / Stats\n"
        "• 100 questions तक\n\n"
        "PDF/Text में question + A-D options + उत्तर स्पष्ट होना चाहिए।"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user)
    args = context.args or []
    if args and args[0].startswith("quiz_"):
        qid = args[0][5:]
        if db.get_quiz(qid):
            await show_quiz(update, qid)
            return
    await update.effective_message.reply_text(
        "🎯 <b>QuizBot Pro</b>\n\n"
        "Telegram native quiz बनाने और चलाने के लिए नीचे विकल्प चुनें।",
        parse_mode="HTML", reply_markup=main_menu()
    )

async def help_cmd(update, context):
    await update.effective_message.reply_text(help_text(), parse_mode="HTML")

async def cancel(update, context):
    context.user_data.pop("creator", None)
    await update.effective_message.reply_text("❌ Creation cancelled.", reply_markup=main_menu())
    return ConversationHandler.END

async def new_quiz_cmd(update, context):
    db.upsert_user(update.effective_user)
    context.user_data["creator"] = {"owner_id": update.effective_user.id, "questions": []}
    await update.effective_message.reply_text(
        "1️⃣ Quiz का <b>नाम</b> भेजें।\n\n/cancel से रद्द करें।",
        parse_mode="HTML"
    )
    return TITLE

async def cb_new(update, context):
    await update.callback_query.answer()
    return await new_quiz_cmd(update, context)

async def got_title(update, context):
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("नाम खाली नहीं हो सकता।")
        return TITLE
    context.user_data["creator"]["title"] = title
    await update.message.reply_text(
        "2️⃣ Description भेजें। नहीं चाहिए तो <code>-</code> भेजें।",
        parse_mode="HTML"
    )
    return DESCRIPTION

async def got_description(update, context):
    text = update.message.text.strip()
    context.user_data["creator"]["description"] = "" if text == "-" else text
    await update.message.reply_text(
        "3️⃣ Pre-question text/media चाहिए तो text भेजें। नहीं चाहिए तो <code>-</code> भेजें।",
        parse_mode="HTML"
    )
    return PRETEXT

async def got_pretext(update, context):
    text = update.message.text.strip()
    context.user_data["creator"]["pre_text"] = "" if text == "-" else text
    await ask_question_text(update, context)
    return QTEXT

async def ask_question_text(update, context):
    n = len(context.user_data["creator"]["questions"]) + 1
    await update.effective_message.reply_text(
        f"📝 <b>Question {n}</b>\nपूरा question text भेजें।\n"
        "सिर्फ <code>/done</code> भेजकर quiz पूरा कर सकते हैं।",
        parse_mode="HTML"
    )

async def got_question(update, context):
    text = update.message.text.strip()
    if text == "/done":
        return await finalize_creation(update, context)
    context.user_data["creator"]["current_q"] = text
    await update.message.reply_text(
        "अब options भेजें — एक line में एक option.\n"
        "उदाहरण:\nA) विकल्प 1\nB) विकल्प 2\nC) विकल्प 3\nD) विकल्प 4"
    )
    return OPTIONS

async def got_options(update, context):
    raw = update.message.text.strip()
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    cleaned = []
    for line in lines:
        if len(line) >= 2 and line[0].upper() in "ABCD" and line[1] in ").:-":
            line = line[2:].strip()
        cleaned.append(line)
    if len(cleaned) != 4:
        await update.message.reply_text("ठीक 4 options भेजें, 1 line = 1 option.")
        return OPTIONS
    if any(not x for x in cleaned):
        await update.message.reply_text("कोई option खाली नहीं होना चाहिए।")
        return OPTIONS
    context.user_data["creator"]["current_options"] = cleaned
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("A", callback_data="correct:0"),
        InlineKeyboardButton("B", callback_data="correct:1"),
        InlineKeyboardButton("C", callback_data="correct:2"),
        InlineKeyboardButton("D", callback_data="correct:3"),
    ]])
    await update.message.reply_text("सही उत्तर चुनें:", reply_markup=kb)
    return CORRECT

async def got_correct(update, context):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("correct:"):
        return CORRECT
    idx = int(q.data.split(":")[1])
    context.user_data["creator"]["current_correct"] = idx
    await q.message.reply_text(
        "Explanation भेजें। नहीं चाहिए तो <code>-</code> भेजें।",
        parse_mode="HTML"
    )
    return EXPLANATION

async def got_explanation(update, context):
    text = update.message.text.strip()
    c = context.user_data["creator"]
    c["questions"].append({
        "question": c.pop("current_q"),
        "options": c.pop("current_options"),
        "correct_index": c.pop("current_correct"),
        "explanation": "" if text == "-" else text,
    })
    if len(c["questions"]) >= MAX_QUESTIONS:
        return await finalize_creation(update, context)
    await update.message.reply_text(
        f"✅ Question saved. अभी {len(c['questions'])} question हैं।\n"
        "अगला question भेजें, या /done."
    )
    return QTEXT

async def finalize_creation(update, context):
    c = context.user_data.get("creator")
    if not c or not c.get("title"):
        await update.effective_message.reply_text("Creation data नहीं मिला। /newquiz से शुरू करें।")
        return ConversationHandler.END
    if not c["questions"]:
        await update.effective_message.reply_text("कम से कम 1 question चाहिए।")
        return QTEXT

    qid = db.new_quiz(c["owner_id"], c["title"], c.get("description",""))
    db.update_quiz(qid, pre_text=c.get("pre_text",""))
    for q in c["questions"]:
        db.add_question(qid, q["question"], q["options"], q["correct_index"], q["explanation"])
    context.user_data.pop("creator", None)

    await update.effective_message.reply_text(
        f"🎉 <b>Quiz तैयार है!</b>\n\n"
        f"📌 {c['title']}\n"
        f"📝 Questions: {len(c['questions'])}",
        parse_mode="HTML",
        reply_markup=quiz_menu(qid)
    )
    return ConversationHandler.END

async def show_quiz(update, qid):
    q = db.get_quiz(qid)
    if not q:
        await update.effective_message.reply_text("Quiz नहीं मिला।")
        return
    questions = db.get_questions(qid)
    text = (
        f"🎯 <b>{q['title']}</b>\n"
        f"📄 {q['description'] or 'No description'}\n\n"
        f"📝 Questions: {len(questions)}\n"
        f"⏱ Timer: {'No timer' if q['timer']==0 else str(q['timer'])+' sec'}\n"
        f"🔀 Questions: {'ON' if q['shuffle_questions'] else 'OFF'}\n"
        f"🔀 Options: {'ON' if q['shuffle_options'] else 'OFF'}"
    )
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=quiz_menu(qid)
    )

async def my_quizzes(update, context):
    user = update.effective_user
    rows = db.get_owner_quizzes(user.id)
    if not rows:
        await update.effective_message.reply_text(
            "अभी कोई quiz नहीं है।", reply_markup=main_menu()
        )
        return
    buttons = []
    for r in rows[:30]:
        buttons.append([InlineKeyboardButton(
            f"📘 {r['title'][:45]}", callback_data=f"quiz:{r['id']}"
        )])
    buttons.append([InlineKeyboardButton("➕ New Quiz", callback_data="new")])
    await update.effective_message.reply_text(
        "📚 <b>My Quizzes</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def import_prompt(update, context):
    await update.effective_message.reply_text(
        "📥 PDF या plain text भेजें।\n\n"
        "PDF selectable-text होना चाहिए। Bot question formats जैसे statement, "
        "matching, assertion-reason और ordering को पहचानता है।\n"
        "हर question में सही उत्तर स्पष्ट होना जरूरी है।"
    )

async def handle_source(update, context):
    user = update.effective_user
    db.upsert_user(user)

    if update.message.document and update.message.document.file_name.lower().endswith(".pdf"):
        doc = update.message.document
        tmp = Path(tempfile.gettempdir()) / f"{doc.file_unique_id}.pdf"
        tgfile = await doc.get_file()
        await tgfile.download_to_drive(tmp)
        try:
            questions = parse_pdf(str(tmp))
        except Exception as e:
            await update.message.reply_text(f"❌ PDF parse error:\n{e}")
            return
        finally:
            tmp.unlink(missing_ok=True)
    else:
        text = update.message.text or ""
        if not text.strip():
            return
        try:
            questions = parse_text(text)
        except Exception as e:
            await update.message.reply_text(f"❌ Text parse नहीं हुआ:\n{e}")
            return

    # Do not let the generic text/source handler consume messages that belong
    # to an active manual-creation/edit session.
    if update.effective_user.id in CREATORS or context.user_data.get("creator"):
        return

    # Store as a quiz owned by uploader.
    title = (update.message.caption or "").strip() or "Imported Quiz"
    qid = db.new_quiz(user.id, title, "Imported from PDF/Text")
    for q in questions:
        db.add_question(qid, q["question"], q["options"], q["correct_index"], q.get("explanation",""))
    await update.message.reply_text(
        f"✅ Import successful!\n\n📘 {title}\n📝 {len(questions)} questions",
        reply_markup=quiz_menu(qid)
    )

async def cb_handler(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "my":
        return await my_quizzes(update, context)
    if data == "new":
        return await cb_new(update, context)
    if data == "import":
        return await import_prompt(update, context)
    if data == "help":
        await q.message.reply_text(help_text(), parse_mode="HTML")
        return
    if data == "settings":
        await q.message.reply_text(
            "Settings quiz के अंदर available हैं। My Quizzes → Quiz → Edit Quiz."
        )
        return

    if ":" not in data:
        return
    action, qid, *rest = data.split(":")
    quiz = db.get_quiz(qid)
    if not quiz:
        await q.message.reply_text("Quiz नहीं मिला।")
        return

    if action == "quiz":
        await show_quiz(update, qid)
    elif action == "start":
        await start_quiz(update, context, qid)
    elif action == "share":
        await share_quiz(update, context, qid)
    elif action == "edit":
        await q.message.reply_text("✏️ Edit Quiz", reply_markup=edit_menu(qid))
    elif action == "stats":
        await send_stats(q.message, qid)
    elif action == "del":
        if quiz["owner_id"] != update.effective_user.id:
            await q.message.reply_text("यह quiz आपका नहीं है।")
            return
        db.delete_quiz(qid, update.effective_user.id)
        await q.message.reply_text("🗑 Quiz deleted.", reply_markup=main_menu())
    elif action == "etitle":
        CREATORS[update.effective_user.id] = {"mode":"etitle","qid":qid}
        await q.message.reply_text("नया title भेजें।")
    elif action == "edesc":
        CREATORS[update.effective_user.id] = {"mode":"edesc","qid":qid}
        await q.message.reply_text("नई description भेजें।")
    elif action == "addq":
        if quiz["owner_id"] != update.effective_user.id:
            await q.message.reply_text("यह quiz आपका नहीं है।")
            return
        if len(db.get_questions(qid)) >= MAX_QUESTIONS:
            await q.message.reply_text("100 questions की सीमा पूरी हो चुकी है।")
            return
        CREATORS[update.effective_user.id] = {"mode":"addq","qid":qid,"step":"q"}
        await q.message.reply_text("नया question भेजें।")
    elif action == "delqmenu":
        questions = db.get_questions(qid)
        if not questions:
            await q.message.reply_text("कोई question नहीं है।")
            return
        rows = [[InlineKeyboardButton(
            f"{i+1}. {x['question'][:35]}", callback_data=f"delq:{qid}:{x['id']}"
        )] for i,x in enumerate(questions)]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"edit:{qid}")])
        await q.message.reply_text("Delete करने वाला question चुनें:", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "delq":
        db.delete_question(int(rest[0]), qid)
        await q.message.reply_text("✅ Question deleted.", reply_markup=edit_menu(qid))
    elif action == "timer":
        await q.message.reply_text("⏱ Timer चुनें:", reply_markup=timer_menu(qid))
    elif action == "settimer":
        value = int(rest[0])
        db.update_quiz(qid, timer=value)
        await q.message.reply_text("✅ Timer updated.", reply_markup=edit_menu(qid))
    elif action == "sq":
        db.update_quiz(qid, shuffle_questions=0 if quiz["shuffle_questions"] else 1)
        await q.message.reply_text("✅ Question shuffle updated.", reply_markup=edit_menu(qid))
    elif action == "so":
        db.update_quiz(qid, shuffle_options=0 if quiz["shuffle_options"] else 1)
        await q.message.reply_text("✅ Option shuffle updated.", reply_markup=edit_menu(qid))

async def text_state_router(update, context):
    user_id = update.effective_user.id
    state = CREATORS.get(user_id)
    if not state:
        return
    qid = state["qid"]
    mode = state["mode"]
    text = update.message.text.strip()

    if mode == "etitle":
        db.update_quiz(qid, title=text)
        CREATORS.pop(user_id, None)
        await update.message.reply_text("✅ Title updated.", reply_markup=quiz_menu(qid))
    elif mode == "edesc":
        db.update_quiz(qid, description="" if text=="-" else text)
        CREATORS.pop(user_id, None)
        await update.message.reply_text("✅ Description updated.", reply_markup=quiz_menu(qid))
    elif mode == "addq":
        if state["step"] == "q":
            state["question"] = text
            state["step"] = "options"
            await update.message.reply_text("4 options भेजें, एक line में एक।")
        elif state["step"] == "options":
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            cleaned = []
            for line in lines:
                if len(line)>=2 and line[0].upper() in "ABCD" and line[1] in ").:-":
                    line=line[2:].strip()
                cleaned.append(line)
            if len(cleaned)!=4:
                await update.message.reply_text("ठीक 4 options भेजें।")
                return
            state["options"]=cleaned
            state["step"]="correct"
            await update.message.reply_text(
                "सही answer चुनें:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("A",callback_data=f"ac:{qid}:0"),
                    InlineKeyboardButton("B",callback_data=f"ac:{qid}:1"),
                    InlineKeyboardButton("C",callback_data=f"ac:{qid}:2"),
                    InlineKeyboardButton("D",callback_data=f"ac:{qid}:3"),
                ]])
            )

async def add_correct_callback(update, context):
    q = update.callback_query
    await q.answer()
    data=q.data
    _,qid,idx=data.split(":")
    state=CREATORS.get(update.effective_user.id)
    if not state or state.get("qid")!=qid:
        await q.message.reply_text("Session expired. फिर से Add Question करें।")
        return
    state["correct"]=int(idx)
    db.add_question(qid,state["question"],state["options"],state["correct"],"")
    CREATORS.pop(update.effective_user.id,None)
    await q.message.reply_text("✅ Question added.",reply_markup=quiz_menu(qid))

async def share_quiz(update, context, qid):
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=quiz_{qid}"
    await update.effective_message.reply_text(
        f"🔗 <b>Quiz Share Link</b>\n\n{link}\n\n"
        "यह link users को bot की private chat में quiz शुरू करने देगा।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start Quiz", url=link)]
        ])
    )

async def start_quiz(update, context, qid):
    user = update.effective_user
    quiz = db.get_quiz(qid)
    if not quiz:
        await update.effective_message.reply_text("Quiz नहीं मिला।")
        return
    questions = db.get_questions(qid)
    if not questions:
        await update.effective_message.reply_text("इस quiz में questions नहीं हैं।")
        return

    # Native poll sessions are kept per user in private chat.
    if update.effective_chat.type != ChatType.PRIVATE:
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start=quiz_{qid}"
        await update.effective_message.reply_text(
            "👤 इस quiz का independent result/session private chat में चलेगा।\n"
            "नीचे Start Quiz दबाएँ:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Start Privately", url=link)]
            ])
        )
        return

    order = build_order(questions, bool(quiz["shuffle_questions"]))
    attempt = db.create_attempt(qid,user.id,update.effective_chat.id,len(order))
    RUNNERS[user.id] = {
        "qid":qid,
        "quiz":dict(quiz),
        "questions":order,
        "index":0,
        "attempt_id":attempt,
        "correct":0,
        "wrong":0,
        "skipped":0,
        "started":time.monotonic(),
        "current":None,
        "finished":False,
    }
    if quiz["pre_text"]:
        await update.effective_message.reply_text(quiz["pre_text"])
    await update.effective_message.reply_text(
        f"🎯 <b>{quiz['title']}</b>\n"
        f"📝 {len(order)} questions\n"
        f"⏱ {quiz['timer'] or 'No'} timer\n\n"
        "Quiz शुरू हो रहा है…",
        parse_mode="HTML"
    )
    await send_next(user.id, context)

async def send_next(user_id, context):
    s = RUNNERS.get(user_id)
    if not s or s["finished"]:
        return
    if s["index"] >= len(s["questions"]):
        await finish_runner(user_id, context)
        return

    q = s["questions"][s["index"]]
    options, correct = poll_data(q, bool(s["quiz"]["shuffle_options"]))
    poll_q = short_text(q["question"], 300)

    # Full source wording is always sent before the native poll.
    full = (
        f"❓ <b>Question {s['index']+1}/{len(s['questions'])}</b>\n\n"
        f"{q['question']}\n\n"
        + "\n".join(f"{chr(65+i)}) {x}" for i,x in enumerate(q["options"]))
    )
    if len(full) <= 4096:
        await context.bot.send_message(user_id, full)
    else:
        # Preserve text exactly, but split at Telegram's message limit.
        raw = (
            f"Question {s['index']+1}/{len(s['questions'])}\n\n"
            f"{q['question']}\n\n"
            + "\n".join(f"{chr(65+i)}) {x}" for i,x in enumerate(q["options"]))
        )
        for i in range(0, len(raw), 3900):
            await context.bot.send_message(user_id, raw[i:i+3900])

    # Native poll options have a hard 100-char limit, so very long source options
    # are displayed fully above and the poll uses compact labels.
    poll_options = options
    if any(len(x) > 100 for x in poll_options):
        poll_options = [f"{chr(65+i)})" for i in range(4)]
        # The compact A/B/C/D labels use the original source order.
        correct = q["correct_index"]
    explanation = q.get("explanation","") or None

    msg = await context.bot.send_poll(
        chat_id=user_id,
        question=poll_q,
        options=poll_options,
        type="quiz",
        correct_option_ids=[correct],
        is_anonymous=False,
        allows_multiple_answers=False,
        allows_revoting=False,
        shuffle_options=False,
        explanation=short_text(explanation,200) if explanation else None,
        open_period=int(s["quiz"]["timer"]) if s["quiz"]["timer"] else None,
    )
    s["current"] = {
        "poll_id":msg.poll.id,
        "question_id":q["id"],
        "correct":correct,
        "started":time.monotonic(),
    }
    POLL_MAP[msg.poll.id] = user_id
    if s["quiz"]["timer"]:
        context.job_queue.run_once(
            poll_timeout_job,
            when=int(s["quiz"]["timer"]) + 1,
            data=user_id,
            name=f"timeout:{user_id}:{msg.poll.id}",
        )

async def poll_answer(update, context):
    ans = update.poll_answer
    user_id = ans.user.id
    s = RUNNERS.get(user_id)
    if not s or s["finished"]:
        return
    cur = s.get("current")
    if not cur or ans.poll_id != cur["poll_id"]:
        return
    chosen = ans.option_ids[0] if ans.option_ids else None
    elapsed = time.monotonic() - cur["started"]
    if chosen is None:
        s["skipped"] += 1
        correct = False
    else:
        correct = chosen == cur["correct"]
        if correct:
            s["correct"] += 1
        else:
            s["wrong"] += 1
    db.save_answer(s["attempt_id"],cur["question_id"],chosen,correct,elapsed)
    s["index"] += 1
    s["current"] = None
    await asyncio.sleep(0.4)
    await send_next(user_id, context)

async def poll_timeout_job(context):
    user_id = context.job.data
    s = RUNNERS.get(user_id)
    if not s or s["finished"] or not s.get("current"):
        return
    cur = s["current"]
    # If Telegram auto-closed the poll, mark as skipped.
    if s["quiz"]["timer"]:
        elapsed = time.monotonic() - cur["started"]
        if elapsed >= s["quiz"]["timer"] - 0.5:
            s["skipped"] += 1
            db.save_answer(s["attempt_id"],cur["question_id"],None,False,elapsed)
            s["index"] += 1
            s["current"] = None
            await context.bot.send_message(user_id, "⏰ Time up! अगला question…")
            await send_next(user_id, context)

async def finish_runner(user_id, context):
    s = RUNNERS.get(user_id)
    if not s or s["finished"]:
        return
    s["finished"] = True
    total=len(s["questions"])
    correct=s["correct"]; wrong=s["wrong"]; skipped=s["skipped"]
    score=correct
    db.finish_attempt(s["attempt_id"],score,correct,wrong,skipped)
    pct=(correct/total*100) if total else 0
    await context.bot.send_message(
        user_id,
        f"🏁 <b>Quiz Complete</b>\n\n"
        f"📊 Total: {total}\n"
        f"✅ Correct: {correct}\n"
        f"❌ Wrong: {wrong}\n"
        f"⏭ Skipped: {skipped}\n"
        f"🎯 Score: {score}/{total}\n"
        f"📈 Percentage: {pct:.2f}%",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Restart", callback_data=f"start:{s['qid']}")],
            [InlineKeyboardButton("📊 Stats", callback_data=f"stats:{s['qid']}")],
        ])
    )

async def send_stats(message, qid):
    stat, leaders = db.quiz_stats(qid)
    q = db.get_quiz(qid)
    lines = [
        f"📊 <b>{q['title']}</b>",
        f"👥 Attempts: {stat['attempts']}",
        f"✅ Total correct answers: {stat['correct']}",
        f"❌ Total wrong answers: {stat['wrong']}",
        f"⏭ Total skipped: {stat['skipped']}",
    ]
    if leaders:
        lines.append("\n🏆 Recent top results:")
        for i,r in enumerate(leaders,1):
            name=(r["username"] and "@"+r["username"]) or r["first_name"] or str(r["user_id"])
            lines.append(f"{i}. {name} — {r['correct']} correct")
    await message.reply_text("\n".join(lines))

async def stop_cmd(update, context):
    user_id=update.effective_user.id
    s=RUNNERS.get(user_id)
    if not s:
        await update.effective_message.reply_text("कोई active quiz नहीं है।")
        return
    s["finished"]=True
    db.finish_attempt(s["attempt_id"],s["correct"],s["correct"],s["wrong"],s["skipped"])
    RUNNERS.pop(user_id,None)
    await update.effective_message.reply_text(
        "⏹ Quiz stopped.",
        reply_markup=main_menu()
    )

async def error_handler(update, context):
    log.exception("Unhandled exception", exc_info=context.error)

def build_app():
    db.init_db()
    app=Application.builder().token(BOT_TOKEN).build()

    conv=ConversationHandler(
        entry_points=[
            CommandHandler("newquiz", new_quiz_cmd),
            CallbackQueryHandler(cb_new, pattern="^new$")
        ],
        states={
            TITLE:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)],
            DESCRIPTION:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_description)],
            PRETEXT:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_pretext)],
            QTEXT:[MessageHandler(filters.TEXT, got_question)],
            OPTIONS:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_options)],
            CORRECT:[CallbackQueryHandler(got_correct, pattern="^correct:")],
            EXPLANATION:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_explanation)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("newquiz", new_quiz_cmd))
    app.add_handler(CommandHandler("myquizzes", my_quizzes))
    app.add_handler(CommandHandler("stop", stop_cmd))

    app.add_handler(CallbackQueryHandler(add_correct_callback, pattern="^ac:"))
    app.add_handler(CallbackQueryHandler(cb_handler))

    # Creator edit text router before generic source text.
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_state_router,
        block=False
    ))
    app.add_handler(MessageHandler(
        filters.Document.PDF | (filters.TEXT & ~filters.COMMAND),
        handle_source
    ))
    app.add_handler(PollAnswerHandler(poll_answer))

    app.add_error_handler(error_handler)
    return app

if __name__ == "__main__":
    app=build_app()
    log.info("QuizBot Pro starting…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
