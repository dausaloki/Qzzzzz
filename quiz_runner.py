"""Runs quizzes: one native Telegram quiz poll at a time, per user, in private chat.

All session state lives in the ``attempts`` table, so answers to polls sent
before a restart are still processed and timers are re-armed on startup.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Optional

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import Application, ContextTypes

import config
import database as db
import keyboards as kb
import quiz_engine as engine
from keyboards import esc

log = logging.getLogger(__name__)

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_send_failures: dict[int, int] = defaultdict(int)


def user_lock(user_id: int) -> asyncio.Lock:
    return _locks[user_id]


def _job_name(kind: str, attempt_id: int) -> str:
    return f"{kind}:{attempt_id}"


def _cancel_jobs(app: Application, attempt_id: int, kinds=("timeout", "next")) -> None:
    jq = app.job_queue
    if jq is None:
        return
    for kind in kinds:
        for job in jq.get_jobs_by_name(_job_name(kind, attempt_id)):
            job.schedule_removal()


def _schedule(app: Application, kind: str, attempt_id: int, when: float, data: dict) -> None:
    jq = app.job_queue
    if jq is None:
        raise RuntimeError("JobQueue not available — install python-telegram-bot[job-queue]")
    for job in jq.get_jobs_by_name(_job_name(kind, attempt_id)):
        job.schedule_removal()
    callback = {"timeout": _timeout_job, "next": _next_job}[kind]
    jq.run_once(callback, when=max(0.05, when), data=data, name=_job_name(kind, attempt_id))


async def _send_text(bot, chat_id: int, text: str, **kw):
    """Send text of any length losslessly (split into <=4000-char chunks)."""
    last = None
    chunks = engine.split_message(text)
    for i, chunk in enumerate(chunks):
        extra = kw if i == len(chunks) - 1 else {}
        last = await bot.send_message(chat_id, chunk, **extra)
    return last


# ------------------------------------------------------------ start / stop
async def start_quiz(app: Application, user, chat_id: int, qid: str, chat_type: str = ChatType.PRIVATE) -> Optional[int]:
    """Start a new attempt for ``user``. Returns attempt id or None."""
    bot = app.bot
    if chat_type != ChatType.PRIVATE:
        # Safety net: sessions are always private (see bot.py group handling).
        raise ValueError("Quiz sessions run only in private chat")
    quiz = db.get_quiz(qid)
    if not quiz:
        await bot.send_message(chat_id, "❌ Quiz नहीं मिला (शायद delete हो चुका है)।", reply_markup=kb.main_menu())
        return None
    questions = db.get_questions(qid)
    if not questions:
        await bot.send_message(chat_id, "⚠️ इस quiz में अभी कोई प्रश्न नहीं है।", reply_markup=kb.main_menu())
        return None
    if quiz.get("status") == "draft" and quiz["owner_id"] != user.id:
        await bot.send_message(chat_id, "⚠️ यह quiz अभी तैयार नहीं हुआ है।")
        return None

    async with user_lock(user.id):
        old = db.get_active_attempt(user.id)
        if old:
            await _stop_locked(app, old, notify=True, reason="नया quiz शुरू करने पर पिछला quiz रोक दिया गया।")
        order = engine.build_order([q["id"] for q in questions], bool(quiz["shuffle_questions"]))
        attempt_id = db.create_attempt(qid, user.id, chat_id, order, int(quiz["timer"] or 0),
                                       bool(quiz["shuffle_options"]), time.time())
    intro = [f"🎯 <b>{esc(quiz['title'])}</b>"]
    if quiz.get("description"):
        intro.append(esc(quiz["description"]))
    intro += [
        "",
        f"📝 प्रश्न: {len(order)}",
        f"⏱ Timer: {config.timer_label(quiz['timer'])} प्रति प्रश्न",
        f"🔀 Shuffle: questions {'ON' if quiz['shuffle_questions'] else 'OFF'}, "
        f"options {'ON' if quiz['shuffle_options'] else 'OFF'}",
        "",
        "/skip — प्रश्न छोड़ें  •  /stop — quiz रोकें",
    ]
    try:
        await bot.send_message(chat_id, "\n".join(intro)[:config.MESSAGE_MAX], parse_mode=ParseMode.HTML)
        if quiz.get("pre_text"):
            await _send_text(bot, chat_id, quiz["pre_text"])
    except Forbidden:
        db.finish_attempt(attempt_id, "stopped", 0)
        return None
    async with user_lock(user.id):
        await send_current(app, attempt_id)
    return attempt_id


async def _send_pre_media(bot, chat_id: int, q: dict) -> None:
    mtype, mid, text = q.get("pre_media_type") or "", q.get("pre_media_id") or "", q.get("pre_text") or ""
    if mtype and mid:
        caption = text if len(text) <= config.CAPTION_MAX else None
        sender = {
            "photo": bot.send_photo, "video": bot.send_video, "document": bot.send_document,
            "animation": bot.send_animation, "audio": bot.send_audio, "voice": bot.send_voice,
        }.get(mtype)
        if sender:
            try:
                await sender(chat_id, mid, caption=caption)
            except BadRequest as exc:
                log.warning("pre-media failed: %s", exc)
                await bot.send_message(chat_id, "⚠️ इस प्रश्न का media भेजा नहीं जा सका।")
            if text and caption is None:
                await _send_text(bot, chat_id, text)
            return
    if text:
        await _send_text(bot, chat_id, text)


async def send_current(app: Application, attempt_id: int) -> None:
    """Send the question at ``current_index``. Caller should hold the user lock."""
    bot = app.bot
    att = db.get_attempt(attempt_id)
    if not att or att["status"] != "active" or att.get("cur_poll_id"):
        return
    idx, order = att["current_index"], att["order"]
    total = len(order)
    if idx >= total:
        await _finish_locked(app, att, status="finished")
        return
    q = db.get_question(att["quiz_id"], order[idx])
    if q is None:
        # question deleted while quiz running → count as skipped, continue
        db.record_answer(attempt_id, order[idx], idx, None, False, "skipped", 0)
        await send_current(app, attempt_id)
        return
    chat_id = att["chat_id"]
    perm = engine.make_permutation(len(q["options"]), bool(att["shuffle_options"]))
    try:
        payload = engine.build_poll_payload(q, perm, idx, total)
    except Exception:  # never let one bad question crash the quiz
        log.exception("payload build failed for question %s", q["id"])
        payload = engine.compact_fallback(q, perm, idx, total)

    timer = int(att.get("timer") or 0)
    # Telegram accepts open_period 5..600 s; shorter values are enforced by our job only
    open_period = timer if 5 <= timer <= 600 else None
    try:
        await _send_pre_media(bot, chat_id, q)
        msg = await _send_poll(bot, chat_id, payload, open_period)
    except BadRequest as exc:
        log.warning("send_poll rejected (%s) — using compact fallback", exc)
        payload = engine.compact_fallback(q, perm, idx, total)
        try:
            msg = await _send_poll(bot, chat_id, payload, open_period)
        except BadRequest as exc2:
            log.error("compact poll also failed for question %s: %s", q["id"], exc2)
            await bot.send_message(chat_id, f"⚠️ प्रश्न {idx + 1} भेजा नहीं जा सका ({exc2.message}). इसे skip किया गया।")
            db.record_answer(attempt_id, q["id"], idx, None, False, "skipped", 0)
            await send_current(app, attempt_id)
            return
    except Forbidden:
        log.info("user blocked the bot; stopping attempt %s", attempt_id)
        db.finish_attempt(attempt_id, "stopped", time.time() - (att.get("started_ts") or time.time()))
        return
    except (TimedOut, NetworkError) as exc:
        _send_failures[attempt_id] += 1
        if _send_failures[attempt_id] <= 5:
            log.warning("network error sending question, retrying: %s", exc)
            _schedule(app, "next", attempt_id, 3.0 * _send_failures[attempt_id],
                      {"attempt_id": attempt_id, "index": idx, "user_id": att["user_id"]})
        else:
            log.error("giving up sending question for attempt %s", attempt_id)
        return
    _send_failures.pop(attempt_id, None)
    db.set_current_poll(attempt_id, msg.poll.id, msg.message_id, q["id"], payload.perm,
                        payload.correct_option_id, time.time(), payload.post_explanation)
    if timer > 0:
        _schedule(app, "timeout", attempt_id, timer + config.TIMER_GRACE,
                  {"attempt_id": attempt_id, "poll_id": msg.poll.id, "user_id": att["user_id"]})


async def _send_poll(bot, chat_id: int, payload: engine.PollPayload, open_period: Optional[int]):
    for chunk in payload.full_text_chunks:
        await bot.send_message(chat_id, chunk)
    return await bot.send_poll(
        chat_id=chat_id,
        question=payload.question,
        options=payload.options,
        type="quiz",
        correct_option_id=payload.correct_option_id,
        is_anonymous=False,
        allows_multiple_answers=False,
        explanation=payload.explanation,
        open_period=open_period,
        reply_markup=kb.running_markup(),
    )


# ------------------------------------------------------------ poll answers
async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ans = update.poll_answer
    if ans is None or ans.user is None:
        return
    if not ans.option_ids:  # vote retracted (not possible for quizzes, but be safe)
        return
    att = db.get_attempt_by_poll(ans.poll_id)
    if not att or att["user_id"] != ans.user.id:
        return  # expired session / someone else's poll
    app = context.application
    async with user_lock(ans.user.id):
        att = db.get_attempt(att["id"])
        if not att or att["status"] != "active" or att.get("cur_poll_id") != ans.poll_id:
            return
        _cancel_jobs(app, att["id"], ("timeout",))
        chosen = ans.option_ids[0]
        perm = att.get("cur_perm") or [0, 1, 2, 3]
        if not 0 <= chosen < len(perm):
            return
        is_correct = chosen == att["cur_correct"]
        elapsed = time.time() - (att.get("cur_sent_ts") or time.time())
        post_expl = att.get("cur_post_explanation") or ""
        db.record_answer(att["id"], att["cur_question_id"], att["current_index"],
                         engine.original_choice(perm, chosen), is_correct,
                         "correct" if is_correct else "wrong", elapsed)
        if post_expl:
            try:
                await _send_text(context.bot, att["chat_id"],
                                 ("✅ सही!" if is_correct else "❌ गलत।") + "\n💡 Explanation:\n" + post_expl)
            except TelegramError as exc:
                log.warning("could not send explanation: %s", exc)
        _schedule(app, "next", att["id"], config.NEXT_QUESTION_DELAY,
                  {"attempt_id": att["id"], "index": att["current_index"] + 1, "user_id": att["user_id"]})


async def _next_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    async with user_lock(data["user_id"]):
        att = db.get_attempt(data["attempt_id"])
        if not att or att["status"] != "active" or att.get("cur_poll_id"):
            return
        if att["current_index"] != data["index"]:
            return
        await send_current(context.application, att["id"])


async def _timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    app = context.application
    async with user_lock(data["user_id"]):
        att = db.get_attempt(data["attempt_id"])
        if not att or att["status"] != "active" or att.get("cur_poll_id") != data["poll_id"]:
            return
        await _skip_current_locked(app, att, reason="⏰ समय समाप्त! प्रश्न skipped।")


async def _skip_current_locked(app: Application, att: dict, reason: str) -> None:
    bot = app.bot
    _cancel_jobs(app, att["id"], ("timeout",))
    if att.get("cur_message_id"):
        try:
            await bot.stop_poll(att["chat_id"], att["cur_message_id"])
        except TelegramError:
            pass  # already closed by open_period
    elapsed = time.time() - (att.get("cur_sent_ts") or time.time())
    post_expl = att.get("cur_post_explanation") or ""
    db.record_answer(att["id"], att["cur_question_id"], att["current_index"], None, False, "skipped", elapsed)
    text = reason
    if post_expl:
        text += "\n💡 Explanation:\n" + post_expl
    try:
        await _send_text(bot, att["chat_id"], text)
    except Forbidden:
        db.finish_attempt(att["id"], "stopped", time.time() - (att.get("started_ts") or time.time()))
        return
    except TelegramError as exc:
        log.warning("could not send skip notice: %s", exc)
    _schedule(app, "next", att["id"], config.NEXT_QUESTION_DELAY / 2,
              {"attempt_id": att["id"], "index": att["current_index"] + 1, "user_id": att["user_id"]})


async def skip_current(app: Application, user_id: int, message_id: Optional[int] = None) -> str:
    async with user_lock(user_id):
        att = db.get_active_attempt(user_id)
        if not att:
            return "none"
        if not att.get("cur_poll_id"):
            return "busy"
        if message_id is not None and att.get("cur_message_id") != message_id:
            return "expired"
        await _skip_current_locked(app, att, reason="⏭ प्रश्न skipped।")
        return "ok"


# ------------------------------------------------------------ finishing
def result_text(att: dict, quiz_title: str, stopped: bool) -> str:
    r = engine.compute_result(att["total"], att["correct"], att["wrong"], att["skipped"],
                              att.get("duration_sec") or 0)
    head = "⏹ <b>Quiz Stopped</b> (partial result saved)" if stopped else "🏁 <b>Quiz Complete</b>"
    lines = [head, f"📘 {esc(quiz_title)}", "",
             f"📝 Total: {r.total}",
             f"✅ Correct: {r.correct}",
             f"❌ Wrong: {r.wrong}",
             f"⏭ Skipped: {r.skipped}"]
    if r.unanswered:
        lines.append(f"⏸ Not attempted: {r.unanswered}")
    lines += [f"🎯 Score: {r.score}/{r.total}",
              f"📈 Percentage: {r.percentage:.2f}%",
              f"⏱ Time: {engine.format_duration(r.duration_sec)}"]
    return "\n".join(lines)


async def _finish_locked(app: Application, att: dict, status: str) -> Optional[dict]:
    _cancel_jobs(app, att["id"])
    duration = time.time() - (att.get("started_ts") or time.time())
    done = db.finish_attempt(att["id"], status, duration)
    if not done:
        return None
    quiz = db.get_quiz(done["quiz_id"])
    title = quiz["title"] if quiz else "Quiz"
    try:
        await app.bot.send_message(done["chat_id"], result_text(done, title, status == "stopped"),
                                   parse_mode=ParseMode.HTML, reply_markup=kb.result_markup(done["quiz_id"]))
    except TelegramError as exc:
        log.warning("could not send result: %s", exc)
    return done


async def _stop_locked(app: Application, att: dict, notify: bool = True, reason: str = "") -> Optional[dict]:
    _cancel_jobs(app, att["id"])
    if att.get("cur_message_id"):
        try:
            await app.bot.stop_poll(att["chat_id"], att["cur_message_id"])
        except TelegramError:
            pass
    if reason and notify:
        try:
            await app.bot.send_message(att["chat_id"], reason)
        except TelegramError:
            pass
    return await _finish_locked(app, att, status="stopped")


async def stop_quiz(app: Application, user_id: int) -> Optional[dict]:
    async with user_lock(user_id):
        att = db.get_active_attempt(user_id)
        if not att:
            return None
        return await _stop_locked(app, att)


# ------------------------------------------------------------ recovery
async def recover_sessions(app: Application) -> dict:
    """Re-arm timers / resend questions for sessions that were active when
    the bot stopped. Very old sessions are expired."""
    stats = {"expired": 0, "timers": 0, "resent": 0, "waiting": 0}
    now = time.time()
    for att in db.list_active_attempts():
        started = att.get("started_ts") or 0
        if started and now - started > config.SESSION_EXPIRY_HOURS * 3600:
            db.finish_attempt(att["id"], "expired", now - started)
            stats["expired"] += 1
            continue
        if att.get("cur_poll_id"):
            timer = int(att.get("timer") or 0)
            if timer > 0:
                remaining = (att.get("cur_sent_ts") or now) + timer + config.TIMER_GRACE - now
                _schedule(app, "timeout", att["id"], max(1.0, remaining),
                          {"attempt_id": att["id"], "poll_id": att["cur_poll_id"], "user_id": att["user_id"]})
                stats["timers"] += 1
            else:
                stats["waiting"] += 1
        else:
            _schedule(app, "next", att["id"], 2.0,
                      {"attempt_id": att["id"], "index": att["current_index"], "user_id": att["user_id"]})
            stats["resent"] += 1
    return stats
