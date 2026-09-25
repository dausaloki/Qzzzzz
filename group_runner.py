"""Quizzes played INSIDE a Telegram group with native quiz polls.

* One native, non-anonymous ``sendPoll(type="quiz")`` at a time, in the group.
* Every group member can answer; each ``poll_answer`` is stored per
  participant (``group_answers``, unique per session+question+user), so one
  participant's answer can never overwrite another's.
* Sessions are keyed by ``group_sessions.id`` (quiz_id + chat_id + session):
  the same quiz can run in many groups and private chats at the same time.
  There is no global "current question".
* A question stays open for the quiz timer (``GROUP_DEFAULT_TIMER`` if the
  quiz has no timer), then the poll is closed and the next one is sent.
* After the last question a leaderboard computed from the stored answers is
  posted.  Everything is in SQLite; timers are re-armed after a restart.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Optional

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import Application, ContextTypes

import config
import database as db
import quiz_engine as engine
from keyboards import esc

log = logging.getLogger(__name__)

_locks: dict[int, tuple[object, asyncio.Lock]] = {}
_send_failures: dict[int, int] = defaultdict(int)


def chat_lock(chat_id: int) -> asyncio.Lock:
    """One lock per group chat (bound to the running event loop)."""
    loop = asyncio.get_running_loop()
    entry = _locks.get(chat_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _locks[chat_id] = entry
    return entry[1]


def group_timer(quiz: dict) -> int:
    t = int(quiz.get("timer") or 0)
    return t if t > 0 else config.GROUP_DEFAULT_TIMER


def _job(kind: str, sid: int) -> str:
    return f"g{kind}:{sid}"


def _schedule(app: Application, kind: str, sid: int, when: float, data: dict) -> None:
    jq = app.job_queue
    for job in jq.get_jobs_by_name(_job(kind, sid)):
        job.schedule_removal()
    cb = {"timeout": _timeout_job, "next": _next_job}[kind]
    jq.run_once(cb, when=max(0.05, when), data=data, name=_job(kind, sid))


def _cancel(app: Application, sid: int) -> None:
    for kind in ("timeout", "next"):
        for job in app.job_queue.get_jobs_by_name(_job(kind, sid)):
            job.schedule_removal()


def ready_text(quiz: dict) -> str:
    n = quiz.get("question_count", 0)
    return (f"🎲 <b>Group Quiz: “{esc(quiz['title'])}”</b>\n"
            f"🖊 {n} {'question' if n == 1 else 'questions'} · ⏱ {config.timer_label(group_timer(quiz))} per question\n\n"
            "▶️ दबाते ही quiz <b>इसी group में</b> शुरू होगा — हर सदस्य native quiz poll में उत्तर दे सकता है। "
            "आख़िरी प्रश्न के बाद leaderboard आएगा।\n"
            "रोकने के लिए (quiz शुरू करने वाला या admin): /stop")


# ------------------------------------------------------------ start / send
async def start_group_quiz(app: Application, chat_id: int, qid: str, user) -> str:
    """'ok' | 'busy' | 'missing' | 'empty' | 'draft'"""
    quiz = db.get_quiz(qid)
    if not quiz:
        return "missing"
    if quiz.get("status") == "draft":
        return "draft"
    questions = db.get_questions(qid)
    if not questions:
        return "empty"
    order = engine.build_order([q["id"] for q in questions], bool(quiz["shuffle_questions"]))
    async with chat_lock(chat_id):
        sid = db.create_group_session(qid, chat_id, user.id, order, group_timer(quiz),
                                      bool(quiz["shuffle_options"]), time.time())
        if sid is None:
            return "busy"
        try:
            await app.bot.send_message(
                chat_id, f"🏁 <b>{esc(quiz['title'])}</b> — quiz शुरू!\n"
                         f"📝 {len(order)} प्रश्न · ⏱ {config.timer_label(group_timer(quiz))} प्रति प्रश्न",
                parse_mode=ParseMode.HTML)
            if quiz.get("pre_text"):
                for chunk in engine.split_message(quiz["pre_text"]):
                    await app.bot.send_message(chat_id, chunk)
        except Forbidden:
            db.finish_group_session(sid, "stopped", time.time())
            return "missing"
        await _send_current(app, sid)
    return "ok"


async def _send_pre(bot, chat_id: int, q: dict) -> None:
    mtype, mid, text = q.get("pre_media_type") or "", q.get("pre_media_id") or "", q.get("pre_text") or ""
    if mtype and mid:
        sender = {"photo": bot.send_photo, "video": bot.send_video, "document": bot.send_document,
                  "animation": bot.send_animation, "audio": bot.send_audio, "voice": bot.send_voice}.get(mtype)
        caption = text if engine.tg_len(text) <= config.CAPTION_MAX else None
        if sender:
            try:
                await sender(chat_id, mid, caption=caption)
            except BadRequest as exc:
                log.warning("group pre-media failed: %s", exc)
            if text and caption is None:
                for chunk in engine.split_message(text):
                    await bot.send_message(chat_id, chunk)
            return
    if text:
        for chunk in engine.split_message(text):
            await bot.send_message(chat_id, chunk)


async def _send_poll(bot, chat_id: int, payload, open_period: Optional[int]):
    for chunk in payload.full_text_chunks:
        await bot.send_message(chat_id, chunk)
    return await bot.send_poll(
        chat_id=chat_id, question=payload.question, options=payload.options, type="quiz",
        correct_option_ids=[payload.correct_option_id], is_anonymous=False,
        allows_multiple_answers=False, explanation=payload.explanation, open_period=open_period)


async def _send_current(app: Application, sid: int) -> None:
    """Send the question at current_index.  Caller holds the chat lock."""
    bot = app.bot
    s = db.get_group_session(sid)
    if not s or s["status"] != "active" or s.get("cur_poll_id"):
        return
    idx, order, chat_id = s["current_index"], s["order"], s["chat_id"]
    total = len(order)
    if idx >= total:
        await _finish_locked(app, s, "finished")
        return
    q = db.get_question(s["quiz_id"], order[idx])
    if q is None:                                   # deleted meanwhile
        db.close_group_question(sid, None)
        await _send_current(app, sid)
        return
    perm = engine.make_permutation(len(q["options"]), bool(s["shuffle_options"]))
    try:
        payload = engine.build_poll_payload(q, perm, idx, total)
    except Exception:  # noqa: BLE001 — one bad question never stops the quiz
        log.exception("group payload failed for question %s", q["id"])
        payload = engine.compact_fallback(q, perm, idx, total)
    timer = int(s["timer"] or config.GROUP_DEFAULT_TIMER)
    open_period = timer if 5 <= timer <= 600 else None
    try:
        await _send_pre(bot, chat_id, q)
        msg = await _send_poll(bot, chat_id, payload, open_period)
    except BadRequest as exc:
        log.warning("group poll rejected (%s) — compact fallback", exc)
        payload = engine.compact_fallback(q, perm, idx, total)
        try:
            msg = await _send_poll(bot, chat_id, payload, open_period)
        except BadRequest as exc2:
            await bot.send_message(chat_id, f"⚠️ प्रश्न {idx + 1} भेजा नहीं जा सका ({exc2.message}) — अगला प्रश्न।")
            db.close_group_question(sid, None)
            await _send_current(app, sid)
            return
    except Forbidden:
        log.info("bot removed from group %s — stopping session %s", chat_id, sid)
        db.finish_group_session(sid, "stopped", time.time())
        return
    except (TimedOut, NetworkError) as exc:
        _send_failures[sid] += 1
        if _send_failures[sid] <= 5:
            log.warning("network error sending group question, retrying: %s", exc)
            _schedule(app, "next", sid, 3.0 * _send_failures[sid], {"sid": sid, "index": idx, "chat_id": chat_id})
        return
    _send_failures.pop(sid, None)
    db.set_group_current(sid, msg.poll.id, msg.message_id, q["id"], idx, payload.perm,
                         payload.correct_option_id, time.time(), payload.post_explanation)
    _schedule(app, "timeout", sid, timer + config.TIMER_GRACE,
              {"sid": sid, "poll_id": msg.poll.id, "chat_id": chat_id})


async def _close_current_locked(app: Application, s: dict) -> None:
    bot = app.bot
    if s.get("cur_message_id"):
        try:
            await bot.stop_poll(s["chat_id"], s["cur_message_id"])
        except TelegramError:
            pass                                    # already closed by open_period
    post = s.get("cur_post_explanation") or ""
    if not db.close_group_question(s["id"], s.get("cur_poll_id")):
        return
    if post:
        try:
            for chunk in engine.split_message(f"💡 प्रश्न {s['current_index'] + 1} — Explanation:\n{post}"):
                await bot.send_message(s["chat_id"], chunk)
        except TelegramError as exc:
            log.warning("could not send group explanation: %s", exc)
    _schedule(app, "next", s["id"], config.GROUP_NEXT_DELAY,
              {"sid": s["id"], "index": s["current_index"] + 1, "chat_id": s["chat_id"]})


async def _timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    async with chat_lock(d["chat_id"]):
        s = db.get_group_session(d["sid"])
        if not s or s["status"] != "active" or s.get("cur_poll_id") != d["poll_id"]:
            return
        await _close_current_locked(context.application, s)


async def _next_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    async with chat_lock(d["chat_id"]):
        s = db.get_group_session(d["sid"])
        if not s or s["status"] != "active" or s.get("cur_poll_id") or s["current_index"] != d["index"]:
            return
        await _send_current(context.application, d["sid"])


# ------------------------------------------------------------ answers
async def handle_group_answer(update, context) -> bool:
    """Store one participant's answer.  Returns True if the poll belonged to a group session."""
    ans = update.poll_answer
    info = db.get_group_poll(ans.poll_id)
    if info is None:
        return False
    if info["session_status"] != "active" or ans.user is None or not ans.option_ids:
        return True
    if info.get("cur_poll_id") != ans.poll_id:
        return True                                 # question already closed — late answers never count
    chosen = ans.option_ids[0]
    perm = info["perm"]
    if not 0 <= chosen < len(perm):
        return True
    db.upsert_user(ans.user)                       # name for the leaderboard
    now = time.time()
    db.record_group_answer(info["session_id"], info["quiz_id"], info["chat_id"], ans.poll_id,
                           info["question_id"], info["q_index"], ans.user.id, chosen,
                           engine.original_choice(perm, chosen), chosen == info["correct_display"],
                           max(0.0, now - (info.get("sent_ts") or now)), now)
    return True


