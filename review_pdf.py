"""Complete quiz-review PDF + private delivery (Feature 2).

Every finished/stopped quiz attempt produces one personalised PDF with **every
question of the quiz** (answered correctly, answered incorrectly or not
attempted at all), every original option in the original order, the user's
choice, the verified correct answer, a ✓/✗/— marker with an explicit text label,
and the explanation when the quiz data has one.

Rendering uses PyMuPDF directly (no reportlab/weasyprint dependency): text is
drawn through two bundled fonts — Noto Sans Devanagari for Devanagari and
DejaVu Sans for Latin — so Hindi conjuncts/matras keep their shape and the text
layer stays searchable.  Colours are always paired with a symbol **and** a
label, so the PDF is readable even in grayscale.

Delivery rules
--------------
* private attempt → automatic PDF right after the existing result message
  (``REVIEW_PDF_PRIVATE_AUTO``), once per attempt (``review_pdfs`` table);
* group quiz → the result message gets a "📄 my review PDF" button that sends
  the participant's own PDF to their private chat (``REVIEW_PDF_GROUP_AUTO=1``
  also sends it to every participant automatically);
* ``/review`` re-sends the last PDF on explicit request;
* a failure is logged, recorded and retried with a JobQueue job
  (``REVIEW_PDF_RETRIES``) — the attempt result itself is never lost;
* a user who never opened the bot privately gets a clear "send /start" hint
  instead of a silent failure.
"""
from __future__ import annotations

import asyncio
import html as _html
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import fitz

import config
import database as db

log = logging.getLogger("quizbot.review_pdf")

PAGE_W, PAGE_H = 595.0, 842.0         # A4 portrait (points)
MARGIN_L, MARGIN_R = 44.0, 44.0
MARGIN_TOP, MARGIN_BOTTOM = 46.0, 46.0
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R

C_DARK = (0.10, 0.11, 0.13)
C_GREY = (0.36, 0.38, 0.42)
C_GREEN = (0.04, 0.45, 0.20)
C_RED = (0.68, 0.05, 0.14)
C_BLUE = (0.07, 0.22, 0.45)
C_LINE = (0.80, 0.82, 0.86)
C_BOX = (0.955, 0.965, 0.985)
C_GREEN_BG = (0.92, 0.97, 0.93)
C_RED_BG = (0.99, 0.93, 0.94)
C_GREY_BG = (0.95, 0.95, 0.96)

STATUS_CORRECT = "correct"
STATUS_WRONG = "wrong"
STATUS_UNANSWERED = "unanswered"

STATUS_LABEL = {
    STATUS_CORRECT: ("CORRECT", "सही"),
    STATUS_WRONG: ("INCORRECT", "गलत"),
    STATUS_UNANSWERED: ("NOT ATTEMPTED", "प्रयास नहीं किया"),
}


_DEV_SPAN_RE = re.compile(r"([\u0900-\u097F\u200c\u200d]+(?:\s+[\u0900-\u097F\u200c\u200d]+)*)")
_BUILTIN_CACHE_DIR = Path("/tmp/qzzzzz_pdf_fonts")


def _extract_builtin_fonts() -> dict[str, Path]:
    """Extract MuPDF's compiled-in Devanagari, Latin and Symbol fonts to disk.

    PyMuPDF links Noto Serif Devanagari, Nimbus Sans and Noto Sans Symbols2
    directly inside its binary.  Materialising them into ``/tmp/qzzzzz_pdf_fonts``
    guarantees that Hindi rendering works even if the ``fonts/`` folder was
    omitted from a deployment.
    """
    out: dict[str, Path] = {}
    try:
        _BUILTIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        dev_p = _BUILTIN_CACHE_DIR / "BuiltinDevanagari.otf"
        lat_p = _BUILTIN_CACHE_DIR / "BuiltinSans.cff"
        latb_p = _BUILTIN_CACHE_DIR / "BuiltinSansBold.cff"
        if dev_p.exists() and lat_p.exists() and latb_p.exists():
            return {"dev": dev_p, "lat": lat_p, "latb": latb_p}
        tmp = fitz.open()
        pg = tmp.new_page(width=200, height=80)
        pg.insert_htmlbox(
            fitz.Rect(0, 0, 200, 80),
            "<div style='font-family:sans-serif'>क ख ✓ ✗ — Result <b>Bold</b></div>",
        )
        for item in pg.get_fonts():
            name, _ext, _sub, content = tmp.extract_font(item[0])
            if not content:
                continue
            low = (name or "").lower()
            if "devanagari" in low and "dev" not in out:
                dev_p.write_bytes(content)
                out["dev"] = dev_p
            elif "bold" in low and "latb" not in out:
                latb_p.write_bytes(content)
                out["latb"] = latb_p
            elif ("sans" in low or "nimbus" in low or "charis" in low) and "symbol" not in low and "lat" not in out:
                lat_p.write_bytes(content)
                out["lat"] = lat_p
        tmp.close()
    except Exception as exc:                        # noqa: BLE001
        log.warning("builtin font extraction failed: %s", exc)
    return out


