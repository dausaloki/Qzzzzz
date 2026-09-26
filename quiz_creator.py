"""Quiz creation wizard, quiz editor and PDF/Text import.

State is persisted in the ``user_states`` table (not in RAM), so a bot restart
in the middle of creating a quiz loses nothing.

States
  c_title, c_desc, c_q, c_correct, c_expl            (creation / add questions)
  fw_choose, fw_title                                 (forwarded questions → which quiz?)
  e_title, e_desc, e_expl, e_qtext, e_opts, e_optspick (editor)
  imp_wait, imp_review, imp_title                     (PDF/Text import)
"""
from __future__ import annotations

import asyncio
import re
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, InputFile, Update
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


# ============================================================ helpers
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


def owns(quiz: Optional[dict], user_id: int) -> bool:
    return bool(quiz) and quiz["owner_id"] == user_id


def shuffle_label(quiz: dict) -> str:
    sq, so = bool(quiz.get("shuffle_questions")), bool(quiz.get("shuffle_options"))
    if sq and so:
        return "Shuffle: प्रश्न + options"
    if sq:
        return "Shuffle: प्रश्न"
    if so:
        return "Shuffle: options"
    return "No shuffle"


def quiz_summary(quiz: dict, link: str = "") -> str:
    """Text of the quiz management card."""
    n = quiz.get("question_count", db.count_questions(quiz["id"]))
    lines = [f"📚 <b>{esc(quiz['title'])}</b>"]
    if quiz.get("description"):
        lines.append(esc(quiz["description"]))
    if quiz.get("series_id") and quiz.get("part_no"):
        total_parts = len(db.series_parts(quiz["series_id"]))
        lines.append(f"📦 Part {quiz['part_no']}/{total_parts}")
    lines += ["", f"📝 Questions: {n}",
              f"⏱ {config.timer_label(quiz.get('timer', 0))} · 🔀 {shuffle_label(quiz)}"]
    if link:
        lines += ["", f"🔗 {link}"]
    if quiz.get("status") == "draft":
        lines.append("\n📝 <i>Draft — /done से पूरा करें</i>")
    return "\n".join(lines)


async def send_quiz_card(update: Update, qid: str, *, edit: bool = False, note: str = "") -> None:
    """The management screen (owner) / play screen (everyone else)."""
    quiz = db.get_quiz(qid)
    if not quiz:
        await respond(update, "❌ Quiz नहीं मिला।", kb.main_menu(), edit=edit)
        return
    owner = quiz["owner_id"] == update.effective_user.id
    link = ""
    if owner and quiz.get("status") == "ready":
        me = await update.get_bot().get_me()
        link = engine.deep_link(me.username, qid)
    text = (note + "\n\n" if note else "") + quiz_summary(quiz, link)
    await respond(update, text, kb.quiz_card(qid, owner), edit=edit)


def question_full_text(q: dict, number: int, total: Optional[int] = None) -> str:
    head = f"प्रश्न {number}" + (f"/{total}" if total else "")
    lines = [f"{head} ({pdf_parser.QTYPE_LABELS.get(q.get('qtype', 'mcq'), 'MCQ')})"]
    if q.get("pre_text") or q.get("pre_media_type"):
        pre = q.get("pre_text") or ""
        media = f"[{q['pre_media_type']}] " if q.get("pre_media_type") else ""
        lines.append(f"🖼 प्रश्न से पहले: {media}{pre}".rstrip())
    lines += ["", q["question"], ""]
    for i, o in enumerate(q["options"]):
        mark = " ✅" if i == q["correct_index"] else ""
        lines.append(f"({config.OPTION_LABELS[i]}) {o}{mark}")
    if q.get("explanation"):
        lines += ["", "💡 " + q["explanation"]]
    return "\n".join(lines)


def _media_from_message(msg) -> tuple[str, str]:
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    for kind in ("video", "animation", "document", "audio", "voice"):
        obj = getattr(msg, kind, None)
        if obj:
            return kind, obj.file_id
    return "", ""


# ============================================================ CREATION
# Conversational, like @QuizBot:
#   /newquiz → title → description (/skip) → questions … → /done
# A question arrives as (1) a native Telegram quiz poll made with the
# "📝 प्रश्न बनाएँ" button or FORWARDED from anywhere, (2) one text message with
# (A) (B) … options (typed or forwarded; a forwarded message may contain many
# numbered questions).  Any other text/media message is pre-question content
# attached to the NEXT question.
#
# Forwarded content while no quiz is being created starts the forward import:
#   fw_choose (➕ New Quiz / 📚 Existing Quiz) → fw_title | pick → c_q …
# Items that arrive while the bot waits for something (quiz choice, title,
# a missing correct answer) are queued in order and processed afterwards, so
# forwarding 17 questions in a burst always gives ONE quiz with 17 questions
# in the original order.
#
# States: c_title, c_desc, c_q, c_correct, c_expl, fw_choose, fw_title.
_LEGACY_Q_STATES = {"c_pre", "c_opts", "c_more", "c_q_confirm"}   # previous step-by-step wizard
_PRE_KEYS = ("pre_text", "pre_media_type", "pre_media_id")
FORWARD_NO_ANSWER = ("⚠️ इस Forwarded Quiz Poll का सही उत्तर Telegram से उपलब्ध नहीं है।\n\n"
                     "कृपया सही Option चुनें।")


def normalize_creation_state(state: Optional[str], data: dict) -> Optional[str]:
    """States saved by the previous wizard version are mapped onto the new
    flow after an upgrade: any unsaved half-question is dropped, pre-question
    content and all saved questions are kept."""
    if state in _LEGACY_Q_STATES:
        cur = data.get("cur") or {}
        data["cur"] = {k: cur[k] for k in _PRE_KEYS if cur.get(k)}
        data.setdefault("added", [])
        return "c_q"
    return state


def _has_pre(cur: dict) -> bool:
    return bool(cur.get("pre_text") or cur.get("pre_media_id"))


# ------------------------------------------------------------ forwarded / received items
def source_ref(msg) -> str:
    """Short description of where a forwarded message came from (stored with the question)."""
    origin = getattr(msg, "forward_origin", None)
    if origin is None:
        return ""
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is not None:
        name = f"@{chat.username}" if getattr(chat, "username", None) else (chat.title or str(chat.id))
        mid = getattr(origin, "message_id", None)
        return f"forward:{name}" + (f"/{mid}" if mid else "")
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return f"forward:user:{user.id}"
    hidden = getattr(origin, "sender_user_name", None)
    return f"forward:{hidden}" if hidden else "forward"


def item_from_message(msg) -> Optional[dict]:
    """A poll (sent or forwarded) or a forwarded text → a queueable question item."""
    fwd = getattr(msg, "forward_origin", None) is not None
    if msg.poll:
        p = msg.poll
        return {"kind": "poll", "question": p.question, "options": [o.text for o in p.options],
                "quiz": p.type == "quiz", "multi": bool(p.allows_multiple_answers),
                "correct_ids": engine.poll_correct_ids(p), "explanation": p.explanation or "",
                "fwd": fwd, "src": source_ref(msg)}
    if fwd and msg.text:
        return {"kind": "text", "text": msg.text, "fwd": True, "src": source_ref(msg)}
    return None


def is_question_item(item: Optional[dict]) -> bool:
    """Polls always; forwarded text only if it really contains a question."""
    if not item:
        return False
    if item["kind"] == "poll":
        return True
    try:
        engine.parse_forwarded_text(item["text"])
    except engine.NoOptionsError:
        return False
    except ValueError:
        return True          # a question with problems — it is reported, never ignored
    return True


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
        "🆕 नया Quiz बनाते हैं। सबसे पहले अपने Quiz का <b>नाम</b> भेजें।\n\n"
        "जैसे: <i>राजस्थान का भूगोल</i> या <i>भारतीय इतिहास के 20 प्रश्न</i>",
        parse_mode=ParseMode.HTML, reply_markup=kb.remove_keyboard())


