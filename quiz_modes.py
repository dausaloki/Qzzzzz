"""Multiple Quiz Modes, Sectional Timers & Negative Marking (Feature 2, 3, 4).

Supported quiz modes:
* ``standard``  — Native Telegram Quiz Polls with overall quiz timer (default).
* ``sectional`` — Section-wise quiz with configurable timer per section and
                  section-wise score breakdown on completion.
* ``practice``  — Practice mode with immediate detailed answer feedback and
                  explanation after every question.
* ``exam``      — Exam mode where correct answers and explanations are hidden
                  during the attempt (using non-revealing regular polls) and
                  revealed only upon final submission/completion.
"""
from __future__ import annotations

import json
from typing import Optional

import config
import database as db
from keyboards import esc

VALID_MODES = ("standard", "practice", "exam", "sectional")

MODE_LABELS = {
    "standard": "🎯 Standard Mode",
    "practice": "📘 Practice Mode (Immediate Feedback)",
    "exam": "📝 Exam Mode (Answers Hidden Until Finish)",
    "sectional": "📂 Sectional Mode (Per-Section Timers)",
}

NEGATIVE_CHOICES = {
    "0": (0.0, "No Negative (0)"),
    "0.25": (0.25, "-1/4 (0.25)"),
    "0.33": (1.0 / 3.0, "-1/3 (0.33)"),
    "0.5": (0.5, "-1/2 (0.50)"),
    "1": (1.0, "-1.0"),
}


def normalize_mode(mode: Optional[str]) -> str:
    m = (mode or "standard").strip().lower()
    return m if m in VALID_MODES else "standard"


def mode_label(mode: Optional[str]) -> str:
    return MODE_LABELS.get(normalize_mode(mode), MODE_LABELS["standard"])


def get_section_timers(quiz: Optional[dict]) -> dict[str, int]:
    if not quiz:
        return {}
    raw = quiz.get("section_timers_json") or "{}"
    if isinstance(raw, dict):
        return {str(k).strip(): int(v) for k, v in raw.items() if str(k).strip()}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k).strip(): int(v) for k, v in parsed.items() if str(k).strip()}
    except Exception:
        pass
    return {}


def set_section_timer(quiz_id: str, section_name: str, timer_sec: int) -> dict[str, int]:
    sec = (section_name or "").strip()
    if not sec:
        raise ValueError("Section name is empty")
    val = int(timer_sec)
    if val != 0 and not (config.CUSTOM_TIMER_MIN <= val <= config.CUSTOM_TIMER_MAX):
        raise ValueError(f"Timer must be 0 or {config.CUSTOM_TIMER_MIN}..{config.CUSTOM_TIMER_MAX} seconds")
    quiz = db.get_quiz(quiz_id)
    if not quiz:
        raise ValueError("Quiz not found")
    timers = get_section_timers(quiz)
    timers[sec] = val
    db.update_quiz(quiz_id, section_timers_json=json.dumps(timers, ensure_ascii=False))
    return timers


def resolve_question_timer(quiz: Optional[dict], question: Optional[dict], default_timer: int = 45) -> int:
    """Resolve effective timer (in seconds) for a specific question."""
    base_timer = int((quiz or {}).get("timer", default_timer) or 0)
    if not quiz or not question:
        return base_timer
    sec = (question.get("section") or question.get("section_label") or "").strip()
    if not sec:
        return base_timer
    timers = get_section_timers(quiz)
    for k, v in timers.items():
        if k.lower() == sec.lower():
            return int(v)
    return base_timer


def quiz_sections(quiz_id: str) -> list[str]:
    """Ordered unique section names present in a quiz's questions + configured timers."""
    seen: list[str] = []
    for q in db.get_questions(quiz_id):
        sec = (q.get("section") or "").strip()
        if sec and sec not in seen:
            seen.append(sec)
    quiz = db.get_quiz(quiz_id)
    for sec in get_section_timers(quiz):
        if sec and sec not in seen:
            seen.append(sec)
    return seen


