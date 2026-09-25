"""Quiz creation wizard, quiz editor and PDF/Text import.

State is persisted in the ``user_states`` table (not in RAM), so a bot restart
in the middle of creating a quiz loses nothing.

States
  c_title, c_desc, c_pre, c_q, c_q_confirm, c_opts, c_correct, c_expl, c_more
  e_title, e_desc, e_expl
  imp_wait, imp_review, imp_title
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

import config
import database as db
import keyboards as kb
import pdf_parser
import quiz_engine as engine
from keyboards import esc

log = logging.getLogger(__name__)

TITLE_MAX = 150
DESC_MAX = 1000
EXPL_MAX = 3000


# ------------------------------------------------------------ helpers
async def respond(update: Update, text: str, markup=None, parse_mode=ParseMode.HTML, edit: bool = True):
    """Edit the callback message if possible, otherwise send a new message."""
    q = update.callback_query
    if q is not None and edit and q.message is not None and q.message.text is not None:
        try:
            return await q.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode,
                                             disable_web_page_preview=True)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return None
    return await update.effective_chat.send_message(text, reply_markup=markup, parse_mode=parse_mode,
                                                    disable_web_page_preview=True)


def skip_kb(cb: str, label: str = "⏭ Skip") -> M:
    return M([[B(label, callback_data=cb)], [B("❌ Cancel", callback_data="c:cancel")]])


def owns(quiz: Optional[dict], user_id: int) -> bool:
    return bool(quiz) and quiz["owner_id"] == user_id


def quiz_summary(quiz: dict) -> str:
    lines = [f"🎯 <b>{esc(quiz['title'])}</b>"]
    if quiz.get("description"):
        lines.append(esc(quiz["description"]))
    lines += [
        "",
        f"📝 Questions: {quiz.get('question_count', db.count_questions(quiz['id']))}/{config.MAX_QUESTIONS}",
        f"⏱ Timer: {config.timer_label(quiz.get('timer', 0))}",
        f"🔀 Shuffle questions: {'ON' if quiz.get('shuffle_questions') else 'OFF'}",
        f"🔀 Shuffle options: {'ON' if quiz.get('shuffle_options') else 'OFF'}",
        f"🆔 <code>{quiz['id']}</code>",
    ]
    if quiz.get("status") == "draft":
        lines.append("\n📝 <i>Draft — /done से पूरा करें</i>")
    return "\n".join(lines)


def question_full_text(q: dict, number: int) -> str:
    lines = [f"प्रश्न {number} ({pdf_parser.QTYPE_LABELS.get(q.get('qtype', 'mcq'), 'MCQ')})", "", q["question"], ""]
    for i, o in enumerate(q["options"]):
        mark = " ✅" if i == q["correct_index"] else ""
        lines.append(f"({config.OPTION_LABELS[i]}) {o}{mark}")
    if q.get("explanation"):
        lines += ["", "💡 " + q["explanation"]]
    return "\n".join(lines)


# ============================================================ WIZARD
async def begin_new_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_user(user)
    state, data = db.get_state(user.id)
    if state and state.startswith("c_") and data.get("mode") == "new" and data.get("quiz_id"):
        # abandon the old draft cleanly if it has no questions
        if db.count_questions(data["quiz_id"]) == 0:
            db.delete_quiz(data["quiz_id"], user.id)
    db.set_state(user.id, "c_title", {"mode": "new"})
    await update.effective_chat.send_message(
        "➕ <b>New Quiz</b>\n\n1️⃣ <b>Step 1:</b> Quiz का <b>नाम</b> भेजें।\n\n/cancel — रद्द करें",
        parse_mode=ParseMode.HTML)


async def _prompt_pre(update: Update, data: dict) -> None:
    n = data.get("n_saved", 0) + 1
    done_hint = "\n/done — quiz पूरा करें" if data.get("n_saved", 0) else ""
    await update.effective_chat.send_message(
        f"3️⃣ <b>Step 3 (optional):</b> प्रश्न {n} से पहले दिखाने के लिए कोई text, photo, video "
        f"या document भेजें — या <b>Skip</b> दबाएँ।{done_hint}",
        parse_mode=ParseMode.HTML, reply_markup=skip_kb("c:skippre"))


async def _prompt_question(update: Update, data: dict) -> None:
    n = data.get("n_saved", 0) + 1
    await update.effective_chat.send_message(
        f"4️⃣ <b>Step 4:</b> प्रश्न {n} का <b>पूरा text</b> भेजें।\n\n"
        "Statement, matching, List-I/List-II, assertion-reason, ordering — सब multi-line लिख सकते हैं; "
        "text exactly वैसा ही save होगा।\n"
        "💡 Tip: प्रश्न के साथ (A)(B)(C)(D) options भी paste कर सकते हैं — bot पहचान लेगा।",
        parse_mode=ParseMode.HTML)


async def _prompt_options(update: Update) -> None:
    await update.effective_chat.send_message(
        f"5️⃣ <b>Step 5:</b> <b>{config.MIN_OPTIONS}–{config.MAX_OPTIONS} options</b> भेजें — हर line में एक option:\n\n"
        "<code>(A) पहला विकल्प\n(B) दूसरा विकल्प\n(C) तीसरा विकल्प\n(D) चौथा विकल्प</code>\n\n"
        "(labels optional हैं; लंबे options भी चलेंगे)",
        parse_mode=ParseMode.HTML)


async def _prompt_correct(update: Update, options: list[str]) -> None:
    lines = ["6️⃣ <b>Step 6:</b> सही उत्तर चुनें:", ""]
    for i, o in enumerate(options):
        lines.append(f"<b>({config.OPTION_LABELS[i]})</b> {esc(o)}")
    text = "\n".join(lines)
    if len(text) > config.MESSAGE_MAX:
        text = "6️⃣ <b>Step 6:</b> सही उत्तर चुनें (options ऊपर के message में हैं):"
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML,
                                             reply_markup=kb.correct_picker(options))


async def _prompt_expl(update: Update) -> None:
    await update.effective_chat.send_message(
        "7️⃣ <b>Step 7 (optional):</b> Explanation भेजें (उत्तर देने के बाद दिखेगी) — या Skip दबाएँ।",
        parse_mode=ParseMode.HTML, reply_markup=skip_kb("c:skipexpl"))


async def _prompt_more(update: Update, data: dict) -> None:
    n = data.get("n_saved", 0)
    rows = []
    if n < config.MAX_QUESTIONS:
        rows.append([B("➕ Add another question", callback_data="c:more")])
    rows.append([B("✅ Done — quiz पूरा करें", callback_data="c:done")])
    await update.effective_chat.send_message(
        f"✅ प्रश्न saved! कुल: <b>{n}/{config.MAX_QUESTIONS}</b>\n\n"
        "8️⃣ <b>Step 8:</b> और प्रश्न जोड़ना है?\n(या /done भेजें)",
        parse_mode=ParseMode.HTML, reply_markup=M(rows))


def _media_from_message(msg) -> tuple[str, str]:
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    for kind in ("video", "animation", "document", "audio", "voice"):
        obj = getattr(msg, kind, None)
        if obj:
            return kind, obj.file_id
    return "", ""


async def _save_current_question(update: Update, user_id: int, data: dict, explanation: str) -> None:
    cur = data.get("cur", {})
    qid = data["quiz_id"]
    try:
        db.add_question(
            qid, cur["question"], cur["options"], int(cur["correct"]), explanation,
            qtype=engine.detect_qtype(cur["question"]), pre_text=cur.get("pre_text", ""),
            pre_media_type=cur.get("pre_media_type", ""), pre_media_id=cur.get("pre_media_id", ""))
    except (ValueError, KeyError) as exc:
        await update.effective_chat.send_message(f"❌ प्रश्न save नहीं हुआ: {esc(exc)}", parse_mode=ParseMode.HTML)
        data["cur"] = {}
        db.set_state(user_id, "c_pre", data)
        await _prompt_pre(update, data)
        return
    data["n_saved"] = db.count_questions(qid)
    data["cur"] = {}
    total = db.count_questions(qid)
    if total >= config.MAX_QUESTIONS:
        await update.effective_chat.send_message(f"ℹ️ {config.MAX_QUESTIONS} प्रश्नों की सीमा पूरी हो गई।")
        await finalize(update, context=None)
        return
    db.set_state(user_id, "c_more", data)
    await _prompt_more(update, data)


async def finalize(update: Update, context) -> None:
    """/done — finish creating (or adding questions)."""
    user = update.effective_user
    state, data = db.get_state(user.id)
    if not state or not state.startswith("c_"):
        await update.effective_chat.send_message("ℹ️ अभी कोई quiz creation चालू नहीं है।", reply_markup=kb.main_menu())
        return
    qid = data.get("quiz_id")
    if not qid:
        db.clear_state(user.id)
        await update.effective_chat.send_message("❌ Quiz बनाना रद्द हुआ (नाम नहीं मिला)।", reply_markup=kb.main_menu())
        return
    n = db.count_questions(qid)
    if n == 0:
        await update.effective_chat.send_message(
            "⚠️ Quiz में कम से कम 1 प्रश्न चाहिए। प्रश्न जोड़ें या /cancel भेजें।")
        return
    partial = bool(data.get("cur", {}).get("question"))
    db.clear_state(user.id)
    if data.get("mode") == "new":
        db.update_quiz(qid, status="ready")
    quiz = db.get_quiz(qid)
    note = "\n⚠️ अधूरा प्रश्न (बिना सही उत्तर/options) save नहीं हुआ।" if partial else ""
    if data.get("mode") == "add":
        await update.effective_chat.send_message(
            f"✅ प्रश्न जोड़ दिए गए। अब कुल {n} प्रश्न।{note}", reply_markup=kb.edit_menu(qid, quiz))
        return
    me = await update.get_bot().get_me()
    link = engine.deep_link(me.username, qid)
    await update.effective_chat.send_message(
        f"🎉 <b>Quiz तैयार है!</b>{note}\n\n{quiz_summary(quiz)}\n\n🔗 Share link:\n{link}",
        parse_mode=ParseMode.HTML, reply_markup=kb.quiz_card(qid, True), disable_web_page_preview=True)


async def cancel(update: Update, context) -> None:
    user = update.effective_user
    state, data = db.get_state(user.id)
    if not state:
        await update.effective_chat.send_message("ℹ️ रद्द करने के लिए कुछ नहीं है।", reply_markup=kb.main_menu())
        return
    db.clear_state(user.id)
    msg = "❌ रद्द किया गया।"
    if state.startswith("c_") and data.get("mode") == "new" and data.get("quiz_id"):
        db.delete_quiz(data["quiz_id"], user.id)
        msg = "❌ Quiz creation रद्द — draft delete कर दिया गया।"
    elif state.startswith("c_") and data.get("mode") == "add":
        msg = "❌ Add question रद्द। पहले से saved प्रश्न quiz में रहेंगे।"
    elif state.startswith("imp_"):
        msg = "❌ Import रद्द।"
    await update.effective_chat.send_message(msg, reply_markup=kb.main_menu())


async def handle_creator_message(update: Update, context, state: str, data: dict) -> None:
    msg = update.effective_message
    user = update.effective_user
    text = (msg.text or msg.caption or "")
    stripped = text.strip()

    if state == "c_title":
        if not stripped or msg.text is None:
            await msg.reply_text("⚠️ Quiz का नाम text में भेजें।")
            return
        if len(stripped) > TITLE_MAX:
            await msg.reply_text(f"⚠️ नाम अधिकतम {TITLE_MAX} characters का हो सकता है।")
            return
        qid = db.new_quiz(user.id, stripped, "", status="draft", source="manual")
        data.update({"quiz_id": qid, "n_saved": 0, "cur": {}})
        db.set_state(user.id, "c_desc", data)
        await msg.reply_text("2️⃣ <b>Step 2:</b> Quiz का description भेजें — या Skip दबाएँ।",
                             parse_mode=ParseMode.HTML, reply_markup=skip_kb("c:skipdesc"))
        return

    if state == "c_desc":
        if msg.text is None:
            await msg.reply_text("⚠️ Description text में भेजें या Skip दबाएँ।")
            return
        if len(stripped) > DESC_MAX:
            await msg.reply_text(f"⚠️ Description अधिकतम {DESC_MAX} characters।")
            return
        db.update_quiz(data["quiz_id"], description=stripped)
        db.set_state(user.id, "c_pre", data)
        await _prompt_pre(update, data)
        return

    if state == "c_pre":
        mtype, mid = _media_from_message(msg)
        if not mtype and not stripped:
            await msg.reply_text("⚠️ Text/media भेजें या Skip दबाएँ।")
            return
        data["cur"] = {"pre_text": text if text.strip() else "", "pre_media_type": mtype, "pre_media_id": mid}
        db.set_state(user.id, "c_q", data)
        await msg.reply_text("✅ Pre-question content saved.")
        await _prompt_question(update, data)
        return

    if state == "c_q":
        if msg.text is None or not stripped:
            await msg.reply_text("⚠️ प्रश्न text में भेजें।")
            return
        detected = engine.detect_embedded_options(msg.text)
        cur = data.setdefault("cur", {})
        if detected:
            cur["full_text"] = msg.text.strip()
            cur["question"], cur["options"] = detected[0], detected[1]
            db.set_state(user.id, "c_q_confirm", data)
            # Plain text (no HTML) so nothing needs escaping; split losslessly —
            # the user must see EVERY detected option before confirming.
            preview = "\n".join(f"({config.OPTION_LABELS[i]}) {o}" for i, o in enumerate(detected[1]))
            chunks = engine.split_message(
                f"🔎 आपके text में ये {len(detected[1])} options मिले:\n\n{preview}\n\nक्या इन्हें options मानें?")
            confirm = M([[B("✅ हाँ, ये options हैं", callback_data="c:useopts")],
                         [B("✏️ नहीं, options अलग भेजूँगा", callback_data="c:sepopts")]])
            for n, chunk in enumerate(chunks):
                await msg.reply_text(chunk, reply_markup=confirm if n == len(chunks) - 1 else None)
            return
        cur["question"] = msg.text.strip()
        db.set_state(user.id, "c_opts", data)
        await _prompt_options(update)
        return

    if state == "c_opts":
        if msg.text is None:
            await msg.reply_text("⚠️ Options text में भेजें।")
            return
        try:
            options = engine.parse_manual_options(msg.text)
        except ValueError as exc:
            await msg.reply_text(f"⚠️ {exc}")
            return
        data["cur"]["options"] = options
        db.set_state(user.id, "c_correct", data)
        await _prompt_correct(update, options)
        return

    if state == "c_correct":
        await msg.reply_text("👆 ऊपर के buttons से सही उत्तर चुनें।")
        return

    if state == "c_expl":
        if msg.text is None:
            await msg.reply_text("⚠️ Explanation text में भेजें या Skip दबाएँ।")
            return
        if len(stripped) > EXPL_MAX:
            await msg.reply_text(f"⚠️ Explanation अधिकतम {EXPL_MAX} characters।")
            return
        await _save_current_question(update, user.id, data, stripped)
        return

    if state == "c_q_confirm":
        await msg.reply_text("👆 ऊपर के buttons से चुनें।")
        return

    if state == "c_more":
        # Typing a question directly is treated as "add another"
        if msg.text and stripped:
            data["cur"] = {}
            db.set_state(user.id, "c_q", data)
            await handle_creator_message(update, context, "c_q", data)
            return
        await msg.reply_text("👆 'Add another' या 'Done' चुनें, या /done भेजें।")


async def handle_creator_callback(update: Update, context) -> None:
    q = update.callback_query
    user = update.effective_user
    action = q.data.split(":", 1)[1]
    state, data = db.get_state(user.id)

    if action == "cancel":
        await q.answer()
        await cancel(update, context)
        return
    if not state or not state.startswith("c_"):
        await q.answer("⌛ यह step expire हो चुका है।", show_alert=True)
        return
    await q.answer()

    if action == "skipdesc" and state == "c_desc":
        db.set_state(user.id, "c_pre", data)
        await _prompt_pre(update, data)
    elif action == "skippre" and state == "c_pre":
        data["cur"] = {}
        db.set_state(user.id, "c_q", data)
        await _prompt_question(update, data)
    elif action == "useopts" and state == "c_q_confirm":
        cur = data["cur"]
        cur.pop("full_text", None)
        db.set_state(user.id, "c_correct", data)
        await _prompt_correct(update, cur["options"])
    elif action == "sepopts" and state == "c_q_confirm":
        cur = data["cur"]
        cur["question"] = cur.pop("full_text", cur.get("question", ""))
        cur.pop("options", None)
        db.set_state(user.id, "c_opts", data)
        await _prompt_options(update)
    elif action.startswith("correct:") and state == "c_correct":
        idx = int(action.split(":")[1])
        if not 0 <= idx < len(data["cur"].get("options") or []):
            return
        data["cur"]["correct"] = idx
        db.set_state(user.id, "c_expl", data)
        try:
            await q.edit_message_reply_markup(None)
        except TelegramError:
            pass
        await update.effective_chat.send_message(f"✅ सही उत्तर: ({config.OPTION_LABELS[idx]})")
        await _prompt_expl(update)
    elif action == "skipexpl" and state == "c_expl":
        await _save_current_question(update, user.id, data, "")
    elif action == "more" and state == "c_more":
        if db.count_questions(data["quiz_id"]) >= config.MAX_QUESTIONS:
            await update.effective_chat.send_message(f"⚠️ {config.MAX_QUESTIONS} प्रश्नों की सीमा पूरी है।")
            return
        data["cur"] = {}
        db.set_state(user.id, "c_pre", data)
        await _prompt_pre(update, data)
    elif action == "done":
        await finalize(update, context)
    else:
        await q.answer("⌛ यह button अब valid नहीं है।")


# ============================================================ EDITOR
async def show_edit_menu(update: Update, qid: str) -> None:
    quiz = db.get_quiz(qid)
    await respond(update, "✏️ <b>Edit Quiz</b>\n\n" + quiz_summary(quiz), kb.edit_menu(qid, quiz))


async def handle_edit_callback(update: Update, context) -> None:
    q = update.callback_query
    user = update.effective_user
    parts = q.data.split(":")
    action, qid = parts[1], parts[2] if len(parts) > 2 else ""
    quiz = db.get_quiz(qid)
    if not quiz:
        await q.answer("Quiz नहीं मिला।", show_alert=True)
        return
    if not owns(quiz, user.id):
        await q.answer("⛔ यह quiz आपका नहीं है।", show_alert=True)
        return
    await q.answer()
    back = f"q:edit:{qid}"

    if action == "title":
        db.set_state(user.id, "e_title", {"quiz_id": qid})
        await update.effective_chat.send_message(f"📝 नया title भेजें (अभी: {quiz['title']})\n/cancel — रद्द")
    elif action == "desc":
        db.set_state(user.id, "e_desc", {"quiz_id": qid})
        await update.effective_chat.send_message("📄 नई description भेजें (हटाने के लिए '-' भेजें)\n/cancel — रद्द")
    elif action == "addq":
        n = db.count_questions(qid)
        if n >= config.MAX_QUESTIONS:
            await update.effective_chat.send_message(f"⚠️ {config.MAX_QUESTIONS} प्रश्नों की सीमा पूरी है।")
            return
        data = {"mode": "add", "quiz_id": qid, "n_saved": n, "cur": {}}
        db.set_state(user.id, "c_pre", data)
        await _prompt_pre(update, data)
    elif action in ("delq", "expl", "view"):
        page = int(parts[3]) if len(parts) > 3 else 0
        questions = db.get_questions(qid)
        if not questions:
            await respond(update, "ℹ️ इस quiz में कोई प्रश्न नहीं है।", kb.edit_menu(qid, quiz))
            return
        sub = {"delq": "e:delqx", "expl": "e:explq", "view": "e:viewq"}[action]
        title = {"delq": "🗑 Delete करने वाला प्रश्न चुनें:", "expl": "💡 Explanation edit करने के लिए प्रश्न चुनें:",
                 "view": "👁 प्रश्न चुनें:"}[action]
        await respond(update, title, kb.question_picker(qid, questions, page, sub, back))
    elif action == "delqx":
        qs = db.get_question(qid, int(parts[3]))
        if not qs:
            await update.effective_chat.send_message("प्रश्न नहीं मिला।")
            return
        preview = qs["question"][:700]
        await respond(update, f"🗑 यह प्रश्न delete करें?\n\n{esc(preview)}",
                      M([[B("✅ Delete", callback_data=f"e:delqok:{qid}:{qs['id']}"),
                          B("❌ Cancel", callback_data=f"e:delq:{qid}:0")]]))
    elif action == "delqok":
        ok = db.delete_question(int(parts[3]), qid)
        quiz = db.get_quiz(qid)
        await respond(update, ("✅ प्रश्न delete हुआ।" if ok else "प्रश्न नहीं मिला।") + "\n\n" + quiz_summary(quiz),
                      kb.edit_menu(qid, quiz))
    elif action == "explq":
        qs = db.get_question(qid, int(parts[3]))
        if not qs:
            await update.effective_chat.send_message("प्रश्न नहीं मिला।")
            return
        db.set_state(user.id, "e_expl", {"quiz_id": qid, "question_id": qs["id"]})
        cur = qs.get("explanation") or "(कोई explanation नहीं)"
        text = f"💡 अभी की explanation:\n{cur}\n\nनई explanation भेजें।"
        for chunk in engine.split_message(text):
            await update.effective_chat.send_message(chunk)
        await update.effective_chat.send_message(
            "या:", reply_markup=M([[B("🗑 Explanation हटाएँ", callback_data=f"e:explrm:{qid}:{qs['id']}")],
                                   [B("❌ Cancel", callback_data=back)]]))
    elif action == "explrm":
        db.update_question(int(parts[3]), explanation="")
        db.clear_state(user.id)
        await respond(update, "✅ Explanation हटा दी गई।", kb.edit_menu(qid, db.get_quiz(qid)))
    elif action == "viewq":
        qs = db.get_question(qid, int(parts[3]))
        if not qs:
            await update.effective_chat.send_message("प्रश्न नहीं मिला।")
            return
        questions = db.get_questions(qid)
        pos = next((i for i, x in enumerate(questions, 1) if x["id"] == qs["id"]), 0)
        for chunk in engine.split_message(question_full_text(qs, pos)):
            await update.effective_chat.send_message(chunk)
        await update.effective_chat.send_message("⬆️", reply_markup=M([[B("⬅️ Back", callback_data=f"e:view:{qid}:0")]]))
    elif action == "timer":
        await respond(update, "⏱ प्रति प्रश्न timer चुनें:",
                      kb.timer_menu(f"e:settimer:{qid}", quiz["timer"], back))
    elif action == "settimer":
        val = int(parts[3])
        if val not in config.TIMER_CHOICES:
            return
        db.update_quiz(qid, timer=val)
        await show_edit_menu(update, qid)
    elif action == "sq":
        db.update_quiz(qid, shuffle_questions=0 if quiz["shuffle_questions"] else 1)
        await show_edit_menu(update, qid)
    elif action == "so":
        db.update_quiz(qid, shuffle_options=0 if quiz["shuffle_options"] else 1)
        await show_edit_menu(update, qid)


async def handle_edit_message(update: Update, context, state: str, data: dict) -> None:
    msg = update.effective_message
    user = update.effective_user
    qid = data.get("quiz_id")
    quiz = db.get_quiz(qid)
    if not owns(quiz, user.id):
        db.clear_state(user.id)
        await msg.reply_text("⌛ Session expire हो गया।", reply_markup=kb.main_menu())
        return
    text = (msg.text or "").strip()
    if not text:
        await msg.reply_text("⚠️ Text भेजें या /cancel।")
        return
    if state == "e_title":
        if len(text) > TITLE_MAX:
            await msg.reply_text(f"⚠️ अधिकतम {TITLE_MAX} characters।")
            return
        db.update_quiz(qid, title=text)
        done = "✅ Title updated."
    elif state == "e_desc":
        if len(text) > DESC_MAX:
            await msg.reply_text(f"⚠️ अधिकतम {DESC_MAX} characters।")
            return
        db.update_quiz(qid, description="" if text == "-" else text)
        done = "✅ Description updated."
    elif state == "e_expl":
        if len(text) > EXPL_MAX:
            await msg.reply_text(f"⚠️ अधिकतम {EXPL_MAX} characters।")
            return
        if not db.get_question(qid, data["question_id"]):
            db.clear_state(user.id)
            await msg.reply_text("प्रश्न नहीं मिला।")
            return
        db.update_question(data["question_id"], explanation="" if text == "-" else text)
        done = "✅ Explanation updated."
    else:
        return
    db.clear_state(user.id)
    quiz = db.get_quiz(qid)
    await msg.reply_text(done + "\n\n" + quiz_summary(quiz), parse_mode=ParseMode.HTML,
                         reply_markup=kb.edit_menu(qid, quiz))


# ============================================================ IMPORT
IMPORT_HELP = (
    "📥 <b>Import PDF/Text</b>\n\n"
    "<b>PDF</b> (text वाली या scanned — हिंदी/English OCR), <b>.txt</b> file, या सीधे <b>text</b> भेजें।\n\n"
    "• प्रश्न: <code>प्रश्न 1.</code> / <code>1.</code> / <code>1)</code> / <code>Q1</code> / <code>Q.1</code>\n"
    "• Options: <code>(A) (B) …</code> 2 से 12 तक, (E) भी (matching/assertion/ordering data के बाद आने वाले असली options पहचाने जाते हैं)\n"
    "• Numbering भाग/section में दोबारा 1 से शुरू हो सकती है (भाग-अ, भाग-ब…)\n"
    "• उत्तर: <code>उत्तर: (A)</code>, <code>Answer: B</code> या अंत में answer-key section "
    "(<code>1-A, 2-C</code>)\n"
    "• Explanation (optional): <code>व्याख्या:</code> / <code>Explanation:</code>\n\n"
    "Bot कभी उत्तर का अनुमान नहीं लगाता। लंबा text कई messages में भेज सकते हैं — सब जोड़ दिया जाएगा।\n"
    "/cancel — रद्द"
)


async def begin_import(update: Update, context) -> None:
    db.upsert_user(update.effective_user)
    db.set_state(update.effective_user.id, "imp_wait", {"buffer": ""})
    await update.effective_chat.send_message(IMPORT_HELP, parse_mode=ParseMode.HTML,
                                             reply_markup=M([[B("❌ Cancel", callback_data="i:cancel")]]))


def is_import_document(doc) -> bool:
    if doc is None:
        return False
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    return name.endswith((".pdf", ".txt")) or mime in ("application/pdf", "text/plain")


async def _safe_edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text)
    except TelegramError:
        pass


async def handle_import_document(update: Update, context) -> None:
    msg = update.effective_message
    user = update.effective_user
    doc = msg.document
    if doc.file_size and doc.file_size > config.MAX_DOWNLOAD_BYTES:
        await msg.reply_text("❌ File 20 MB से बड़ी है (Telegram bot limit)। छोटी file भेजें।")
        return
    name = doc.file_name or "source"
    is_pdf = name.lower().endswith(".pdf") or (doc.mime_type or "") == "application/pdf"
    status = await msg.reply_text("⏳ File पढ़ी जा रही है…")
    default_title = (msg.caption or "").strip() or Path(name).stem[:TITLE_MAX] or "Imported Quiz"
    try:
        tg_file = await doc.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
        if is_pdf:
            loop = asyncio.get_running_loop()
            last = [0.0]

            def progress(pno: int, total: int, method: str) -> None:
                # called from the worker thread
                if method != "ocr":
                    return
                now = time.monotonic()
                if now - last[0] < 2.0:
                    return
                last[0] = now
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(status, f"🔍 OCR चल रहा है: पेज {pno}/{total} (scan/legacy-font page)…"), loop)
            # PDF extraction + OCR + parsing are CPU heavy: never block the event loop
            res = await asyncio.to_thread(pdf_parser.parse_pdf_result, raw, progress=progress)
            text = ""
        else:
            text = pdf_parser.decode_text_bytes(raw)
            res = None
    except pdf_parser.SourceError as exc:
        await status.edit_text(f"❌ {exc}")
        return
    except TelegramError as exc:
        await status.edit_text(f"❌ File download नहीं हुई: {exc}")
        return
    except Exception as exc:  # malformed file etc.
        log.exception("import read failure")
        await status.edit_text(f"❌ File पढ़ने में समस्या: {exc}")
        return
    await _parse_and_review(update, user.id, text, default_title, status_msg=status, res=res)


async def handle_import_text(update: Update, context, state: str, data: dict) -> None:
    msg = update.effective_message
    user = update.effective_user
    if state == "imp_title":
        title = (msg.text or "").strip()
        if not title:
            await msg.reply_text("⚠️ Quiz का नाम text में भेजें।")
            return
        if len(title) > TITLE_MAX:
            await msg.reply_text(f"⚠️ अधिकतम {TITLE_MAX} characters।")
            return
        await _create_imported(update, user.id, data, title)
        return
    if msg.document and is_import_document(msg.document):
        await handle_import_document(update, context)
        return
    if not msg.text:
        await msg.reply_text("⚠️ PDF/.txt file या text भेजें।")
        return
    buffer = (data.get("buffer") or "")
    buffer = (buffer + "\n" + msg.text) if buffer else msg.text
    parts = int(data.get("parts", 0)) + 1
    await _parse_and_review(update, user.id, buffer, data.get("default_title") or "Imported Quiz",
                            parts=parts)


def _format_errors(res: pdf_parser.ParseResult) -> list[str]:
    return [f"• {esc(str(e))}" for e in res.errors]


_REMOVED_NAMES = {"answer-key": "answer-key lines", "section-heading": "भाग/section headings",
                  "preamble": "शुरुआती heading/निर्देश lines", "page-number": "page numbers",
                  "header/footer": "header/footer lines"}


def _preview_lines(q: dict) -> list[str]:
    head = q.get("section_label") and f"{q['section_label']} – " or ""
    out = [f"👁 <b>{esc(head)}प्रश्न {q['number']}</b> ({len(q['options'])} options):", esc(q["question"][:700]) +
           ("…" if len(q["question"]) > 700 else "")]
    for i, o in enumerate(q["options"]):
        mark = " ✅" if i == q["correct_index"] else ""
        out.append(f"({config.OPTION_LABELS[i]}) {esc(o[:200])}{'…' if len(o) > 200 else ''}{mark}")
    return out


async def _parse_and_review(update: Update, user_id: int, text: str, default_title: str,
                            status_msg=None, parts: int = 0, res=None) -> None:
    if res is None:
        try:
            res = await asyncio.to_thread(pdf_parser.parse_source, text)
        except Exception as exc:  # parser must never take the bot down
            log.exception("parser crashed")
            res = pdf_parser.ParseResult(errors=[pdf_parser.ParseError(None, f"Parser error: {exc}")])
    questions = [q.to_dict() for q in res.questions]
    data = {"buffer": text if parts else "", "parts": parts, "default_title": default_title,
            "questions": questions, "errors": [str(e) for e in res.errors],
            "review": list(res.review)}
    db.set_state(user_id, "imp_review", data)

    n, ne = len(questions), len(res.errors)
    types = Counter(pdf_parser.QTYPE_LABELS.get(q["qtype"], q["qtype"]) for q in questions)
    head = ["📥 <b>Import Result</b>", ""]
    if parts:
        head.append(f"📩 Text parts received: {parts} ({len(text)} characters)")
    head += [f"✅ सही parse हुए प्रश्न: <b>{n}</b>", f"❌ समस्या वाले प्रश्न/हिस्से: <b>{ne}</b>"]
    if types:
        head.append("📚 " + ", ".join(f"{k}: {v}" for k, v in types.items()))
    opt_counts = Counter(len(q["options"]) for q in questions)
    if opt_counts:
        head.append("🔢 Options: " + ", ".join(f"{k} options × {v}" for k, v in sorted(opt_counts.items())))
    removed = Counter(kind for kind, _ in res.removed)
    if removed:
        head.append("🧹 अलग किए गए (प्रश्न नहीं): " +
                    ", ".join(f"{_REMOVED_NAMES.get(k, k)}: {v}" for k, v in removed.items()))
    for w in res.warnings:
        head.append(f"⚠️ {esc(w)}")
    if res.review:
        head += ["", "🔎 <b>Import से पहले जांचें:</b>"] + [f"• {esc(r)}" for r in res.review]

    previews: list[dict] = []
    if n:
        previews.append(questions[0])
        if res.review:
            major = opt_counts.most_common(1)[0][0]
            previews += [q for q in questions if len(q["options"]) != major][:4]
            if res.ocr_pages:
                previews += questions[1:3]
        seen_ids = set()
        uniq = []
        for q in previews:
            key = (q.get("section", 0), q["number"])
            if key not in seen_ids:
                seen_ids.add(key)
                uniq.append(q)
        for q in uniq:
            head += [""] + _preview_lines(q)

    rows = []
    review = bool(res.review)
    if n and not ne:
        if n <= config.MAX_QUESTIONS:
            label = (f"✅ Preview जांच लिया — Quiz बनाएँ ({n})" if review else f"✅ Quiz बनाएँ ({n} प्रश्न)")
            rows.append([B(label, callback_data="i:reviewed" if review else "i:create")])
        else:
            k = -(-n // config.MAX_QUESTIONS)
            label = (f"✅ Preview जांचा — {k} quizzes में बाँटें" if review
                     else f"✂️ {k} quizzes में बाँटें (≤{config.MAX_QUESTIONS} each)")
            rows.append([B(label, callback_data="i:reviewedsplit" if review else "i:split")])
    elif n and ne:
        head += ["", f"⚠️ {ne} समस्याएँ हैं (ऊपर list)। Partial quiz तभी बनेगा जब आप नीचे confirm करें।"]
        if n <= config.MAX_QUESTIONS:
            rows.append([B(f"⚠️ Confirm: सिर्फ {n} सही प्रश्नों से quiz बनाएँ", callback_data="i:partial")])
        else:
            rows.append([B(f"⚠️ Confirm: {n} सही प्रश्न, quizzes में बाँटें", callback_data="i:partialsplit")])
    head += ["", "ℹ️ Text कटा हुआ है तो बाकी हिस्सा भेजें — जोड़ कर दोबारा parse होगा। "
             "या सुधरी हुई file/text दोबारा भेजें।"]
    rows.append([B("❌ Cancel", callback_data="i:cancel")])

    chat = update.effective_chat
    summary = "\n".join(head)
    if status_msg is not None:
        try:
            await status_msg.delete()
        except TelegramError:
            pass
    # errors: every problem is reported, split across messages if needed
    if ne:
        err_text = "❌ <b>Parsing errors</b>\n" + "\n".join(_format_errors(res))
        for chunk in engine.split_message(err_text, 3800):
            await chat.send_message(chunk, parse_mode=ParseMode.HTML)
    chunks = engine.split_message(summary, 3800)
    for i, chunk in enumerate(chunks):
        await chat.send_message(chunk, parse_mode=ParseMode.HTML,
                                reply_markup=M(rows) if i == len(chunks) - 1 else None)


async def handle_import_callback(update: Update, context) -> None:
    q = update.callback_query
    user = update.effective_user
    action = q.data.split(":", 1)[1]
    state, data = db.get_state(user.id)
    if action == "cancel":
        await q.answer()
        if state and state.startswith("imp_"):
            db.clear_state(user.id)
        await respond(update, "❌ Import रद्द।", kb.main_menu(), edit=False)
        return
    if state not in ("imp_review", "imp_title"):
        await q.answer("⌛ यह import expire हो चुका है। फिर से file भेजें।", show_alert=True)
        return
    await q.answer()
    if action in ("create", "partial", "split", "partialsplit", "reviewed", "reviewedsplit"):
        if not data.get("questions"):
            await update.effective_chat.send_message("❌ कोई valid प्रश्न नहीं है।")
            return
        if data.get("review") and action in ("create", "split"):
            await update.effective_chat.send_message(
                "🔎 इस import में जांचने लायक बातें हैं — ऊपर preview देख कर 'Preview जांच लिया' button दबाएँ।")
            return
        data["split"] = action in ("split", "partialsplit", "reviewedsplit")
        data["confirmed_partial"] = action in ("partial", "partialsplit")
        data["reviewed"] = action in ("reviewed", "reviewedsplit", "partial", "partialsplit")
        db.set_state(user.id, "imp_title", data)
        title = data.get("default_title") or "Imported Quiz"
        await update.effective_chat.send_message(
            "📝 Quiz का नाम भेजें, या default नाम use करें:",
            reply_markup=M([[B(f"✅ Use: {title[:40]}", callback_data="i:usedefault")],
                            [B("❌ Cancel", callback_data="i:cancel")]]))
    elif action == "usedefault" and state == "imp_title":
        await _create_imported(update, user.id, data, data.get("default_title") or "Imported Quiz")


async def _create_imported(update: Update, user_id: int, data: dict, title: str) -> None:
    questions = data.get("questions") or []
    if data.get("errors") and not data.get("confirmed_partial"):
        await update.effective_chat.send_message("⚠️ Errors हैं — partial quiz के लिए confirm button दबाएँ।")
        return
    if data.get("review") and not data.get("reviewed"):
        await update.effective_chat.send_message("🔎 पहले preview जांच कर confirm button दबाएँ।")
        return
    chunks = [questions[i:i + config.MAX_QUESTIONS] for i in range(0, len(questions), config.MAX_QUESTIONS)]
    if len(chunks) > 1 and not data.get("split"):
        await update.effective_chat.send_message("⚠️ 100 से अधिक प्रश्न — split option चुनें।")
        return
    created = []
    try:
        for part, chunk in enumerate(chunks, 1):
            t = title if len(chunks) == 1 else f"{title} (Part {part})"
            qid = db.new_quiz(user_id, t[:TITLE_MAX + 20], "Imported from PDF/Text", status="ready", source="import")
            db.add_questions_bulk(qid, chunk)
            created.append(qid)
    except (db.DatabaseError, ValueError) as exc:
        for qid in created:
            db.delete_quiz(qid, user_id)
        await update.effective_chat.send_message(f"❌ Quiz save नहीं हुआ: {exc}")
        return
    db.clear_state(user_id)
    me = await update.get_bot().get_me()
    for qid in created:
        quiz = db.get_quiz(qid)
        link = engine.deep_link(me.username, qid)
        await update.effective_chat.send_message(
            f"✅ <b>Import successful!</b>\n\n{quiz_summary(quiz)}\n\n🔗 {link}",
            parse_mode=ParseMode.HTML, reply_markup=kb.quiz_card(qid, True), disable_web_page_preview=True)