async def begin_forward_import(update: Update, item: dict) -> None:
    """First forwarded question while no quiz is being created."""
    user = update.effective_user
    db.upsert_user(user)
    db.set_state(user.id, "fw_choose", {"queue": [item]})
    await update.effective_chat.send_message(
        "📚 यह Question किस Quiz में जोड़ना है?\n\n"
        "(आगे forward किए गए प्रश्न भी इसी क्रम में उसी quiz में जुड़ेंगे।)",
        reply_markup=kb.forward_target_menu())


CUSTOM_TIMER_PROMPT = ("⏱ Custom timer भेजें — सेकंड या मिनट में (जैसे <code>45</code>, <code>45 sec</code>, "
                       "<code>2 min</code>, <code>1:30</code>; <code>0</code> = No Timer)।")


def _set_creation_timer(data: dict, val: int) -> None:
    """Timer chosen during creation applies to the quiz and all its parts."""
    for p in set((data.get("parts") or []) + [data["quiz_id"]]):
        db.update_quiz(p, timer=val)


async def _send_timer_choice(update: Update, qid: str) -> None:
    quiz = db.get_quiz(qid)
    await update.effective_chat.send_message(
        f"⏱ <b>Timer</b> (हर प्रश्न का समय): <b>{config.timer_label(quiz['timer'])}</b>\n"
        "बदलना हो तो नीचे चुनें — बाद में ⚙️ Settings से भी बदल सकते हैं। समय खत्म = Timeout → Skipped → "
        "अगला प्रश्न।", parse_mode=ParseMode.HTML, reply_markup=kb.timer_menu("c:timer", quiz["timer"], None))


async def _ask_question(update: Update, data: dict, first: bool) -> None:
    n = db.count_questions(data["quiz_id"])
    if first and data.get("mode") == "new":
        await _send_timer_choice(update, data["quiz_id"])
    if first:
        head = "बढ़िया। अब अपना <b>पहला प्रश्न</b> भेजें।" if data.get("mode") == "new" else \
            f"प्रश्न {n + 1} भेजें।"
        poll_hint = ("📝 नीचे <b>“प्रश्न बनाएँ”</b> button से Telegram का quiz poll भी बना सकते हैं "
                     "(उसमें Telegram की 300-character सीमा है)।\n\n" if config.NATIVE_POLL_BUTTON else "")
        text = (head + "\n\n"
                "✍️ पूरा प्रश्न <b>एक message में text</b> में भेजें — (A) (B) (C)… options के साथ, "
                "चाहें तो <code>उत्तर: B</code> और <code>व्याख्या: …</code> भी। लंबे प्रश्न/options "
                "(poll की सीमा से बड़े) ऐसे ही भेजें — पूरा text सुरक्षित रहेगा।\n\n"
                "📨 किसी दूसरे chat/channel से quiz poll या प्रश्न <b>forward</b> भी कर सकते हैं।\n\n"
                + poll_hint +
                "🖼 प्रश्न से पहले कुछ दिखाना है? पहले वह text या photo/video भेजें।")
    else:
        text = f"प्रश्न {n + 1} भेजें, या quiz पूरा करने के लिए /done भेजें।"
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML,
                                             reply_markup=kb.creation_keyboard(n > 0))


async def _stage_question(update: Update, user_id: int, data: dict, question: str, options: list[str],
                          correct: Optional[int], explanation: str, ask_expl: bool, *,
                          qtype: str = "", fwd: bool = False, src: str = "", ask_text: str = "") -> None:
    """Hold a received question; ask only for what is missing (never guess)."""
    cur = data.setdefault("cur", {})
    cur.update({"question": question, "options": list(options), "correct": correct,
                "explanation": explanation or "", "ask_expl": bool(ask_expl and not explanation),
                "qtype": qtype or engine.detect_qtype(question), "fwd": fwd, "src": src})
    if correct is None:
        db.set_state(user_id, "c_correct", data)
        chat = update.effective_chat
        listing = "\n".join(f"({config.OPTION_LABELS[i]}) {o}" for i, o in enumerate(options))
        head = ask_text or "इस प्रश्न का सही उत्तर कौन-सा है?"
        chunks = engine.split_message(f"{head}\n\n{question}\n\n{listing}")
        for i, chunk in enumerate(chunks):
            await chat.send_message(chunk, reply_markup=kb.correct_picker(options) if i == len(chunks) - 1 else None)
        return
    if cur["ask_expl"]:
        db.set_state(user_id, "c_expl", data)
        await update.effective_chat.send_message(
            "अब explanation भेजें (उत्तर देने के बाद दिखेगी) — यह optional है, /skip कर सकते हैं।")
        return
    await _save_staged(update, user_id, data)


_PART_SUFFIX_RE = re.compile(r"\s*—\s*Part\s+\d+\s*$")


def part_title(title: str, part_no: int) -> str:
    base = _PART_SUFFIX_RE.sub("", title or "").strip() or "Quiz"
    suffix = f" — Part {part_no}"
    return base[:max(1, TITLE_MAX - len(suffix))] + suffix


def _roll_part(data: dict) -> Optional[str]:
    """When the current quiz part holds QUESTIONS_PER_PART questions, open the
    next part (same title + " — Part N", same settings, linked by series_id)
    and continue there.  Returns a notice for the user, or None."""
    qid = data["quiz_id"]
    per = config.QUESTIONS_PER_PART
    if db.count_questions(qid) < per:
        return None
    quiz = db.get_quiz(qid)
    series = quiz.get("series_id") or qid
    part = int(quiz.get("part_no") or 1)
    if not quiz.get("series_id"):
        db.set_part(qid, series, 1)
        db.update_quiz(qid, title=part_title(quiz["title"], 1))
    new_title = part_title(quiz["title"], part + 1)
    new = db.new_quiz(quiz["owner_id"], new_title, quiz.get("description") or "",
                      status=quiz.get("status") or "draft", source=quiz.get("source") or "manual",
                      timer=quiz.get("timer"), shuffle_questions=quiz.get("shuffle_questions"),
                      shuffle_options=quiz.get("shuffle_options"))
    db.set_part(new, series, part + 1)
    parts = data.setdefault("parts", [])
    if qid not in parts:
        parts.append(qid)
    parts.append(new)
    data["quiz_id"] = new
    return (f"📦 Part {part} पूरा ({per} प्रश्न)। आगे के प्रश्न अपने-आप “{new_title}” में जुड़ेंगे "
            "(हर part अलग quiz है, अपनी link के साथ)।")


def _save_one(qid: str, cur: dict, q: dict) -> int:
    return db.add_question(
        qid, q["question"], q["options"], int(q["correct"]), q.get("explanation", ""),
        qtype=q.get("qtype") or engine.detect_qtype(q["question"]), pre_text=cur.get("pre_text", ""),
        pre_media_type=cur.get("pre_media_type", ""), pre_media_id=cur.get("pre_media_id", ""),
        source_ref=q.get("src", ""))