# ------------------------------------------------------------ finish / stop
def _name(r: dict) -> str:
    if r.get("first_name"):
        return r["first_name"]
    if r.get("username"):
        return "@" + r["username"]
    return f"User {r['user_id']}"


def result_text(s: dict, title: str, stopped: bool) -> str:
    total = int(s["total"])
    asked = total if not stopped else int(s.get("asked") or 0)
    board = db.group_leaderboard(s["id"])
    head = "⏹ <b>Quiz stopped — partial result</b>" if stopped else "🏆 <b>Quiz Result</b>"
    lines = [head, "", f"📚 {esc(title)}",
             f"📝 प्रश्न: {asked}/{total} · 👥 Participants: {len(board)}", ""]
    if not board:
        lines.append("किसी participant ने उत्तर नहीं दिया।")
        return "\n".join(lines)
    medals = ["🥇", "🥈", "🥉"]
    denom = asked or total
    for i, r in enumerate(board):
        correct, wrong, score = int(r["correct"]), int(r["wrong"]), int(r.get("score") or 0)
        skipped = max(0, denom - int(r["answered"]))
        pct = 100.0 * correct / denom if denom else 0.0
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {esc(_name(r))} — {correct}/{denom}")
        lines.append(f"      ✅ Correct {correct} · ❌ Wrong {wrong} · ⌛ Skipped {skipped} · "
                     f"Score {score}/{denom} · Percentage {pct:.2f}%")
    return "\n".join(lines)