# --------------------------------------------------------------------- fonts
class FontBook:
    """Devanagari + Latin fonts with per-character fallback and HarfBuzz support.

    ``family(ch)`` returns "dev" or "lat"; a text is split into runs so each run
    is measured and shaped with the font that really has the glyph.
    """

    def __init__(self, font_dir: Optional[Path] = None):
        self.dir = Path(font_dir or config.PDF_FONT_DIR)
        self.dev = self.lat = self.latb = None
        self.dev_name = self.lat_name = self.latb_name = None
        self.dev_file: Optional[str] = None
        self.lat_file: Optional[str] = None
        self.latb_file: Optional[str] = None
        self.degraded = False
        self._load()

    def _candidates(self, filename: str) -> list[Path]:
        return [self.dir / filename,
                Path(__file__).resolve().parent / "fonts" / filename,
                Path("/usr/share/fonts/truetype/dejavu") / filename,
                Path("/usr/share/fonts/truetype/noto") / filename,
                Path("/usr/share/fonts/opentype/noto") / filename]

    def _font(self, filename: str):
        for p in self._candidates(filename):
            try:
                if p.exists():
                    return fitz.Font(fontfile=str(p)), str(p)
            except Exception as exc:                    # noqa: BLE001
                log.warning("font %s unreadable: %s", p, exc)
        return None, ""

    def _load(self) -> None:
        self.dev, self.dev_file = self._font(config.PDF_FONT_DEVANAGARI)
        self.lat, self.lat_file = self._font(config.PDF_FONT_LATIN)
        self.latb, self.latb_file = self._font(config.PDF_FONT_LATIN_BOLD)
        if self.dev is None or self.lat is None or self.latb is None:
            builtin = _extract_builtin_fonts()
            if self.dev is None and "dev" in builtin:
                try:
                    p = builtin["dev"]
                    self.dev, self.dev_file = fitz.Font(fontfile=str(p)), str(p)
                except Exception:                       # noqa: BLE001
                    pass
            if self.lat is None and "lat" in builtin:
                try:
                    p = builtin["lat"]
                    self.lat, self.lat_file = fitz.Font(fontfile=str(p)), str(p)
                except Exception:                       # noqa: BLE001
                    pass
            if self.latb is None and "latb" in builtin:
                try:
                    p = builtin["latb"]
                    self.latb, self.latb_file = fitz.Font(fontfile=str(p)), str(p)
                except Exception:                       # noqa: BLE001
                    pass
        if self.lat is None:                            # PyMuPDF built-in as last resort
            try:
                self.lat = fitz.Font("helv")
                self.lat_file = None
                self.lat_name = "helv"
            except Exception:                           # noqa: BLE001
                self.lat, self.lat_file = None, None
        if self.latb is None:
            self.latb, self.latb_file = self.lat, self.lat_file
        if self.dev is None:
            self.degraded = True
            self.dev, self.dev_file = self.lat, self.lat_file
            log.error("Devanagari font not found in %s — using built-in Story shaper",
                      self.dir)
        self.dev_name = Path(self.dev_file).name if self.dev_file else "NotoSerifDevanagari"
        self.lat_name = Path(self.lat_file).name if self.lat_file else "helv"
        self.latb_name = Path(self.latb_file).name if self.latb_file else self.lat_name

    # -- glyph handling ------------------------------------------------
    def has_dev(self, ch: str) -> bool:
        if "\u0900" <= ch <= "\u097F" or ch in "\u200c\u200d":
            return True
        if self.dev is None or self.degraded:
            return False
        try:
            return bool(self.dev.has_glyph(ord(ch)))
        except Exception:                               # noqa: BLE001
            return False

    def has_lat(self, ch: str) -> bool:
        if self.lat is None:
            return False
        try:
            return bool(self.lat.has_glyph(ord(ch)))
        except Exception:                               # noqa: BLE001
            return False

    def family(self, ch: str) -> str:
        if self.has_dev(ch):
            return "dev"
        return "lat"

    def runs(self, text: str, bold: bool = False) -> list[tuple[str, str]]:
        """Split ``text`` into (family, chunk) runs without ever corrupting characters."""
        out: list[tuple[str, str]] = []
        for ch in text:
            if ch in " \t\u00a0":
                fam = out[-1][0] if out else "lat"     # keep spaces in the current run
            else:
                fam = "dev" if (ch in "\u200c\u200d" or self.has_dev(ch)) else "lat"
            if bold and fam == "lat":
                fam = "latb"
            if out and out[-1][0] == fam:
                out[-1] = (fam, out[-1][1] + ch)
            else:
                out.append((fam, ch))
        return out

    def font(self, family: str):
        return {"dev": self.dev, "lat": self.lat, "latb": self.latb}.get(family) or self.lat

    def width(self, text: str, size: float, bold: bool = False) -> float:
        total = 0.0
        for fam, chunk in self.runs(text, bold):
            f = self.font(fam)
            if f is None:
                total += 0.55 * size * len(chunk)
                continue
            try:
                total += f.text_length(chunk, size)
            except Exception:                           # noqa: BLE001
                total += 0.55 * size * len(chunk)
        return total


_FONTS: dict[str, FontBook] = {}


def fonts() -> FontBook:
    key = str(config.PDF_FONT_DIR)
    fb = _FONTS.get(key)
    if fb is None:
        fb = _FONTS[key] = FontBook()
    return fb


# --------------------------------------------------------------------- layout
def _wrap(text: str, size: float, width: float, bold: bool = False) -> list[str]:
    """Greedy word wrap that measures with the real fonts (and keeps \\n)."""
    fb = fonts()
    eff_width = max(24.0, width * 0.97)
    lines: list[str] = []
    for para in str(text or "").split("\n"):
        para = para.rstrip()
        if not para:
            lines.append("")
            continue
        words = re.split(r"(\s+)", para)
        cur = ""
        for w in words:
            if not w:
                continue
            trial = cur + w
            if fb.width(trial.rstrip(), size, bold) <= eff_width or not cur.strip():
                cur = trial
            else:
                lines.append(cur.rstrip())
                cur = w.lstrip()
        if cur:
            lines.append(cur.rstrip())
    # hard-break anything still too wide (very long unbroken words/URLs)
    out: list[str] = []
    for ln in lines:
        if fb.width(ln, size, bold) <= eff_width or not ln:
            out.append(ln)
            continue
        part = ""
        for ch in ln:
            if fb.width(part + ch, size, bold) > eff_width and part:
                out.append(part)
                part = ch
            else:
                part += ch
        if part:
            out.append(part)
    return out


def _pdf_utf16be_hex(text: str) -> bytes:
    """Encode ``text`` as a UTF-16BE hex string ``<FEFF...>`` for PDF ``/ActualText``."""
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _preserve_spaces(text: str) -> str:
    """Preserve leading and repeated spaces in HTML without breaking shaping."""
    if not text:
        return ""
    lead = len(text) - len(text.lstrip(" "))
    body = ("\u00a0" * lead) + text[lead:]
    return re.sub(r" {2,}", lambda m: "\u00a0" * len(m.group(0)), body)


def _runs_to_html(text: str) -> str:
    """Wrap Devanagari phrases in ``<span class='dev'>`` so HarfBuzz shapes them
    with the embedded Devanagari font while Latin/symbols use DejaVu/Symbols."""
    preserved = _preserve_spaces(text)
    parts = _DEV_SPAN_RE.split(preserved)
    out: list[str] = []
    for idx, part in enumerate(parts):
        if not part:
            continue
        esc = _html.escape(part)
        if idx % 2 == 1:
            out.append(f"<span class='dev'>{esc}</span>")
        else:
            out.append(esc)
    return "".join(out)