async def _confirm_saved(update: Update, data: dict, fwd: bool, added: int, n_options: int) -> None:
    qid = data["quiz_id"]
    chat = update.effective_chat
    n = db.count_questions(qid)
    if fwd:
        quiz = db.get_quiz(qid)
        head = "✅ Question imported" if added == 1 else f"✅ {added} Questions imported"
        await chat.send_message(f"{head}\n\n📚 Quiz: {quiz['title']}\n📝 Total Questions: {n}\n\n"
                                "अगला प्रश्न forward करें, या /done भेजें।",
                                reply_markup=kb.creation_keyboard(True))
    else:
        extra = f" ({n_options} options)" if n_options != 4 else ""
        await chat.send_message(
            f"✅ प्रश्न {n} जुड़ गया{extra}।\nअब अगला प्रश्न भेजें, या quiz पूरा करने के लिए /done भेजें।\n"
            "(/undo — आख़िरी प्रश्न हटाएँ)", reply_markup=kb.creation_keyboard(True))


async def _save_staged(update: Update, user_id: int, data: dict) -> None:
    cur = data.get("cur") or {}
    chat = update.effective_chat
    roll = _roll_part(data)
    if roll:
        await chat.send_message(roll)
    qid = data["quiz_id"]
    try:
        new_id = _save_one(qid, cur, {**cur, "correct": cur["correct"]})
    except (ValueError, KeyError, TypeError) as exc:
        data["cur"] = {k: cur[k] for k in _PRE_KEYS if cur.get(k)}
        db.set_state(user_id, "c_q", data)
        await chat.send_message(f"❌ प्रश्न save नहीं हुआ: {exc}\nप्रश्न दोबारा भेजें।",
                                reply_markup=kb.creation_keyboard(db.count_questions(qid) > 0))
        return
    data.setdefault("added", []).append(new_id)
    data["cur"] = {}
    n = db.count_questions(qid)
    data["n_saved"] = n
    db.set_state(user_id, "c_q", data)
    await _confirm_saved(update, data, bool(cur.get("fwd")), 1, len(cur["options"]))


async def _process_poll(update: Update, user_id: int, data: dict, item: dict) -> None:
    reply = update.effective_chat.send_message
    options = item["options"]
    if item["multi"]:
        await reply(f"⚠️ Poll “{item['question'][:80]}” में एक से अधिक उत्तर चुने जा सकते हैं — यह quiz प्रश्न "
                    "नहीं बन सकता (एक सही उत्तर वाला quiz poll भेजें)। यह poll save नहीं हुआ।")
        return
    if not config.MIN_OPTIONS <= len(options) <= config.MAX_OPTIONS:
        await reply(f"⚠️ Poll में {config.MIN_OPTIONS}–{config.MAX_OPTIONS} options चाहिए। यह poll save नहीं हुआ।")
        return
    correct = None
    ids = item["correct_ids"] if item["quiz"] else None
    if ids is not None:
        if len(ids) != 1 or not 0 <= ids[0] < len(options):
            await reply("⚠️ इस quiz poll में एक से अधिक सही उत्तर हैं — यह bot हर प्रश्न का ठीक एक सही उत्तर "
                        "रखता है, इसलिए यह poll save नहीं हुआ।")
            return
        correct = ids[0]
    # Telegram reveals correct_option_ids/explanation for quiz polls created in
    # this private chat (or closed ones); a forwarded open quiz usually hides
    # them → the creator is asked.  Nothing is ever guessed.
    ask_text = FORWARD_NO_ANSWER if (item["fwd"] and item["quiz"]) else ""
    await _stage_question(update, user_id, data, item["question"], options, correct,
                          item["explanation"] if item["quiz"] else "",
                          ask_expl=not item["fwd"] and (not item["quiz"] or correct is None),
                          fwd=item["fwd"], src=item.get("src", ""), ask_text=ask_text)


async def _process_text(update: Update, user_id: int, data: dict, text: str, *, fwd: bool, src: str) -> None:
    """A text message in c_q: question(s) or pre-question content."""
    cur = data.setdefault("cur", {})
    chat = update.effective_chat
    try:
        found = engine.parse_forwarded_text(text)
    except engine.NoOptionsError:
        found = None
    except ValueError as exc:
        for chunk in engine.split_message(f"⚠️ {exc}\nसुधार कर प्रश्न दोबारा भेजें।"):
            await chat.send_message(chunk)
        return
    if found is None:                               # not a question → pre-question content
        replaced = _has_pre(cur)
        data["cur"] = {"pre_text": text, "pre_media_type": "", "pre_media_id": ""}
        db.set_state(user_id, "c_q", data)
        n = db.count_questions(data["quiz_id"]) + 1
        await chat.send_message(
            ("🔁 पिछला pre-question content बदल दिया गया।\n" if replaced else "") +
            f"👍 यह प्रश्न {n} से पहले दिखाया जाएगा। अब प्रश्न भेजें — text में "
            "(A) (B)… options के साथ (या quiz poll)।\n(अगर यही प्रश्न था, तो options के साथ एक ही message में भेजें।)",
            reply_markup=kb.creation_keyboard(n > 1))
        return
    if len(found) == 1:
        q = found[0]
        await _stage_question(update, user_id, data, q["question"], q["options"], q["correct"],
                              q["explanation"], ask_expl=(q["correct"] is None and not fwd),
                              qtype=q["qtype"], fwd=fwd, src=src)
        return
    # several complete questions in one message (all have answers) → saved in source order
    for i, q in enumerate(found):
        q["src"] = src
        roll = _roll_part(data)
        if roll:
            await chat.send_message(roll)
        data.setdefault("added", []).append(_save_one(data["quiz_id"], cur if i == 0 else {}, q))
    data["cur"] = {}
    data["n_saved"] = db.count_questions(data["quiz_id"])
    db.set_state(user_id, "c_q", data)
    await _confirm_saved(update, data, fwd, len(found), 4)


async def _process_item(update: Update, user_id: int, data: dict, item: dict) -> None:
    if item["kind"] == "poll":
        await _process_poll(update, user_id, data, item)
    else:
        await _process_text(update, user_id, data, item["text"], fwd=item.get("fwd", False),
                            src=item.get("src", ""))


async def _drain_queue(update: Update, user_id: int) -> None:
    """Process queued items in order until one needs input from the user."""
    while True:
        state, data = db.get_state(user_id)
        queue = data.get("queue") or []
        if state != "c_q" or not queue:
            return
        item = queue.pop(0)
        data["queue"] = queue
        db.set_state(user_id, "c_q", data)
        await _process_item(update, user_id, data, item)


def _enqueue(user_id: int, state: str, data: dict, item: dict) -> int:
    data.setdefault("queue", []).append(item)
    db.set_state(user_id, state, data)
    return len(data["queue"])


