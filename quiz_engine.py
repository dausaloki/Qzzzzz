"""Pure quiz logic (no Telegram I/O) — easy to unit test.

* question ordering / option shuffling with correct-answer re-mapping
* building a Telegram-safe native quiz poll payload (length limits!)
* lossless splitting of long messages
* manual option parsing, scoring, formatting
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

import config
import pdf_parser

LABELS = config.OPTION_LABELS


# ----------------------------------------------------------- ordering
def build_order(question_ids: Sequence[int], shuffle: bool,
                rng: Optional[random.Random] = None) -> list[int]:
    order = list(question_ids)
    if shuffle and len(order) > 1:
        (rng or random).shuffle(order)
    return order


def make_permutation(n: int, shuffle: bool, rng: Optional[random.Random] = None) -> list[int]:
    """perm[display_position] = original option index."""
    perm = list(range(n))
    if shuffle and n > 1:
        (rng or random).shuffle(perm)
    return perm


def display_correct(perm: Sequence[int], original_correct: int) -> int:
    return list(perm).index(original_correct)


def original_choice(perm: Sequence[int], display_choice: int) -> int:
    return perm[display_choice]


# Backward-compatible helper from the old quiz_engine
def poll_data(question: dict, shuffle_options: bool = False, rng=None):
    perm = make_permutation(len(question["options"]), shuffle_options, rng)
    options = [question["options"][i] for i in perm]
    return options, display_correct(perm, question["correct_index"])


# ----------------------------------------------------- message splitting
def split_message(text: str, limit: int = config.SAFE_MESSAGE_CHUNK) -> list[str]:
    """Split ``text`` into chunks <= limit WITHOUT losing a single character.

    ``"".join(chunks) == text`` always holds. Splits prefer newlines, then
    spaces, then a hard cut.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= limit // 3:
            cut = rest.rfind(" ", 0, limit)
        if cut <= limit // 3:
            cut = limit
        else:
            cut += 1  # keep the separator at the end of the chunk
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest or not chunks:
        chunks.append(rest)
    return chunks


# ------------------------------------------------------- poll payloads
@dataclass
class PollPayload:
    question: str
    options: list[str]
    correct_option_id: int
    perm: list[int]
    full_text_chunks: list[str] = field(default_factory=list)  # sent before the poll
    explanation: Optional[str] = None       # native poll explanation (fits limits)
    post_explanation: str = ""              # full explanation sent after answering
    compact: bool = False                   # options replaced by A/B/C/D labels
    long_question: bool = False


def explanation_fits(expl: str) -> bool:
    return (len(expl) <= config.POLL_EXPLANATION_MAX
            and expl.count("\n") <= config.POLL_EXPLANATION_MAX_NEWLINES)


def full_question_text(question_text: str, options: Sequence[str], index: int, total: int) -> str:
    lines = [f"❓ प्रश्न {index + 1}/{total}", "", question_text.strip(), ""]
    for i, opt in enumerate(options):
        lines.append(f"({LABELS[i]}) {opt}")
    return "\n".join(lines)


def build_poll_payload(question: dict, perm: Sequence[int], index: int, total: int) -> PollPayload:
    """Build a payload that can NEVER violate Telegram's poll limits.

    * question <= 300 chars and every option <= 100 chars → normal native poll
    * otherwise → the complete question + complete (A)-(D) options are sent as
      a normal message first; the poll then uses a short prompt and, if any
      option is too long, compact A/B/C/D labels.
    """
    perm = list(perm)
    orig_opts = list(question["options"])
    if len(orig_opts) != config.NUM_OPTIONS or sorted(perm) != list(range(len(orig_opts))):
        raise ValueError("invalid options/permutation")
    shown = [str(orig_opts[i]).strip() for i in perm]
    correct = display_correct(perm, int(question["correct_index"]))
    qtext = str(question["question"]).strip()

    prefix = f"[{index + 1}/{total}] "
    long_opts = any(len(o) > config.POLL_OPTION_MAX or not o for o in shown)
    dup_opts = len(set(shown)) != len(shown)
    long_q = len(qtext) > config.POLL_QUESTION_MAX

    payload = PollPayload(question="", options=[], correct_option_id=correct, perm=perm)
    if not long_q and not long_opts and not dup_opts:
        payload.question = prefix + qtext if len(prefix + qtext) <= config.POLL_QUESTION_MAX else qtext
        payload.options = shown
    else:
        payload.long_question = long_q
        payload.full_text_chunks = split_message(full_question_text(qtext, shown, index, total))
        if long_opts or dup_opts:
            payload.compact = True
            payload.options = [f"({LABELS[i]})" for i in range(len(shown))]
            prompt = "ऊपर दिए गए प्रश्न का सही विकल्प चुनें: (A) / (B) / (C) / (D)"
        else:
            payload.options = shown
            prompt = "ऊपर दिए गए प्रश्न का सही उत्तर चुनें 👇"
        payload.question = (prefix + prompt)[:config.POLL_QUESTION_MAX]

    expl = str(question.get("explanation") or "").strip()
    if expl:
        if explanation_fits(expl):
            payload.explanation = expl
        else:
            payload.post_explanation = expl
    # final hard guarantees
    assert 1 <= len(payload.question) <= config.POLL_QUESTION_MAX
    assert all(1 <= len(o) <= config.POLL_OPTION_MAX for o in payload.options)
    assert payload.explanation is None or len(payload.explanation) <= config.POLL_EXPLANATION_MAX
    return payload