async def _finish_locked(app: Application, s: dict, status: str) -> Optional[dict]:
    _cancel(app, s["id"])
    done = db.finish_group_session(s["id"], status, time.time())
    if not done:
        return None
    quiz = db.get_quiz(done["quiz_id"])
    text = result_text(done, quiz["title"] if quiz else "Quiz", status == "stopped")
    try:
        for chunk in engine.split_message(text):
            await app.bot.send_message(done["chat_id"], chunk, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        log.warning("could not send group result: %s", exc)
    return done


async def stop_group_quiz(app: Application, chat_id: int) -> Optional[dict]:
    async with chat_lock(chat_id):
        s = db.get_active_group_session(chat_id)
        if not s:
            return None
        if s.get("cur_message_id"):
            try:
                await app.bot.stop_poll(chat_id, s["cur_message_id"])
            except TelegramError:
                pass
        return await _finish_locked(app, s, "stopped")


async def recover_group_sessions(app: Application) -> dict:
    stats = {"expired": 0, "timers": 0, "resent": 0}
    now = time.time()
    for s in db.list_active_group_sessions():
        if s.get("started_ts") and now - s["started_ts"] > config.SESSION_EXPIRY_HOURS * 3600:
            db.finish_group_session(s["id"], "expired", now)
            stats["expired"] += 1
        elif s.get("cur_poll_id"):
            remaining = (s.get("cur_sent_ts") or now) + int(s["timer"]) + config.TIMER_GRACE - now
            _schedule(app, "timeout", s["id"], max(1.0, remaining),
                      {"sid": s["id"], "poll_id": s["cur_poll_id"], "chat_id": s["chat_id"]})
            stats["timers"] += 1
        else:
            _schedule(app, "next", s["id"], 2.0, {"sid": s["id"], "index": s["current_index"], "chat_id": s["chat_id"]})
            stats["resent"] += 1
    return stats
