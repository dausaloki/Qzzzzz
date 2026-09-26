"""Devanagari orthography checks used to detect broken PDF text layers and
OCR misreads.  Nothing here ever CHANGES text — it only reports words that a
human must verify ("Verification Required — Question X").

Unicode Hindi has strict rules that real words always follow, so typical
extraction/OCR damage is detectable without a dictionary:

* a vowel sign (ा ि ी ु ू ृ े ै ो ौ …) after another vowel sign        — "रािस्थाि", "चुिें"
* a vowel sign after a virama (्)                                    — "निम्िलिखित"
* a vowel sign after ं / ँ / ः, or after an independent vowel (अा)
* a vowel sign, virama, nukta or ं/ँ/ः at the very start of a word

Some misreads produce valid-looking words (व→न: "निकल्पों" for "विकल्पों");
the most frequent of those in exam papers are listed in ``KNOWN_MISREADS``.
"""
from __future__ import annotations

import re

_MATRA = "\u093a\u093b\u093e-\u094c\u094e\u094f\u0955-\u0957\u0962\u0963"
_SIGN = "\u0900-\u0903"
_VIRAMA = "\u094d"
_NUKTA = "\u093c"
_INDEP_VOWEL = "\u0904-\u0914\u0960\u0961\u0972-\u0977"
_DEV = "\u0900-\u097f"

_INVALID_RE = re.compile(
    "(?:"
    f"[{_MATRA}][{_MATRA}]"                    # vowel sign + vowel sign
    f"|{_VIRAMA}[{_MATRA}{_SIGN}{_VIRAMA}]"     # virama + vowel sign / sign / virama
    f"|[{_SIGN}][{_MATRA}{_VIRAMA}{_NUKTA}]"    # ं/ँ/ः + vowel sign
    f"|[{_INDEP_VOWEL}][{_MATRA}{_VIRAMA}]"     # अ + ा  (should be आ)
    f"|(?<![{_DEV}\u200c\u200d])[{_MATRA}{_SIGN}{_VIRAMA}{_NUKTA}]"   # combining mark at word start
    ")"
)

# Valid-looking OCR misreads that are common in Hindi exam papers (never auto-corrected).
KNOWN_MISREADS = ("निकल्प", "निम्िलिखित", "रािस्थाि", "चुिें", "ननम्न", "नलखखत")

_STRIP = ".,;:!?()[]{}\"'।॥|-–—‘’“”"


def words(text: str) -> list[str]:
    out = []
    for t in str(text or "").split():
        t = t.strip(_STRIP)
        if t:
            out.append(t)
    return out


def invalid_words(text: str) -> list[str]:
    """Words that break Devanagari spelling rules or are known OCR misreads (in order, unique)."""
    found: list[str] = []
    for w in words(text):
        if not re.search(f"[{_DEV}]", w):
            continue
        if _INVALID_RE.search(w) or any(k in w for k in KNOWN_MISREADS):
            if w not in found:
                found.append(w)
    return found


def invalid_ratio(text: str) -> tuple[int, int]:
    """(invalid Devanagari words, all Devanagari words)."""
    dev = [w for w in words(text) if re.search(f"[{_DEV}]", w)]
    bad = sum(1 for w in dev if _INVALID_RE.search(w))
    return bad, len(dev)