def compact_fallback(question: dict, perm: Sequence[int], index: int, total: int) -> PollPayload:
    """Used if Telegram still rejects a poll: always-safe labels + full text."""
    perm = list(perm)
    shown = [str(question["options"][i]).strip() for i in perm]
    p = PollPayload(
        question=f"[{index + 1}/{total}] ऊपर दिए गए प्रश्न का सही विकल्प चुनें",
        options=[f"({LABELS[i]})" for i in range(len(shown))],
        correct_option_id=display_correct(perm, int(question["correct_index"])),
        perm=perm,
        full_text_chunks=split_message(full_question_text(str(question["question"]), shown, index, total)),
        compact=True,
    )
    expl = str(question.get("explanation") or "").strip()
    if expl:
        p.post_explanation = expl
    return p


# ----------------------------------------------------- manual input
_LABEL_PREFIX = re.compile(r"^\s*(?:\(\s*([A-Da-d])\s*\)|([A-Da-d])\s*[\)\.:])\s+(.*)$")


def parse_manual_options(text: str) -> list[str]:
    """Parse exactly 4 options typed by a quiz creator.

    Accepted:
      * (A) ... (B) ... (C) ... (D) ... (multi-line options allowed)
      * 4 plain lines, one option per line (text preserved exactly)
    Labels are only stripped when all four lines carry A,B,C,D labels in
    order, so options like ``A-III, B-II, C-IV, D-I`` are preserved.
    """
    if not text or not text.strip():
        raise ValueError("Options खाली हैं")
    block = pdf_parser.parse_options_block(text)
    if block:
        return block
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) != 4:
        raise ValueError(
            f"ठीक 4 options चाहिए — आपने {len(lines)} lines भेजीं। "
            "हर line में एक option लिखें, या (A) (B) (C) (D) labels लगाएँ।")
    ms = [_LABEL_PREFIX.match(ln) for ln in lines]
    if all(ms) and [((m.group(1) or m.group(2)).upper()) for m in ms] == list("ABCD"):
        lines = [m.group(3).strip() for m in ms]
    if any(not ln for ln in lines):
        raise ValueError("कोई option खाली नहीं होना चाहिए")
    return lines


def detect_embedded_options(text: str) -> Optional[tuple[str, list[str]]]:
    """If a creator pastes question + (A)-(D) options together, split them."""
    return pdf_parser.split_question_and_options(text)


# -------------------------------------------------------- results
@dataclass
class Result:
    total: int
    correct: int
    wrong: int
    skipped: int
    unanswered: int
    score: int
    percentage: float
    duration_sec: float


def compute_result(total: int, correct: int, wrong: int, skipped: int,
                   duration_sec: float) -> Result:
    answered_or_skipped = correct + wrong + skipped
    unanswered = max(0, total - answered_or_skipped)
    pct = (100.0 * correct / total) if total else 0.0
    return Result(total, correct, wrong, skipped, unanswered, correct, round(pct, 2), duration_sec)


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def deep_link(bot_username: str, quiz_id: str) -> str:
    return f"https://t.me/{bot_username}?start=quiz_{quiz_id}"


def parse_start_payload(arg: str) -> Optional[str]:
    """``quiz_<id>`` → ``<id>`` (validated)."""
    if not arg:
        return None
    m = re.fullmatch(r"quiz_([A-Za-z0-9]{4,32})", arg.strip())
    return m.group(1) if m else None


def detect_qtype(question: str) -> str:
    return pdf_parser.detect_qtype(question)
