"""PDF / plain-text MCQ parser.

Design goals
------------
* Never guess an answer. A question is valid only if its correct option comes
  from an explicit inline answer (``उत्तर: (A)``, ``Answer: B``...) or from an
  answer-key section (``1-A, 2-C`` / ``1. (B)`` / tables).
* Never silently skip or drop anything: every problem is reported with the
  source question number, every removed line (answer keys, headers, section
  headings, page numbers, preamble) is reported, and a character-level
  coverage check proves that each question's source text reached the output.
* Find the *real* answer choices even when the question body itself contains
  A-D data (List-I/List-II matching, assertion-reason, ordering data,
  statements): the choices are the LAST option run of a question block.
* 2–12 options (Telegram limit) with labels A–L / a–l / क–ठ / (1)–(12) /
  Ⓐ–Ⓛ / ①–⑫.  An (E) option is a normal option — never dropped.
* Question numbering may restart (``भाग-अ 1…50``, ``भाग-ब 1…50``); per-section
  answer keys are matched to the right section.
* Stored text is NOT Unicode-normalised (NFC is used only for comparisons), so
  conjuncts / nukta forms are kept byte-for-byte.  Only a provably broken
  Devanagari sequence (ि-matra before its consonant, as produced by some PDF
  extractors) is repaired, and every repair is counted and reported.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:  # keep the parser importable without Telegram installed
    from telegram.constants import PollLimit
    MAX_OPTIONS = int(PollLimit.MAX_OPTION_NUMBER)   # 12
    MIN_OPTIONS = int(PollLimit.MIN_OPTION_NUMBER)   # 2
except Exception:  # pragma: no cover
    MAX_OPTIONS, MIN_OPTIONS = 12, 2

from pdf_extract import SourceError  # re-exported  # noqa: F401

MAX_QUESTIONS = 100
LETTERS = "ABCDEFGHIJKL"[:MAX_OPTIONS]
_LOWER = LETTERS.lower()
_HINDI_LETTERS = "कखगघङचछजझञटठ"[:MAX_OPTIONS]
_CIRCLED_UP = "".join(chr(0x24B6 + i) for i in range(MAX_OPTIONS))    # Ⓐ…Ⓛ
_CIRCLED_LO = "".join(chr(0x24D0 + i) for i in range(MAX_OPTIONS))    # ⓐ…ⓛ
_CIRCLED_NUM = "".join(chr(0x2460 + i) for i in range(MAX_OPTIONS))   # ①…⑫
_NUMS = [str(i) for i in range(1, MAX_OPTIONS + 1)]
_JUMP = 3  # tolerated gap in question numbering (gaps are reported as errors)


# --------------------------------------------------------------------- data
@dataclass
class ParseError:
    number: Optional[int]
    message: str
    section: str = ""

    def __str__(self) -> str:
        if self.number is None:
            return self.message
        sec = f"{self.section} – " if self.section else ""
        return f"{sec}प्रश्न {self.number}: {self.message}"


@dataclass
class ParsedQuestion:
    number: int
    question: str
    options: list[str]
    correct_index: int
    explanation: str = ""
    qtype: str = "mcq"
    answer_source: str = ""
    section: int = 0
    section_label: str = ""

    @property
    def label(self) -> str:
        sec = f"{self.section_label} – " if self.section_label else ""
        return f"{sec}प्रश्न {self.number}"

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "question": self.question,
            "options": list(self.options),
            "correct_index": self.correct_index,
            "explanation": self.explanation,
            "qtype": self.qtype,
            "answer_source": self.answer_source,
            "section": self.section,
            "section_label": self.section_label,
        }


@dataclass
class ParseResult:
    questions: list[ParsedQuestion] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)      # must be confirmed by the user
    detected_numbers: list[int] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)  # (kind, text)
    sections: list[str] = field(default_factory=list)
    ocr_pages: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.questions) and not self.errors

    @property
    def needs_review(self) -> bool:
        return bool(self.review)

    def to_dict(self) -> dict:
        return {
            "questions": [q.to_dict() for q in self.questions],
            "errors": [{"number": e.number, "message": e.message, "section": e.section}
                       for e in self.errors],
            "warnings": list(self.warnings),
            "review": list(self.review),
        }


# ------------------------------------------------------------------ regexes
_H = _HINDI_LETTERS
_TOK = (r"(?:1[0-2]|[1-9]|[A-La-l]|[" + _H + r"]|[" + _CIRCLED_UP + _CIRCLED_LO + _CIRCLED_NUM + r"])")

STRONG_Q_RE = re.compile(
    r"^\s*(?:प्रश्न|प्र\s*\.|Question|Ques\.?|Que\.?|Q)\s*(?:सं(?:ख्या)?\s*\.?|No\s*\.?)?"
    r"\s*[\.\-:#]?\s*(?P<num>\d{1,3})\s*(?:[\.\):\-–—]+|(?=\s)|$)\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
WEAK_Q_RE = re.compile(r"^\s*(?P<num>\d{1,3})\s*(?:\.(?!\d)|\))\s*(?P<rest>.*)$")

# Option markers at the start of a line
_OPT_RE = re.compile(
    r"^\s*(?:"
    r"(?P<po>[\(\[])\s*(?P<pl>1[0-2]|[1-9]|[A-La-l]|[" + _H + r"])\s*[\)\]]"   # (A) [A] (a) (क) (1)
    r"|(?P<bl>[A-L]|[a-l]|[" + _H + r"])\s*[\)\.:](?=\s|$)"                   # A) A. A: a) क)
    r"|(?P<cl>[" + _CIRCLED_UP + _CIRCLED_LO + _CIRCLED_NUM + r"])"             # Ⓐ ⓐ ①
    r")\s*(?P<rest>.*)$"
)

_D = "\\-–—‒―−·•∙"  # dash-like separators seen in PDF extractions
_ANS_WORD = (r"(?:सही\s*उत्तर|सही\s*विकल्प|उत्तर|Correct\s*(?:Answer|Option|Ans\.?)|"
             r"Right\s*Answer|Answer|Ans\.?)")
ANSWER_LETTER_RE = re.compile(
    r"^\s*" + _ANS_WORD +
    r"\s*(?:[:" + _D + r"=\.]+\s*|\s+(?=[\(\[])|\s*(?=[\(\[]))(?:Option\s*|विकल्प\s*)?"
    r"[\(\[]?\s*(?P<tok>" + _TOK + r")\s*[\)\]]?"
    r"(?=[\s\.,।:;\)\-&/]|$)(?P<rest>.*)$",
    re.IGNORECASE,
)
_MULTI_ANS_RE = re.compile(
    r"^\s*(?:,|और|तथा|एवं|व|and|&|/|\+)\s*(?:Option\s*|विकल्प\s*)?[\(\[]?\s*(?P<tok>" + _TOK +
    r")\s*[\)\]]?(?=[\s\.,।:;\)]|$)", re.IGNORECASE)
ANSWER_TEXT_RE = re.compile(r"^\s*" + _ANS_WORD + r"\s*:\s*(?P<text>.+?)\s*$", re.IGNORECASE)
TRAILING_ANSWER_RE = re.compile(
    r"^(?P<body>.*\S)\s+(?P<ans>" + _ANS_WORD +
    r"\s*[:" + _D + r"=]\s*[\(\[]?\s*(?P<tok>(?:[A-La-l]|[" + _H + r"]))\s*[\)\]]?)\s*$",
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
    r"\s*[:" + _D + r"=]\s*[\(\[]?\s*(?P<tok>" + _TOK + r")\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
KEY_HEADER_RE = re.compile(
    r"^\s*(?:answer\s*keys?|answers?\s*sheet|answers|ans\.?\s*key|key\s*answers?|"
    r"उत्तर\s*[-–]?\s*(?:कुंजी|कुञ्जी|कुंजिका|माला|सूची|तालिका|संकेत)|उत्तरमाला|उत्तरकुंजी|"
    r"सही\s*उत्तर\s*(?:सूची|तालिका))\s*(?:\(?\s*(?:भाग|खण्ड|खंड|Section|Part)\s*[-–—:.]?\s*\S{1,4}\s*\)?)?"
    r"\s*[:\-–—]?\s*$",
    re.IGNORECASE,
)
_KTOK = r"(?:[A-La-l]|[" + _H + r"]|1[0-2]|[1-9])"
# A (number, answer) pair inside answer-key text
KEY_PAIR_RE = re.compile(
    r"(?:प्रश्न|Q\.?)?\s*(?P<num>\d{1,3})\s*"
    r"(?:[\.\):" + _D + r"=]\s*[\(\[]?\s*(?P<a1>" + _KTOK + r")\s*[\)\]]?"
    r"|\s*[\(\[]\s*(?P<a2>" + _KTOK + r")\s*[\)\]]"
    r"|\s+(?P<a3>[A-La-l]|[" + _H + r"]))"
    r"(?![A-Za-z0-9\u0900-\u097F])"
)
_TOKEN_ONLY_RE = re.compile(r"^\s*(?:\d{1,3}\s*[\.\)]?|[\(\[]?\s*(?:[A-La-l]|[" + _H + r"])\s*[\)\]]?)\s*$")
PAGE_NOISE_RE = re.compile(
    r"^\s*(?:(?:Page|पृष्ठ)\s*[-:]?\s*\d+(?:\s*(?:of|/|का)\s*\d+)?|[-–—]\s*\d{1,4}\s*[-–—])\s*$",
    re.IGNORECASE,
)
SECTION_RE = re.compile(
    r"^\s*(?:भाग|खण्ड|खंड|अनुभाग|Section|Part)\s*[-–—:.]?\s*"
    r"(?P<id>[A-Ea-e]|[IVX]{1,4}|\d{1,2}|[अबसदकखगघ]|प्रथम|द्वितीय|तृतीय|चतुर्थ|एक|दो|तीन|"
    r"first|second|third|one|two|three)"
    r"(?![A-Za-z0-9\u0900-\u097f])\s*(?:[-–—:.(\[].{0,50})?$",
    re.IGNORECASE,
)
_PUA_RE = re.compile("[\ue000-\uf8ff\ufffd\x00]")

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
    if len(tok) == 1:
        for alpha in (LETTERS, _LOWER, _HINDI_LETTERS, _CIRCLED_UP, _CIRCLED_LO, _CIRCLED_NUM):
            if tok in alpha:
                return alpha.index(tok)
    if tok in _NUMS:
        return int(tok) - 1
    return None


def label(i: int) -> str:
    return LETTERS[i] if 0 <= i < len(LETTERS) else "?"


def _cc(*texts: str) -> Counter:
    """Counter of non-whitespace characters."""
    c: Counter = Counter()
    for t in texts:
        if t:
            c.update(ch for ch in t if not ch.isspace())
    return c


_ZERO_WIDTH_JUNK = "\ufeff\u200b\u2060"


def normalize_text(text: str) -> str:
    """Line-ending / invisible-junk cleanup only.

    No Unicode normalisation (conjuncts, nukta, ZWJ/ZWNJ are preserved
    byte-for-byte) and no collapsing of inner spaces.
    """
    for ch in _ZERO_WIDTH_JUNK:
        text = text.replace(ch, "")
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return "\n".join(ln.strip() for ln in text.split("\n"))


_CONS = "\u0915-\u0939\u0958-\u095f\u0978-\u097f"
_REPAIR_I_RE = re.compile(
    r"(?<![" + _CONS + r"\u093c\u094d\u200c\u200d])\u093f"
    r"(?P<cl>[" + _CONS + r"]\u093c?(?:\u094d[" + _CONS + r"]\u093c?)*)"
)


def repair_devanagari(text: str) -> tuple[str, int]:
    """Move a ि (U+093F) that precedes its consonant cluster to after it.

    A ि directly after a word boundary / vowel / space is never valid Unicode
    Hindi; PDF extractors that emit glyphs in visual order produce it
    (``ि कताब``→ we only repair the unambiguous ``िकताब`` form).
    """
    count = 0

    def fix(m):
        nonlocal count
        count += 1
        return m.group("cl") + "\u093f"
    return _REPAIR_I_RE.sub(fix, text), count


def _norm_cmp(s: str) -> str:
    s = unicodedata.normalize("NFC", s).lower()
    s = re.sub(r"[\s\.\,।;:'\"]+", " ", s)
    return s.strip()


def _family(tok: str) -> str:
    if tok in _HINDI_LETTERS:
        return "hindi"
    if tok in _CIRCLED_UP or tok in _CIRCLED_LO:
        return "circled"
    if tok in _CIRCLED_NUM:        # before isdigit(): "①".isdigit() is True
        return "cnum"
    if tok.isdigit():
        return "num"
    return "latin"


def parse_marker(line: str) -> Optional[tuple[str, int, str]]:
    """Return (family, index, rest) if the line starts with an option marker."""
    m = _OPT_RE.match(line)
    if not m:
        return None
    tok = m.group("pl") or m.group("bl") or m.group("cl")
    idx = token_to_index(tok)
    if idx is None or idx >= MAX_OPTIONS:
        return None
    fam = _family(tok)
    if fam == "num" and m.group("po") is None:
        return None
    return fam, idx, m.group("rest").strip()


def _marker_seq(start_tok: str) -> list[str]:
    if start_tok in _HINDI_LETTERS:
        return list(_HINDI_LETTERS)
    if start_tok.isdigit():
        return list(_NUMS)
    if start_tok in _CIRCLED_UP:
        return list(_CIRCLED_UP)
    if start_tok in _CIRCLED_LO:
        return list(_CIRCLED_LO)
    if start_tok in _CIRCLED_NUM:
        return list(_CIRCLED_NUM)
    if start_tok.islower():
        return list(_LOWER)
    return list(LETTERS)


def _split_inline(line: str) -> list[str]:
    """Split ``(A) x (B) y (C) z (D) w`` style lines into separate lines.

    Only splits on *consecutive* markers of the same style, so text such as
    ``(A) (A) और (R) दोनों सही हैं`` is left intact.  Character-preserving:
    only whitespace at the split points is dropped.
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
        if not all(re.search(r"\(\s*%s\s*\)" % L, tail) for L in "BC"):
            return [line]
        prefix, body = line[:mm.start()].rstrip(), tail
        m = _OPT_RE.match(body)
        if not m:
            return [line]
    tok = m.group("pl") or m.group("bl") or m.group("cl")
    kind = "po" if m.group("po") else ("bl" if m.group("bl") else "cl")
    seq = _marker_seq(tok)
    if tok not in seq:
        return [line]
    k = seq.index(tok)
    parts = []
    pos = 0
    search_from = m.end(kind if kind != "po" else "pl") + (1 if kind != "cl" else 0)
    while k + 1 < len(seq):
        nxt = re.escape(seq[k + 1])
        if kind == "po":
            pat = re.compile(r"(?<=\s)[\(\[]\s*" + nxt + r"\s*[\)\]]")
        elif kind == "bl":
            pat = re.compile(r"(?<=\s)" + nxt + r"[\)\.](?=\s)")
        else:
            pat = re.compile(r"(?<=\s)" + nxt)
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