async def finalize(update: Update, context) -> None:
    """/done — finish creating (or adding questions)."""
    user = update.effective_user
    state, data = db.get_state(user.id)
    state = normalize_creation_state(state, data)
    chat = update.effective_chat
    if state and state.startswith("fw_"):
        await chat.send_message("⚠️ पहले चुनें कि प्रश्न किस quiz में जोड़ने हैं (ऊपर के buttons), या /cancel भेजें।")
        return
    if not state or not state.startswith("c_"):
        await chat.send_message("ℹ️ अभी कोई quiz creation चालू नहीं है। नया quiz: /newquiz",
                                reply_markup=kb.remove_keyboard())
        return
    qid = data.get("quiz_id")
    if not qid or not db.get_quiz(qid):
        db.clear_state(user.id)
        await chat.send_message("❌ Quiz बनाना रद्द हुआ (नाम नहीं मिला)।", reply_markup=kb.remove_keyboard())
        return
    parts = [p for p in (data.get("parts") or []) if p != qid] + [qid]
    n = db.count_questions(qid)
    if n == 0 and len(parts) == 1:
        await chat.send_message("⚠️ Quiz में कम से कम 1 प्रश्न चाहिए। प्रश्न भेजें या /cancel करें।")
        return
    if n == 0:                              # an empty last part is never left behind
        db.delete_quiz(qid, user.id)
        parts.pop()
        qid = parts[-1]
        n = db.count_questions(qid)
    cur = data.get("cur") or {}
    notes = []
    if cur.get("question"):
        notes.append("⚠️ अधूरा प्रश्न (सही उत्तर नहीं चुना गया) save नहीं हुआ।")
    elif _has_pre(cur):
        notes.append("⚠️ आख़िरी pre-question content के बाद कोई प्रश्न नहीं था — वह save नहीं हुआ।")
    if data.get("queue"):
        notes.append(f"⚠️ {len(data['queue'])} forwarded item(s) अभी process नहीं हुए थे — वे save नहीं हुए।")
    db.clear_state(user.id)
    if data.get("mode") == "new":
        for p in parts:
            db.update_quiz(p, status="ready")
    if len(parts) > 1:
        notes.append(f"📦 {len(parts)} parts बने (हर part में अधिकतम {config.QUESTIONS_PER_PART} प्रश्न): " +
                     ", ".join(f"“{db.get_quiz(p)['title']}” ({db.count_questions(p)})" for p in parts))
    note = "\n".join(notes)
    if data.get("mode") == "add" and not data.get("via_forward"):
        await chat.send_message(f"✅ प्रश्न जोड़ दिए गए। अब कुल {n} प्रश्न।" + (f"\n{note}" if note else ""),
                                reply_markup=kb.remove_keyboard())
        await show_edit_menu(update, qid, edit=False)
        return
    await chat.send_message("✅ Quiz तैयार है!" + (f"\n\n{note}" if note else ""), reply_markup=kb.remove_keyboard())
    await send_quiz_card(update, qid)


async def undo(update: Update, context) -> None:
    """/undo — discard the pending question, else remove the last added one."""
    user = update.effective_user
    state, data = db.get_state(user.id)
    state = normalize_creation_state(state, data)
    chat = update.effective_chat
    if not state or not state.startswith("c_") or not data.get("quiz_id"):
        await chat.send_message("ℹ️ Undo करने के लिए कुछ नहीं है।")
        return
    cur = data.get("cur") or {}
    if cur.get("question"):
        data["cur"] = {k: cur[k] for k in _PRE_KEYS if cur.get(k)}
        db.set_state(user.id, "c_q", data)
        await chat.send_message("↩️ अधूरा प्रश्न हटा दिया गया। प्रश्न दोबारा भेजें।",
                                reply_markup=kb.creation_keyboard(db.count_questions(data["quiz_id"]) > 0))
        await _drain_queue(update, user.id)
        return
    added = data.get("added") or []
    while added:
        last = added.pop()
        owner = next((p for p in [data["quiz_id"]] + list(reversed(data.get("parts") or []))
                      if db.get_question(p, last)), None)
        if owner and db.delete_question(last, owner):
            n = db.count_questions(data["quiz_id"])
            data["n_saved"] = n
            db.set_state(user.id, "c_q", data)
            await chat.send_message(f"↩️ आख़िरी प्रश्न हटा दिया गया। अब कुल {n} प्रश्न।",
                                    reply_markup=kb.creation_keyboard(n > 0))
            return
    db.set_state(user.id, state, data)
    await chat.send_message("ℹ️ इस session में जोड़ा गया कोई प्रश्न नहीं है।")


async def skip(update: Update, context) -> bool:
    """/skip during creation. Returns False if there was nothing to skip here."""
    user = update.effective_user
    state, data = db.get_state(user.id)
    if state == "c_desc":
        db.set_state(user.id, "c_q", data)
        await _ask_question(update, data, first=True)
        await _drain_queue(update, user.id)
        return True
    if state == "c_expl":
        data.setdefault("cur", {})["explanation"] = ""
        await _save_staged(update, user.id, data)
        await _drain_queue(update, user.id)
        return True
    return False


async def cancel(update: Update, context) -> None:
    user = update.effective_user
    state, data = db.get_state(user.id)
    if not state:
        await update.effective_chat.send_message("ℹ️ रद्द करने के लिए कुछ नहीं है।", reply_markup=kb.remove_keyboard())
        return
    db.clear_state(user.id)
    msg = "❌ रद्द किया गया।"
    if state.startswith("c_") and data.get("mode") == "new" and data.get("quiz_id"):
        for p in set((data.get("parts") or []) + [data["quiz_id"]]):
            db.delete_quiz(p, user.id)
        msg = "❌ Quiz बनाना रद्द — draft delete कर दिया गया।"
    elif state.startswith("c_") and data.get("mode") == "add":
        msg = "❌ प्रश्न जोड़ना रद्द। पहले से saved प्रश्न quiz में रहेंगे।"
    elif state.startswith("fw_"):
        msg = "❌ Forward import रद्द — कुछ भी save नहीं हुआ।"
    elif state.startswith("imp_"):
        msg = "❌ Import रद्द।"
    await update.effective_chat.send_message(msg, reply_markup=kb.remove_keyboard())
    await update.effective_chat.send_message("🏠 Main menu:", reply_markup=kb.main_menu())


async def handle_forward_message(update: Update, context, state: str, data: dict) -> None:
    """Messages while choosing the target quiz of a forward import."""
    msg = update.effective_message
    user = update.effective_user
    item = item_from_message(msg)
    if item and is_question_item(item):
        n = _enqueue(user.id, state, data, item)
        await msg.reply_text(f"📥 Queue में जोड़ा गया (कुल {n})।" +
                             (" अब quiz का नाम भेजें।" if state == "fw_title" else " पहले ऊपर से quiz चुनें।"))
        return
    if state == "fw_title" and msg.text and msg.text.strip() and not msg.forward_origin:
        title = msg.text.strip()
        if len(title) > TITLE_MAX:
            await msg.reply_text(f"⚠️ नाम अधिकतम {TITLE_MAX} characters का हो सकता है।")
            return
        qid = db.new_quiz(user.id, title, "", status="draft", source="forward")
        new = {"mode": "new", "via_forward": True, "quiz_id": qid, "n_saved": 0, "cur": {}, "added": [],
               "queue": data.get("queue") or []}
        db.set_state(user.id, "c_q", new)
        await msg.reply_text(f"📚 Quiz “{title}” बनाया गया — forwarded प्रश्न जोड़े जा रहे हैं…",
                             reply_markup=kb.creation_keyboard(False))
        await _drain_queue(update, user.id)
        return
    await msg.reply_text("👆 ऊपर के buttons से quiz चुनें (➕ New Quiz / 📚 Existing Quiz), या /cancel।"
                         if state == "fw_choose" else "📝 नए Quiz का नाम text में भेजें, या /cancel।")


async def handle_forward_callback(update: Update, context) -> None:
    q = update.callback_query
    user = update.effective_user
    parts = q.data.split(":")
    state, data = db.get_state(user.id)
    if state not in ("fw_choose", "fw_title"):
        await q.answer("⌛ यह button अब valid नहीं है।", show_alert=True)
        return
    await q.answer()
    action = parts[1] if len(parts) > 1 else ""
    if action == "new":
        db.set_state(user.id, "fw_title", data)
        await respond(update, "📝 नए Quiz का <b>नाम</b> भेजें।")
    elif action == "exist":
        quizzes = db.get_owner_quizzes(user.id)[:30]
        if not quizzes:
            await respond(update, "ℹ️ आपका कोई quiz नहीं है। नया quiz बनाएँ:",
                          kb.forward_target_menu(existing=False))
            return
        await respond(update, "📚 किस quiz में जोड़ें?", kb.forward_pick_menu(quizzes))
    elif action == "pick" and len(parts) > 2:
        quiz = db.get_quiz(parts[2])
        if not owns(quiz, user.id):                 # server-side ownership check
            await update.effective_chat.send_message("⛔ यह quiz आपका नहीं है।")
            return
        new = {"mode": "add", "via_forward": True, "quiz_id": quiz["id"],
               "n_saved": quiz["question_count"], "cur": {}, "added": [], "queue": data.get("queue") or []}
        db.set_state(user.id, "c_q", new)
        await respond(update, f"📚 “{esc(quiz['title'])}” में forwarded प्रश्न जोड़े जा रहे हैं…")
        await _drain_queue(update, user.id)


