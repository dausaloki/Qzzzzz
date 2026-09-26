"""PDF → text extraction with layout handling and OCR.

* Lines are rebuilt from PyMuPDF's ``dict`` output (with bounding boxes), so
  two-column pages can be read column by column and repeated headers/footers
  can be removed (every removal is reported, nothing disappears silently).
* Pages without a text layer (scans/photos), pages whose text layer is garbled
  (private-use glyphs, broken ligatures) and pages set in legacy Hindi fonts
  (Kruti Dev, DevLys, Chanakya …) are OCR'd with Tesseract through PyMuPDF
  (``hin+eng`` by default).  OCR only needs the ``*.traineddata`` files
  (Debian/Ubuntu package ``tesseract-ocr-hin``); the Dockerfile installs them.
* If OCR is needed but not available the page is reported as an explicit
  error — it is never skipped silently.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# "eng+hin" (English first) keeps digits next to Devanagari ("प्रश्न 1.") that
# "hin+eng" drops; Hindi words are recognised equally well.
OCR_LANGUAGES = os.getenv("OCR_LANGUAGES", "eng+hin").strip() or "eng+hin"
# Second English-only OCR pass that verifies Latin structural tokens (option
# labels, answer/key letters) which the Hindi model misreads ((B)→(8), A→2).
OCR_VERIFY = os.getenv("OCR_VERIFY", "1").strip().lower() not in ("0", "false", "no")
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
# Third, Hindi-only OCR pass at a higher resolution.  Devanagari words on which
# the two readings disagree are NOT changed — they are reported per question as
# "Verification Required" so a human checks them against the page.
OCR_VERIFY_HINDI = os.getenv("OCR_VERIFY_HINDI", "1").strip().lower() not in ("0", "false", "no")
OCR_VERIFY_DPI = int(os.getenv("OCR_VERIFY_DPI", str(OCR_DPI * 4 // 3)))
MAX_OCR_PAGES = int(os.getenv("MAX_OCR_PAGES", "300"))

LEGACY_FONT_RE = re.compile(
    r"kruti|k010|devlys|dev\s*lys|chanakya|shivaji|walkman|agra|kundli|akruti|surekh|"
    r"aps-dv|dv-tt|dvb-tt|dvot|kiran|sanskrit\s*99|marathi-?saral|shusha|narad|amar\s*ujala",
    re.IGNORECASE,
)
_PUA_RE = re.compile("[\ue000-\uf8ff\ufffd\x00]")
# Latin Extended-A/B + IPA: typical output of broken Devanagari ligature glyphs
_LATIN_EXT_RE = re.compile("[\u0100-\u02af]")
_DEVANAGARI_RE = re.compile("[\u0900-\u097f]")


class SourceError(ValueError):
    """The source as a whole could not be read (bad PDF, no text, etc.)."""


@dataclass
class Line:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float = 10.0
    page: int = 0               # 1-based page number (set after ordering)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1.0)


@dataclass
class PageReport:
    number: int                 # 1-based
    method: str = "text"        # text | ocr | failed
    reason: str = ""            # why OCR was used
    columns: bool = False
    error: str = ""
    chars: int = 0


@dataclass
class Extraction:
    text: str
    pages: list[PageReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)   # header/footer/page-number lines
    # line_pages[i] = page number of line i of ``text`` (text.split("\n"))
    line_pages: list[int] = field(default_factory=list)
    # page → Devanagari words the two OCR passes read differently
    uncertain: dict[int, set] = field(default_factory=dict)

    @property
    def ocr_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.method == "ocr"]

    @property
    def column_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.columns]


# ---------------------------------------------------------------- quality
def text_quality(text: str) -> tuple[bool, str]:
    """Return (garbled, reason) for an extracted text layer."""
    body = re.sub(r"\s+", "", text)
    if not body:
        return True, "कोई text layer नहीं"
    bad = len(_PUA_RE.findall(body))
    ext = len(_LATIN_EXT_RE.findall(body))
    dev = len(_DEVANAGARI_RE.findall(body))
    if bad and bad / len(body) > 0.02:
        return True, "text layer में अपठनीय (private-use/टूटे) glyphs हैं"
    if dev and ext / max(dev, 1) > 0.02:
        return True, "हिंदी glyphs टूटे हुए निकल रहे हैं (broken ligatures)"
    if dev >= 40:
        from hindi_check import invalid_ratio
        badw, allw = invalid_ratio(text)
        if badw >= 2 and badw / max(allw, 1) > 0.02:
            return True, "text layer में हिंदी मात्राएँ/अक्षर गलत क्रम में हैं (जैसे 'रािस्थाि')"
    return False, ""


def legacy_fonts(page) -> list[str]:
    names = []
    try:
        for f in page.get_fonts(full=False):
            name = str(f[3] or "") + " " + str(f[4] or "")
            if LEGACY_FONT_RE.search(name):
                names.append(name.strip())
    except Exception:  # noqa: BLE001 - font table problems must not abort
        pass
    return names


# ------------------------------------------------------------- OCR support
def find_tessdata() -> Optional[str]:
    """Directory containing the OCR language files, or None."""
    cands = [os.getenv("TESSDATA_PREFIX", "").strip()]
    try:
        import fitz
        try:
            cands.append(fitz.get_tessdata() or "")
        except Exception:  # noqa: BLE001 - raises when tesseract is absent
            pass
    except ImportError:  # pragma: no cover
        return None
    cands += ["/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tesseract-ocr/4.00/tessdata",
              "/usr/share/tessdata", "/usr/local/share/tessdata",
              str(Path.home() / "tessdata")]
    for c in cands:
        if c and Path(c, "eng.traineddata").is_file():
            return c
    return None


def ocr_status(languages: str = OCR_LANGUAGES) -> tuple[bool, str]:
    td = find_tessdata()
    if not td:
        return False, ("OCR उपलब्ध नहीं है (Tesseract language data नहीं मिला; "
                       "Dockerfile वाला deploy use करें या TESSDATA_PREFIX set करें)")
    missing = [lg for lg in languages.split("+") if not Path(td, f"{lg}.traineddata").is_file()]
    if missing:
        return False, f"OCR language data missing: {', '.join(missing)} ({td})"
    return True, td


# --------------------------------------------------------------- lines
def _lines_from_dict(d: dict, ocr: bool = False) -> list[Line]:
    """Rebuild visual lines from a PyMuPDF text dict.

    Spans are joined; a space is inserted when there is a visible horizontal
    gap and neither side already has whitespace (OCR output has one span per
    word and no space characters).  A line with a very large internal gap
    (> 3× font size) is split into two segments — they usually belong to
    different columns/cells.
    """
    out: list[Line] = []
    for b in d.get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text", "")]
            if not spans:
                continue
            wdir = ln.get("dir", (1, 0))
            if abs(wdir[1]) > 0.5:          # vertical text: keep as its own line
                s = spans[0]
                txt = "".join(sp["text"] for sp in spans)
                bb = ln["bbox"]
                out.append(Line(bb[0], bb[1], bb[2], bb[3], txt, s.get("size", 10)))
                continue
            seg: list[dict] = []

            def flush():
                if not seg:
                    return
                parts = [seg[0]["text"]]
                for a, bsp in zip(seg, seg[1:]):
                    gap = bsp["bbox"][0] - a["bbox"][2]
                    size = max(a.get("size", 10), 1)
                    need = (ocr and gap > -0.5) or gap > 0.15 * size
                    if need and not parts[-1][-1:].isspace() and not bsp["text"][:1].isspace():
                        parts.append(" ")
                    parts.append(bsp["text"])
                txt = "".join(parts)
                if txt.strip():
                    x0 = min(s["bbox"][0] for s in seg)
                    y0 = min(s["bbox"][1] for s in seg)
                    x1 = max(s["bbox"][2] for s in seg)
                    y1 = max(s["bbox"][3] for s in seg)
                    size = Counter(round(s.get("size", 10), 1) for s in seg).most_common(1)[0][0]
                    out.append(Line(x0, y0, x1, y1, txt, size))
                seg.clear()

            for sp in spans:
                if seg:
                    prev = seg[-1]
                    gap = sp["bbox"][0] - prev["bbox"][2]
                    if gap > 3 * max(prev.get("size", 10), 1):
                        flush()
                seg.append(sp)
            flush()
    return out


def _same_row(a: Line, b: Line) -> bool:
    ov = min(a.y1, b.y1) - max(a.y0, b.y0)
    return ov > 0.5 * min(a.h, b.h)


def _rows(lines: list[Line]) -> list[Line]:
    """Sort top→bottom, merging pieces that sit on the same visual row."""
    rows: list[list[Line]] = []
    for ln in sorted(lines, key=lambda l: (round(l.cy, 1), l.x0)):
        for row in rows[-3:]:
            if _same_row(row[0], ln):
                row.append(ln)
                break
        else:
            rows.append([ln])
    out = []
    for row in rows:
        row.sort(key=lambda l: l.x0)
        txt = row[0].text
        for a, b in zip(row, row[1:]):
            sep = "" if txt[-1:].isspace() or b.text[:1].isspace() else " "
            txt += sep + b.text
        out.append(Line(min(l.x0 for l in row), min(l.y0 for l in row),
                        max(l.x1 for l in row), max(l.y1 for l in row), txt,
                        row[0].size))
    out.sort(key=lambda l: (l.y0, l.x0))
    return out


_Q_START_RE = re.compile(
    r"^\s*(?:(?:प्रश्न|प्र\s*\.|Question|Ques\.?|Que\.?|Q)\s*[\.\-:#]?\s*\d{1,4}|\d{1,4}\s*[\.\)])(?!\d)")


_FIRST_OPT_RE = re.compile(r"^\s*(?:[\(\[]\s*(?:[Aa1क]|Ⓐ)\s*[\)\]]|[Aa][\)\.:](?=\s)|[Ⓐ①])")


def _column_has_question(col: list[Line]) -> bool:
    seen_q = False
    for l in sorted(col, key=lambda l: l.y0):
        if _Q_START_RE.match(l.text):
            seen_q = True
        elif seen_q and _FIRST_OPT_RE.match(l.text):
            return True
    return False


def _find_gutter(lines: list[Line], width: float) -> tuple[Optional[float], int]:
    best, best_cross = None, None
    lo, hi = int(width * 0.30), int(width * 0.70)
    for x in range(lo, hi + 1, 2):
        cross = sum(1 for l in lines if l.x0 < x - 2 and l.x1 > x + 2)
        if best_cross is None or cross < best_cross or (
                cross == best_cross and abs(x - width / 2) < abs(best - width / 2)):
            best, best_cross = x, cross
    return best, best_cross or 0


def order_lines(lines: list[Line], width: float) -> tuple[list[Line], bool]:
    """Return lines in reading order and whether a two-column layout was used."""
    if len(lines) < 8:
        return _rows(lines), False
    gx, _ = _find_gutter(lines, width)
    if gx is None:
        return _rows(lines), False
    left = [l for l in lines if l.x1 <= gx + 2]
    right = [l for l in lines if l.x0 >= gx - 2 and l.x1 > gx + 2]
    span = [l for l in lines if l not in left and l not in right]
    if len(left) < 4 or len(right) < 4 or len(span) > 0.25 * len(lines):
        return _rows(lines), False
    # vertical overlap of the two sides
    ov = min(max(l.y1 for l in left), max(l.y1 for l in right)) - max(
        min(l.y0 for l in left), min(l.y0 for l in right))
    if ov <= 0:
        return _rows(lines), False
    # A real column has its own questions: a question start followed (lower in
    # the SAME column) by a first option "(A)"/"A."/"(1)"/"(क)".  A 2×2 option
    # grid or a List-I/List-II table has no such structure on its right side
    # → read row by row instead.
    if not (_column_has_question(right) and _column_has_question(left)):
        return _rows(lines), False
    out: list[Line] = []
    span_sorted = sorted(span, key=lambda l: l.y0)
    bounds = [-1e9] + [l.cy for l in span_sorted] + [1e9]
    for k in range(len(bounds) - 1):
        top, bot = bounds[k], bounds[k + 1]
        out += _rows([l for l in left if top <= l.cy < bot])
        out += _rows([l for l in right if top <= l.cy < bot])
        if k < len(span_sorted):
            out.append(span_sorted[k])
    return out, True


# --------------------------------------------------- headers / footers
_PAGE_NO_RE = re.compile(
    r"^\s*(?:(?:Page|पृष्ठ|पेज)\s*[-:.]?\s*\d{1,4}(?:\s*(?:of|/|का|में\s*से)\s*\d{1,4})?|"
    r"[-–—]?\s*\d{1,4}\s*[-–—]?|\d{1,4}\s*/\s*\d{1,4})\s*$",
    re.IGNORECASE,
)


def remove_headers_footers(pages: list[tuple[list[Line], float]]) -> tuple[list[list[Line]], list[str]]:
    """Drop lines repeated in the top/bottom 6% of ≥50% of pages + page numbers."""
    removed: list[str] = []
    n = len(pages)
    zone_keys: Counter = Counter()

    def key(t: str) -> str:
        return re.sub(r"\d+", "#", re.sub(r"\s+", " ", t.strip().lower()))

    def in_zone(l: Line, h: float) -> bool:
        return l.y1 <= h * 0.06 or l.y0 >= h * 0.94

    for lines, h in pages:
        for k in {key(l.text) for l in lines if in_zone(l, h)}:
            zone_keys[k] += 1
    repeated = {k for k, c in zone_keys.items() if n >= 2 and c >= max(2, (n + 1) // 2)}
    out = []
    for lines, h in pages:
        keep = []
        for l in lines:
            if in_zone(l, h) and (key(l.text) in repeated or _PAGE_NO_RE.match(l.text)):
                removed.append(l.text.strip())
                continue
            keep.append(l)
        out.append(keep)
    return out, removed


# ------------------------------------------------------------ main entry
def _open(source):
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover
        raise SourceError("PyMuPDF install नहीं है") from exc
    try:
        if isinstance(source, (bytes, bytearray)):
            return fitz.open(stream=bytes(source), filetype="pdf")
        return fitz.open(str(source))
    except Exception as exc:
        raise SourceError(f"PDF खुल नहीं सकी (corrupt/invalid file): {exc}") from exc


_LABEL_AT_START = re.compile(r"^(\s*[\(\[]\s*)([^\s()\[\]]{1,3}?)(\s*[\)\]])")
_ENG_LABEL = re.compile(r"^\s*[\(\[]\s*([A-L])\s*[\)\]]")
_ANS_HINT = re.compile(r"उत्तर|Answer|Ans\b|सही", re.IGNORECASE)
_KEY_ROW = re.compile(r"^\s*(?:प्रश्न\s*|Q\.?\s*)?\d{1,4}\s*[-–—.):=]\s*[\(\[]?\s*\S{1,2}\s*[\)\]]?\s*\.?\s*$")
_TRAIL_TOKEN = re.compile(r"([-–—:=(\[\s])([\(\[]?\s*)([^\s()\[\]]{1,2}?)(\s*[\)\]]?\s*\.?\s*)$")
_ENG_TRAIL = re.compile(r"[-–—:=(\[\s]\s*[\(\[]?\s*([A-L])\s*[\)\]]?\s*\.?\s*$")


def _row_text(line: Line, others: list[Line]) -> str:
    row = [o for o in others if _same_row(line, o) and o.x1 > line.x0 - 5 and o.x0 < line.x1 + 5]
    return " ".join(o.text for o in sorted(row, key=lambda o: o.x0))


def verify_tokens(main: list[Line], eng: list[Line], notes: list[str]) -> list[Line]:
    """Cross-check Latin structural tokens of the main (eng+hin) OCR pass with
    an English-only pass of the same page.

    * option label at line start: a non-letter label ("(8)", "(OC)") is
      replaced by the English reading when that is a clean letter;
    * trailing token of an answer line / key row ("उत्तर: (8)", "प्रश्न 1 - 2"):
      replaced by the English letter when the main pass read a non-letter;
      when both passes read DIFFERENT letters the token becomes "?" so the
      question is reported (never guessed).
    """
    out = []
    for l in main:
        txt = l.text
        other = _row_text(l, eng)
        m = _LABEL_AT_START.match(txt)
        e = _ENG_LABEL.match(other) if other else None
        if m and e and m.group(2) != e.group(1) and not re.fullmatch(r"[A-L]", m.group(2)):
            txt = m.group(1) + e.group(1) + m.group(3) + txt[m.end():]
            notes.append(f"label {m.group(2)}→{e.group(1)}")
        if _ANS_HINT.search(txt) or _KEY_ROW.match(txt):
            t = _TRAIL_TOKEN.search(txt)
            e2 = _ENG_TRAIL.search(other) if other else None
            if t and e2 and t.group(3) != e2.group(1):
                if re.fullmatch(r"[A-La-l]", t.group(3)):
                    notes.append(f"CONFLICT:{txt.strip()} ({t.group(3)}/{e2.group(1)})")
                    new_tok = "?"
                else:
                    notes.append(f"answer {t.group(3)}→{e2.group(1)}")
                    new_tok = e2.group(1)
                txt = txt[:t.start(3)] + new_tok + txt[t.end(3):]
        out.append(Line(l.x0, l.y0, l.x1, l.y1, txt, l.size))
    return out


def uncertain_words(main_text: str, alt_text: str) -> set:
    """Devanagari words of the main OCR reading that the second reading does
    not contain anywhere on the page (never used to change text)."""
    from hindi_check import words
    alt = set(words(alt_text))
    return {w for w in words(main_text) if _DEVANAGARI_RE.search(w) and w not in alt}


def _ocr_page(page, tessdata: str, languages: str, notes: Optional[list[str]] = None,
              uncertain: Optional[set] = None) -> list[Line]:
    tp = page.get_textpage_ocr(language=languages, dpi=OCR_DPI, full=True, tessdata=tessdata)
    lines = _lines_from_dict(tp.extractDICT(), ocr=True)
    if OCR_VERIFY and languages != "eng" and Path(tessdata, "eng.traineddata").is_file():
        tp2 = page.get_textpage_ocr(language="eng", dpi=OCR_DPI, full=True, tessdata=tessdata)
        eng = _lines_from_dict(tp2.extractDICT(), ocr=True)
        lines = verify_tokens(_rows(lines), _rows(eng), notes if notes is not None else [])
    if (uncertain is not None and OCR_VERIFY_HINDI and "hin" in languages.split("+")
            and Path(tessdata, "hin.traineddata").is_file()):
        tp3 = page.get_textpage_ocr(language="hin", dpi=OCR_VERIFY_DPI, full=True, tessdata=tessdata)
        alt = _lines_from_dict(tp3.extractDICT(), ocr=True)
        uncertain |= uncertain_words("\n".join(l.text for l in lines), "\n".join(l.text for l in alt))
    return lines


def extract_pdf(source, *, ocr: str = "auto", languages: str = OCR_LANGUAGES,
                max_ocr_pages: int = MAX_OCR_PAGES,
                progress: Optional[Callable[[int, int, str], None]] = None) -> Extraction:
    """Extract text of all pages in reading order.

    ``ocr``: "auto" (OCR pages that need it), "off" (never), "force" (all pages).
    ``progress(page_no, total, method)`` is called for every page (from the
    worker thread — callers must hop back to their loop themselves).
    """
    doc = _open(source)
    ext = Extraction(text="")
    try:
        if doc.needs_pass:
            raise SourceError("PDF password-protected है। बिना password वाली PDF भेजें।")
        total = doc.page_count
        if total == 0:
            raise SourceError("PDF में कोई page नहीं है।")
        ocr_ok, ocr_info = ocr_status(languages) if ocr != "off" else (False, "OCR बंद है")
        ocr_used = 0
        per_page: list[tuple[list[Line], float]] = []
        for pno in range(total):
            page = doc[pno]
            rep = PageReport(number=pno + 1)
            try:
                raw = page.get_text("dict")
                lines = _lines_from_dict(raw)
            except Exception as exc:  # noqa: BLE001
                lines, rep.error = [], f"text निकालने में समस्या: {exc}"
            body = "".join(l.text for l in lines)
            reason = ""
            if ocr == "force":
                reason = "OCR forced"
            elif len(re.sub(r"\s+", "", body)) < 10:
                has_img = bool(page.get_images(full=False))
                if has_img or body.strip():
                    reason = "page पर selectable text नहीं है (scan/image)"
            else:
                bad, why = text_quality(body)
                fonts = legacy_fonts(page)
                if fonts:
                    reason = "legacy Hindi font (" + ", ".join(sorted(set(fonts)))[:80] + ")"
                elif bad:
                    reason = why
            if reason:
                if not ocr_ok:
                    rep.method = "failed"
                    rep.reason = reason
                    rep.error = f"{reason}; {ocr_info}"
                    lines = []
                elif ocr_used >= max_ocr_pages:
                    rep.method = "failed"
                    rep.reason = reason
                    rep.error = f"{reason}; OCR page limit ({max_ocr_pages}) पूरी हो गई"
                    lines = []
                else:
                    if progress:
                        progress(pno + 1, total, "ocr")
                    try:
                        notes: list[str] = []
                        unc: set = set()
                        lines = _ocr_page(page, ocr_info, languages, notes, unc)
                        if unc:
                            ext.uncertain[pno + 1] = unc
                        rep.method, rep.reason = "ocr", reason
                        ocr_used += 1
                        fixes = [n for n in notes if not n.startswith("CONFLICT:")]
                        for n in notes:
                            if n.startswith("CONFLICT:"):
                                ext.warnings.append(
                                    f"पेज {pno + 1}: OCR ने उत्तर दो तरह पढ़ा — '{n[9:]}' — अनुमान नहीं "
                                    "लगाया, वह उत्तर '?' माना गया (प्रश्न error में दिखेगा)")
                        if fixes:
                            ext.warnings.append(
                                f"पेज {pno + 1}: English OCR से {len(fixes)} label/उत्तर-अक्षर सत्यापित करके "
                                "सुधारे गए (" + ", ".join(fixes[:6]) + ("…" if len(fixes) > 6 else "") + ")")
                        if not lines:
                            rep.error = "OCR से भी कोई text नहीं मिला"
                    except Exception as exc:  # noqa: BLE001
                        log.exception("OCR failed on page %s", pno + 1)
                        rep.method, rep.reason = "failed", reason
                        rep.error = f"{reason}; OCR fail हुआ: {exc}"
                        lines = []
            elif progress:
                progress(pno + 1, total, "text")
            ordered, cols = order_lines(lines, page.rect.width)
            for l in ordered:
                l.page = pno + 1
            rep.columns = cols
            rep.chars = sum(len(l.text) for l in ordered)
            per_page.append((ordered, page.rect.height))
            ext.pages.append(rep)
    finally:
        doc.close()

    cleaned, removed = remove_headers_footers(per_page)
    ext.removed = removed
    # Build the text and a parallel page map (one entry per text line).
    out_lines: list[str] = []
    for lines in cleaned:
        for l in lines:
            for piece in l.text.split("\n"):
                if piece.strip():
                    out_lines.append(piece)
                    ext.line_pages.append(l.page)
    ext.text = "\n".join(out_lines)
    for rep in ext.pages:
        if rep.error:
            ext.errors.append(f"पेज {rep.number}: {rep.error}")
    if ext.ocr_pages:
        ext.warnings.append(
            "OCR से पढ़े गए पेज: " + ", ".join(map(str, ext.ocr_pages)) +
            " — OCR में गलती हो सकती है, quiz बनाने से पहले preview ज़रूर जांचें।")
    if ext.column_pages:
        ext.warnings.append("दो-column layout पहचाना गया (पेज " +
                            ", ".join(map(str, ext.column_pages)) + ") — column-wise पढ़ा गया।")
    if removed:
        ext.warnings.append(f"Header/footer/page-number की {len(removed)} lines हटाई गईं: " +
                            " | ".join(sorted(set(removed)))[:300])
    if not ext.text:
        detail = "; ".join(ext.errors) or (
            "PDF में selectable text नहीं मिला (pages खाली हैं, OCR के लिए कोई image भी नहीं)")
        raise SourceError(detail)
    return ext


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)