def _expand_one(ln: str) -> list[str]:
    if not ln.strip():
        return []
    m = TRAILING_ANSWER_RE.match(ln)
    if m and not ANSWER_LETTER_RE.match(ln):
        # keep the ORIGINAL answer text (no synthesised line)
        return _split_inline(m.group("body")) + [m.group("ans").strip()]
    return _split_inline(ln)


def _expand_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        out.extend(_expand_one(ln))
    return out


def _expand_with_pos(lines: list[str], pos: list[int]) -> tuple[list[str], list[int]]:
    out, opos = [], []
    for ln, p in zip(lines, pos):
        parts = _expand_one(ln)
        out.extend(parts)
        opos.extend([p] * len(parts))
    return out, opos


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
    """Group option markers into consecutive A→B→C→… runs.

    A marker that does not continue the current run closes it — except markers
    with index ≥ 4 (E…L, 'I.' roman numerals …): they only count as a marker
    when they continue the run exactly (so ``I. कोलू`` after (D) is text, while
    E after D, or I after H, are options).
    """
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
            if idx < 4:
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
    return [r for r in find_runs(lines) if r.size >= MIN_OPTIONS]


def split_question_and_options(text: str) -> Optional[tuple[str, list[str]]]:
    """For manual input: if ``text`` ends with an option run (2–12), split it."""
    lines = _expand_lines(normalize_text(text).split("\n"))
    runs = complete_runs(lines)
    if not runs:
        return None
    run = runs[-1]
    if not MIN_OPTIONS <= run.size <= MAX_OPTIONS:
        return None
    opts = [" ".join(t).strip() for t in run.texts]
    if any(not o for o in opts):
        return None
    question = "\n".join(lines[:run.start]).strip()
    if not question:
        return None
    return question, opts


