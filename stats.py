"""Statistics formatting."""
from __future__ import annotations

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M

import database as db
import keyboards as kb
import quiz_engine as engine
from keyboards import esc


def _name(r: dict) -> str:
    if r.get("username"):
        return "@" + r["username"]
    return r.get("first_name") or f"User {r.get('user_id')}"


def _pct(correct: int, total: int) -> str:
    return f"{(100.0 * correct / total):.1f}%" if total else "0%"


def quiz_stats_text(qid: str, viewer_id: int) -> str:
    """Owner: aggregate stats + leaderboard + own results.
    Anyone else: only their own results and rank (other users' data is not shown)."""
    quiz = db.get_quiz(qid)
    if quiz["owner_id"] != viewer_id:
        lines = [f"📊 <b>{esc(quiz['title'])}</b>"]
        rank, n = db.user_rank(qid, viewer_id)
        if rank:
            lines += ["", f"🏆 आपकी rank (best attempt): {rank} / {n}"]
        mine = db.user_history(viewer_id, qid, limit=10)
        if not mine:
            lines += ["", "ℹ️ आपने अभी यह quiz पूरा नहीं किया है।"]
        else:
            lines += ["", "📜 <b>आपके results</b>"]
            lines += [_history_line(a) for a in mine]
        return "\n".join(lines)
    s, leaders = db.quiz_stats(qid)
    lines = [f"📊 <b>{esc(quiz['title'])}</b>", "",
             f"👥 Total attempts: {s['attempts']} ({s['users']} users)",
             f"✅ Correct answers: {s['correct']}",
             f"❌ Wrong answers: {s['wrong']}",
             f"⏭ Skipped: {s['skipped']}",
             f"📈 Average score: {s['avg_pct']:.1f}%"]
    g = db.group_quiz_stats(qid)
    if g.get("sessions"):
        lines += ["", f"👥 Group quizzes: {g['sessions']} sessions in {g['groups']} "
                      f"{'group' if g['groups'] == 1 else 'groups'} · {g['participants']} participants"]
    if leaders:
        lines += ["", "🏆 <b>Top users</b>"]
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(leaders):
            m = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{m} {esc(_name(r))} — {r['correct']}/{r['total']} "
                         f"({_pct(r['correct'], r['total'])}) • {engine.format_duration(r['duration_sec'])}")
    mine = db.user_history(viewer_id, qid, limit=5)
    if mine:
        lines += ["", "📜 <b>आपके पिछले results</b>"]
        lines += [_history_line(a) for a in mine]
    return "\n".join(lines)


def _history_line(a: dict) -> str:
    flag = " (stopped)" if a["status"] == "stopped" else ""
    return (f"• {a['correct']}/{a['total']} ({_pct(a['correct'], a['total'])}) "
            f"✅{a['correct']} ❌{a['wrong']} ⏭{a['skipped']} • "
            f"{engine.format_duration(a['duration_sec'])} • {(a['finished_at'] or '')[:16]}{flag}")


def quiz_stats_markup(qid: str) -> M:
    return M([[B("▶ Start", callback_data=f"q:run:{qid}"), B("⬅️ Quiz", callback_data=f"q:view:{qid}")],
              kb.home_button()])


def user_overview(user_id: int) -> tuple[str, M]:
    t = db.user_totals(user_id)
    hist = db.user_history(user_id, limit=10)
    lines = ["📊 <b>Quiz Stats</b>", "",
             f"🎮 आपके attempts: {t['attempts']}",
             f"✅ Correct: {t['correct']}   ❌ Wrong: {t['wrong']}   ⏭ Skipped: {t['skipped']}",
             f"📈 Overall: {_pct(t['correct'], t['total'])}"]
    if hist:
        lines += ["", "📜 <b>Result history</b>"]
        for a in hist:
            flag = " ⏹" if a["status"] == "stopped" else ""
            lines.append(f"• {esc((a.get('title') or 'Deleted quiz')[:35])}: {a['correct']}/{a['total']} "
                         f"({_pct(a['correct'], a['total'])}) • {(a['finished_at'] or '')[:10]}{flag}")
    rows = []
    for q in db.get_owner_quizzes(user_id, include_drafts=False)[:10]:
        rows.append([B(f"📊 {q['title'][:40]}", callback_data=f"q:stats:{q['id']}")])
    if rows:
        lines += ["", "👇 अपने quiz के detailed stats (top users सहित) देखें:"]
    rows.append(kb.home_button())
    return "\n".join(lines), M(rows)