async def handle_creator_message(update: Update, context, state: str, data: dict) -> None:
    msg = update.effective_message
    user = update.effective_user
    state = normalize_creation_state(state, data)
    text = msg.text or ""
    stripped = text.strip()
    item = item_from_message(msg)

    if data.get("await_timer") and msg.text is not None and state in ("c_q", "c_desc"):
        try:
            val = config.parse_timer(stripped)
        except ValueError as exc:
            if len(stripped) <= 20:              # clearly meant as a timer → explain
                await msg.reply_text(f"⚠️ {exc}")
                return
            val = None                           # a question/description → handled normally
        data.pop("await_timer", None)
        db.set_state(user.id, state, data)
        if val is not None:
            _set_creation_timer(data, val)
            await msg.reply_text(f"✅ Timer: {config.timer_label(val)} (हर प्रश्न पर लागू)। अब प्रश्न भेजें।",
                                 reply_markup=kb.creation_keyboard(db.count_questions(data["quiz_id"]) > 0))
            return

    if state == "c_title":
        if not stripped or msg.text is None:
            await msg.reply_text("⚠️ Quiz का नाम text में भेजें।")
            return
        if len(stripped) > TITLE_MAX:
            await msg.reply_text(f"⚠️ नाम अधिकतम {TITLE_MAX} characters का हो सकता है।")
            return
        qid = db.new_quiz(user.id, stripped, "", status="draft", source="manual")
        data.update({"quiz_id": qid, "n_saved": 0, "cur": {}, "added": []})
        db.set_state(user.id, "c_desc", data)
        await msg.reply_text("बढ़िया। अब अपने Quiz का <b>description</b> भेजें। "
                             "यह optional है — आप /skip कर सकते हैं।", parse_mode=ParseMode.HTML)
        return

    if state == "c_desc":
        if item and is_question_item(item):          # went straight to the first question
            db.set_state(user.id, "c_q", data)
            await _process_item(update, user.id, data, item)
            await _drain_queue(update, user.id)
            return
        if msg.text is None:
            await msg.reply_text("⚠️ Description text में भेजें, या /skip करें।")
            return
        if len(stripped) > DESC_MAX:
            await msg.reply_text(f"⚠️ Description अधिकतम {DESC_MAX} characters।")
            return
        db.update_quiz(data["quiz_id"], description=stripped)
        db.set_state(user.id, "c_q", data)
        await _ask_question(update, data, first=True)
        return

    if state == "c_q":
        if item and item["kind"] == "poll":
            await _process_item(update, user.id, data, item)
        elif msg.text is not None:
            if not stripped:
                return
            await _process_text(update, user.id, data, msg.text, fwd=bool(item), src=item["src"] if item else "")
        else:
            cur = data.setdefault("cur", {})
            mtype, mid = _media_from_message(msg)
            if not mtype:
                await msg.reply_text("⚠️ प्रश्न poll या text भेजें।")
                return
            replaced = _has_pre(cur)
            data["cur"] = {"pre_text": msg.caption or "", "pre_media_type": mtype, "pre_media_id": mid}
            db.set_state(user.id, "c_q", data)
            n = db.count_questions(data["quiz_id"]) + 1
            await msg.reply_text(
                ("🔁 पिछला pre-question content बदल दिया गया।\n" if replaced else "") +
                f"👍 यह प्रश्न {n} से पहले दिखाया जाएगा। अब प्रश्न भेजें — text में "
                "(A) (B)… options के साथ।", reply_markup=kb.creation_keyboard(n > 1))
        await _drain_queue(update, user.id)
        return

    if state in ("c_correct", "c_expl") and item and is_question_item(item):
        n = _enqueue(user.id, state, data, item)
        await msg.reply_text(f"📥 Queue में जोड़ा गया (कुल {n}) — पहले ऊपर वाले प्रश्न का "
                             + ("सही उत्तर चुनें।" if state == "c_correct" else "explanation भेजें या /skip करें।"))
        return

    if state == "c_correct":
        await msg.reply_text("👆 ऊपर के buttons से सही उत्तर चुनें (या /undo)।")
        return

    if state == "c_expl":
        if msg.text is None or not stripped:
            await msg.reply_text("⚠️ Explanation text में भेजें, या /skip करें।")
            return
        if len(stripped) > EXPL_MAX:
            await msg.reply_text(f"⚠️ Explanation अधिकतम {EXPL_MAX} characters।")
            return
        data.setdefault("cur", {})["explanation"] = stripped
        await _save_staged(update, user.id, data)
        await _drain_queue(update, user.id)


async def handle_creator_callback(update: Update, context) -> None:
    q = update.callback_query
    user = update.effective_user
    action = q.data.split(":", 1)[1]
    state, data = db.get_state(user.id)
    state = normalize_creation_state(state, data)

    if action == "cancel":
        await q.answer()
        await cancel(update, context)
        return
    if not state or not (state.startswith("c_") or state.startswith("fw_")):
        await q.answer("⌛ यह button अब valid नहीं है।", show_alert=True)
        return
    if action.startswith("correct:") and state == "c_correct":
        try:
            idx = int(action.split(":")[1])
        except ValueError:
            idx = -1
        cur = data.get("cur") or {}
        if not 0 <= idx < len(cur.get("options") or []):
            await q.answer("⌛ यह button अब valid नहीं है।")
            return
        await q.answer(f"सही उत्तर: ({config.OPTION_LABELS[idx]})")
        cur["correct"] = idx
        try:
            await q.edit_message_reply_markup(None)
        except TelegramError:
            pass
        await _stage_question(update, user.id, data, cur["question"], cur["options"], idx,
                              cur.get("explanation", ""), ask_expl=cur.get("ask_expl", False),
                              qtype=cur.get("qtype", ""), fwd=cur.get("fwd", False), src=cur.get("src", ""))
        await _drain_queue(update, user.id)
        return
    if action == "done":
        await q.answer()
        await finalize(update, context)
        return
    if action.startswith("timer:") and state.startswith("c_") and data.get("quiz_id"):
        val_s = action.split(":", 1)[1]
        if val_s == "custom":
            data["await_timer"] = True
            db.set_state(user.id, state, data)
            await q.answer()
            await update.effective_chat.send_message(CUSTOM_TIMER_PROMPT, parse_mode=ParseMode.HTML)
            return
        try:
            val = int(val_s)
        except ValueError:
            val = -1
        if val not in config.TIMER_CHOICES:
            await q.answer("⌛ यह button अब valid नहीं है।")
            return
        _set_creation_timer(data, val)
        data.pop("await_timer", None)
        db.set_state(user.id, state, data)
        await q.answer(f"⏱ {config.timer_label(val)}")
        await respond(update, f"⏱ <b>Timer</b>: <b>{config.timer_label(val)}</b> ✅ (हर प्रश्न पर लागू)",
                      kb.timer_menu("c:timer", val, None))
        return
    await q.answer("⌛ यह button अब valid नहीं है।")