def parse_options_block(text: str) -> Optional[list[str]]:
    """Parse text that consists of exactly one option run (2–12 options)."""
    lines = _expand_lines(normalize_text(text).split("\n"))
    runs = complete_runs(lines)
    if len(runs) != 1 or runs[0].start != 0 or not MIN_OPTIONS <= runs[0].size <= MAX_OPTIONS:
        return None
    opts = [" ".join(t).strip() for t in runs[0].texts]
    return opts if all(opts) else None


# ------------------------------------------------------------ OCR labels
_OCR_LABEL_RE = re.compile(r"^(\s*[\(\[]\s*)([^\s()\[\]]{1,2}?)(\s*[\)\]])")
_OCR_CONFUSABLE = {1: "83ß6B", 2: "0©€G(cOC", 3: "0Oo9D"}


def fix_ocr_labels(lines: list[str]) -> tuple[list[str], int]:
    """OCR often reads (B) as (8) and (C)/(D) as (0).  Replace such a label
    ONLY when it is exactly the expected next letter of the run in progress
    (A was seen, then B, …).  Answer tokens are never touched."""
    out, fixes = [], 0
    expect, since = None, 0
    for ln in lines:
        mk = parse_marker(ln)
        if mk and mk[0] == "latin":
            expect, since = (mk[1] + 1, 0) if mk[1] + 1 < MAX_OPTIONS else (None, 0)
            out.append(ln)
            continue
        m = _OCR_LABEL_RE.match(ln)
        if (m and expect in _OCR_CONFUSABLE and m.group(2) != LETTERS[expect]
                and all(ch in _OCR_CONFUSABLE[expect] for ch in m.group(2)) and since <= 4):
            ln = m.group(1) + LETTERS[expect] + m.group(3) + ln[m.end():]
            fixes += 1
            expect += 1
            since = 0
            out.append(ln)
            continue
        since += 1
        out.append(ln)
    return out, fixes


# ------------------------------------------------------------ answer keys
@dataclass
class KeyEntry:
    num: int
    tok: str
    pos: int            # source position (original line index)
    group: int
    expl: str = ""


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