def format_practice_feedback(
    question: dict,
    *,
    chosen_index: Optional[int],
    status: str,
) -> str:
    """Build immediate feedback card for Practice mode."""
    opts = question.get("options") or []
    ci = int(question.get("correct_index") or 0)
    correct_letter = chr(65 + ci) if 0 <= ci < 26 else str(ci + 1)
    correct_text = opts[ci] if 0 <= ci < len(opts) else ""

    if status == "correct":
        header = f"✅ <b>सही उत्तर!</b> ({correct_letter}) {esc(correct_text)}"
    elif status == "wrong":
        chosen_letter = (
            chr(65 + int(chosen_index))
            if chosen_index is not None and 0 <= int(chosen_index) < 26
            else "?"
        )
        header = (
            f"❌ <b>गलत उत्तर!</b> (आपका चुनाव: {chosen_letter})\n"
            f"✔️ <b>सही उत्तर:</b> ({correct_letter}) {esc(correct_text)}"
        )
    elif status == "timeout":
        header = f"⏱️ <b>समय समाप्त!</b>\n✔️ <b>सही उत्तर:</b> ({correct_letter}) {esc(correct_text)}"
    else:
        header = f"⏭️ <b>Skipped!</b>\n✔️ <b>सही उत्तर:</b> ({correct_letter}) {esc(correct_text)}"

    expl = (question.get("explanation") or "").strip()
    if expl:
        return f"{header}\n\n💡 <b>Explanation:</b> {esc(expl)}"
    return header


def compute_net_score(correct: int, wrong: int, negative_mark: float = 0.0) -> float:
    neg = max(0.0, float(negative_mark or 0.0))
    return round(float(correct) - float(wrong) * neg, 2)


def negative_of(row: Optional[dict]) -> float:
    """Stored negative mark of a quiz / attempt / group session (0 = disabled)."""
    try:
        return max(0.0, float((row or {}).get("negative_mark") or 0.0))
    except (TypeError, ValueError):
        return 0.0


_FRACTIONS = ((1.0 / 3.0, "1/3"), (0.25, "1/4"), (0.5, "1/2"), (1.0 / 5.0, "1/5"))


def negative_label(negative_mark: float) -> str:
    """Human label: ``1/3``, ``1/4``, ``1/2`` … (``""`` when disabled)."""
    neg = max(0.0, float(negative_mark or 0.0))
    if neg <= 0:
        return ""
    for value, label in _FRACTIONS:
        if abs(neg - value) < 0.005:
            return label
    return f"{neg:.2f}".rstrip("0").rstrip(".")


def format_score(value: float) -> str:
    """``2`` for whole numbers, otherwise two decimals (``1.67``)."""
    v = round(float(value or 0.0), 2)
    return str(int(v)) if abs(v - round(v)) < 1e-9 else f"{v:.2f}"


def compute_section_breakdown(
    questions: list[dict],
    answers: list[dict],
    negative_mark: float = 0.0,
) -> dict[str, dict]:
    """Compute per-section totals, correct, wrong, skipped, and net score."""
    ans_by_qid = {int(a["question_id"]): a for a in answers if a.get("question_id") is not None}

    has_named_section = any((q.get("section") or "").strip() for q in questions)
    if not has_named_section:
        return {}

    breakdown: dict[str, dict] = {}
    for q in questions:
        sec = (q.get("section") or "").strip() or "General"
        bucket = breakdown.setdefault(
            sec,
            {"total": 0, "correct": 0, "wrong": 0, "skipped": 0, "score": 0.0},
        )
        bucket["total"] += 1
        a = ans_by_qid.get(int(q["id"]))
        if not a:
            bucket["skipped"] += 1
        elif a.get("status") == "correct" or int(a.get("correct") or 0) == 1:
            bucket["correct"] += 1
        elif a.get("status") == "wrong":
            bucket["wrong"] += 1
        else:
            bucket["skipped"] += 1

    for sec, b in breakdown.items():
        b["score"] = compute_net_score(b["correct"], b["wrong"], negative_mark)
    return breakdown


def format_section_breakdown_lines(breakdown: dict[str, dict]) -> list[str]:
    if not breakdown:
        return []
    lines = ["", "📂 <b>Section-wise Score:</b>"]
    for sec, b in breakdown.items():
        lines.append(
            f"  • <b>{esc(sec)}</b>: ✅ {b.get('correct', 0)}/{b.get('total', 0)} "
            f"(❌ {b.get('wrong', 0)}, ⏭ {b.get('skipped', 0)}) — Score: <b>{format_score(b.get('score', 0))}</b>"
        )
    return lines