def _silence_story_text(story_doc) -> None:
    """Wrap every ``BT ... ET`` block in ``story_doc`` with an empty ``/ActualText ()``
    so the shaped glyph stream renders visually while the exact Unicode text layer
    controls text extraction and copy/search."""
    for pno in range(len(story_doc)):
        page = story_doc[pno]
        for xref in page.get_contents():
            s = story_doc.xref_stream(xref)
            if b"BT" not in s:
                continue
            out = re.sub(
                rb"(BT\b.*?\bET)",
                rb"/Span << /ActualText () >> BDC\n\1\nEMC",
                s,
                flags=re.DOTALL,
            )
            story_doc.update_stream(xref, out)


class _Canvas:
    """Flowing page layout on top of PyMuPDF with HarfBuzz shaping via ``fitz.Story``
    and an exact Unicode ``/ActualText`` layer on every page."""

    def __init__(self, header: str = ""):
        self.doc = fitz.open()
        self.fb = fonts()
        self.header = header
        self.page = None
        self.y = 0.0
        self.pages = 0
        self._cur_page_idx = 0
        self._page_items: dict[int, list[tuple[float, float, str, float, tuple, bool, int]]] = {}

    # -- pages ---------------------------------------------------------
    def new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.pages += 1
        self._cur_page_idx = self.pages - 1
        self._page_items.setdefault(self._cur_page_idx, [])
        self.y = MARGIN_TOP
        if self.pages > 1 and self.header:
            self._draw_runs(MARGIN_L, self.y + 2, self._short(self.header, 96, 8.5), 8.5, C_GREY)
            self.page.draw_line(fitz.Point(MARGIN_L, self.y + 9), fitz.Point(PAGE_W - MARGIN_R, self.y + 9),
                                color=C_LINE, width=0.6)
            self.y += 22

    def ensure(self, height: float) -> None:
        if self.page is None:
            self.new_page()
        if self.y + height > PAGE_H - MARGIN_BOTTOM:
            self.new_page()

    # -- primitives ----------------------------------------------------
    def _draw_runs(self, x: float, baseline: float, text: str, size: float, color,
                   bold: bool = False, *, bold_prefix: int = 0) -> float:
        clean = " ".join(str(text or "").splitlines())
        if clean.strip():
            self._page_items.setdefault(self._cur_page_idx, []).append(
                (float(x), float(baseline), clean, float(size), tuple(color), bool(bold), int(bold_prefix))
            )
        return x + self.fb.width(clean, size, bold)

    def line(self, text: str, size: float = 10.5, color=C_DARK, *, bold: bool = False,
             indent: float = 0.0, gap: float = 0.0, leading: Optional[float] = None) -> None:
        width = CONTENT_W - indent
        lead = leading if leading is not None else size * 1.42
        for ln in _wrap(text, size, width, bold):
            self.ensure(lead)
            self._draw_runs(MARGIN_L + indent, self.y + size * 0.95, ln, size, color, bold)
            self.y += lead
        self.y += gap

    def space(self, amount: float = 6.0) -> None:
        self.y += amount

    def rule(self, color=C_LINE, width: float = 0.7, gap_before: float = 2.0, gap_after: float = 8.0) -> None:
        self.ensure(gap_before + gap_after + 2)
        self.y += gap_before
        self.page.draw_line(fitz.Point(MARGIN_L, self.y), fitz.Point(PAGE_W - MARGIN_R, self.y),
                            color=color, width=width)
        self.y += gap_after

    def box(self, lines: list[str], *, size: float = 9.5, color=C_DARK, bg=C_BOX,
            pad: float = 7.0, gap: float = 8.0, border=None) -> None:
        """A light background box with wrapped lines (passages, explanations)."""
        if not lines:
            return
        wrapped: list[str] = []
        for ln in lines:
            wrapped.extend(_wrap(ln, size, CONTENT_W - 2 * pad) or [""])
        height = len(wrapped) * size * 1.4 + 2 * pad
        if height > PAGE_H - MARGIN_TOP - MARGIN_BOTTOM:      # huge passage: flow normally
            for ln in lines:
                self.line(ln, size, color, indent=pad, gap=1)
            self.space(gap)
            return
        self.ensure(height + gap)
        rect = fitz.Rect(MARGIN_L, self.y, PAGE_W - MARGIN_R, self.y + height)
        self.page.draw_rect(rect, color=border or bg, fill=bg, width=0.5)
        yy = self.y + pad
        for ln in wrapped:
            self._draw_runs(MARGIN_L + pad, yy + size * 0.95, ln, size, color)
            yy += size * 1.4
        self.y += height + gap

    def chip(self, x_right: float, text: str, *, size: float = 9.0, color=C_DARK, bg=C_GREY_BG) -> None:
        """Status chip drawn at the right edge of the current line."""
        w = self.fb.width(text, size) + 14
        x0 = x_right - w
        self.ensure(size * 1.6)
        rect = fitz.Rect(x0, self.y + 1.5, x_right, self.y + size * 1.55)
        self.page.draw_rect(rect, color=bg, fill=bg, width=0)
        self._draw_runs(x0 + 6, rect.y0 + size * 1.02, text, size, color, bold=False)

    def _story_css_and_archive(self) -> tuple[str, Optional[fitz.Archive]]:
        dirs: list[str] = []
        css_rules: list[str] = []
        if self.fb.dev_file and Path(self.fb.dev_file).exists():
            dp = Path(self.fb.dev_file).resolve()
            dirs.append(str(dp.parent))
            css_rules.append(
                f"@font-face {{ font-family: 'NotoDev'; src: url({dp.name}); font-weight: normal; }}"
                f"@font-face {{ font-family: 'NotoDev'; src: url({dp.name}); font-weight: bold; }}"
            )
        if self.fb.lat_file and Path(self.fb.lat_file).exists():
            lp = Path(self.fb.lat_file).resolve()
            if str(lp.parent) not in dirs:
                dirs.append(str(lp.parent))
            css_rules.append(
                f"@font-face {{ font-family: 'DejaVu'; src: url({lp.name}); font-weight: normal; }}"
            )
        if self.fb.latb_file and Path(self.fb.latb_file).exists():
            lbp = Path(self.fb.latb_file).resolve()
            if str(lbp.parent) not in dirs:
                dirs.append(str(lbp.parent))
            css_rules.append(
                f"@font-face {{ font-family: 'DejaVu'; src: url({lbp.name}); font-weight: bold; }}"
            )
        css_rules.append(
            "body, div, p, span { font-family: 'DejaVu', sans-serif; margin: 0; padding: 0; }"
            ".dev { font-family: 'NotoDev', sans-serif; }"
        )
        arch = fitz.Archive(dirs) if dirs else None
        return "\n".join(css_rules), arch

    def _flush_text_layers(self) -> None:
        flat: list[tuple[int, float, float, str, float, tuple, bool, int]] = []
        for pno in range(self.pages):
            for item in self._page_items.get(pno, []):
                flat.append((pno, *item))
        if not flat:
            return

        chunks: list[str] = []
        for _pno, _x, _bl, txt, sz, col, bld, bpref in flat:
            r, g, b = [max(0, min(255, int(round(c * 255)))) for c in col[:3]]
            if bpref > 0:
                inner = f"<b>{_runs_to_html(txt[:bpref])}</b>{_runs_to_html(txt[bpref:])}"
                wt = "normal"
            else:
                inner = _runs_to_html(txt)
                wt = "bold" if bld else "normal"
            chunks.append(
                f"<div style='font-size:{sz:.2f}pt;color:#{r:02x}{g:02x}{b:02x};"
                f"font-weight:{wt};line-height:1.18;margin:0;padding:0;white-space:nowrap'>{inner}</div>"
            )

        css, arch = self._story_css_and_archive()
        html_doc = "<p style='page-break-after:always;margin:0;padding:0'></p>".join(chunks)
        st = fitz.Story(html=html_doc, user_css=css, archive=arch)

        mediabox = fitz.Rect(0, 0, PAGE_W, PAGE_H)
        last_pno = [-1]
        story_page_for_pno: dict[int, int] = {}
        cur_story_page = [-1]

        def rectfn(rect_num, _filled):
            if rect_num >= len(flat):
                return mediabox, fitz.Rect(0, 0, 2000.0, 200.0), fitz.Identity
            pno, x, baseline, _txt, sz, _col, _bld, _bp = flat[rect_num]
            is_new = (pno != last_pno[0])
            if is_new:
                last_pno[0] = pno
                cur_story_page[0] += 1
                story_page_for_pno[pno] = cur_story_page[0]
            top = max(0.0, baseline - sz * 0.98)
            rect = fitz.Rect(x, top, x + 2000.0, top + max(40.0, sz * 4.0))
            return (mediabox if is_new else None), rect, fitz.Identity

        story_doc = st.write_with_links(rectfn)
        _silence_story_text(story_doc)

        for pno in range(self.pages):
            items = self._page_items.get(pno, [])
            if not items:
                continue
            page = self.doc[pno]
            spno = story_page_for_pno.get(pno)
            if spno is not None and spno < len(story_doc):
                page.show_pdf_page(mediabox, story_doc, spno, overlay=True)

            # Write exact Unicode text layer via /ActualText so get_text(), search
            # and copy-paste return the verbatim Hindi + English strings.
            page.insert_text((0, 0), " ", fontname="helv", fontsize=1, render_mode=3)
            xref = page.get_contents()[-1]
            s = self.doc.xref_stream(xref)
            m = re.search(rb"/(F\d+|helv)\s+1\s+Tf", s)
            fname = m.group(1).decode("ascii") if m else "helv"
            ops: list[bytes] = []
            for x, baseline, txt, sz, _col, _bld, _bp in items:
                ops.append(
                    b"/Span << /ActualText " + _pdf_utf16be_hex(txt) + b" >> BDC\n"
                    + f"BT /{fname} {sz:.2f} Tf 3 Tr 1 0 0 1 {x:.2f} {PAGE_H - baseline:.2f} Tm ( ) Tj ET\n".encode("ascii")
                    + b"EMC"
                )
            idx_bt = s.rindex(b"BT")
            self.doc.update_stream(xref, s[:idx_bt] + b"\n".join(ops) + b"\n")

        story_doc.close()

    def save(self, footer_left: str = "") -> bytes:
        total = self.pages
        for i, page in enumerate(self.doc, 1):
            short_left = self._short(footer_left, 72, 8.0) if footer_left else ""
            footer = f"{short_left}  ·  Page {i}/{total}" if short_left else f"Page {i}/{total}"
            self.page = page
            self._cur_page_idx = i - 1
            self._draw_runs(MARGIN_L, PAGE_H - 24, footer, 8.0, C_GREY)
            page.draw_line(fitz.Point(MARGIN_L, PAGE_H - 32), fitz.Point(PAGE_W - MARGIN_R, PAGE_H - 32),
                           color=C_LINE, width=0.5)
        self._flush_text_layers()
        out = self.doc.tobytes(garbage=4, deflate=True)
        self.doc.close()
        return out

    # -- helpers -------------------------------------------------------
    def _short(self, text: str, n: int, size: float) -> str:
        text = " ".join(str(text).split())
        if self.fb.width(text, size) <= CONTENT_W:
            return text
        while text and self.fb.width(text + "…", size) > CONTENT_W:
            text = text[:-1]
        return text + "…"