def _extract_key_entries(lines: list[str], pos: Optional[list[int]] = None):
    """Find and remove answer-key material.

    Returns (keep_mask, entries, warnings, removed_lines).
    """
    pos = pos if pos is not None else list(range(len(lines)))
    entries: list[KeyEntry] = []
    warnings: list[str] = []
    removed: list[str] = []
    keep = [True] * len(lines)
    gid = [0]

    def new_group() -> int:
        gid[0] += 1
        return gid[0]

    # 1) explicit header sections
    i = 0
    while i < len(lines):
        if KEY_HEADER_RE.match(lines[i]):
            g = new_group()
            keep[i] = False
            j = i + 1
            last: Optional[KeyEntry] = None
            token_buf: list[tuple[str, int]] = []

            def flush():
                nonlocal last
                if token_buf:
                    for n, t in _key_pairs("\n".join(s for s, _ in token_buf)):
                        last = KeyEntry(n, t, token_buf[0][1], g)
                        entries.append(last)
                    token_buf.clear()
            while j < len(lines):
                ln = lines[j]
                if _TOKEN_ONLY_RE.match(ln):
                    token_buf.append((ln, pos[j]))
                    keep[j] = False
                    j += 1
                    continue
                flush()
                if KEY_HEADER_RE.match(ln) or SECTION_RE.match(ln):
                    break
                if STRONG_Q_RE.match(ln) and not _is_key_like(ln) and not NUMBERED_ANSWER_RE.match(ln):
                    break
                if (_is_candidate_line(ln) and not _is_key_like(ln) and not NUMBERED_ANSWER_RE.match(ln)
                        and any(parse_marker(lines[t]) for t in range(j + 1, min(j + 8, len(lines))))):
                    break  # a question with options follows: the key section has ended
                keep[j] = False
                na = NUMBERED_ANSWER_RE.match(ln)
                if na:
                    last = KeyEntry(int(na.group("num")), na.group("tok"), pos[j], g)
                    entries.append(last)
                elif _is_key_like(ln):
                    for n, t in _key_pairs(ln):
                        last = KeyEntry(n, t, pos[j], g)
                        entries.append(last)
                else:
                    m = re.match(
                        r"^\s*(?:प्रश्न|Q\.?)?\s*(\d{1,3})\s*[\.\):\-–—=]?\s*[\(\[]\s*"
                        r"(" + _KTOK + r")\s*[\)\]]\s*(.*)$", ln) or re.match(
                        r"^\s*(?:प्रश्न|Q\.?)?\s*(\d{1,3})\s*[\.\):\-–—=]\s*([A-La-l])[\.\)]?\s+(.*)$", ln)
                    if m:
                        last = KeyEntry(int(m.group(1)), m.group(2), pos[j], g, m.group(3).strip())
                        entries.append(last)
                    elif last is not None:
                        # continuation of an explanation inside the key section
                        e = EXPL_RE.match(ln)
                        piece = e.group("rest") if e else ln
                        last.expl = (last.expl + "\n" + piece).strip()
                    else:
                        removed.append(ln)
                        warnings.append(f"Answer-key section की यह line समझ नहीं आई (उपयोग नहीं हुई): {ln}")
                j += 1
            flush()
            removed.extend(lines[i:j])
            i = j
            continue
        i += 1

    # 2) old-style numbered answer lines anywhere ("12. उत्तर: B")
    for idx, ln in enumerate(lines):
        if keep[idx]:
            na = NUMBERED_ANSWER_RE.match(ln)
            if na:
                entries.append(KeyEntry(int(na.group("num")), na.group("tok"), pos[idx], new_group()))
                keep[idx] = False
                removed.append(ln)

    # 3) lines with >=3 pairs anywhere, and a trailing block of key-like lines
    g = None
    for idx, ln in enumerate(lines):
        if keep[idx] and _is_key_like(ln) and len(_key_pairs(ln)) >= 3:
            if g is None:
                g = new_group()
            for n, t in _key_pairs(ln):
                entries.append(KeyEntry(n, t, pos[idx], g))
            keep[idx] = False
            removed.append(ln)
        elif keep[idx]:
            g = None
    tail = len(lines) - 1
    while tail >= 0 and (not keep[tail] or not lines[tail].strip()):
        tail -= 1
    tail_block = []
    while tail >= 0 and keep[tail] and _is_key_like(lines[tail]) and not parse_marker(lines[tail]):
        tail_block.append(tail)
        tail -= 1
    # a single trailing "5. A" line alone is ambiguous; require >=2 lines or >=2 pairs
    if tail_block and (len(tail_block) >= 2 or len(_key_pairs(lines[tail_block[0]])) >= 2):
        g = new_group()
        for idx in reversed(tail_block):
            for n, t in _key_pairs(lines[idx]):
                entries.append(KeyEntry(n, t, pos[idx], g))
            keep[idx] = False
            removed.append(lines[idx])
    return keep, entries, warnings, removed


def extract_answer_key(lines: list[str]) -> tuple[list[str], dict[int, str], dict[int, str], list[str]]:
    """Backward-compatible wrapper: (remaining_lines, key, key_expl, warnings)."""
    keep, entries, warnings, _ = _extract_key_entries(lines)
    key: dict[int, str] = {}
    expl: dict[int, str] = {}
    conflicts = set()
    for e in entries:
        if e.num in key and token_to_index(key[e.num]) != token_to_index(e.tok):
            conflicts.add(e.num)
        key[e.num] = e.tok
        if e.expl:
            expl[e.num] = e.expl
    warnings = warnings + [f"CONFLICT:{n}" for n in sorted(conflicts)]
    return [ln for k, ln in zip(keep, lines) if k], key, expl, warnings


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


