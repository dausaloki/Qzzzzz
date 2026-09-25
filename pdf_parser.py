"""PDF / plain-text MCQ parser.

Design goals
------------
* Never guess an answer. A question is valid only if its correct option comes
  from an explicit inline answer (``उत्तर: (A)``, ``Answer: B``...) or from an
  answer-key section (``1-A, 2-C`` / ``1. (B)`` / tables).
* Never silently skip anything: every problem is reported with the source
  question number.
* Correctly find the *real* A/B/C/D answer choices even when the question body
  itself contains A-D data (List-I/List-II matching, assertion-reason, ordering
  data, statements).  The answer choices are the LAST complete A→B→C→D run of a
  question block.
* Statement numbers (``1. …  2. …``) and List-II numbers inside a question are
  NOT mistaken for new questions: question markers are validated as an
  increasing sequence and must be preceded by a completed option run.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MAX_QUESTIONS = 100
LETTERS = "ABCD"
_HINDI_LETTERS = "कखगघङ"
_JUMP = 3  # tolerated gap in question numbering (gaps are reported as errors)

# --------------------------------------------------------------------- data


@dataclass
class ParseError:
    number: Optional[int]
    message: str

    def __str__(self) -> str:
        if self.number is None:
            return self.message
        return f"प्रश्न {self.number}: {self.message}"


@dataclass
class ParsedQuestion:
    number: int
    question: str
    options: list[str]
    correct_index: int
    explanation: str = ""
    qtype: str = "mcq"
    answer_source: str = ""

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "question": self.question,
            "options": list(self.options),
            "correct_index": self.correct_index,
            "explanation": self.explanation,
            "qtype": self.qtype,
            "answer_source": self.answer_source,
        }


@dataclass
class ParseResult:
    questions: list[ParsedQuestion] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    detected_numbers: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.questions) and not self.errors

    def to_dict(self) -> dict:
        return {
            "questions": [q.to_dict() for q in self.questions],
            "errors": [{"number": e.number, "message": e.message} for e in self.errors],
            "warnings": list(self.warnings),
        }


class SourceError(ValueError):
    """The source as a whole could not be read (bad PDF, no text, etc.)."""


# ------------------------------------------------------------------ regexes
_SEP = r"[\.\):\-–—]"

STRONG_Q_RE = re.compile(
    r"^\s*(?:प्रश्न|प्र\s*\.|Question|Ques\.?|Que\.?|Q)\s*(?:सं(?:ख्या)?\s*\.?|No\s*\.?)?"
    r"\s*[\.\-:#]?\s*(?P<num>\d{1,3})\s*(?:[\.\):\-–—]+|(?=\s)|$)\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
WEAK_Q_RE = re.compile(r"^\s*(?P<num>\d{1,3})\s*(?:\.(?!\d)|\))\s*(?P<rest>.*)$")

# Option markers at the start of a line
_OPT_RE = re.compile(
    r"^\s*(?:"
    r"(?P<po>[\(\[])\s*(?P<pl>[A-Ea-e]|[कखगघङ]|[1-5])\s*[\)\]]"   # (A) [A] (a) (क) (1)
    r"|(?P<bl>[A-E]|[a-e]|[कखगघङ])\s*[\)\.:](?=\s|$)"              # A) A. A: a) क)
    r")\s*(?P<rest>.*)$"
)

_D = "\\-–—‒―−·•∙"  # dash-like separators seen in PDF extractions
_ANS_WORD = (r"(?:सही\s*उत्तर|सही\s*विकल्प|उत्तर|Correct\s*(?:Answer|Option|Ans\.?)|"
             r"Right\s*Answer|Answer|Ans\.?)")
_ANS_TOKEN = r"(?:[A-Ea-e]|[कखगघङ]|[1-5])"
ANSWER_LETTER_RE = re.compile(
    r"^\s*" + _ANS_WORD +
    r"\s*(?:[:" + _D + r"=\.]+\s*|\s+(?=[\(\[])|\s*(?=[\(\[]))(?:Option\s*|विकल्प\s*)?"
    r"[\(\[]?\s*(?P<tok>" + _ANS_TOKEN + r")\s*[\)\]]?"
    r"(?=[\s\.,।:;\)\-]|$)(?P<rest>.*)$",
    re.IGNORECASE,
)
ANSWER_TEXT_RE = re.compile(r"^\s*" + _ANS_WORD + r"\s*:\s*(?P<text>.+?)\s*$", re.IGNORECASE)
TRAILING_ANSWER_RE = re.compile(
    r"^(?P<body>.*\S)\s+" + _ANS_WORD +
    r"\s*[:" + _D + r"=]\s*[\(\[]?\s*(?P<tok>(?:[A-Da-d]|[कखगघ]))\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
EXPL_RE = re.compile(
    r"^\s*(?:व्याख्या|स्पष्टीकरण|विवरण|हल|Explanation|Expl|Exp|Solution|Sol)"
    r"(?:\s*[:\-–—\.]+\s*|\s*$)(?P<rest>.*)$",
    re.IGNORECASE,
)
# Old-style global answer line: "12. उत्तर: B" / "प्रश्न 12 - Answer: (C)"
NUMBERED_ANSWER_RE = re.compile(
    r"^\s*(?:प्रश्न|Q\.?|Question)?\s*(?P<num>\d{1,3})\s*[\.\):" + _D + r"]?\s*" + _ANS_WORD +
    r"\s*[:" + _D + r"=]\s*[\(\[]?\s*(?P<tok>" + _ANS_TOKEN + r")\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
KEY_HEADER_RE = re.compile(
    r"^\s*(?:answer\s*keys?|answers?\s*sheet|answers|ans\.?\s*key|key\s*answers?|"
    r"उत्तर\s*[-–]?\s*(?:कुंजी|कुञ्जी|कुंजिका|माला|सूची|तालिका|संकेत)|उत्तरमाला|उत्तरकुंजी|"
    r"सही\s*उत्तर\s*(?:सूची|तालिका))\s*[:\-–—]?\s*$",
    re.IGNORECASE,
)
# A (number, answer) pair inside answer-key text
KEY_PAIR_RE = re.compile(
    r"(?:प्रश्न|Q\.?)?\s*(?P<num>\d{1,3})\s*"
    r"(?:[\.\):" + _D + r"=]\s*[\(\[]?\s*(?P<a1>[A-Ea-e]|[कखगघ]|[1-5])\s*[\)\]]?"
    r"|\s*[\(\[]\s*(?P<a2>[A-Ea-e]|[कखगघ]|[1-5])\s*[\)\]]"
    r"|\s+(?P<a3>[A-Ea-e]|[कखगघ]))"
    r"(?![A-Za-z0-9\u0900-\u097F])"
)
_TOKEN_ONLY_RE = re.compile(r"^\s*(?:\d{1,3}\s*[\.\)]?|[\(\[]?\s*(?:[A-Ea-e]|[कखगघ])\s*[\)\]]?)\s*$")
PAGE_NOISE_RE = re.compile(
    r"^\s*(?:(?:Page|पृष्ठ)\s*[-:]?\s*\d+(?:\s*(?:of|/|का)\s*\d+)?|[-–—]\s*\d{1,4}\s*[-–—])\s*$",
    re.IGNORECASE,
)

_QTYPE_PATTERNS = [
    ("assertion_reason", re.compile(r"कथन\s*\(\s*A\s*\)|कारण\s*\(\s*R\s*\)|Assertion|Reason\s*\(\s*R\s*\)", re.I)),
    ("list_matching", re.compile(r"सूची\s*[-–]?\s*(?:I|1|एक|प्रथम)|List\s*[-–]?\s*(?:I|1)\b", re.I)),
    ("matching", re.compile(r"सुमेलित|सुमेल|मिलान|Match\s+the|Match\s+List|Match\s+the\s+following", re.I)),
    ("ordering", re.compile(r"क्रम|अवरोही|आरोही|व्यवस्थित|Arrange|ascending|descending|chronological|sequence|order", re.I)),
    ("statement", re.compile(r"कथन|कथनों|Statement|statements", re.I)),
]


def detect_qtype(question: str) -> str:
    for name, pat in _QTYPE_PATTERNS:
        if pat.search(question):
            return name
    return "mcq"


QTYPE_LABELS = {
    "mcq": "MCQ",
    "statement": "Statement based",
    "matching": "Matching",
    "list_matching": "List-I/List-II matching",
    "assertion_reason": "Assertion-Reason",
    "ordering": "Ordering/Ranking",
}


# ---------------------------------------------------------------- helpers
def token_to_index(tok: str) -> Optional[int]:
    tok = (tok or "").strip()
    if not tok:
        return None
    up = tok.upper()
    if up in "ABCDE" and len(up) == 1:
        return "ABCDE".index(up)
    if tok in _HINDI_LETTERS:
        return _HINDI_LETTERS.index(tok)
    if tok in "12345" and len(tok) == 1:
        return int(tok) - 1
    return None


def normalize_text(text: str) -> str:
    text = text.replace("\ufeff", "").replace("\u00a0", " ").replace("\u200b", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = unicodedata.normalize("NFC", text)
    lines = [re.sub(r"[ \u2000-\u200a\u3000]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(lines)


def _norm_cmp(s: str) -> str:
    s = unicodedata.normalize("NFC", s).lower()
    s = re.sub(r"[\s\.\,।;:'\"]+", " ", s)
    return s.strip()


def _family(tok: str) -> str:
    if tok in _HINDI_LETTERS:
        return "hindi"
    if tok.isdigit():
        return "num"
    return "latin"


def parse_marker(line: str) -> Optional[tuple[str, int, str]]:
    """Return (family, index, rest) if the line starts with an option marker."""
    m = _OPT_RE.match(line)
    if not m:
        return None
    tok = m.group("pl") or m.group("bl")
    if m.group("bl") is None and tok.isdigit() and not m.group("po"):
        return None
    idx = token_to_index(tok)
    if idx is None:
        return None
    fam = _family(tok)
    if fam == "num" and m.group("po") is None:
        return None
    return fam, idx, m.group("rest").strip()


def _marker_seq(start_tok: str, paren: bool, bracket: bool) -> list[str]:
    if start_tok in _HINDI_LETTERS:
        seq = list(_HINDI_LETTERS)
    elif start_tok.isdigit():
        seq = list("12345")
    elif start_tok.islower():
        seq = list("abcde")
    else:
        seq = list("ABCDE")
    return seq


def _split_inline(line: str) -> list[str]:
    """Split ``(A) x (B) y (C) z (D) w`` style lines into separate lines.

    Only splits on *consecutive* markers of the same style, so text such as
    ``(A) (A) और (R) दोनों सही हैं`` is left intact.
    """
    m = _OPT_RE.match(line)
    prefix = ""
    body = line
    if not m:
        # Options embedded after a lead-in, e.g. "कूट: (A) ... (B) ... (C) ... (D) ..."
        mm = re.search(r"(?<=\s)\(\s*A\s*\)|^\(\s*A\s*\)", line)
        if not mm:
            return [line]
        tail = line[mm.start():]
        if not all(re.search(r"\(\s*%s\s*\)" % L, tail) for L in "BCD"):
            return [line]
        prefix, body = line[:mm.start()].rstrip(), tail
        m = _OPT_RE.match(body)
        if not m:
            return [line]
    tok = m.group("pl") or m.group("bl")
    paren = m.group("po") is not None
    seq = _marker_seq(tok, paren, False)
    if tok not in seq:
        return [line]
    k = seq.index(tok)
    parts = []
    pos = 0
    search_from = m.end("pl") + 1 if paren else m.end("bl") + 1
    while k + 1 < len(seq):
        nxt = re.escape(seq[k + 1])
        if paren:
            pat = re.compile(r"(?<=\s)[\(\[]\s*" + nxt + r"\s*[\)\]]")
        else:
            pat = re.compile(r"(?<=\s)" + nxt + r"[\)\.](?=\s)")
        found = pat.search(body, search_from)
        if not found:
            break
        parts.append(body[pos:found.start()].strip())
        pos = found.start()
        search_from = found.end()
        k += 1
    parts.append(body[pos:].strip())
    if len(parts) == 1:
        return [line]
    out = [prefix] if prefix else []
    out.extend(p for p in parts if p)
    return out


def _expand_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        if not ln.strip():
            continue
        m = TRAILING_ANSWER_RE.match(ln)
        if m and not ANSWER_LETTER_RE.match(ln):
            out.extend(_split_inline(m.group("body")))
            out.append(f"उत्तर: {m.group('tok')}")
            continue
        out.extend(_split_inline(ln))
    return out


@dataclass
class _Run:
    family: str
    start: int
    marker_lines: list[int]
    texts: list[list[str]]
    tail_lines: list[int] = field(default_factory=list)  # continuation lines after last marker

    @property
    def size(self) -> int:
        return len(self.texts)


def find_runs(lines: list[str]) -> list[_Run]:
    """Group option markers into consecutive A→B→C→D(→E) runs."""
    runs: list[_Run] = []
    cur: Optional[_Run] = None
    for i, line in enumerate(lines):
        mk = parse_marker(line)
        if mk:
            fam, idx, rest = mk
            if idx == 0:
                if cur:
                    runs.append(cur)
                cur = _Run(fam, i, [i], [[rest] if rest else []])
                continue
            if cur and fam == cur.family and idx == cur.size:
                cur.marker_lines.append(i)
                cur.texts.append([rest] if rest else [])
                cur.tail_lines = []
                continue
            if cur:
                runs.append(cur)
                cur = None
            continue
        if cur:
            cur.texts[-1].append(line)
            cur.tail_lines.append(i)
    if cur:
        runs.append(cur)
    return runs


def complete_runs(lines: list[str]) -> list[_Run]:
    return [r for r in find_runs(lines) if r.size >= 4]


_UNATTEMPTED_RE = re.compile(r"अनुत्तरित|unattempted|not\s+attempted|question\s+not\s+attempted", re.I)


def split_question_and_options(text: str) -> Optional[tuple[str, list[str]]]:
    """For manual input: if ``text`` ends with a complete A-D run, split it.

    Returns (question_text, [4 options]) or None.
    """
    lines = _expand_lines(normalize_text(text).split("\n"))
    runs = complete_runs(lines)
    if not runs:
        return None
    run = runs[-1]
    if run.size != 4:
        return None
    opts = [" ".join(t).strip() for t in run.texts]
    if any(not o for o in opts):
        return None
    question = "\n".join(lines[:run.start]).strip()
    if not question:
        return None
    return question, opts


def parse_options_block(text: str) -> Optional[list[str]]:
    """Parse text that consists of exactly one complete A-D option run."""
    lines = _expand_lines(normalize_text(text).split("\n"))
    runs = complete_runs(lines)
    if len(runs) != 1 or runs[0].size != 4 or runs[0].start != 0:
        return None
    opts = [" ".join(t).strip() for t in runs[0].texts]
    return opts if all(opts) else None


# ------------------------------------------------------------ answer keys
def _is_key_like(line: str) -> bool:
    if not line.strip():
        return False
    pairs = list(KEY_PAIR_RE.finditer(line))
    if not pairs:
        return False
    rest = KEY_PAIR_RE.sub("", line)
    return re.fullmatch(r"[\s,;|/\.\-–—]*", rest) is not None


def _key_pairs(text: str) -> list[tuple[int, str]]:
    out = []
    for m in KEY_PAIR_RE.finditer(text):
        tok = m.group("a1") or m.group("a2") or m.group("a3")
        out.append((int(m.group("num")), tok))
    return out


def extract_answer_key(lines: list[str]) -> tuple[list[str], dict[int, str], dict[int, str], list[str]]:
    """Remove answer-key sections from ``lines``.

    Returns (remaining_lines, key{num: token}, key_explanations{num: text}, warnings)
    """
    key: dict[int, str] = {}
    expl: dict[int, str] = {}
    warnings: list[str] = []
    conflicts: set[int] = set()

    def put(num: int, tok: str):
        if num in key and token_to_index(key[num]) != token_to_index(tok):
            conflicts.add(num)
        key[num] = tok

    keep = [True] * len(lines)

    # 1) explicit header sections
    i = 0
    while i < len(lines):
        if KEY_HEADER_RE.match(lines[i]):
            keep[i] = False
            j = i + 1
            last_num = None
            token_buf: list[str] = []

            def flush():
                if token_buf:
                    for n, t in _key_pairs("\n".join(token_buf)):
                        put(n, t)
                    token_buf.clear()
            while j < len(lines):
                ln = lines[j]
                if _TOKEN_ONLY_RE.match(ln):
                    token_buf.append(ln)
                    keep[j] = False
                    j += 1
                    continue
                flush()
                if STRONG_Q_RE.match(ln) and not _is_key_like(ln) and not NUMBERED_ANSWER_RE.match(ln):
                    break
                keep[j] = False
                na = NUMBERED_ANSWER_RE.match(ln)
                if na:
                    put(int(na.group("num")), na.group("tok"))
                    last_num = int(na.group("num"))
                elif _is_key_like(ln):
                    for n, t in _key_pairs(ln):
                        put(n, t)
                        last_num = n
                else:
                    m = re.match(
                        r"^\s*(?:प्रश्न|Q\.?)?\s*(\d{1,3})\s*[\.\):\-–—=]?\s*[\(\[]\s*"
                        r"([A-Ea-e]|[कखगघ])\s*[\)\]]\s*(.*)$", ln) or re.match(
                        r"^\s*(?:प्रश्न|Q\.?)?\s*(\d{1,3})\s*[\.\):\-–—=]\s*([A-Da-d])[\.\)]?\s+(.*)$", ln)
                    if m:
                        n = int(m.group(1))
                        put(n, m.group(2))
                        if m.group(3).strip():
                            expl[n] = m.group(3).strip()
                        last_num = n
                    elif last_num is not None:
                        # continuation of an explanation inside the key section
                        e = EXPL_RE.match(ln)
                        piece = e.group("rest") if e else ln
                        expl[last_num] = (expl.get(last_num, "") + "\n" + piece).strip()
                j += 1
            flush()
            i = j
            continue
        i += 1

    # 2) old-style numbered answer lines anywhere ("12. उत्तर: B")
    for idx, ln in enumerate(lines):
        if keep[idx]:
            na = NUMBERED_ANSWER_RE.match(ln)
            if na:
                put(int(na.group("num")), na.group("tok"))
                keep[idx] = False

    # 3) lines with >=3 pairs anywhere, and a trailing block of key-like lines
    for idx, ln in enumerate(lines):
        if keep[idx] and _is_key_like(ln) and len(_key_pairs(ln)) >= 3:
            for n, t in _key_pairs(ln):
                put(n, t)
            keep[idx] = False
    tail = len(lines) - 1
    while tail >= 0 and (not keep[tail] or not lines[tail].strip()):
        tail -= 1
    tail_block = []
    while tail >= 0 and keep[tail] and _is_key_like(lines[tail]) and not parse_marker(lines[tail]):
        tail_block.append(tail)
        tail -= 1
    # a single trailing "5. A" line alone is ambiguous; require >=2 lines or >=2 pairs
    if tail_block and (len(tail_block) >= 2 or len(_key_pairs(lines[tail_block[0]])) >= 2):
        for idx in reversed(tail_block):
            for n, t in _key_pairs(lines[idx]):
                put(n, t)
            keep[idx] = False

    for n in sorted(conflicts):
        warnings.append(f"CONFLICT:{n}")
    remaining = [ln for k, ln in zip(keep, lines) if k]
    return remaining, key, expl, warnings


# -------------------------------------------------------- question splitting
@dataclass
class _Cand:
    line: int
    num: int
    strong: bool


def _question_candidates(lines: list[str]) -> list[_Cand]:
    cands = []
    for i, ln in enumerate(lines):
        m = STRONG_Q_RE.match(ln)
        if m:
            cands.append(_Cand(i, int(m.group("num")), True))
            continue
        m = WEAK_Q_RE.match(ln)
        if m:
            cands.append(_Cand(i, int(m.group("num")), False))
    return cands


def _is_candidate_line(line: str) -> bool:
    return bool(STRONG_Q_RE.match(line) or WEAK_Q_RE.match(line))


def _content_end(lines: list[str], start: int, end: int) -> int:
    """Index where answer/explanation lines begin inside lines[start:end]."""
    for i in range(start, end):
        if ANSWER_LETTER_RE.match(lines[i]) or EXPL_RE.match(lines[i]) or ANSWER_TEXT_RE.match(lines[i]):
            return i
    return end


def _backward_ok(lines: list[str], start: int, end: int) -> bool:
    """The block lines[start:end] must contain a completed option run whose
    last option is not followed by question-number-like lines (that would mean
    the run was List-I data, not the answer choices)."""
    stop = _content_end(lines, start + 1, end)
    seg = lines[start + 1:stop] if stop > start + 1 else []
    m = STRONG_Q_RE.match(lines[start]) or WEAK_Q_RE.match(lines[start])
    head = [m.group("rest")] if m and m.group("rest") else []
    seg = _expand_lines(head) + seg
    runs = complete_runs(seg)
    if not runs:
        return False
    last = runs[-1]
    for li in last.tail_lines:
        if li < len(seg) and _is_candidate_line(seg[li]):
            return False
    return True


def _answer_line_before(lines: list[str], start: int, end: int) -> bool:
    for i in range(end - 1, start, -1):
        ln = lines[i]
        if ANSWER_LETTER_RE.match(ln) or ANSWER_TEXT_RE.match(ln):
            return True
        if EXPL_RE.match(ln):
            continue
        if _is_candidate_line(ln):
            return False
    return False


def _monotone_ok(cands: list[_Cand], ci: int, prev_num: int, run_starts: list[int]) -> bool:
    """Between a candidate and the next option run, in-range question numbers
    must not go backwards (rejects numbered points inside explanations)."""
    c = cands[ci]
    k = bisect.bisect_right(run_starts, c.line)
    if k >= len(run_starts):
        return True
    nxt_run = run_starts[k]
    seq = [c.num] + [d.num for d in cands[ci + 1:]
                     if d.line < nxt_run and prev_num < d.num <= prev_num + _JUMP]
    return all(a <= b for a, b in zip(seq, seq[1:]))


def _greedy(lines, cands, start_ci, run_starts) -> list[_Cand]:
    accepted = [cands[start_ci]]
    for ci in range(start_ci + 1, len(cands)):
        c = cands[ci]
        prev = accepted[-1]
        if not (prev.num < c.num <= prev.num + _JUMP):
            continue
        if not _backward_ok(lines, prev.line, c.line):
            # A broken question (e.g. only A-C) is still closed by an explicit
            # answer line directly before the next consecutive number.
            if not (c.num == prev.num + 1 and _answer_line_before(lines, prev.line, c.line)):
                continue
        if not _monotone_ok(cands, ci, prev.num, run_starts):
            continue
        accepted.append(c)
    return accepted


def split_blocks(lines: list[str]) -> tuple[list[tuple[int, int, int]], list[str]]:
    """Return list of (number, start_line, end_line)."""
    cands = _question_candidates(lines)
    run_starts = [r.start for r in complete_runs(lines)]
    if not cands or not run_starts:
        return [], []
    # plausible first markers = candidates before the first option run
    first_group = [i for i, c in enumerate(cands) if c.line < run_starts[0]]
    if not first_group:
        limit = run_starts[1] if len(run_starts) > 1 else len(lines)
        first_group = [i for i, c in enumerate(cands) if c.line < limit][:1]
    if not first_group:
        return [], []
    best, best_score = None, None
    for gi in first_group[-12:]:
        acc = _greedy(lines, cands, gi, run_starts)
        gaps = sum(b.num - a.num - 1 for a, b in zip(acc, acc[1:]))
        score = len(acc) * 3 - gaps * 2
        # Preamble penalty: numbers go up and then return to the start number
        # before any options (e.g. instructions "1. … 2. …" then "1. question").
        start = acc[0]
        k = bisect.bisect_right(run_starts, start.line)
        limit = run_starts[k] if k < len(run_starts) else len(lines)
        seq = [c.num for c in cands if start.line < c.line < limit]
        went_up = False
        for n in seq:
            if n > start.num:
                went_up = True
            elif n == start.num and went_up:
                score -= 4
                break
        if best_score is None or score > best_score:
            best, best_score = acc, score
    blocks = []
    for k, c in enumerate(best):
        end = best[k + 1].line if k + 1 < len(best) else len(lines)
        blocks.append((c.num, c.line, end))
    return blocks, []


# --------------------------------------------------------- block parsing
def _parse_block(num: int, raw_lines: list[str], key: dict[int, str],
                 key_expl: dict[int, str], conflicts: set[int]) -> ParsedQuestion:
    first = raw_lines[0]
    m = STRONG_Q_RE.match(first) or WEAK_Q_RE.match(first)
    head_rest = m.group("rest").strip() if m else first
    lines = ([head_rest] if head_rest else []) + raw_lines[1:]
    lines = _expand_lines(lines)

    inline_tokens: list[str] = []
    answer_texts: list[str] = []
    content: list[str] = []
    expl_lines: list[str] = []
    in_expl = False
    for ln in lines:
        am = ANSWER_LETTER_RE.match(ln)
        if am:
            inline_tokens.append(am.group("tok"))
            rest = am.group("rest").strip(" .:-–—")
            e = EXPL_RE.match(rest) if rest else None
            if e:
                in_expl = True
                if e.group("rest").strip():
                    expl_lines.append(e.group("rest").strip())
            continue
        em = EXPL_RE.match(ln)
        if em:
            in_expl = True
            if em.group("rest").strip():
                expl_lines.append(em.group("rest").strip())
            continue
        if not in_expl:
            tm = ANSWER_TEXT_RE.match(ln)
            if tm and content and complete_runs(content):
                answer_texts.append(tm.group("text"))
                continue
        if in_expl:
            expl_lines.append(ln)
        else:
            content.append(ln)

    runs = find_runs(content)
    full = [r for r in runs if r.size >= 4]
    if not full:
        best = max(runs, key=lambda r: r.size, default=None)
        if best is None:
            raise ValueError("A/B/C/D answer options नहीं मिले")
        got = ", ".join("ABCDE"[i] for i in range(best.size))
        raise ValueError(f"4 options (A-D) पूरे नहीं मिले — केवल {got} मिले")
    run = full[-1]
    texts = [" ".join(t).strip() for t in run.texts]
    if run.size == 5:
        if _UNATTEMPTED_RE.search(texts[4]):
            texts = texts[:4]
        else:
            raise ValueError("5 options (A-E) मिले; bot केवल 4-option (A-D) प्रश्न support करता है")
    elif run.size > 5:
        raise ValueError(f"{run.size} options मिले; केवल 4 (A-D) चाहिए")
    for i, t in enumerate(texts):
        if not t:
            raise ValueError(f"option ({LETTERS[i]}) खाली है")

    question = "\n".join(content[:run.start]).strip()
    if not question:
        raise ValueError("question text खाली है")

    # --- answer resolution: never guess ---
    idx_inline = None
    distinct = {token_to_index(t) for t in inline_tokens}
    if len(distinct) > 1:
        raise ValueError("एक से अधिक अलग-अलग उत्तर लिखे हैं (" + ", ".join(inline_tokens) + ")")
    if distinct:
        idx_inline = distinct.pop()
    if idx_inline is None and answer_texts:
        matches = [i for i, o in enumerate(texts) if _norm_cmp(o) == _norm_cmp(answer_texts[0])]
        if len(matches) == 1:
            idx_inline = matches[0]
        else:
            raise ValueError(f"उत्तर '{answer_texts[0]}' किसी option से exactly match नहीं हुआ")
    idx_key = None
    if num in key:
        if num in conflicts:
            raise ValueError("answer key में इस प्रश्न के दो अलग-अलग उत्तर हैं")
        idx_key = token_to_index(key[num])
    if idx_inline is not None and idx_key is not None and idx_inline != idx_key:
        raise ValueError(
            f"inline उत्तर ({LETTERS[idx_inline] if idx_inline < 4 else '?'}) और answer key "
            f"({LETTERS[idx_key] if idx_key < 4 else '?'}) अलग हैं")
    correct = idx_inline if idx_inline is not None else idx_key
    if correct is None:
        raise ValueError("सही उत्तर नहीं मिला (उत्तर: (A) / Answer: B या answer key जोड़ें)")
    if not 0 <= correct < 4:
        raise ValueError("सही उत्तर A-D के बाहर है")
    explanation = "\n".join(expl_lines).strip() or key_expl.get(num, "").strip()
    return ParsedQuestion(
        number=num, question=question, options=texts, correct_index=correct,
        explanation=explanation, qtype=detect_qtype(question),
        answer_source="inline" if idx_inline is not None else "answer_key",
    )


def parse_source(text: str) -> ParseResult:
    """Parse MCQs from raw text. Never raises for per-question problems."""
    result = ParseResult()
    if not text or not text.strip():
        result.errors.append(ParseError(None, "Text खाली है"))
        return result
    text = normalize_text(text)
    lines = [ln for ln in text.split("\n") if ln.strip() and not PAGE_NOISE_RE.match(ln)]
    lines, key, key_expl, kwarn = extract_answer_key(lines)
    lines = _expand_lines(lines)
    conflicts = {int(w.split(":")[1]) for w in kwarn if w.startswith("CONFLICT:")}
    blocks, _ = split_blocks(lines)
    if not blocks:
        result.errors.append(ParseError(
            None,
            "कोई प्रश्न नहीं मिला। प्रश्न 'प्रश्न 1.', '1.', '1)', 'Q1', 'Q.1' से शुरू होने चाहिए "
            "और हर प्रश्न में (A)-(D) options होने चाहिए। Scanned PDF या Kruti Dev जैसे "
            "legacy font वाली PDF से text सही नहीं निकलता।"))
        return result

    numbers = [b[0] for b in blocks]
    result.detected_numbers = numbers
    seen: set[int] = set()
    for k, (num, start, end) in enumerate(blocks):
        if num in seen:
            result.errors.append(ParseError(num, "यह प्रश्न संख्या दोबारा आई है"))
            continue
        seen.add(num)
        try:
            q = _parse_block(num, lines[start:end], key, key_expl, conflicts)
            result.questions.append(q)
        except ValueError as exc:
            result.errors.append(ParseError(num, str(exc)))
        # gap detection: a missing number means something could not be read
        if k + 1 < len(blocks):
            nxt = blocks[k + 1][0]
            for missing in range(num + 1, nxt):
                merged = any(
                    (m := (STRONG_Q_RE.match(ln) or WEAK_Q_RE.match(ln))) and int(m.group("num")) == missing
                    for ln in lines[start + 1:end])
                msg = ("प्रश्न नहीं पढ़ा जा सका (numbering gap)" +
                       (f" — यह शायद प्रश्न {num} के साथ जुड़ गया है; दोनों को जांचें" if merged else
                        " — options/format जांचें"))
                result.errors.append(ParseError(missing, msg))
                if merged and result.questions and result.questions[-1].number == num:
                    result.questions.pop()
                    result.errors.append(ParseError(num, f"प्रश्न {missing} इसमें merge हो गया लगता है"))

    unused = sorted(set(key) - seen)
    if unused and seen:
        for n in unused:
            result.errors.append(ParseError(
                n, "answer key में इसका उत्तर है, पर प्रश्न/options पढ़े नहीं जा सके — format जांचें"))
    first_num = blocks[0][0]
    if first_num > 1:
        before = [int(m.group("num")) for ln in lines[:blocks[0][1]]
                  if (m := (STRONG_Q_RE.match(ln) or WEAK_Q_RE.match(ln)))]
        if before:
            result.warnings.append(
                f"प्रश्न {first_num} से पहले कुछ numbered text था जिसे प्रश्न नहीं माना गया "
                f"(options नहीं मिले)। अगर वे प्रश्न थे तो format जांचें।")
    result.errors.sort(key=lambda e: (e.number is None, e.number or 0))
    if len(result.questions) > MAX_QUESTIONS:
        result.warnings.append(
            f"{len(result.questions)} प्रश्न मिले; एक quiz में अधिकतम {MAX_QUESTIONS} होते हैं।")
    return result


# ------------------------------------------------------------ file readers
def extract_pdf_text(source) -> str:
    """Extract selectable text from a PDF path or bytes."""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover
        raise SourceError("PyMuPDF install नहीं है") from exc
    try:
        if isinstance(source, (bytes, bytearray)):
            doc = fitz.open(stream=bytes(source), filetype="pdf")
        else:
            doc = fitz.open(str(source))
    except Exception as exc:
        raise SourceError(f"PDF खुल नहीं सकी (corrupt/invalid file): {exc}") from exc
    try:
        if doc.needs_pass:
            raise SourceError("PDF password-protected है। बिना password वाली PDF भेजें।")
        parts = [page.get_text("text") for page in doc]
    finally:
        doc.close()
    text = "\n".join(parts).strip()
    if not text:
        raise SourceError(
            "PDF में selectable text नहीं मिला। यह scanned/image PDF लगती है — "
            "पहले OCR करके text वाली PDF भेजें।")
    return text


def decode_text_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            txt = data.decode(enc)
            if enc == "utf-16" and not (data[:2] in (b"\xff\xfe", b"\xfe\xff")):
                continue
            return txt
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    raise SourceError("Text file UTF-8 encoding में नहीं है। UTF-8 में save करके भेजें।")


def parse_pdf_result(source) -> ParseResult:
    return parse_source(extract_pdf_text(source))


# --------------------------------------------------- backward compatibility
def parse_text(text: str) -> list[dict]:
    """Old API: return question dicts, raise ValueError listing all errors."""
    res = parse_source(text)
    if res.errors:
        raise ValueError("कुछ प्रश्न पूरी तरह parse नहीं हुए:\n" + "\n".join(map(str, res.errors)))
    if len(res.questions) > MAX_QUESTIONS:
        raise ValueError("एक quiz में अधिकतम 100 प्रश्न रखे जा सकते हैं।")
    return [q.to_dict() for q in res.questions]


def parse_pdf(path) -> list[dict]:
    return parse_text(extract_pdf_text(path))


def parse_txt(path) -> list[dict]:
    return parse_text(decode_text_bytes(Path(path).read_bytes()))