# --------------------------------------------------------------------- report
@dataclass
class ReportQuestion:
    number: int
    text: str
    options: list[str]
    chosen: Optional[int]
    correct: list[int]
    status: str
    explanation: str = ""
    section: str = ""
    pre_text: str = ""
    skipped: bool = False


@dataclass
class Report:
    quiz_title: str
    user_label: str
    mode: str
    when_utc: str
    when_local: str
    total: int
    attempted: int
    correct: int
    incorrect: int
    unanswered: int
    skipped: int
    score: int
    max_score: int
    percentage: float
    questions: list[ReportQuestion] = field(default_factory=list)
    negative: str = "लागू नहीं (हर सही उत्तर पर 1 अंक)"
    stopped: bool = False

    @property
    def consistent(self) -> bool:
        return self.correct + self.incorrect + self.unanswered == self.total


def _dt(finished_at: Optional[str], ts: Optional[float] = None) -> tuple[str, str]:
    """(UTC, IST) timestamps from SQLite's ``YYYY-MM-DD HH:MM:SS`` (UTC)."""
    dt = None
    if ts:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    elif finished_at:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(str(finished_at).split("+")[0], fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    if dt is None:
        dt = datetime.now(timezone.utc)
    ist = dt + timedelta(hours=5, minutes=30)
    return (dt.strftime("%Y-%m-%d %H:%M UTC"), ist.strftime("%Y-%m-%d %H:%M IST"))


def _user_label(user_id: int) -> str:
    u = db.get_user(user_id) or {}
    name = (u.get("first_name") or "").strip()
    uname = (u.get("username") or "").strip()
    label = ""
    if name and uname:
        label = f"{name} (@{uname})"
    elif name:
        label = name
    elif uname:
        label = "@" + uname
    else:
        label = f"User {user_id}"
    return f"{label} · ID {user_id}"


def _number_of(q: dict) -> int:
    sn = q.get("source_number")
    if isinstance(sn, int) and sn > 0:
        return sn
    return int(q.get("position") or 0)


def _correct_set(q: dict) -> list[int]:
    ci = q.get("correct_index")
    if isinstance(ci, (list, tuple)):
        return [int(x) for x in ci]
    if isinstance(ci, int):
        return [ci]
    return []


def _label(i: int) -> str:
    return f"({config.OPTION_LABELS[i]})" if 0 <= i < len(config.OPTION_LABELS) else f"({i + 1})"


# --------------------------------------------------------------- report build
def report_for_attempt(attempt: dict) -> Report:
    quiz = db.get_quiz(attempt["quiz_id"]) or {"title": "Quiz"}
    questions = db.get_questions(attempt["quiz_id"])
    answers = {a["question_id"]: a for a in db.get_answers(attempt["id"])}
    finished = attempt.get("finished_at")
    utc, ist = _dt(finished, attempt.get("started_ts") if not finished else None)
    rows: list[ReportQuestion] = []
    correct = wrong = skipped = 0
    for q in questions:
        a = answers.get(q["id"])
        cs = _correct_set(q)
        chosen = None
        status = STATUS_UNANSWERED
        was_skipped = False
        if a is not None:
            chosen = a.get("chosen_index")
            st = (a.get("status") or "").lower()
            if st in ("skipped", "timeout"):
                was_skipped = True
                skipped += 1
                status = STATUS_UNANSWERED
            elif int(a.get("correct") or 0) == 1 and chosen in cs:
                correct += 1
                status = STATUS_CORRECT
            else:
                wrong += 1
                status = STATUS_WRONG
        rows.append(ReportQuestion(
            number=_number_of(q), text=q["question"], options=list(q["options"]),
            chosen=chosen, correct=cs, status=status, explanation=q.get("explanation") or "",
            section=q.get("section_label") or "", pre_text=q.get("pre_text") or "",
            skipped=was_skipped))
    total = len(rows)
    unanswered = total - correct - wrong
    score = int(attempt.get("correct") or correct)
    pct = round(100.0 * correct / total, 2) if total else 0.0
    return Report(
        quiz_title=quiz.get("title") or "Quiz",
        user_label=_user_label(attempt["user_id"]),
        mode="private", when_utc=utc, when_local=ist,
        total=total, attempted=correct + wrong, correct=correct, incorrect=wrong,
        unanswered=unanswered, skipped=skipped, score=score, max_score=total,
        percentage=pct, questions=rows,
        stopped=(attempt.get("status") == "stopped"))


def report_for_group(session: dict, user_id: int) -> Report:
    quiz = db.get_quiz(session["quiz_id"]) or {"title": "Quiz"}
    questions = db.get_questions(session["quiz_id"])
    answers = {a["question_id"]: a for a in db.get_group_answers(session["id"]) if a["user_id"] == user_id}
    utc, ist = _dt(None, session.get("finished_ts") or session.get("started_ts"))
    rows: list[ReportQuestion] = []
    correct = wrong = 0
    for q in questions:
        a = answers.get(q["id"])
        cs = _correct_set(q)
        chosen = a.get("chosen_index") if a else None
        if a is None:
            status = STATUS_UNANSWERED
        elif int(a.get("is_correct") or 0) == 1:
            status = STATUS_CORRECT
            correct += 1
        else:
            status = STATUS_WRONG
            wrong += 1
        rows.append(ReportQuestion(
            number=_number_of(q), text=q["question"], options=list(q["options"]),
            chosen=chosen, correct=cs, status=status, explanation=q.get("explanation") or "",
            section=q.get("section_label") or "", pre_text=q.get("pre_text") or ""))
    total = len(rows)
    return Report(
        quiz_title=quiz.get("title") or "Quiz",
        user_label=_user_label(user_id),
        mode="group", when_utc=utc, when_local=ist,
        total=total, attempted=correct + wrong, correct=correct, incorrect=wrong,
        unanswered=total - correct - wrong, skipped=0,
        score=correct, max_score=total, percentage=round(100.0 * correct / total, 2) if total else 0.0,
        questions=rows, stopped=(session.get("status") == "stopped"))


# --------------------------------------------------------------------- render
def _status_style(status: str) -> tuple[tuple, str]:
    if status == STATUS_CORRECT:
        en, hi = STATUS_LABEL[status]
        return C_GREEN, f"✓ {hi} · {en}"
    if status == STATUS_WRONG:
        en, hi = STATUS_LABEL[status]
        return C_RED, f"✗ {hi} · {en}"
    en, hi = STATUS_LABEL[status]
    return C_GREY, f"— {hi} · {en}"


def render_report(rep: Report) -> tuple[bytes, int]:
    rep.questions = list(rep.questions or [])
    """Report → (PDF bytes, page count).  Draws every question, never truncates."""
    fb = fonts()
    cv = _Canvas(header=rep.quiz_title)

    # ---------------------------------------------------------- cover header
    cv.new_page()
    cv.page.draw_rect(fitz.Rect(0, 0, PAGE_W, 86), color=C_BLUE, fill=C_BLUE, width=0)
    cv._draw_runs(MARGIN_L, 44, "Complete Quiz Review" if rep.mode == "private" else "Quiz Review (Group)",
                  19, (1, 1, 1), bold=True)
    cv._draw_runs(MARGIN_L, 66, cv._short(rep.quiz_title, 180, 11.5), 11.5, (0.86, 0.90, 0.97))
    cv.y = 104

    # ---------------------------------------------------------- summary
    cv.line("परिणाम सारांश · Result Summary", 13, C_BLUE, bold=True, gap=3)
    rows = [
        ("Quiz", rep.quiz_title),
        ("User / उपयोगकर्ता", rep.user_label),
        ("Date & time / दिनांक-समय", f"{rep.when_local}  ({rep.when_utc})"),
        ("Total questions / कुल प्रश्न", str(rep.total)),
        ("Attempted / प्रयास किए", f"{rep.attempted}"),
        ("Correct / सही", f"{rep.correct}"),
        ("Incorrect / गलत", f"{rep.incorrect}"),
        ("Unanswered / अनुत्तरित", f"{rep.unanswered}"),
        ("Score / प्राप्तांक", f"{rep.score} / {rep.max_score}"),
        ("Percentage / प्रतिशत", f"{rep.percentage:.2f}%"),
        ("Negative marking / ऋणात्मक अंकन", rep.negative),
    ]
    if rep.skipped:
        rows.append(("Skipped or timed out / छोड़े गए", str(rep.skipped)))
    for k, v in rows:
        v_lines = _wrap(str(v), 10.2, CONTENT_W - 190) or [""]
        cv.ensure(15 * len(v_lines))
        cv._draw_runs(MARGIN_L, cv.y + 10, k, 9.6, C_GREY)
        for vi, vln in enumerate(v_lines):
            cv._draw_runs(MARGIN_L + 190, cv.y + 10, vln, 10.2, C_DARK)
            cv.y += 15.5 if vi == len(v_lines) - 1 else 14.0
    cv.space(4)
    verdict = ("सभी प्रश्नों की संख्या = सही + गलत + अनुत्तरित ✓"
               if rep.consistent else "⚠ गणना में अंतर — कृपया जाँचें")
    cv.line(verdict, 9.2, C_GREEN if rep.consistent else C_RED, gap=6)
    if rep.stopped:
        cv.box(["यह quiz पूरा होने से पहले रोका गया था — बाकी प्रश्न NOT ATTEMPTED दिखाए गए हैं।"],
              size=9.2, color=C_RED, bg=C_RED_BG, gap=6)
    cv.box(["संकेत / Legend:  ✓ सही उत्तर या आपका सही उत्तर · ✗ आपका गलत उत्तर · "
            "— प्रयास नहीं किया गया। हर प्रश्न पर लिखा label (CORRECT / INCORRECT / "
            "NOT ATTEMPTED) भी देखें — रंग उपलब्ध न हो तो भी PDF पढ़ा जा सकता है।"],
           size=8.8, color=C_GREY, gap=10)

    # ---------------------------------------------------------- questions
    last_section = None
    last_pre = None
    cv.rule(gap_before=0, gap_after=10)
    for idx, q in enumerate(rep.questions, 1):
        if q.section and q.section != last_section:
            cv.ensure(120)                     # never leave a heading alone at the page foot
            cv.line(f"▌ {q.section}", 11, C_BLUE, bold=True, gap=4)
            last_section = q.section
        if q.pre_text and q.pre_text != last_pre:
            cv.box(["📖 " + ln for ln in q.pre_text.split("\n")], size=8.8, color=C_GREY, gap=6)
        last_pre = q.pre_text or None
        if idx > 1:
            cv.rule(gap_before=2, gap_after=8)

        color, label = _status_style(q.status)
        bg = C_GREEN_BG if q.status == STATUS_CORRECT else (
            C_RED_BG if q.status == STATUS_WRONG else C_GREY_BG)
        cv.ensure(40)
        head_y = cv.y
        cv._draw_runs(MARGIN_L, head_y + 12, f"प्रश्न {q.number}", 12, C_DARK, bold=True)
        cv.chip(PAGE_W - MARGIN_R, label, size=8.6, color=color, bg=bg)
        cv.y += 20
        cv.line(q.text, 10.4, C_DARK, gap=5, leading=14.6)

        correct_set = list(q.correct or [])
        for i, opt in enumerate(q.options):
            is_correct = i in correct_set
            is_chosen = q.chosen is not None and i == q.chosen
            if is_correct and is_chosen:
                mark, suffix, oc = "✓", "  ← आपका उत्तर · सही (YOUR ANSWER · CORRECT)", C_GREEN
            elif is_correct:
                mark, suffix, oc = "✓", "  ← सही उत्तर (CORRECT ANSWER)", C_GREEN
            elif is_chosen:
                mark, suffix, oc = "✗", "  ← आपका उत्तर · गलत (YOUR ANSWER · INCORRECT)", C_RED
            else:
                mark, suffix, oc = " ", "", C_DARK
            prefix = f"{mark} {_label(i)} "
            width = CONTENT_W - 14 - fb.width(prefix, 10.2, False)
            lines = _wrap(opt, 10.2, width)
            for j, ln in enumerate(lines):
                cv.ensure(13.6)
                if j == 0:
                    cv._draw_runs(
                        MARGIN_L + 14, cv.y + 10, prefix + ln, 10.2, oc,
                        bold=False, bold_prefix=len(prefix) if (is_correct or is_chosen) else 0,
                    )
                else:
                    cv._draw_runs(MARGIN_L + 14 + fb.width(prefix, 10.2), cv.y + 10, ln, 10.2, oc)
                cv.y += 13.4
            if suffix:
                cv.ensure(12)
                cv._draw_runs(MARGIN_L + 14, cv.y + 9, suffix.strip(), 8.8, oc)
                cv.y += 12.5
        cv.space(1)
        if not correct_set:                      # answer missing in the source — say so
            cv.line("— सही उत्तर सत्यापित नहीं · CORRECT ANSWER NOT VERIFIED", 9.2, C_GREY, gap=3)
        if q.status == STATUS_UNANSWERED:
            note = ("छोड़ा गया / timed out" if q.skipped else "प्रयास नहीं किया")
            cv.line(f"— NOT ATTEMPTED · अनुत्तरित ({note})", 9.2, C_GREY, gap=3)
        if q.explanation:
            cv.box(["व्याख्या / Explanation: " + ln for ln in q.explanation.split("\n")],
                   size=9.0, color=C_GREY, gap=6)
        else:
            cv.space(6)

    if not rep.questions:
        cv.line("इस quiz में कोई प्रश्न नहीं है।", 10, C_RED)

    pdf = cv.save(footer_left=f"Quiz review · {rep.quiz_title}")
    return pdf, cv.pages or 1


def pdf_font_status() -> tuple[bool, str]:
    """Self-test for build/startup: render a Hindi+English report and verify
    both embedded Devanagari fonts and exact Unicode text extraction."""
    try:
        fb = fonts()
        sample = Report(
            quiz_title="राजस्थान का सामान्य ज्ञान — परीक्षा",
            user_label="राहुल शर्मा (@rahul) · ID 1",
            mode="group",
            when_utc="2026-10-18 07:25 UTC",
            when_local="2026-10-18 12:55 IST",
            total=2, attempted=1, correct=0, incorrect=1, unanswered=1, skipped=0,
            score=0, max_score=2, percentage=0.0,
            questions=[
                ReportQuestion(
                    1,
                    "निम्नलिखित में से कौन-सा कथन भारतीय संविधान की प्रस्तावना के संदर्भ में सही है?",
                    ["केवल I तथा II", "केवल II तथा III", "केवल I तथा III", "I, II तथा III सभी"],
                    1, [0], STATUS_WRONG,
                    explanation="भारतीय संविधान की प्रस्तावना में स्वतंत्रता, समता और बंधुत्व के आदर्श हैं।",
                ),
                ReportQuestion(
                    2,
                    "क्षेत्रफल की दृष्टि से भारत का सबसे बड़ा राज्य कौन-सा है?",
                    ["राजस्थान", "मध्य प्रदेश", "महाराष्ट्र", "उत्तर प्रदेश"],
                    None, [0], STATUS_UNANSWERED,
                ),
            ],
        )
        pdf, pages = render_report(sample)
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        font_names = [f[3] for page in doc for f in page.get_fonts()]
        doc.close()
        required = ("राजस्थान", "परिणाम सारांश", "निम्नलिखित", "प्रस्तावना", "संदर्भ", "क्षेत्रफल", "✓", "✗")
        missing = [w for w in required if w not in text]
        has_dev_font = any("devanagari" in (fn or "").lower() for fn in font_names)
        if missing or "????" in text or not has_dev_font or pages < 1:
            return False, f"missing={missing} has_dev_font={has_dev_font} fonts={font_names}"
        return True, f"dev={fb.dev_name} lat={fb.lat_name} embedded={sorted(set(font_names))}"
    except Exception as exc:                            # noqa: BLE001
        return False, f"error: {exc}"


# --------------------------------------------------------------------- names
_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def pdf_filename(rep: Report, suffix: str = "") -> str:
    base = _BAD.sub("_", rep.quiz_title).strip("_")[:48] or "quiz"
    u = _BAD.sub("_", rep.user_label.split("@")[0] if "@" in rep.user_label else rep.user_label).strip("_")[:24]
    return f"review_{base}{('_' + u) if u else ''}{suffix}.pdf"


def summary_caption(rep: Report) -> str:
    """Short HTML caption that goes with the PDF (Telegram caption limit)."""
    head = "📄 <b>Complete Review PDF</b>"
    if rep.mode == "group":
        head += " · <i>group quiz</i>"
    lines = [head,
             f"📚 {_html.escape(rep.quiz_title)}",
             f"📝 प्रश्न: {rep.total} · ✅ सही: {rep.correct} · ❌ गलत: {rep.incorrect} · "
             f"— अनुत्तरित: {rep.unanswered}",
             f"🏆 Score: {rep.score}/{rep.max_score} · {rep.percentage:.2f}%"]
    text = "\n".join(lines)
    return text[:1020]


# --------------------------------------------------------------------- delivery
async def _send(bot, chat_id: int, pdf: bytes, filename: str, caption: str):
    from telegram import InputFile
    return await bot.send_document(chat_id=chat_id, document=InputFile(io.BytesIO(pdf), filename=filename),
                                   caption=caption, parse_mode="HTML")


def _too_big(pdf: bytes) -> bool:
    return len(pdf) > 49 * 1024 * 1024          # Telegram bot upload limit is 50 MB


async def deliver_attempt(bot, attempt: dict, *, force: bool = False, reason: str = "auto") -> dict:
    """Generate + send one private attempt's PDF. Never raises."""
    out: dict = {"ok": False, "skipped": "", "reason": reason, "attempt_id": attempt.get("id")}
    if not config.REVIEW_PDF_ENABLED:
        out["skipped"] = "disabled"
        return out
    aid = int(attempt["id"])
    record = db.review_pdf_get(attempt_id=aid)
    if record and record.get("status") == "sent" and not force:
        out["skipped"] = "already-sent"
        return out
    try:
        rep = await asyncio.to_thread(report_for_attempt, attempt)
        pdf, pages = await asyncio.to_thread(render_report, rep)
    except Exception as exc:                            # noqa: BLE001
        log.exception("review PDF generation failed for attempt %s", aid)
        db.review_pdf_save(attempt_id=aid, user_id=attempt["user_id"], chat_id=attempt["chat_id"],
                           quiz_id=attempt["quiz_id"], status="failed", error=f"render: {exc}",
                           bump=True)
        out["error"] = f"render: {exc}"
        return out
    if _too_big(pdf):
        db.review_pdf_save(attempt_id=aid, user_id=attempt["user_id"], chat_id=attempt["chat_id"],
                           quiz_id=attempt["quiz_id"], status="failed",
                           error=f"PDF too large ({len(pdf)} bytes)", bump=True)
        out["error"] = "too-large"
        return out
    try:
        msg = await _send(bot, attempt["chat_id"], pdf, pdf_filename(rep), summary_caption(rep))
        db.review_pdf_save(attempt_id=aid, user_id=attempt["user_id"], chat_id=attempt["chat_id"],
                           quiz_id=attempt["quiz_id"], status="sent", bytes_=len(pdf), pages=pages,
                           message_id=getattr(msg, "message_id", None), error="", bump=True)
        out.update(ok=True, bytes=len(pdf), pages=pages)
        log.info("review PDF sent (attempt %s, %s bytes, %s pages)", aid, f"{len(pdf):,}", pages)
        return out
    except Exception as exc:                            # noqa: BLE001 - Forbidden/Network/…
        name = type(exc).__name__
        blocked = name == "Forbidden"
        log.warning("review PDF delivery failed for attempt %s (%s): %s", aid, name, exc)
        db.review_pdf_save(attempt_id=aid, user_id=attempt["user_id"], chat_id=attempt["chat_id"],
                           quiz_id=attempt["quiz_id"], status="blocked" if blocked else "failed",
                           error=f"{name}: {exc}", bump=True)
        out.update(error=f"{name}: {exc}", blocked=blocked)
        return out


async def deliver_group_member(bot, session_id: int, user_id: int, *, force: bool = True,
                               reason: str = "button") -> dict:
    """One group participant's own PDF → their private chat. Never raises."""
    out: dict = {"ok": False, "skipped": "", "reason": reason, "session_id": session_id}
    if not config.REVIEW_PDF_ENABLED:
        out["skipped"] = "disabled"
        return out
    session = db.get_group_session(int(session_id))
    if not session:
        out["error"] = "session-missing"
        return out
    rec = db.review_pdf_get(session_id=int(session_id), user_id=int(user_id))
    if rec and rec.get("status") == "sent" and not force:
        out["skipped"] = "already-sent"
        return out
    try:
        rep = await asyncio.to_thread(report_for_group, session, int(user_id))
        pdf, pages = await asyncio.to_thread(render_report, rep)
    except Exception as exc:                            # noqa: BLE001
        log.exception("group review PDF render failed (session %s user %s)", session_id, user_id)
        db.review_pdf_save(session_id=int(session_id), user_id=int(user_id), chat_id=int(user_id),
                           quiz_id=session["quiz_id"], status="failed", error=f"render: {exc}", bump=True)
        out["error"] = f"render: {exc}"
        return out
    if _too_big(pdf):
        out["error"] = "too-large"
        db.review_pdf_save(session_id=int(session_id), user_id=int(user_id), chat_id=int(user_id),
                           quiz_id=session["quiz_id"], status="failed", error="PDF too large", bump=True)
        return out
    try:
        msg = await _send(bot, int(user_id), pdf, pdf_filename(rep, f"_s{session_id}"),
                          summary_caption(rep))
        db.review_pdf_save(session_id=int(session_id), user_id=int(user_id), chat_id=int(user_id),
                           quiz_id=session["quiz_id"], status="sent", bytes_=len(pdf), pages=pages,
                           message_id=getattr(msg, "message_id", None), error="", bump=True)
        out.update(ok=True, bytes=len(pdf), pages=pages)
        return out
    except Exception as exc:                            # noqa: BLE001
        blocked = type(exc).__name__ == "Forbidden"
        log.info("group review PDF for user %s not delivered (%s)", user_id, exc)
        db.review_pdf_save(session_id=int(session_id), user_id=int(user_id), chat_id=int(user_id),
                           quiz_id=session["quiz_id"], status="blocked" if blocked else "failed",
                           error=f"{type(exc).__name__}: {exc}", bump=True)
        out.update(error=str(exc), blocked=blocked)
        return out


# --------------------------------------------------------------- auto + retry
async def _retry_job(context) -> None:
    data = context.job.data or {}
    kind = data.get("kind")
    if kind == "attempt":
        att = db.get_attempt(int(data["attempt_id"]))
        if not att:
            return
        rec = db.review_pdf_get(attempt_id=att["id"])
        if rec and rec.get("status") == "sent":
            return
        await deliver_attempt(context.bot, att, reason="retry")
    elif kind == "group":
        await deliver_group_member(context.bot, int(data["session_id"]), int(data["user_id"]),
                                   reason="retry")


def schedule_retry(app, *, kind: str, attempt_id: Optional[int] = None, session_id: Optional[int] = None,
                   user_id: Optional[int] = None, tries: int = 0) -> bool:
    """Queue a retry (5 s, 20 s, 60 s …), bounded by REVIEW_PDF_RETRIES."""
    if tries >= max(0, int(config.REVIEW_PDF_RETRIES)):
        return False
    if app is None or app.job_queue is None:
        return False
    delay = (5, 20, 60, 180)[min(tries, 3)]
    job_data = {"kind": kind, "attempt_id": attempt_id, "session_id": session_id,
                "user_id": user_id, "tries": tries + 1}
    app.job_queue.run_once(_retry_job, delay, data=job_data, name=f"review_pdf_retry_{kind}_{attempt_id or session_id}")
    return True


async def auto_after_finish(app, attempt: dict) -> dict:
    """Hook called by quiz_runner after the result message. Never raises."""
    if not config.REVIEW_PDF_ENABLED or not config.REVIEW_PDF_PRIVATE_AUTO:
        return {"skipped": "auto-off"}
    try:
        res = await deliver_attempt(app.bot, attempt, reason="auto")
        if not res.get("ok") and res.get("error") and not res.get("skipped"):
            rec = db.review_pdf_get(attempt_id=attempt["id"])
            schedule_retry(app, kind="attempt", attempt_id=attempt["id"],
                           tries=int((rec or {}).get("tries") or 0))
        return res
    except Exception:                                   # noqa: BLE001 — never break the result flow
        log.exception("automatic review PDF failed")
        return {"error": "exception"}


async def auto_group_after_finish(app, session: dict) -> dict:
    """Optional: send every participant their PDF (REVIEW_PDF_GROUP_AUTO=1)."""
    if not (config.REVIEW_PDF_ENABLED and config.REVIEW_PDF_GROUP_AUTO):
        return {"skipped": "group-auto-off"}
    sent = blocked = 0
    try:
        for row in db.group_leaderboard(session["id"]):
            res = await deliver_group_member(app.bot, session["id"], int(row["user_id"]), force=False,
                                             reason="group-auto")
            if res.get("ok"):
                sent += 1
            elif res.get("blocked"):
                blocked += 1
        return {"sent": sent, "blocked": blocked}
    except Exception:                                   # noqa: BLE001
        log.exception("automatic group review PDFs failed")
        return {"error": "exception"}


def last_attempt_for(user_id: int) -> Optional[dict]:
    """Most recent finished/stopped attempt of this user (for /review)."""
    rows = db.user_history(user_id, limit=5)
    return rows[0] if rows else None