def split_blocks(lines: list[str], evidence: Optional[list[int]] = None
                 ) -> tuple[list[tuple[int, int, int, int]], list[str]]:
    """Return ([(number, start_line, end_line, section)], notes).

    Numbering may restart (new section / page) when the restart is proven:
    the previous block is complete AND either the restarted numbering
    continues as a chain of real questions before the old numbering would
    have continued, or there is explicit evidence (an answer line, a section
    heading or an answer-key group) between the two blocks.
    """
    cands = _question_candidates(lines)
    run_starts = [r.start for r in complete_runs(lines)]
    if not cands or not run_starts:
        return [], []
    ev = sorted(evidence or [])
    bw_cache: dict[tuple[int, int], bool] = {}

    def bw(a: int, b: int) -> bool:
        key = (a, b)
        if key not in bw_cache:
            bw_cache[key] = _backward_ok(lines, a, b)
        return bw_cache[key]

    def has_evidence(a: int, b: int) -> bool:
        k = bisect.bisect_right(ev, a)
        if k < len(ev) and ev[k] <= b:
            return True
        return any(ANSWER_LETTER_RE.match(lines[i]) or ANSWER_TEXT_RE.match(lines[i])
                   for i in range(a + 1, b))

    def preamble_penalty(ci: int) -> int:
        start = cands[ci]
        k = bisect.bisect_right(run_starts, start.line)
        limit = run_starts[k] if k < len(run_starts) else len(lines)
        went_up = False
        for c in cands:
            if start.line < c.line < limit:
                if c.num > start.num:
                    went_up = True
                elif c.num == start.num and went_up:
                    return 4
        return 0

    def score(ch: list[tuple[int, bool]]) -> int:
        gaps = sum(cands[b].num - cands[a].num - 1
                   for (a, _), (b, brk) in zip(ch, ch[1:]) if not brk)
        return len(ch) * 3 - gaps * 2 - len([1 for _, brk in ch if brk])

    def dup_later(j: int) -> bool:
        """Same number again before the next options, with no '1.' in between →
        this candidate is a numbered point (e.g. in an explanation), the later
        one is the real question."""
        c = cands[j]
        k = bisect.bisect_right(run_starts, c.line)
        nr = run_starts[k] if k < len(run_starts) else len(lines)
        for d in cands[j + 1:]:
            if d.line >= nr:
                break
            if d.num == 1:
                return False
            if d.num == c.num:
                return True
        return False

    def numbered_point(z: int) -> bool:
        """True if candidate z is a numbered point (explanation/instruction
        list), not a question: before its options appear, the NEXT number
        follows it directly (a statement sub-list that restarts at 1 inside
        the question is allowed)."""
        c = cands[z]
        k = bisect.bisect_right(run_starts, c.line)
        nr = run_starts[k] if k < len(run_starts) else len(lines)
        sub = None
        for d in cands[z + 1:]:
            if d.line >= nr:
                break
            if d.num == 1 and sub is None:
                sub = 1
                continue
            if sub is not None and d.num == sub + 1:
                sub += 1
                continue
            if d.num == c.num + 1:
                return True
        return False

    memo: dict[int, list[tuple[int, bool]]] = {}

    def chain(ci: int) -> list[tuple[int, bool]]:
        if ci in memo:
            return memo[ci]
        memo[ci] = [(ci, False)]  # recursion guard
        acc: list[tuple[int, bool]] = [(ci, False)]
        j = ci + 1
        while j < len(cands):
            c, prev = cands[j], cands[acc[-1][0]]
            if prev.num < c.num <= prev.num + _JUMP:
                ok = bw(prev.line, c.line) or (
                    c.num == prev.num + 1 and _answer_line_before(lines, prev.line, c.line))
                if ok and _monotone_ok(cands, j, prev.num, run_starts) and not dup_later(j):
                    acc.append((j, False))
                    j += 1
                    continue
            elif c.num == 1 and bw(prev.line, c.line):
                rest = try_restart(acc[-1][0], j)
                if rest is not None:
                    acc.extend([(rest[0][0], True)] + rest[1:])
                    break
            j += 1
        memo[ci] = acc
        return acc

    def try_restart(pi: int, j: int) -> Optional[list[tuple[int, bool]]]:
        prev = cands[pi]
        x = None
        for k in range(j + 1, len(cands)):
            d = cands[k]
            if prev.num < d.num <= prev.num + _JUMP and bw(prev.line, d.line):
                x = d
                break
        xl = x.line if x else len(lines)
        k = bisect.bisect_right(run_starts, cands[j].line)
        rs = run_starts[k] if k < len(run_starts) else len(lines)
        zone = [z for z in range(j, len(cands))
                if cands[z].line < min(rs, xl) and cands[z].num == 1]
        best, best_score = None, None
        for z in zone:
            zc = cands[z]
            if not bw(prev.line, zc.line) or numbered_point(z):
                continue
            ch = chain(z)
            chained = len(ch) >= 2 and not ch[1][1] and cands[ch[1][0]].line < xl
            proven = has_evidence(prev.line, zc.line) and bw(zc.line, xl)
            if not (chained or proven):
                continue
            s = score(ch) - preamble_penalty(z)
            if best_score is None or s > best_score:
                best, best_score = ch, s
        return best

    first_group = [i for i, c in enumerate(cands) if c.line < run_starts[0]]
    if not first_group:
        limit = run_starts[1] if len(run_starts) > 1 else len(lines)
        first_group = [i for i, c in enumerate(cands) if c.line < limit][:1]
    if not first_group:
        return [], []
    best, best_score = None, None
    for gi in first_group[-12:]:
        ch = chain(gi)
        s = score(ch) - preamble_penalty(gi)
        if best_score is None or s > best_score:
            best, best_score = ch, s
    blocks = []
    section = 0
    for k, (ci, brk) in enumerate(best):
        if brk:
            section += 1
        c = cands[ci]
        end = cands[best[k + 1][0]].line if k + 1 < len(best) else len(lines)
        blocks.append((c.num, c.line, end, section))
    return blocks, []


# --------------------------------------------------------- block parsing
class _BlockError(ValueError):
    pass