# ============================================================ EDITOR
async def show_edit_menu(update: Update, qid: str, edit: bool = True) -> None:
    quiz = db.get_quiz(qid)
    await respond(update, "✏️ <b>Edit Quiz</b>\n\n" + quiz_summary(quiz), kb.edit_menu(qid, quiz), edit=edit)


async def show_quiz_settings(update: Update, qid: str, edit: bool = True) -> None:
    quiz = db.get_quiz(qid)
    await respond(update, f"⚙️ <b>Settings — {esc(quiz['title'])}</b>\n\n"
                          "Timer: हर प्रश्न का समय (समय पूरा होने पर प्रश्न skipped गिना जाता है)।\n"
                          "Shuffle: हर attempt में प्रश्न/options का क्रम बदलता है — सही उत्तर वही रहता है।",
                  kb.quiz_settings_menu(qid, quiz), edit=edit)


async def show_question(update: Update, qid: str, question_id: int, note: str = "") -> None:
    questions = db.get_questions(qid)
    pos = next((i for i, x in enumerate(questions, 1) if x["id"] == question_id), 0)
    if not pos:
        await respond(update, "प्रश्न नहीं मिला।", kb.edit_menu(qid, db.get_quiz(qid)), edit=False)
        return
    qs = questions[pos - 1]
    chat = update.effective_chat
    for chunk in engine.split_message(question_full_text(qs, pos, len(questions))):
        await chat.send_message(chunk)
    await chat.send_message((note + "\n" if note else "") + f"✏️ प्रश्न {pos}/{len(questions)} — क्या बदलना है?",
                            reply_markup=kb.question_menu(qid, question_id, pos, len(questions)))


