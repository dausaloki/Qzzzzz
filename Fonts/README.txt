Bundled PDF fonts (used by review_pdf.py to render Hindi + English text).

* NotoSansDevanagari-Regular.ttf — Noto Sans Devanagari, Copyright Google Inc.
  Licensed under the SIL Open Font License 1.1 (OFL). Devanagari shaping
  (conjuncts, matras) is done by MuPDF.
* DejaVuSans.ttf, DejaVuSans-Bold.ttf — DejaVu Fonts, based on Bitstream Vera /
  Arev fonts; DejaVu licence (Bitstream Vera + public domain additions), which
  allows redistribution and embedding.

These files are only used to draw the quiz-review PDF; they are not modified.
If PDF_FONT_DIR points at a directory with these file names, that copy is used
instead (e.g. a different font version on your server).