def _parse_block(num: int, raw_lines: list[str], key_tok: Optional[str], key_expl: str,
                 key_err: Optional[str], ocr: bool = False) -> ParsedQuestion:
    raw_counter = _cc(*raw_lines)
    consumed: Counter = Counter()
    first = raw_lines[0]
    m = STRONG_Q_RE.match(first) or WEAK_Q_RE.match(first)
    head_rest = m.group("rest").strip() if m else first
    consumed += _cc(first) - _cc(head_rest)
    lines = _expand_lines(([head_rest] if head_rest else []) + raw_lines[1:])

    inline_tokens: list[str] = []
    answer_rests: list[str] = []
    answer_texts: list[str] = []
    content: list[str] = []
    expl_lines: list[str] = []
    in_expl = False
    for ln in lines:
        am = ANSWER_LETTER_RE.match(ln)
        if am:
            mm = _MULTI_ANS_RE.match(am.group("rest"))
            if mm:
                raise _BlockError(
                    f"एक से अधिक सही उत्तर लिखे हैं ('{ln.strip()}') — Telegram quiz में केवल "
                    "एक सही उत्तर हो सकता है")
            inline_tokens.append(am.group("tok"))
            rest = am.group("rest").strip(" .:-–—")
            kept = ""
            e = EXPL_RE.match(rest) if rest else None
            if e:
                in_expl = True
                kept = e.group("rest").strip()
                if kept:
                    expl_lines.append(kept)
            elif rest:
                kept = rest
                answer_rests.append(rest)
            consumed += _cc(ln) - _cc(kept)
            in_expl = True   # anything after the answer line is explanation, never option text
            continue
        em = EXPL_RE.match(ln)
        if em:
            in_expl = True
            kept = em.group("rest").strip()
            if kept:
                expl_lines.append(kept)
            consumed += _cc(ln) - _cc(kept)
            continue
        if not in_expl:
            tm = ANSWER_TEXT_RE.match(ln)
            if tm and content and complete_runs(content):
                answer_texts.append(tm.group("text"))
                consumed += _cc(ln) - _cc(tm.group("text"))
                continue
        if in_expl:
            expl_lines.append(ln)
        else:
            content.append(ln)

    runs = find_runs(content)
    full = [r for r in runs if r.size >= MIN_OPTIONS]
    if not full:
        if runs:
            raise _BlockError("केवल एक option (A) मिला — कम से कम 2 options चाहिए")
        raise _BlockError("answer options ((A)/(B)/… या (1)/(2)/…) नहीं मिले")
    run = full[-1]
    if run.size > MAX_OPTIONS:
        raise _BlockError(f"{run.size} options मिले; Telegram अधिकतम {MAX_OPTIONS} options allow करता है")
    for li in run.tail_lines:
        t = content[li]
        if _is_candidate_line(t) or SECTION_RE.match(t) or KEY_HEADER_RE.match(t):
            raise _BlockError(
                f"अंतिम option ({label(run.size - 1)}) के बाद अनपेक्षित text है: '{t}' — "
                "यह option का हिस्सा है या नया प्रश्न, तय नहीं हो सका")
    for li in run.marker_lines:
        mk = parse_marker(content[li])
        consumed += _cc(content[li]) - _cc(mk[2] if mk else "")
    texts = [" ".join(t).strip() for t in run.texts]
    for i, t in enumerate(texts):
        if not t:
            raise _BlockError(f"option ({label(i)}) खाली है")

    question = "\n".join(content[:run.start]).strip()
    if not question:
        raise _BlockError("question text खाली है")
    bad = _PUA_RE.search(question + "".join(texts))
    if bad:
        raise _BlockError(
            f"text में अपठनीय character (U+{ord(bad.group()):04X}) है — PDF का font "
            "(Kruti Dev जैसा legacy font / टूटा glyph) सही text नहीं दे रहा")

    # --- answer resolution: never guess ---
    n_opt = len(texts)
    idx_inline = None
    distinct = {token_to_index(t) for t in inline_tokens}
    if len(distinct) > 1:
        raise _BlockError("एक से अधिक अलग-अलग उत्तर लिखे हैं (" + ", ".join(inline_tokens) + ")")
    if distinct:
        idx_inline = distinct.pop()
    if idx_inline is None and answer_texts:
        matches = [i for i, o in enumerate(texts) if _norm_cmp(o) == _norm_cmp(answer_texts[0])]
        if len(matches) == 1:
            idx_inline = matches[0]
            consumed += _cc(answer_texts[0])
        else:
            raise _BlockError(f"उत्तर '{answer_texts[0]}' किसी option से exactly match नहीं हुआ")
    elif answer_texts:
        # "उत्तर: …" text lines after an explicit letter: keep them as explanation
        expl_lines = answer_texts + expl_lines
    if key_err and idx_inline is None:
        raise _BlockError(key_err)
    if ocr and run.family in ("latin", "circled"):
        for t in inline_tokens + ([key_tok] if key_tok else []):
            if _family(t) not in ("latin", "circled"):
                raise _BlockError(
                    f"options अक्षरों ((A), (B)…) में हैं पर OCR ने उत्तर '{t}' पढ़ा — OCR की गलती हो "
                    "सकती है; अनुमान नहीं लगाया गया, उत्तर जांच कर सुधारें")
    idx_key = token_to_index(key_tok) if key_tok else None
    if idx_inline is not None and idx_key is not None and idx_inline != idx_key:
        raise _BlockError(
            f"inline उत्तर ({label(idx_inline)}) और answer key ({label(idx_key)}) अलग हैं")
    correct = idx_inline if idx_inline is not None else idx_key
    if correct is None:
        raise _BlockError("सही उत्तर नहीं मिला (उत्तर: (A) / Answer: B या answer key जोड़ें)")
    if not 0 <= correct < n_opt:
        tok_txt = inline_tokens[0] if idx_inline is not None and inline_tokens else (key_tok or "")
        if ocr and tok_txt in {"8", "3", "0", "6"}:
            raise _BlockError(f"उत्तर में OCR ने '{tok_txt}' पढ़ा — यह शायद कोई अक्षर (जैसे B/D) है; "
                              "अनुमान नहीं लगाया गया, कृपया उत्तर जांच कर सुधारें")
        raise _BlockError(f"सही उत्तर ({label(correct)}) है, पर केवल {n_opt} options "
                          f"({label(0)}-{label(n_opt - 1)}) मिले")
    for rest in answer_rests:
        same = [i for i, o in enumerate(texts) if _norm_cmp(o) == _norm_cmp(rest)]
        if same == [correct]:
            consumed += _cc(rest)          # the answer line just repeats the option text
        elif same:
            raise _BlockError(f"उत्तर ({label(correct)}) लिखा है पर साथ का text option "
                              f"({label(same[0])}) का है: '{rest}'")
        else:
            expl_lines.insert(0, rest)
    inline_expl = "\n".join(expl_lines).strip()
    stray = complete_runs(_expand_lines(expl_lines))
    if stray:
        raise _BlockError(
            "उत्तर के बाद फिर से options ((A), (B)…) मिले — शायद अगला प्रश्न (जिसका क्रमांक नहीं "
            "पढ़ा जा सका) इसमें जुड़ गया है; दोनों प्रश्न जांचें")

    # --- coverage safety net: every source character is accounted for ---
    produced = _cc(question, *texts, inline_expl)
    if produced + consumed != raw_counter:
        missing = raw_counter - (produced + consumed)
        extra = (produced + consumed) - raw_counter
        detail = "".join(sorted(missing.elements()))[:40] or "".join(sorted(extra.elements()))[:40]
        raise _BlockError(f"आंतरिक जांच fail: source का कुछ text output में नहीं पहुँचा ('{detail}') "
                          "— कृपया यह प्रश्न जांचें")
    explanation = inline_expl or (key_expl or "").strip()
    return ParsedQuestion(
        number=num, question=question, options=texts, correct_index=correct,
        explanation=explanation, qtype=detect_qtype(question),
        answer_source="inline" if idx_inline is not None else "answer_key",
    )


def _has_inline_answer(lines: list[str]) -> bool:
    return any(ANSWER_LETTER_RE.match(ln) or TRAILING_ANSWER_RE.match(ln) for ln in lines)