async def _question_list(update: Update, qid: str, page: int) -> None:
    quiz = db.get_quiz(qid)
    questions = db.get_questions(qid)
    if not questions:
        await respond(update, "ℹ️ इस quiz में कोई प्रश्न नहीं है।", kb.edit_menu(qid, quiz))
        return
    page = max(0, min(page, (len(questions) - 1) // kb.PAGE_SIZE))
    await respond(update, f"📋 <b>{esc(quiz['title'])}</b> — {len(questions)} प्रश्न\nप्रश्न चुनें:",
                  kb.question_picker(qid, questions, page, "e:q", f"q:edit:{qid}"))


def _qid_arg(parts: list[str], i: int) -> Optional[int]:
    try:
        return int(parts[i])
    except (IndexError, ValueError):
        return None


async def handle_edit_callback(update: Update, context) -> None:
    q = update.callback_query
    user = update.effective_user
    parts = q.data.split(":")
    action, qid = parts[1], parts[2] if len(parts) > 2 else ""
    quiz = db.get_quiz(qid)
    if not quiz:
        await q.answer("Quiz नहीं मिला।", show_alert=True)
        return
    if not owns(quiz, user.id):                    # ownership is always checked server-side
        await q.answer("⛔ यह quiz आपका नहीं है।", show_alert=True)
        return
    back = f"q:edit:{qid}"
    # actions that address one question: it must belong to THIS quiz
    q_actions = {"q", "qt", "qo", "qc", "sc", "oc", "up", "dn", "delqx", "delqok", "explq", "explrm", "viewq"}
    question = None
    if action in q_actions:
        question = db.get_question(qid, _qid_arg(parts, 3) or -1)
        if not question:
            await q.answer("प्रश्न नहीं मिला (शायद delete हो चुका है)।", show_alert=True)
            return
    await q.answer()

    if action == "title":
        db.set_state(user.id, "e_title", {"quiz_id": qid})
        await update.effective_chat.send_message(f"📝 नया title भेजें (अभी: {quiz['title']})\n/cancel — रद्द")
    elif action == "desc":
        db.set_state(user.id, "e_desc", {"quiz_id": qid})
        await update.effective_chat.send_message("📄 नई description भेजें (हटाने के लिए '-' भेजें)\n/cancel — रद्द")
    elif action == "addq":
        n = db.count_questions(qid)
        data = {"mode": "add", "quiz_id": qid, "n_saved": n, "cur": {}, "added": []}
        db.set_state(user.id, "c_q", data)
        await _ask_question(update, data, first=True)
    elif action == "list":
        await _question_list(update, qid, _qid_arg(parts, 3) or 0)
    elif action in ("q", "viewq"):
        await show_question(update, qid, question["id"])
    elif action == "qt":
        db.set_state(user.id, "e_qtext", {"quiz_id": qid, "question_id": question["id"]})
        await update.effective_chat.send_message("✏️ प्रश्न का नया text भेजें (options नहीं — सिर्फ प्रश्न)।\n"
                                                 "/cancel — रद्द")
    elif action == "qo":
        db.set_state(user.id, "e_opts", {"quiz_id": qid, "question_id": question["id"]})
        await update.effective_chat.send_message(
            f"🔤 नए options भेजें — {config.MIN_OPTIONS} से {config.MAX_OPTIONS}, हर line में एक, "
            "या (A) (B) (C)… labels के साथ। उसके बाद सही उत्तर चुनना होगा।\n/cancel — रद्द")
    elif action == "qc":
        listing = "\n".join(f"({config.OPTION_LABELS[i]}) {o}" for i, o in enumerate(question["options"]))
        for i, chunk in enumerate(c := engine.split_message(f"✅ सही उत्तर चुनें:\n\n{listing}")):
            await update.effective_chat.send_message(chunk, reply_markup=kb.correct_picker(
                question["options"], f"e:sc:{qid}:{question['id']}", question["correct_index"],
                f"e:q:{qid}:{question['id']}") if i == len(c) - 1 else None)
    elif action == "sc":
        idx = _qid_arg(parts, 4)
        if idx is None or not 0 <= idx < len(question["options"]):
            return
        db.update_question(question["id"], correct_index=idx)
        await show_question(update, qid, question["id"], note=f"✅ सही उत्तर अब ({config.OPTION_LABELS[idx]}) है।")
    elif action == "oc":
        state, data = db.get_state(user.id)
        idx = _qid_arg(parts, 4)
        opts = data.get("options") if state == "e_optspick" and data.get("question_id") == question["id"] else None
        if not opts or idx is None or not 0 <= idx < len(opts):
            await update.effective_chat.send_message("⌛ यह button अब valid नहीं है। Options दोबारा भेजें।")
            return
        db.update_question(question["id"], options=opts, correct_index=idx)   # one atomic update
        db.clear_state(user.id)
        await show_question(update, qid, question["id"], note="✅ Options और सही उत्तर update हुए।")
    elif action in ("up", "dn"):
        db.move_question(qid, question["id"], -1 if action == "up" else 1)
        await show_question(update, qid, question["id"], note="↕️ क्रम बदला गया।")
    elif action in ("delq", "expl", "view"):
        page = _qid_arg(parts, 3) or 0
        questions = db.get_questions(qid)
        if not questions:
            await respond(update, "ℹ️ इस quiz में कोई प्रश्न नहीं है।", kb.edit_menu(qid, quiz))
            return
        sub = {"delq": "e:delqx", "expl": "e:explq", "view": "e:viewq"}[action]
        title = {"delq": "🗑 Delete करने वाला प्रश्न चुनें:", "expl": "💡 Explanation edit करने के लिए प्रश्न चुनें:",
                 "view": "👁 प्रश्न चुनें:"}[action]
        await respond(update, title, kb.question_picker(qid, questions, page, sub, back))
    elif action == "delqx":
        preview = question["question"][:700]
        await respond(update, f"🗑 यह प्रश्न delete करें?\n\n{esc(preview)}",
                      M([[B("✅ Delete", callback_data=f"e:delqok:{qid}:{question['id']}"),
                          B("❌ Cancel", callback_data=f"e:q:{qid}:{question['id']}")]]))
    elif action == "delqok":
        db.delete_question(question["id"], qid)
        await respond(update, "✅ प्रश्न delete हुआ।\n\n" + quiz_summary(db.get_quiz(qid)),
                      kb.edit_menu(qid, db.get_quiz(qid)))
    elif action == "explq":
        db.set_state(user.id, "e_expl", {"quiz_id": qid, "question_id": question["id"]})
        cur = question.get("explanation") or "(कोई explanation नहीं)"
        for chunk in engine.split_message(f"💡 अभी की explanation:\n{cur}\n\nनई explanation भेजें।"):
            await update.effective_chat.send_message(chunk)
        await update.effective_chat.send_message(
            "या:", reply_markup=M([[B("🗑 Explanation हटाएँ", callback_data=f"e:explrm:{qid}:{question['id']}")],
                                   [B("❌ Cancel", callback_data=f"e:q:{qid}:{question['id']}")]]))
    elif action == "explrm":
        db.update_question(question["id"], explanation="")
        db.clear_state(user.id)
        await show_question(update, qid, question["id"], note="✅ Explanation हटा दी गई।")
    elif action == "timer":
        await respond(update, "⏱ प्रति प्रश्न timer चुनें:",
                      kb.timer_menu(f"e:settimer:{qid}", quiz["timer"], f"q:set:{qid}"))
    elif action == "settimer":
        if len(parts) > 3 and parts[3] == "custom":
            db.set_state(user.id, "e_timer", {"quiz_id": qid})
            await update.effective_chat.send_message(
                f"⏱ Custom timer भेजें — सेकंड या मिनट में (जैसे <code>45</code>, <code>45 sec</code>, "
                f"<code>2 min</code>, <code>1:30</code>)। सीमा: {config.CUSTOM_TIMER_MIN} sec – "
                f"{config.timer_label(config.CUSTOM_TIMER_MAX)}; <code>0</code> = No Timer।\n/cancel — रद्द",
                parse_mode=ParseMode.HTML)
            return
        val = _qid_arg(parts, 3)
        if val not in config.TIMER_CHOICES:
            return
        db.update_quiz(qid, timer=val)
        await show_quiz_settings(update, qid)
    elif action == "sq":
        db.update_quiz(qid, shuffle_questions=0 if quiz["shuffle_questions"] else 1)
        await show_quiz_settings(update, qid)
    elif action == "so":
        db.update_quiz(qid, shuffle_options=0 if quiz["shuffle_options"] else 1)
        await show_quiz_settings(update, qid)


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
    if state == "e_optspick":
        await msg.reply_text("👆 ऊपर के buttons से सही उत्तर चुनें (या /cancel)।")
        return
    if not text:
        await msg.reply_text("⚠️ Text भेजें या /cancel।")
        return
    question_id = data.get("question_id")
    if state in ("e_expl", "e_qtext", "e_opts") and not db.get_question(qid, question_id):
        db.clear_state(user.id)
        await msg.reply_text("प्रश्न नहीं मिला (शायद delete हो चुका है)।")
        return
    if state == "e_timer":
        try:
            val = config.parse_timer(text)
        except ValueError as exc:
            await msg.reply_text(f"⚠️ {exc}")
            return
        db.update_quiz(qid, timer=val)
        db.clear_state(user.id)
        await msg.reply_text(f"✅ Timer: {config.timer_label(val)} (हर प्रश्न पर लागू)")
        await show_quiz_settings(update, qid, edit=False)
        return
    if state == "e_title":
        if len(text) > TITLE_MAX:
            await msg.reply_text(f"⚠️ अधिकतम {TITLE_MAX} characters।")
            return
        db.update_quiz(qid, title=text)
        db.clear_state(user.id)
        await msg.reply_text("✅ Title updated.")
        await show_edit_menu(update, qid, edit=False)
    elif state == "e_desc":
        if len(text) > DESC_MAX:
            await msg.reply_text(f"⚠️ अधिकतम {DESC_MAX} characters।")
            return
        db.update_quiz(qid, description="" if text == "-" else text)
        db.clear_state(user.id)
        await msg.reply_text("✅ Description updated.")
        await show_edit_menu(update, qid, edit=False)
    elif state == "e_expl":
        if len(text) > EXPL_MAX:
            await msg.reply_text(f"⚠️ अधिकतम {EXPL_MAX} characters।")
            return
        db.update_question(question_id, explanation="" if text == "-" else text)
        db.clear_state(user.id)
        await show_question(update, qid, question_id, note="✅ Explanation updated.")
    elif state == "e_qtext":
        db.update_question(question_id, question=msg.text.strip(), qtype=engine.detect_qtype(msg.text))
        db.clear_state(user.id)
        await show_question(update, qid, question_id, note="✅ प्रश्न का text updated.")
    elif state == "e_opts":
        try:
            options = engine.parse_manual_options(msg.text)
        except ValueError as exc:
            await msg.reply_text(f"⚠️ {exc}")
            return
        current = db.get_question(qid, question_id)
        keep = current["correct_index"] if len(options) == len(current["options"]) else None
        db.set_state(user.id, "e_optspick", {"quiz_id": qid, "question_id": question_id, "options": options})
        listing = "\n".join(f"({config.OPTION_LABELS[i]}) {o}" for i, o in enumerate(options))
        chunks = engine.split_message(f"नए options:\n\n{listing}\n\nअब सही उत्तर चुनें — options और उत्तर साथ में save होंगे:")
        for i, chunk in enumerate(chunks):
            await msg.reply_text(chunk, reply_markup=kb.correct_picker(
                options, f"e:oc:{qid}:{question_id}", keep, f"e:q:{qid}:{question_id}") if i == len(chunks) - 1 else None)


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


def plan_parts(questions: list) -> list[list]:
    """Split questions (source order) into parts of QUESTIONS_PER_PART.
    456 → [456]; 500 → [500]; 501 → [500, 1]; 2378 → [500×4, 378]."""
    per = max(1, int(config.QUESTIONS_PER_PART))
    return [questions[i:i + per] for i in range(0, len(questions), per)] or []


def _qref(q: dict) -> str:
    sec = q.get("section_label")
    return f"{sec} – Q{q['number']}" if sec else f"Q{q['number']}"


def part_ranges(chunks: list[list]) -> list[str]:
    return [f"📝 Part {k}: {_qref(c[0])}–{_qref(c[-1])} ({len(c)} प्रश्न)" if len(c) > 1
            else f"📝 Part {k}: {_qref(c[0])} (1 प्रश्न)" for k, c in enumerate(chunks, 1)]


def report_lines(rep: dict, parts: int) -> list[str]:
    """Import report (plain text; the caller escapes for HTML)."""
    out = ["📊 Import Report",
           f"• Total detected: {rep['total_detected']}",
           f"• Parsed: {rep['parsed']}",
           f"• Verification Required: {rep['verification_required']}",
           f"• Failed: {rep['failed']}" + (f" (+{rep['other_errors']} अन्य समस्याएँ)" if rep.get("other_errors") else ""),
           f"• Answer mapped: {rep['answer_key'] + rep['answer_inline']} "
           f"(answer key से {rep['answer_key']}, प्रश्न के साथ लिखे {rep['answer_inline']})",
           f"• Parts created: {parts}"]
    if rep.get("problems"):
        out.append("समस्याएँ (पेज · प्रश्न · कारण):")
        out += [f"  - पेज {pg or '?'} · {ref} · {why}" for pg, ref, why in rep["problems"]]
    return out


def full_preview_text(questions: list, errors: list, review: list, title: str = "",
                      report: Optional[dict] = None) -> str:
    """Complete, untruncated preview of every parsed question (sent as a .txt)."""
    out = [f"FULL PREVIEW — {title}".rstrip(" —"), f"Total Questions: {len(questions)}", ""]
    if report:
        out += report_lines(report, len(plan_parts(questions))) + [""]
    chunks = plan_parts(questions)
    if len(chunks) > 1:
        out += [f"Total Parts: {len(chunks)}"] + [r[2:].strip() for r in part_ranges(chunks)] + [""]
    if errors:
        out += ["=== PARSING ERRORS (import नहीं होंगे) ==="] + [f"• {e}" for e in errors] + [""]
    if review:
        out += ["=== जांचें / Verification Required ==="] + [f"• {r}" for r in review] + [""]
    for k, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            out += [f"################ Part {k} ################", ""]
        last_pre = None
        for q in chunk:
            sec = f"{q['section_label']} – " if q.get("section_label") else ""
            pg = f"  (पेज {q['page']})" if q.get("page") else ""
            if q.get("pre_text") and q["pre_text"] != last_pre:
                out += ["----- गद्यांश / तालिका / निर्देश -----", q["pre_text"], "-------------------------------------"]
            last_pre = q.get("pre_text") or None
            out.append(f"{sec}प्रश्न {q['number']}{pg}")
            if q.get("verify"):
                out.append("⚠️ Verification Required — जांचें: " + ", ".join(q["verify"]))
            for f in q.get("flags") or []:
                out.append("⚠️ Verification Required — " + f)
            out.append(q["question"])
            for i, o in enumerate(q["options"]):
                out.append(f"({config.OPTION_LABELS[i]}) {o}")
            ci = q["correct_index"]
            out.append(f"Correct Answer: ({config.OPTION_LABELS[ci]}) {q['options'][ci]}")
            if q.get("explanation"):
                out.append("व्याख्या: " + q["explanation"])
            out.append("")
    return "\n".join(out)


def _preview_lines(q: dict) -> list[str]:
    head = q.get("section_label") and f"{q['section_label']} – " or ""
    pg = f" · पेज {q['page']}" if q.get("page") else ""
    out = [f"👁 <b>{esc(head)}प्रश्न {q['number']}</b> ({len(q['options'])} options{pg}):"]
    if q.get("verify"):
        out.append("⚠️ <b>Verification Required</b> — " + esc(", ".join(q["verify"][:8])))
    for f in q.get("flags") or []:
        out.append("⚠️ <b>Verification Required</b> — " + esc(f))
    if q.get("pre_text"):
        out.append("📖 <i>" + esc(q["pre_text"][:300]) + ("…" if len(q["pre_text"]) > 300 else "") + "</i>")
    out += [esc(q["question"][:700]) +
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
    report = pdf_parser.import_report(res)
    data = {"buffer": text if parts else "", "parts": parts, "default_title": default_title,
            "questions": questions, "errors": [str(e) for e in res.errors],
            "review": list(res.review), "report": report}
    db.set_state(user_id, "imp_review", data)

    n, ne = len(questions), len(res.errors)
    types = Counter(pdf_parser.QTYPE_LABELS.get(q["qtype"], q["qtype"]) for q in questions)
    head = ["📥 <b>Import Result</b>", ""]
    if parts:
        head.append(f"📩 Text parts received: {parts} ({len(text)} characters)")
    head += [f"✅ सही parse हुए प्रश्न: <b>{n}</b>", f"❌ समस्या वाले प्रश्न/हिस्से: <b>{ne}</b>"]
    chunks = plan_parts(questions)
    if len(chunks) > 1:
        head.append(f"📦 {len(chunks)} parts बनेंगे (हर part में अधिकतम {config.QUESTIONS_PER_PART} प्रश्न, "
                    "मूल क्रमांक वही रहेंगे):")
        head += part_ranges(chunks)
    head += [""] + [esc(x) for x in report_lines({**report, "problems": report["problems"][:15]}, len(chunks))]
    if len(report["problems"]) > 15:
        head.append(f"  … कुल {len(report['problems'])} समस्याएँ — पूरी list full_preview.txt में")
    head.append("")
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
            previews += [q for q in questions if q.get("verify") or q.get("flags")][:4]
        previews += [q for q in questions if q.get("pre_text")][:1]
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
    kparts = f" · {len(chunks)} parts" if len(chunks) > 1 else ""
    if n and not ne:
        label = (f"✅ Preview जांच लिया — Quiz बनाएँ ({n}{kparts})" if review
                 else f"✅ Quiz बनाएँ ({n} प्रश्न{kparts})")
        rows.append([B(label, callback_data="i:reviewed" if review else "i:create")])
    elif n and ne:
        head += ["", f"⚠️ {ne} समस्याएँ हैं (ऊपर list)। Partial quiz तभी बनेगा जब आप नीचे confirm करें।"]
        rows.append([B(f"⚠️ Confirm: सिर्फ {n} सही प्रश्नों से quiz बनाएँ{kparts}", callback_data="i:partial")])
    if n:
        head += ["", "📄 सभी प्रश्नों का पूरा preview (प्रश्न, सभी options, Correct Answer) .txt file में भेजा गया है।"]
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
    if n:
        preview = full_preview_text(questions, data["errors"], data["review"], default_title, report)
        try:
            await chat.send_document(InputFile(preview.encode("utf-8"), filename="full_preview.txt"),
                                     caption=f"📄 Full Preview — {n} प्रश्न")
        except TelegramError as exc:
            log.warning("full preview upload failed: %s", exc)
    msgs = engine.split_message(summary, 3800)
    for i, chunk in enumerate(msgs):
        await chat.send_message(chunk, parse_mode=ParseMode.HTML,
                                reply_markup=M(rows) if i == len(msgs) - 1 else None)


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
    chunks = plan_parts(questions)
    created: list[str] = []
    try:
        for part, chunk in enumerate(chunks, 1):
            t = title if len(chunks) == 1 else part_title(title, part)
            qid = db.new_quiz(user_id, t, "Imported from PDF/Text", status="ready", source="import")
            created.append(qid)
            if len(chunks) > 1:
                db.set_part(qid, created[0], part)
            db.add_questions_bulk(qid, chunk)
    except (db.DatabaseError, ValueError) as exc:
        for qid in created:                  # all or nothing
            db.delete_quiz(qid, user_id)
        await update.effective_chat.send_message(f"❌ Quiz save नहीं हुआ: {exc}")
        return
    db.clear_state(user_id)
    me = await update.get_bot().get_me()
    chat = update.effective_chat
    report = ["✅ <b>Import Complete</b>", "", f"📚 Total Questions: {len(questions)}",
              f"📦 Total Parts: {len(chunks)}", ""] + [esc(r) for r in part_ranges(chunks)]
    if data.get("report"):
        rep = data["report"]
        report += [""] + [esc(x) for x in report_lines({**rep, "problems": rep["problems"][:15]}, len(chunks))]
    for chunk in engine.split_message("\n".join(report), 3800):
        await chat.send_message(chunk, parse_mode=ParseMode.HTML)
    for qid in created:
        quiz = db.get_quiz(qid)
        link = engine.deep_link(me.username, qid)
        await chat.send_message(
            f"✅ <b>Import successful!</b>\n\n{quiz_summary(quiz, link)}",
            parse_mode=ParseMode.HTML, reply_markup=kb.quiz_card(qid, True), disable_web_page_preview=True)
