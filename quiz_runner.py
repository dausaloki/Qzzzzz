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

import access_control
import config
import database as db
import group_runner
import html_report
import keyboards as kb
import quiz_engine as engine
import quiz_modes
import review_pdf
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
def ready_text(quiz: dict) -> str:
    """The "get ready" card shown before a quiz starts (Start button: r:go)."""
    n = quiz.get("question_count", db.count_questions(quiz["id"]))
    lines = [f"🎲 <b>Get ready for the quiz “{esc(quiz['title'])}”</b>"]
    if quiz.get("description"):
        lines.append(esc(quiz["description"]))
    lines += ["",
              f"🖊 {n} {'question' if n == 1 else 'questions'}",
              f"⏱ {config.timer_label(quiz['timer'])} per question" if quiz.get("timer")
              else "⏱ No time limit",
              f"🔀 {'Questions and answers shuffled' if quiz['shuffle_questions'] and quiz['shuffle_options'] else 'Questions shuffled' if quiz['shuffle_questions'] else 'Answers shuffled' if quiz['shuffle_options'] else 'No shuffle'}"]
    mode = quiz_modes.normalize_mode(quiz.get("quiz_mode"))
    if mode != "standard":
        lines.append(f"🎯 Mode: {esc(quiz_modes.mode_label(mode))}")
    neg = quiz_modes.negative_of(quiz)
    if neg > 0:
        lines.append(f"➖ Negative Marking: -{quiz_modes.negative_label(neg)} प्रति गलत उत्तर")
    lines += ["",
              "तैयार होने पर नीचे button दबाएँ। रोकने के लिए /stop, प्रश्न छोड़ने के लिए /skip भेजें।"]
    return "\n".join(lines)


async def send_ready(app: Application, user, chat_id: int, qid: str) -> bool:
    """Show the ready card; the quiz itself starts on the r:go button."""
    bot = app.bot
    quiz = db.get_quiz(qid)
    if not quiz:
        await bot.send_message(chat_id, "❌ Quiz नहीं मिला (शायद delete हो चुका है)।", reply_markup=kb.main_menu())
        return False
    if quiz.get("status") == "draft" and quiz["owner_id"] != user.id:
        await bot.send_message(chat_id, "⚠️ यह quiz अभी तैयार नहीं हुआ है।")
        return False
    if not quiz.get("question_count"):
        await bot.send_message(chat_id, "⚠️ इस quiz में अभी कोई प्रश्न नहीं है।", reply_markup=kb.main_menu())
        return False
    ok_access, deny_msg = access_control.check_quiz_access(user.id, chat_id, quiz)
    if not ok_access:
        await bot.send_message(chat_id, deny_msg, parse_mode=ParseMode.HTML, reply_markup=kb.main_menu())
        return False
    await bot.send_message(chat_id, ready_text(quiz), parse_mode=ParseMode.HTML, reply_markup=kb.ready_markup(qid))
    return True