def _resolve_keys(entries: list[KeyEntry], blocks, block_pos_start: list[int],
                  block_inline: list[bool]) -> tuple[dict, dict, dict, set]:
    """Map key entries to blocks.

    Returns (tok_by_block, expl_by_block, err_by_block, used_entry_ids).
    """
    tok: dict[int, str] = {}
    expl: dict[int, str] = {}
    err: dict[int, str] = {}
    used: set[int] = set()
    sections = sorted({b[3] for b in blocks})
    by_sec_num: dict[tuple[int, int], int] = {}
    for bi, b in enumerate(blocks):
        by_sec_num.setdefault((b[3], b[0]), bi)
    sec_start = {}
    for bi, b in enumerate(blocks):
        sec_start.setdefault(b[3], block_pos_start[bi])
    starts = [sec_start[s] for s in sections]

    def owner(pos: int) -> int:
        k = bisect.bisect_right(starts, pos) - 1
        return sections[max(k, 0)]

    def assign(bi: int, e: KeyEntry, eid: int):
        used.add(eid)
        if bi in tok and token_to_index(tok[bi]) != token_to_index(e.tok):
            err[bi] = "answer key में इस प्रश्न के दो अलग-अलग उत्तर हैं"
        tok[bi] = e.tok
        if e.expl:
            expl[bi] = e.expl

    groups: dict[int, list[int]] = {}
    for eid, e in enumerate(entries):
        groups.setdefault(e.group, []).append(eid)
    if len(sections) == 1:
        for eid, e in enumerate(entries):
            bi = by_sec_num.get((sections[0], e.num))
            if bi is not None:
                assign(bi, e, eid)
        return tok, expl, err, used

    positional, ranked = [], []
    for g, ids in groups.items():
        nums = [entries[i].num for i in ids]
        (ranked if len(nums) != len(set(nums)) else positional).append(g)
    # a key that is not duplicated but sits after ALL sections and covers
    # numbers present in several sections is ambiguous → rank mode too
    for g in list(positional):
        ids = groups[g]
        sec = owner(entries[ids[0]].pos)
        missing_here = [entries[i].num for i in ids if (sec, entries[i].num) not in by_sec_num]
        if missing_here:
            positional.remove(g)
            ranked.append(g)
    for g in positional:
        for eid in groups[g]:
            e = entries[eid]
            bi = by_sec_num.get((owner(e.pos), e.num))
            if bi is not None:
                assign(bi, e, eid)
    occ: dict[int, list[int]] = {}
    for g in sorted(ranked, key=lambda g: entries[groups[g][0]].pos):
        for eid in groups[g]:
            occ.setdefault(entries[eid].num, []).append(eid)
    for n, eids in occ.items():
        with_n = [by_sec_num[(s, n)] for s in sections if (s, n) in by_sec_num]
        needing = [bi for bi in with_n if not block_inline[bi] and bi not in tok]
        if len(eids) == len(with_n):
            for bi, eid in zip(with_n, eids):
                assign(bi, entries[eid], eid)
        elif len(eids) == len(needing):
            for bi, eid in zip(needing, eids):
                assign(bi, entries[eid], eid)
        elif len(with_n) == 1:
            for eid in eids:
                assign(with_n[0], entries[eid], eid)
        else:
            for bi in with_n:
                if bi not in tok:
                    err[bi] = (f"answer key में प्रश्न {n} के {len(eids)} उत्तर हैं पर {len(with_n)} "
                               f"sections में प्रश्न {n} है — कौन सा उत्तर किस section का है, तय नहीं हो सका")
            used.update(eids)
    return tok, expl, err, used


