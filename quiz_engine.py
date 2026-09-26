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


# ----------------------------------------------------- Telegram text length
def tg_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units).

    Python's len() counts code points; emoji and math-alphanumerics such as
    '𝑥' are ONE code point but TWO UTF-16 units.  All limit checks use this,
    so a poll option that Telegram would see as >100 is never sent natively.
    """
    return len(text.encode("utf-16-le")) // 2


def _tg_prefix_len(text: str, limit: int) -> int:
    """Largest k such that tg_len(text[:k]) <= limit."""
    units = 0
    for k, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return k
    return len(text)


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
    while tg_len(rest) > limit:
        room = max(1, _tg_prefix_len(rest, limit))
        cut = rest.rfind("\n", 0, room)
        if cut <= room // 3:
            cut = rest.rfind(" ", 0, room)
        if cut <= room // 3:
            cut = room
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
    return (tg_len(expl) <= config.POLL_EXPLANATION_MAX
            and expl.count("\n") <= config.POLL_EXPLANATION_MAX_NEWLINES)


# ----------------------------------------------------- question tag
def split_tag(text: str) -> str:
    """The question body without a leading QUESTION_TAG (if the source already has one)."""
    tag = config.QUESTION_TAG
    body = str(text or "").strip()
    if tag and body.startswith(tag):
        body = body[len(tag):].lstrip(" \t\r\n,।:-–—")
    return body


def tag_text(text: str) -> str:
    """Prefix config.QUESTION_TAG exactly once ("<tag>\n\n<text>")."""
    tag = config.QUESTION_TAG
    body = split_tag(text)
    return f"{tag}\n\n{body}" if tag else body


def _tag_head() -> str:
    return f"{config.QUESTION_TAG}\n\n" if config.QUESTION_TAG else ""


def source_no(question: dict, index: int) -> Optional[int]:
    """Original (PDF) question number when it differs from the play position."""
    try:
        src = int(question.get("source_number") or 0)
    except (TypeError, ValueError):
        return None
    return src if src > 0 and src != index + 1 else None


def full_question_text(question_text: str, options: Sequence[str], index: int, total: int,
                       src: Optional[int] = None) -> str:
    head = f"❓ प्रश्न {src} ({index + 1}/{total})" if src else f"❓ प्रश्न {index + 1}/{total}"
    lines = [head, "", split_tag(question_text), ""]
    if config.QUESTION_TAG:
        lines = [config.QUESTION_TAG, ""] + lines
    for i, opt in enumerate(options):
        lines.append(f"({LABELS[i]}) {opt}")
    return "\n".join(lines)


def build_poll_payload(question: dict, perm: Sequence[int], index: int, total: int) -> PollPayload:
    """Build a payload that can NEVER violate Telegram's poll limits.

    * question <= 300 chars and every option <= 100 chars → normal native poll
    * otherwise → the complete question + complete (A)-(D) options are sent as
      a normal message first; the poll then uses a short prompt and, if any
      option is too long, compact (A)…(L) labels (2–12 options).
    """
    perm = list(perm)
    orig_opts = list(question["options"])
    if not (config.MIN_OPTIONS <= len(orig_opts) <= config.MAX_OPTIONS) or \
            sorted(perm) != list(range(len(orig_opts))):
        raise ValueError("invalid options/permutation")
    shown = [str(orig_opts[i]).strip() for i in perm]
    correct = display_correct(perm, int(question["correct_index"]))
    qtext = split_tag(question["question"])       # the tag is added exactly once below
    head = _tag_head()

    src = source_no(question, index)
    prefix = f"[Q{src} · {index + 1}/{total}] " if src else f"[{index + 1}/{total}] "
    # Poll options are single-line: an option with a line break is shown
    # flattened in the poll AND verbatim in the full-text message.
    multiline = any("\n" in o for o in shown)
    poll_opts = [" ".join(o.split()) for o in shown]
    long_opts = any(tg_len(o) > config.POLL_OPTION_MAX or not o for o in poll_opts)
    dup_opts = len(set(poll_opts)) != len(poll_opts)
    # the tag counts toward the 300-unit poll question limit
    long_q = tg_len(head + qtext) > config.POLL_QUESTION_MAX

    payload = PollPayload(question="", options=[], correct_option_id=correct, perm=perm)
    if not long_q and not long_opts and not dup_opts and not multiline:
        full = head + prefix + qtext
        payload.question = full if tg_len(full) <= config.POLL_QUESTION_MAX else head + qtext
        payload.options = shown
    else:
        payload.long_question = long_q
        payload.full_text_chunks = split_message(full_question_text(qtext, shown, index, total, src))
        if long_opts or dup_opts:
            payload.compact = True
            payload.options = [f"({LABELS[i]})" for i in range(len(shown))]
            prompt = "ऊपर दिए गए प्रश्न का सही विकल्प चुनें: " + " / ".join(payload.options)
        else:
            payload.options = poll_opts
            prompt = "ऊपर दिए गए प्रश्न का सही उत्तर चुनें 👇"
        full = head + prefix + prompt       # only our own prompt can be shortened, never the source
        payload.question = full[:_tg_prefix_len(full, config.POLL_QUESTION_MAX)]

    expl = str(question.get("explanation") or "").strip()
    if expl:
        if explanation_fits(expl):
            payload.explanation = expl
        else:
            payload.post_explanation = expl
    # final hard guarantees
    assert 1 <= tg_len(payload.question) <= config.POLL_QUESTION_MAX
    assert all(1 <= tg_len(o) <= config.POLL_OPTION_MAX and "\n" not in o for o in payload.options)
    assert config.MIN_OPTIONS <= len(payload.options) <= config.MAX_OPTIONS
    assert payload.explanation is None or tg_len(payload.explanation) <= config.POLL_EXPLANATION_MAX
    return payload


def compact_fallback(question: dict, perm: Sequence[int], index: int, total: int) -> PollPayload:
    """Used if Telegram still rejects a poll: always-safe labels + full text."""
    perm = list(perm)
    shown = [str(question["options"][i]).strip() for i in perm]
    p = PollPayload(
        question=f"{_tag_head()}[{index + 1}/{total}] ऊपर दिए गए प्रश्न का सही विकल्प चुनें",
        options=[f"({LABELS[i]})" for i in range(len(shown))],
        correct_option_id=display_correct(perm, int(question["correct_index"])),
        perm=perm,
        full_text_chunks=split_message(full_question_text(str(question["question"]), shown, index, total,
                                                         source_no(question, index))),
        compact=True,
    )
    expl = str(question.get("explanation") or "").strip()
    if expl:
        p.post_explanation = expl
    return p


# ----------------------------------------------------- manual input
_LABEL_PREFIX = re.compile(r"^\s*(?:\(\s*([A-La-l])\s*\)|([A-La-l])\s*[\)\.:])\s+(.*)$")


def parse_manual_options(text: str) -> list[str]:
    """Parse 2–12 options typed by a quiz creator.

    Accepted:
      * (A) ... (B) ... (C) ... (multi-line options allowed)
      * 2–12 plain lines, one option per line (text preserved exactly)
    Labels are only stripped when ALL lines carry A,B,C,… labels in order, so
    options like ``A-III, B-II, C-IV, D-I`` are preserved.
    """
    if not text or not text.strip():
        raise ValueError("Options खाली हैं")
    block = pdf_parser.parse_options_block(text)
    if block:
        return block
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not config.MIN_OPTIONS <= len(lines) <= config.MAX_OPTIONS:
        raise ValueError(
            f"{config.MIN_OPTIONS} से {config.MAX_OPTIONS} options चाहिए — आपने {len(lines)} "
            "line(s) भेजीं। हर line में एक option लिखें, या (A) (B) (C)… labels लगाएँ।")
    ms = [_LABEL_PREFIX.match(ln) for ln in lines]
    if all(ms) and [((m.group(1) or m.group(2)).upper()) for m in ms] == list(LABELS[:len(lines)]):
        lines = [m.group(3).strip() for m in ms]
    if any(not ln for ln in lines):
        raise ValueError("कोई option खाली नहीं होना चाहिए")
    return lines


def detect_embedded_options(text: str) -> Optional[tuple[str, list[str]]]:
    """If a creator pastes question + (A)-(D) options together, split them."""
    return pdf_parser.split_question_and_options(text)


_PAREN_OPTION_RE = re.compile(r"^\s*\(\s*[A-La-l]\s*\)\s*\S")


class NoOptionsError(ValueError):
    """The text has no (A)/(B)/… option block — it is not a complete question."""


@dataclass
class TypedQuestion:
    question: str
    options: list[str]
    correct: Optional[int]          # None → the creator must pick it (never guessed)
    explanation: str = ""


def parse_typed_question(text: str) -> TypedQuestion:
    """Parse ONE question typed/pasted by a creator in a single message.

    Layout (answer and explanation lines are optional)::

        question text (any number of lines, may contain its own A-D data)
        (A) … (B) … [up to (L)]        ← the LAST complete option run
        उत्तर: (C)  /  Answer: C
        व्याख्या: …  /  Explanation: …

    Answer/explanation lines are separated BEFORE option detection so they
    are never glued onto the last option.  Every non-answer line after the
    options is kept as explanation — nothing is dropped.

    Raises ValueError (with a user-facing reason) if the text has no usable
    option block, or if the stated answer does not exist among the options.
    """
    lines = (text or "").strip("\n").split("\n")
    ans_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if pdf_parser.ANSWER_LETTER_RE.match(lines[i]):
            ans_idx = i
            break
    expl_idx = None
    start = ans_idx + 1 if ans_idx is not None else 0
    for i in range(start, len(lines)):
        if pdf_parser.EXPL_RE.match(lines[i]):
            expl_idx = i
            break
    if ans_idx is None and expl_idx is None:
        # an explanation without an answer line: the last EXPL line after an option run
        for i in range(len(lines) - 1, -1, -1):
            if pdf_parser.EXPL_RE.match(lines[i]) and pdf_parser.split_question_and_options("\n".join(lines[:i])):
                expl_idx = i
                break
    cut = min(x for x in (ans_idx, expl_idx, len(lines)) if x is not None)
    split = pdf_parser.split_question_and_options("\n".join(lines[:cut]))
    if not split:
        labelled = [ln for ln in lines[:cut] if _PAREN_OPTION_RE.match(ln)]
        if ans_idx is not None or labelled:
            # it was clearly meant as a question → report instead of treating it as pre-question text
            raise ValueError(f"पूरे options नहीं मिले — {config.MIN_OPTIONS}–{config.MAX_OPTIONS} options "
                             "(A) (B) (C)… क्रम में, हर option नई line पर भेजें")
        raise NoOptionsError("options नहीं मिले")
    question, options = split
    correct = None
    tail: list[str] = []
    for i in range(cut, len(lines)):
        ln = lines[i]
        if i == ans_idx:
            m = pdf_parser.ANSWER_LETTER_RE.match(ln)
            correct = pdf_parser.token_to_index(m.group("tok"))
            if correct is None:
                raise ValueError(f"उत्तर '{m.group('tok')}' समझ नहीं आया")
            rest = m.group("rest").strip().lstrip(".,।:;-–— ").strip()
            if rest:
                tail.append(rest)
            continue
        if i == expl_idx:
            rest = pdf_parser.EXPL_RE.match(ln).group("rest").strip()
            if rest:
                tail.append(rest)
            continue
        tail.append(ln)
    if correct is not None and not 0 <= correct < len(options):
        raise ValueError(f"उत्तर ({LABELS[correct] if correct < len(LABELS) else '?'}) दिया है, "
                         f"पर केवल {len(options)} options हैं")
    return TypedQuestion(question, options, correct, "\n".join(tail).strip())


# -------------------------------------------------------- polls received from users
def poll_correct_ids(poll) -> Optional[list[int]]:
    """Correct option(s) of a received quiz poll, exactly as Telegram reports them.

    Bot API 9.6 replaced ``correct_option_id`` by ``correct_option_ids``; both are
    read.  ``None`` means Telegram did not reveal the answer (e.g. a forwarded
    quiz that is still open) — the caller must ask, never guess."""
    ids = getattr(poll, "correct_option_ids", None)
    extra = getattr(poll, "api_kwargs", None) or {}
    if not ids:
        # PTB ≥ 22.8 turns an absent field into an empty tuple; older PTB keeps
        # unknown fields in api_kwargs or only has the scalar correct_option_id
        ids = extra.get("correct_option_ids")
    if not ids:
        one = extra.get("correct_option_id")
        if one is None and not hasattr(poll, "correct_option_ids"):
            one = getattr(poll, "correct_option_id", None)
        ids = [one] if one is not None else None
    return [int(i) for i in ids] if ids else None


# -------------------------------------------------------- forwarded text
def _strip_question_number(text: str) -> str:
    lines = text.strip("\n").split("\n")
    if lines:
        m = pdf_parser.STRONG_Q_RE.match(lines[0]) or pdf_parser.WEAK_Q_RE.match(lines[0])
        if m and m.group("rest").strip():
            lines[0] = m.group("rest").strip()
    return "\n".join(lines)


def parse_forwarded_text(text: str) -> list[dict]:
    """Questions contained in one forwarded/pasted text message, in source order.

    * one or many numbered questions with answers (``प्रश्न 1.`` / ``1.`` / ``Q1``,
      inline ``उत्तर:`` or an answer key) → parsed by the same parser as the
      PDF/Text import (all formats: statements, matching, List-I/II, कूट,
      assertion-reason, ordering; the LAST complete option block is used);
    * a single question without an answer → returned with ``correct=None`` so
      the creator is asked (never guessed).

    Raises NoOptionsError if the text contains no question at all and
    ValueError (listing question numbers) if a multi-question message has
    problems — nothing from such a message is saved.
    """
    first = next((ln for ln in (text or "").split("\n") if ln.strip()), "")
    if not (pdf_parser.STRONG_Q_RE.match(first) or pdf_parser.WEAK_Q_RE.match(first)):
        # no leading question number (e.g. "निम्नलिखित कथन…\n1. …\n2. …"): exactly one
        # question — numbered statements inside it must not be taken as questions
        t = parse_typed_question(text)
        return [{"question": t.question, "options": t.options, "correct": t.correct,
                 "explanation": t.explanation, "qtype": detect_qtype(t.question)}]
    res = pdf_parser.parse_source(text)
    if res.questions and not res.errors:
        return [{"question": q.question, "options": list(q.options), "correct": q.correct_index,
                 "explanation": q.explanation, "qtype": q.qtype} for q in res.questions]
    numbered = [e for e in res.errors if e.number is not None]
    if len(res.questions) + len(numbered) >= 2:
        raise ValueError("इस message के ये प्रश्न parse नहीं हुए (कुछ भी save नहीं किया गया):\n"
                         + "\n".join(map(str, res.errors)))
    t = parse_typed_question(_strip_question_number(text))
    return [{"question": t.question, "options": t.options, "correct": t.correct,
             "explanation": t.explanation, "qtype": detect_qtype(t.question)}]


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
    timeouts: int = 0           # subset of ``skipped``: the timer ran out


def compute_result(total: int, correct: int, wrong: int, skipped: int,
                   duration_sec: float, timeouts: int = 0) -> Result:
    answered_or_skipped = correct + wrong + skipped
    unanswered = max(0, total - answered_or_skipped)
    pct = (100.0 * correct / total) if total else 0.0
    return Result(total, correct, wrong, skipped, unanswered, correct, round(pct, 2), duration_sec,
                  min(int(timeouts or 0), skipped))


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


def group_link(bot_username: str, quiz_id: str) -> str:
    """Telegram's ``startgroup`` deep link: the user picks a group, the bot is
    added (if needed) and ``/start quiz_<id>`` is sent there."""
    return f"https://t.me/{bot_username}?startgroup=quiz_{quiz_id}"


def parse_start_payload(arg: str) -> Optional[str]:
    """``quiz_<id>`` → ``<id>`` (validated)."""
    if not arg:
        return None
    m = re.fullmatch(r"quiz_([A-Za-z0-9]{4,32})", arg.strip())
    return m.group(1) if m else None


def detect_qtype(question: str) -> str:
    return pdf_parser.detect_qtype(question)