async def start_quiz(app: Application, user, chat_id: int, qid: str, chat_type: str = ChatType.PRIVATE,
                     start_msg_id: Optional[int] = None) -> Optional[int]:
    """Start a new attempt for ``user``. Returns attempt id or None.

    ``start_msg_id`` is the message whose button started the quiz: a second
    tap on the same button while that attempt is running is ignored."""
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
    ok_access, deny_msg = access_control.check_quiz_access(user.id, chat_id, quiz)
    if not ok_access:
        await bot.send_message(chat_id, deny_msg, parse_mode=ParseMode.HTML, reply_markup=kb.main_menu())
        return None

    async with user_lock(user.id):
        old = db.get_active_attempt(user.id)
        if old and start_msg_id is not None and old.get("start_msg_id") == start_msg_id \
                and old["quiz_id"] == qid:
            return None                      # double tap on the same Start button
        if old:
            had_answers = bool((old.get("correct") or 0) + (old.get("wrong") or 0) > 0)
            await _stop_locked(
                app, old, notify=True,
                reason="नया quiz शुरू करने पर पिछला quiz रोक दिया गया।",
                auto_pdf=had_answers,
            )
        order = engine.build_order([q["id"] for q in questions], bool(quiz["shuffle_questions"]))
        attempt_id = db.create_attempt(qid, user.id, chat_id, order, int(quiz["timer"] or 0),
                                       bool(quiz["shuffle_options"]), time.time(), start_msg_id,
                                       snapshot_rules=True)
    try:
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
    quiz = db.get_quiz(att["quiz_id"])
    mode = quiz_modes.normalize_mode(att.get("quiz_mode"))
    perm = engine.make_permutation(len(q["options"]), bool(att["shuffle_options"]))
    try:
        payload = engine.build_poll_payload(q, perm, idx, total)
    except Exception:  # never let one bad question crash the quiz
        log.exception("payload build failed for question %s", q["id"])
        payload = engine.compact_fallback(q, perm, idx, total)

    timer = quiz_modes.resolve_question_timer(quiz, q, default_timer=int(att.get("timer") or 0))
    # Section header notice when entering a new section in sectional mode (or when section timers exist)
    cur_sec = (q.get("section") or "").strip()
    if cur_sec and (mode == "sectional" or quiz_modes.get_section_timers(quiz)):
        prev_sec = ""
        if idx > 0:
            prev_q = db.get_question(att["quiz_id"], order[idx - 1])
            prev_sec = (prev_q.get("section") or "").strip() if prev_q else ""
        if cur_sec.lower() != prev_sec.lower():
            try:
                await bot.send_message(
                    chat_id,
                    f"📂 <b>Section: {esc(cur_sec)}</b> (⏱ {config.timer_label(timer)})",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass

    # Telegram accepts open_period 5..600 s; shorter values are enforced by our job only
    open_period = timer if 5 <= timer <= 600 else None
    exam_mode = (mode == "exam")
    sent = {"text": False}
    try:
        await _send_pre_media(bot, chat_id, q)
        msg = await _send_poll(bot, chat_id, payload, open_period, sent, exam_mode=exam_mode)
    except BadRequest as exc:
        log.warning("send_poll rejected (%s) — using compact fallback", exc)
        had_text = bool(payload.full_text_chunks) and sent["text"]
        payload = engine.compact_fallback(q, perm, idx, total)
        if had_text:          # the identical full question text is already in the chat
            payload.full_text_chunks = []
        try:
            msg = await _send_poll(bot, chat_id, payload, open_period, exam_mode=exam_mode)
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


async def _send_poll(bot, chat_id: int, payload: engine.PollPayload, open_period: Optional[int],
                     sent: Optional[dict] = None, *, exam_mode: bool = False):
    for chunk in payload.full_text_chunks:
        await bot.send_message(chat_id, chunk)
    if sent is not None and payload.full_text_chunks:
        sent["text"] = True
    if exam_mode:
        # In Exam mode, answers and explanations remain hidden until final submission
        return await bot.send_poll(
            chat_id=chat_id,
            question=payload.question,
            options=payload.options,
            type="regular",
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=open_period,
            reply_markup=kb.running_markup(),
        )
    return await bot.send_poll(
        chat_id=chat_id,
        question=payload.question,
        options=payload.options,
        type="quiz",
        correct_option_ids=[payload.correct_option_id],     # Bot API 9.6+ field
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
    app = context.application
    async with user_lock(ans.user.id):
        att = db.get_attempt_by_poll(ans.poll_id)
        if att and att["user_id"] == ans.user.id:
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
            orig_choice = engine.original_choice(perm, chosen)
            status_str = "correct" if is_correct else "wrong"
            q_id = att["cur_question_id"]
            db.record_answer(att["id"], q_id, att["current_index"],
                             orig_choice, is_correct, status_str, elapsed)
            mode = quiz_modes.normalize_mode(att.get("quiz_mode"))
            if mode == "exam":
                pass  # Keep answers/explanations hidden until the exam finishes
            elif mode == "practice":
                q_obj = db.get_question(att["quiz_id"], q_id)
                if q_obj:
                    fb = quiz_modes.format_practice_feedback(q_obj, chosen_index=orig_choice, status=status_str)
                    try:
                        await context.bot.send_message(att["chat_id"], fb, parse_mode=ParseMode.HTML)
                    except TelegramError as exc:
                        log.warning("could not send practice feedback: %s", exc)
            elif post_expl:
                try:
                    await _send_text(context.bot, att["chat_id"],
                                     ("✅ सही!" if is_correct else "❌ गलत।") + "\n💡 Explanation:\n" + post_expl)
                except TelegramError as exc:
                    log.warning("could not send explanation: %s", exc)
            _schedule(app, "next", att["id"], config.NEXT_QUESTION_DELAY,
                      {"attempt_id": att["id"], "index": att["current_index"] + 1, "user_id": att["user_id"]})
            return
    if att and att["user_id"] != ans.user.id:
        return  # someone else's poll
    # not a private session → maybe a poll of a group quiz session
    await group_runner.handle_group_answer(update, context)


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
        await _skip_current_locked(app, att, reason="⏰ समय समाप्त! प्रश्न skipped।", status="timeout")


async def _skip_current_locked(app: Application, att: dict, reason: str, status: str = "skipped") -> None:
    bot = app.bot
    _cancel_jobs(app, att["id"], ("timeout",))
    if att.get("cur_message_id"):
        try:
            await bot.stop_poll(att["chat_id"], att["cur_message_id"])
        except TelegramError:
            pass  # already closed by open_period
    elapsed = time.time() - (att.get("cur_sent_ts") or time.time())
    post_expl = att.get("cur_post_explanation") or ""
    q_id = att["cur_question_id"]
    db.record_answer(att["id"], q_id, att["current_index"], None, False, status, elapsed)
    mode = quiz_modes.normalize_mode(att.get("quiz_mode"))
    text = reason
    if mode == "exam":
        pass  # Do not reveal explanation during exam mode
    elif mode == "practice":
        q_obj = db.get_question(att["quiz_id"], q_id)
        if q_obj:
            text += "\n" + quiz_modes.format_practice_feedback(q_obj, chosen_index=None, status=status)
    elif post_expl:
        text += "\n💡 Explanation:\n" + post_expl
    try:
        if mode == "practice":
            await bot.send_message(att["chat_id"], text, parse_mode=ParseMode.HTML)
        else:
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
def result_text(att: dict, quiz_title: str, stopped: bool, *,
                section_scores: Optional[dict] = None,
                negative_mark: float = 0.0,
                net_score: Optional[float] = None) -> str:
    r = engine.compute_result(att["total"], att["correct"], att["wrong"], att["skipped"],
                              att.get("duration_sec") or 0, att.get("timeouts") or 0)
    head = "⏹ <b>Quiz stopped</b> — partial result saved" if stopped else "🏁 <b>The quiz has finished!</b>"
    lines = [head, "", "📊 <b>Result</b>",
             f"Quiz: {esc(quiz_title)}",
             f"Total: {r.total}",
             f"Correct: {r.correct} ✅",
             f"Wrong: {r.wrong} ❌",
             f"Skipped: {r.skipped} ⌛",
             f"Timeout: {r.timeouts} ⏰" + (" (Skipped में शामिल)" if r.timeouts else "")]
    if r.unanswered:
        lines.append(f"Not attempted: {r.unanswered}")
    lines += [f"Score: {r.score}/{r.total}",
              f"Percentage: {r.percentage:.2f}%",
              f"Time: {engine.format_duration(r.duration_sec)}"]
    if negative_mark > 0 and net_score is not None:
        penalty = round(float(r.wrong) * float(negative_mark), 2)
        lines.append(f"Negative Marking: -{quiz_modes.negative_label(negative_mark)} प्रति गलत उत्तर "
                     f"(−{quiz_modes.format_score(penalty)})")
        lines.append(f"Net Score: <b>{quiz_modes.format_score(net_score)}/{r.total}</b>")
    if section_scores:
        lines += quiz_modes.format_section_breakdown_lines(section_scores)
    if not stopped:
        rank, n = db.user_rank(att["quiz_id"], att["user_id"])
        if rank and n > 1:
            lines += ["", f"🏆 आपकी rank (best attempt): {rank} / {n} participants"]
    return "\n".join(lines)


async def _finish_locked(app: Application, att: dict, status: str, *, auto_pdf: bool = True) -> Optional[dict]:
    _cancel_jobs(app, att["id"])
    duration = time.time() - (att.get("started_ts") or time.time())
    done = db.finish_attempt(att["id"], status, duration)
    if not done:
        return None
    quiz = db.get_quiz(done["quiz_id"])
    title = quiz["title"] if quiz else "Quiz"
    mode = quiz_modes.normalize_mode(done.get("quiz_mode"))
    neg_mark = quiz_modes.negative_of(done)
    questions = db.get_questions(done["quiz_id"])
    ans_rows = db.get_attempt_answers(done["id"])
    sec_scores = quiz_modes.compute_section_breakdown(questions, ans_rows, neg_mark)
    net_val = quiz_modes.compute_net_score(done["correct"], done["wrong"], neg_mark)
    try:
        db.update_attempt_metadata(
            done["id"],
            quiz_mode=mode,
            section_scores=sec_scores,
            negative_mark=neg_mark,
            net_score=net_val,
        )
    except Exception:  # noqa: BLE001
        log.warning("could not save attempt metadata for attempt %s", done["id"])
    try:
        await app.bot.send_message(
            done["chat_id"],
            result_text(done, title, status == "stopped", section_scores=sec_scores,
                        negative_mark=neg_mark, net_score=net_val),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.result_markup(done["quiz_id"], db.next_part(done["quiz_id"])),
        )
    except TelegramError as exc:
        log.warning("could not send result: %s", exc)
    # Complete review PDF (every question), sent right after the existing result.
    # Guarded: a PDF problem can never affect the result that was already saved.
    if auto_pdf:
        try:
            await review_pdf.auto_after_finish(app, done)
        except Exception:                       # noqa: BLE001
            log.exception("review PDF hook failed")
        if getattr(config, "AUTO_HTML_REPORT", False):
            try:
                h_bytes, h_name, _ = html_report.build_html_report_for_attempt(done["id"])
                await app.bot.send_document(
                    chat_id=done["chat_id"],
                    document=h_bytes,
                    filename=h_name,
                    caption=f"🌐 <b>Interactive HTML Report</b> — {esc(title)}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:                   # noqa: BLE001
                log.exception("auto HTML report failed")
    return done


async def _stop_locked(app: Application, att: dict, notify: bool = True, reason: str = "",
                       *, auto_pdf: bool = True) -> Optional[dict]:
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
    return await _finish_locked(app, att, status="stopped", auto_pdf=auto_pdf)


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