def parse_source(text: str, *, ocr: bool = False) -> ParseResult:
    """Parse MCQs from raw text. Never raises for per-question problems."""
    result = ParseResult()
    if not text or not text.strip():
        result.errors.append(ParseError(None, "Text खाली है"))
        return result
    text = normalize_text(text)
    text, repairs = repair_devanagari(text)
    if repairs:
        result.warnings.append(f"हिंदी मात्रा क्रम (ि) {repairs} जगह ठीक किया गया (PDF extraction दोष)।")
        result.review.append(f"{repairs} जगह हिंदी 'ि' मात्रा का क्रम ठीक किया गया — preview जांचें")
    all_lines = text.split("\n")
    src = [(i, ln) for i, ln in enumerate(all_lines) if ln.strip()]
    if ocr:
        fixed, nfix = fix_ocr_labels([ln for _, ln in src])
        src = [(p, ln) for (p, _), ln in zip(src, fixed)]
        if nfix:
            result.warnings.append(f"OCR option-labels {nfix} जगह sequence से ठीक किए गए (जैसे (8)→(B))।")
            result.review.append(f"OCR से {nfix} option-labels ठीक किए गए — preview जांचें")
    source_counter = _cc(*(ln for _, ln in src))
    accounted: Counter = Counter()

    # page numbers
    kept: list[tuple[int, str]] = []
    noise = 0
    for p, ln in src:
        if PAGE_NOISE_RE.match(ln):
            result.removed.append(("page-number", ln))
            accounted += _cc(ln)
            noise += 1
        else:
            kept.append((p, ln))
    if noise:
        result.warnings.append(f"{noise} page-number lines हटाई गईं।")

    # answer-key material (a section heading ends a key section, so this runs first)
    lines = [ln for _, ln in kept]
    pos = [p for p, _ in kept]
    keep, entries, kwarn, key_removed = _extract_key_entries(lines, pos)
    result.warnings.extend(kwarn)
    for ln in key_removed:
        result.removed.append(("answer-key", ln))
        accounted += _cc(ln)

    # section headings ("भाग-अ", "Section B", "PART - II") are separators
    headings: list[tuple[int, str]] = []
    rest_lines, rest_pos = [], []
    for k, ln, p in zip(keep, lines, pos):
        if not k:
            continue
        if len(ln) <= 70 and SECTION_RE.match(ln) and not _is_candidate_line(ln):
            headings.append((p, ln))
            result.removed.append(("section-heading", ln))
            accounted += _cc(ln)
        else:
            rest_lines.append(ln)
            rest_pos.append(p)
    lines, pos = rest_lines, rest_pos
    lines, pos = _expand_with_pos(lines, pos)

    # evidence points (expanded coordinates) for numbering restarts
    ev_src = [p for p, _ in headings] + sorted({e.pos for e in entries})
    evidence = sorted({bisect.bisect_left(pos, p) for p in ev_src})
    blocks, _ = split_blocks(lines, evidence)
    if not blocks:
        result.errors.append(ParseError(
            None,
            "कोई प्रश्न नहीं मिला। प्रश्न 'प्रश्न 1.', '1.', '1)', 'Q1', 'Q.1' से शुरू होने चाहिए "
            "और हर प्रश्न में (A)/(B)/… options होने चाहिए। Scanned PDF या Kruti Dev जैसे "
            "legacy font वाली PDF के लिए OCR चाहिए।"))
        return result

    # section labels
    sec_label: dict[int, str] = {}
    n_sections = max(b[3] for b in blocks) + 1
    for s in range(n_sections):
        first_bi = next(i for i, b in enumerate(blocks) if b[3] == s)
        prev_start = pos[blocks[first_bi - 1][1]] if first_bi else -1
        here = pos[blocks[first_bi][1]]
        labels = [t for p, t in headings if prev_start < p <= here]
        sec_label[s] = labels[-1] if labels else (f"भाग {s + 1}" if n_sections > 1 else "")
    result.sections = [sec_label[s] for s in range(n_sections)] if n_sections > 1 else []
    if n_sections > 1:
        result.warnings.append(
            f"प्रश्न-क्रमांक {n_sections - 1} बार दोबारा 1 से शुरू हुआ — {n_sections} भाग पहचाने गए: "
            + ", ".join(result.sections))

    # a section heading inside a block ends it; the rest (section instructions)
    # is reported as preamble of the next section
    heading_pos = sorted(p for p, _ in headings)
    section_pre: list[str] = []
    cut_blocks = []
    for num, start, end, sec in blocks:
        hp = next((h for h in heading_pos if pos[start] < h < (pos[end] if end < len(pos) else 10**9)), None)
        if hp is not None:
            cut = bisect.bisect_left(pos, hp, start, end)
            section_pre.extend(lines[cut:end])
            end = cut
        cut_blocks.append((num, start, end, sec))
    blocks = cut_blocks
    for ln in section_pre:
        accounted += _cc(ln)
        result.removed.append(("preamble", ln))
    if section_pre:
        result.warnings.append(
            f"भाग/section heading के बाद की {len(section_pre)} line(s) प्रश्न नहीं मानी गईं: "
            + " | ".join(section_pre[:5]))

    block_pos = [pos[b[1]] for b in blocks]
    block_inline = [_has_inline_answer(lines[b[1]:b[2]]) for b in blocks]
    ktok, kexpl, kerr, used = _resolve_keys(entries, blocks, block_pos, block_inline)

    result.detected_numbers = [b[0] for b in blocks]
    seen: set[tuple[int, int]] = set()
    for k, (num, start, end, sec) in enumerate(blocks):
        slabel = sec_label.get(sec, "")
        accounted += _cc(*lines[start:end])
        if (sec, num) in seen:
            result.errors.append(ParseError(num, "यह प्रश्न संख्या दोबारा आई है", slabel))
            continue
        seen.add((sec, num))
        try:
            q = _parse_block(num, lines[start:end], ktok.get(k), kexpl.get(k, ""), kerr.get(k), ocr)
            q.section, q.section_label = sec, slabel
            result.questions.append(q)
        except ValueError as exc:
            result.errors.append(ParseError(num, str(exc), slabel))
        # gap detection (within a section): a missing number means something could not be read
        if k + 1 < len(blocks) and blocks[k + 1][3] == sec:
            nxt = blocks[k + 1][0]
            for missing in range(num + 1, nxt):
                merged = any(
                    (m := (STRONG_Q_RE.match(ln) or WEAK_Q_RE.match(ln))) and int(m.group("num")) == missing
                    for ln in lines[start + 1:end])
                msg = ("प्रश्न नहीं पढ़ा जा सका (numbering gap)" +
                       (f" — यह शायद प्रश्न {num} के साथ जुड़ गया है; दोनों को जांचें" if merged else
                        " — options/format जांचें"))
                result.errors.append(ParseError(missing, msg, slabel))
                if merged and result.questions and result.questions[-1].number == num \
                        and result.questions[-1].section == sec:
                    result.questions.pop()
                    result.errors.append(ParseError(num, f"प्रश्न {missing} इसमें merge हो गया लगता है", slabel))

    unused = sorted({entries[i].num for i in range(len(entries)) if i not in used})
    if unused and seen:
        for n in unused:
            result.errors.append(ParseError(
                n, "answer key में इसका उत्तर है, पर प्रश्न/options पढ़े नहीं जा सके — format जांचें"))

    # preamble (before the first question) — never dropped silently
    pre = lines[:blocks[0][1]]
    accounted += _cc(*pre)
    for ln in pre:
        result.removed.append(("preamble", ln))
    if pre:
        shown = " | ".join(pre[:5]) + (f" | … (+{len(pre) - 5} lines)" if len(pre) > 5 else "")
        result.warnings.append(
            f"प्रश्न {blocks[0][0]} से पहले की {len(pre)} line(s) को प्रश्न नहीं माना गया "
            f"(heading/निर्देश): {shown}")
    if complete_runs(pre) or any(ANSWER_LETTER_RE.match(ln) for ln in pre):
        result.errors.append(ParseError(
            None, f"प्रश्न {blocks[0][0]} से पहले options/उत्तर वाला text है, पर उसका प्रश्न-क्रमांक "
                  f"नहीं पढ़ा जा सका (शुरुआत: '{pre[0][:80]}') — यह प्रश्न import नहीं हुआ, format जांचें"))
    first_num = blocks[0][0]
    if first_num > 1:
        before = [ln for ln in pre if _is_candidate_line(ln)]
        if before:
            result.warnings.append(
                f"प्रश्न {first_num} से पहले कुछ numbered text था जिसे प्रश्न नहीं माना गया "
                f"(options नहीं मिले)। अगर वे प्रश्न थे तो format जांचें।")

    # document-level coverage: every non-empty source line went somewhere
    if accounted != source_counter:
        diff = (source_counter - accounted) + (accounted - source_counter)
        result.errors.append(ParseError(
            None, "आंतरिक जांच fail: कुछ source text किसी प्रश्न/हटाए गए हिस्से में नहीं गिना गया: "
                  + "".join(sorted(diff.elements()))[:60]))

    # option-count consistency (3-option question in a 4-option paper = probably a lost option)
    counts = Counter(len(q.options) for q in result.questions)
    if len(counts) > 1:
        major, freq = counts.most_common(1)[0]
        if freq > len(result.questions) / 2:
            odd = [q for q in result.questions if len(q.options) != major]
            names = ", ".join(f"{q.label} ({len(q.options)})" for q in odd[:15])
            more = f" … +{len(odd) - 15}" if len(odd) > 15 else ""
            result.review.append(
                f"ज़्यादातर प्रश्नों में {major} options हैं, पर इनमें अलग संख्या है: {names}{more}"
                " — कोई option छूटा तो नहीं, preview में जांचें")

    result.errors.sort(key=lambda e: (e.number is None, e.section, e.number or 0))
    if len(result.questions) > MAX_QUESTIONS:
        result.warnings.append(
            f"{len(result.questions)} प्रश्न मिले; एक quiz में अधिकतम {MAX_QUESTIONS} होते हैं।")
    return result


# ------------------------------------------------------------ file readers
def extract_pdf_text(source) -> str:
    """Extract text (layout-aware, OCR when needed) from a PDF path or bytes."""
    from pdf_extract import extract_pdf
    return extract_pdf(source).text


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


def parse_pdf_result(source, *, ocr: str = "auto",
                     progress: Optional[Callable[[int, int, str], None]] = None) -> ParseResult:
    """Extract + parse a PDF.  Page-level problems become errors, OCR pages
    and layout decisions become warnings/review items."""
    from pdf_extract import extract_pdf
    ext = extract_pdf(source, ocr=ocr, progress=progress)
    res = parse_source(ext.text, ocr=bool(ext.ocr_pages))
    res.ocr_pages = ext.ocr_pages
    res.warnings = ext.warnings + res.warnings
    res.errors = [ParseError(None, e) for e in ext.errors] + res.errors
    for t in ext.removed:
        res.removed.append(("header/footer", t))
    if ext.ocr_pages:
        res.review.insert(0, "पेज " + ", ".join(map(str, ext.ocr_pages)) +
                          " OCR से पढ़े गए — प्रश्न, options और उत्तर preview में ज़रूर मिलाएँ")
    return res


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
