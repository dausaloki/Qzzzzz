"""Helpers that build realistic test PDFs (text layer, two-column, scanned)."""
from __future__ import annotations

import html
import re
from pathlib import Path

FONT = Path.home() / "fonts" / "NotoSansDevanagari.ttf"
_DEVA_RUN = re.compile(r"[\u0900-\u097F\u200c\u200d]+|[^\u0900-\u097F\u200c\u200d]+")


def have_font() -> bool:
    return FONT.exists()


def write_line(page, x: float, y: float, text: str, size: float = 10) -> float:
    """Write one line with per-script fonts (Devanagari → Noto, rest → Helvetica).
    Returns the x after the text."""
    import fitz
    deva = fitz.Font(fontfile=str(FONT))
    helv = fitz.Font("helv")
    for run in _DEVA_RUN.findall(text):
        is_deva = bool(re.match(r"[\u0900-\u097F]", run))
        page.insert_text((x, y), run, fontname="deva" if is_deva else "helv", fontsize=size)
        x += (deva if is_deva else helv).text_length(run, fontsize=size)
    return x


def new_page(doc, width=595, height=842):
    page = doc.new_page(width=width, height=height)
    page.insert_font(fontname="deva", fontfile=str(FONT))
    return page


def make_text_pdf(path: Path, text: str, per_page: int = 45) -> None:
    import fitz
    doc = fitz.open()
    lines = text.split("\n")
    for i in range(0, len(lines), per_page):
        page = new_page(doc)
        y = 40
        for ln in lines[i:i + per_page]:
            write_line(page, 40, y, ln)
            y += 16
    doc.save(str(path))


def make_two_column_pdf(path: Path, pages: list[tuple[str, str, str]], header: str = "",
                        footer_fmt: str = "") -> None:
    """``pages`` = [(title_spanning_text, left_column_text, right_column_text)].
    Lines of the two columns share the same baselines (like a typeset paper)."""
    import fitz
    doc = fitz.open()
    for pno, (title, left, right) in enumerate(pages, 1):
        page = new_page(doc)
        if header:
            write_line(page, 40, 30, header, 9)
        y0 = 70
        if title:
            write_line(page, 40, y0, title, 11)
            y0 += 22
        # written ROW BY ROW (left line, right line, next row …) so the content
        # stream interleaves the columns — naive extraction would mix them up
        lrows, rrows = left.split("\n"), right.split("\n")
        for k in range(max(len(lrows), len(rrows))):
            y = y0 + 14 * k
            if k < len(lrows):
                write_line(page, 40, y, lrows[k], 9)
            if k < len(rrows):
                write_line(page, 310, y, rrows[k], 9)
        if footer_fmt:
            write_line(page, 270, 825, footer_fmt.format(n=pno), 9)
    doc.save(str(path))


def _shaped_page(doc, text: str, font_px: int = 13):
    """A page whose Devanagari is correctly shaped (HarfBuzz via insert_htmlbox).
    Its own text layer is garbled (ligature glyphs) — like many real PDFs."""
    import fitz
    page = doc.new_page(width=595, height=842)
    body = "".join(
        f"<p style='margin:0 0 3px 0;font-size:{font_px}px'>{html.escape(ln) or '&nbsp;'}</p>"
        for ln in text.split("\n"))
    page.insert_htmlbox(fitz.Rect(40, 40, 555, 810), body)
    return page


def make_garbled_text_pdf(path: Path, text: str) -> None:
    import fitz
    doc = fitz.open()
    _shaped_page(doc, text)
    doc.save(str(path))


def make_scanned_pdf(path: Path, text: str, dpi: int = 200) -> None:
    """Image-only PDF (no text layer at all), like a phone scan."""
    import fitz
    src = fitz.open()
    page = _shaped_page(src, text)
    pix = page.get_pixmap(dpi=dpi)
    out = fitz.open()
    p = out.new_page(width=page.rect.width, height=page.rect.height)
    p.insert_image(p.rect, stream=pix.tobytes("jpeg", jpg_quality=80))   # like a real scan
    out.save(str(path))


def make_grid_pdf(path: Path) -> None:
    """Single-column paper with a 2×2 option grid and a List-I/List-II table."""
    import fitz
    doc = fitz.open()
    page = new_page(doc)
    y = 50
    rows = [
        [(40, "1. राजस्थान का राज्य पशु कौन है?")],
        [(60, "(A) ऊँट"), (300, "(B) चिंकारा")],
        [(60, "(C) बाघ"), (300, "(D) हिरण")],
        [(40, "उत्तर: B")],
        [(40, "2. सुमेलित कीजिए —")],
        [(60, "सूची-I"), (300, "सूची-II")],
        [(60, "(A) रामदेवजी"), (300, "1. रूणीचा")],
        [(60, "(B) गोगाजी"), (300, "2. गोगामेड़ी")],
        [(60, "(C) तेजाजी"), (300, "3. खड़नाल")],
        [(60, "(D) पाबूजी"), (300, "4. कोलू")],
        [(40, "(A) A-1, B-2, C-3, D-4")],
        [(40, "(B) A-2, B-1, C-4, D-3")],
        [(40, "(C) A-4, B-3, C-2, D-1")],
        [(40, "(D) A-3, B-4, C-1, D-2")],
        [(40, "उत्तर: A")],
    ]
    for row in rows:
        for x, t in row:
            write_line(page, x, y, t, 10)
        y += 18
    doc.save(str(path))
