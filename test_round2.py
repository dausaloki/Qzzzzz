"""Round-2 requirements: OCR, Hindi Unicode, two-column PDFs, restarting
numbering, 2–12 options (E kept), all formats, exact preservation, answer keys,
explicit errors and Telegram limits."""
import random
import unicodedata
from pathlib import Path

import pytest

import config
import database as db
import pdf_extract as X
import pdf_parser as P
import quiz_engine as engine
import pdfgen as G

FIX = Path(__file__).parent / "fixtures"
needs_font = pytest.mark.skipif(not G.have_font(), reason="Devanagari font not available")
OCR_OK, OCR_INFO = X.ocr_status()
needs_ocr = pytest.mark.skipif(not OCR_OK, reason=f"OCR data not available: {OCR_INFO}")
OPT4 = "(A) a\n(B) b\n(C) c\n(D) d\n"


def parse(text, **kw):
    return P.parse_source(text, **kw)


def q(n, stem, ans, opts=("a", "b", "c", "d"), marker="{n}."):
    return (marker.format(n=n) + f" {stem}\n" +
            "\n".join(f"({P.LETTERS[i]}) {o}" for i, o in enumerate(opts)) + f"\nउत्तर: {ans}\n")


def key(res):
    return [(x.section, x.number) for x in res.questions]


# ============================================================ 1. OCR
SCAN_TEXT = """प्रश्न 1. राजस्थान की राजधानी क्या है?
(A) जोधपुर
(B) जयपुर
(C) उदयपुर
(D) अजमेर
प्रश्न 2. गुरु शिखर किस पर्वतमाला में स्थित है?
(A) अरावली
(B) विंध्याचल
(C) सतपुड़ा
(D) हिमालय
प्रश्न 3. Which river is called the lifeline of Rajasthan?
(A) Chambal
(B) Luni
(C) Mahi
(D) Banas
उत्तरमाला
प्रश्न 1 - A
प्रश्न 2 - A
प्रश्न 3 - C"""


@needs_ocr
def test_ocr_scanned_hindi_pdf_questions_options_and_answer_key(tmp_path):
    pdf = tmp_path / "scan.pdf"
    G.make_scanned_pdf(pdf, SCAN_TEXT, dpi=300)
    import fitz
    assert fitz.open(str(pdf))[0].get_text().strip() == ""          # truly image-only
    seen = []
    res = P.parse_pdf_result(pdf, progress=lambda p, t, m: seen.append((p, t, m)))
    assert res.ocr_pages == [1] and (1, 1, "ocr") in seen
    assert res.review and "OCR" in res.review[0]                      # confirmation required
    assert res.errors == [], res.errors
    qs = {x.number: x for x in res.questions}
    assert sorted(qs) == [1, 2, 3]
    assert qs[1].question == "राजस्थान की राजधानी क्या है?"
    assert qs[1].options == ["जोधपुर", "जयपुर", "उदयपुर", "अजमेर"] and qs[1].correct_index == 0
    assert "गुरु शिखर" in qs[2].question and "पर्वतमाला" in qs[2].question
    assert qs[2].options == ["अरावली", "विंध्याचल", "सतपुड़ा", "हिमालय"]
    assert qs[2].correct_index == 0 and qs[2].answer_source == "answer_key"
    assert qs[3].options == ["Chambal", "Luni", "Mahi", "Banas"] and qs[3].correct_index == 2
    # Hindi-model misreads of Latin tokens were verified/corrected by the English pass — and reported
    assert any("English OCR" in w for w in res.warnings)


@needs_ocr
@needs_font
def test_garbled_text_layer_is_ocrd_not_imported_as_garbage(tmp_path):
    pdf = tmp_path / "garbled.pdf"
    G.make_garbled_text_pdf(pdf, "प्रश्न 1. राजस्थान की राजधानी क्या है?\n(A) जोधपुर\n(B) जयपुर\n"
                                 "(C) उदयपुर\n(D) अजमेर\nउत्तर: (A)")
    import fitz
    layer = fitz.open(str(pdf))[0].get_text()
    assert X.text_quality(layer)[0]                                     # the text layer IS garbled
    ext = X.extract_pdf(pdf)
    assert ext.pages[0].method == "ocr" and "टूटे" in ext.pages[0].reason
    assert "राजस्थान" in ext.text and "राजधानी" in ext.text


def test_ocr_unavailable_gives_explicit_page_error(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    G.make_scanned_pdf(pdf, "1. a?\n(A) x\n(B) y\nउत्तर: A", dpi=100)
    monkeypatch.setattr(X, "ocr_status", lambda *a: (False, "OCR उपलब्ध नहीं है (test)"))
    with pytest.raises(P.SourceError, match="पेज 1: .*OCR उपलब्ध नहीं है"):
        P.parse_pdf_result(pdf)


@needs_font
def test_ocr_unavailable_on_one_page_of_many_is_reported(tmp_path, monkeypatch):
    import fitz
    good = tmp_path / "good.pdf"
    G.make_text_pdf(good, q(1, "पहला?", "A"))
    scan = tmp_path / "scan.pdf"
    G.make_scanned_pdf(scan, "2. b?\n(A) x\n(B) y\nउत्तर: B", dpi=100)
    doc = fitz.open(str(good))
    doc.insert_pdf(fitz.open(str(scan)))
    both = tmp_path / "both.pdf"
    doc.save(str(both))
    monkeypatch.setattr(X, "ocr_status", lambda *a: (False, "OCR उपलब्ध नहीं है (test)"))
    res = P.parse_pdf_result(both)
    assert [x.number for x in res.questions] == [1]
    assert any("पेज 2" in str(e) and "OCR" in str(e) for e in res.errors)
    assert not res.ok


def test_legacy_font_detection_triggers_ocr(monkeypatch):
    class FakePage:
        def get_fonts(self, full=False):
            return [(5, "ttf", "TrueType", "KrutiDev010", "F1", "")]
    assert X.legacy_fonts(FakePage()) == ["KrutiDev010 F1"]
    assert X.LEGACY_FONT_RE.search("DevLys 010") and X.LEGACY_FONT_RE.search("Chanakya")
    assert not X.LEGACY_FONT_RE.search("NotoSansDevanagari")


def test_garbled_text_quality_detector():
    assert X.text_quality("")[0]
    assert X.text_quality("abc \ue001\ue002\ue003")[0]
    assert X.text_quality("ŚĘन 1. राजĚथान कʡ राजधानी üया है")[0]
    assert not X.text_quality("प्रश्न 1. राजस्थान की राजधानी क्या है? (A) जयपुर")[0]
    assert not X.text_quality("Question 1. Which is the capital? (A) Jaipur")[0]


def test_ocr_label_confusions_fixed_only_in_sequence():
    txt = "1. q?\n(A) a\n(8) b\n(0) c\n(0) d\nउत्तर: (A)\n"
    res = parse(txt, ocr=True)
    assert res.questions[0].options == ["a", "b", "c", "d"] and res.review
    # outside OCR mode nothing is rewritten → the broken run is reported
    plain = parse(txt)
    assert not plain.questions and plain.errors
    # two-character confusion "(OC)" for C
    assert parse("1. q?\n(A) a\n(B) b\n(OC) c\n(D) d\nउत्तर: A", ocr=True).questions[0].options == list("abcd")
    # an OCR-misread ANSWER is never guessed
    bad = parse("1. q?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: (8)\n", ocr=True)
    assert not bad.questions and "OCR" in str(bad.errors[0]) and "अनुमान नहीं" in str(bad.errors[0])


def test_ocr_page_limit_reported(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    G.make_scanned_pdf(pdf, "1. a?\n(A) x\n(B) y\nउत्तर: A", dpi=100)
    monkeypatch.setattr(X, "ocr_status", lambda *a: (True, "/nonexistent"))
    with pytest.raises(P.SourceError, match="OCR page limit"):
        X.extract_pdf(pdf, max_ocr_pages=0)


def test_ocr_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    G.make_scanned_pdf(pdf, "1. a?\n(A) x\n(B) y\nउत्तर: A", dpi=100)
    monkeypatch.setattr(X, "ocr_status", lambda *a: (True, "/nonexistent"))

    def boom(*a, **k):
        raise RuntimeError("tesseract crashed")
    monkeypatch.setattr(X, "_ocr_page", boom)
    with pytest.raises(P.SourceError, match="OCR fail हुआ: tesseract crashed"):
        X.extract_pdf(pdf)


# ============================================================ 2. Hindi Unicode
def test_hindi_text_is_preserved_byte_for_byte():
    decomposed_nukta = "क\u093c"            # NFC would turn U+0958 into this, and vice versa
    precomposed = "\u0958"
    zwj = "क्\u200dष"                        # ZWJ changes conjunct rendering — must stay
    zwnj = "क्\u200cष"
    conj = "क्षत्रिय ज्ञान श्रम द्वार हृदय त्र्यंबक"
    stem = f"{decomposed_nukta}लम और {precomposed}लम, {zwj} {zwnj} {conj}  दो  spaces?"
    res = parse(f"1. {stem}\n(A) {conj}\n(B) {zwj}\nउत्तर: A")
    x = res.questions[0]
    assert x.question == stem                                    # exact, incl. double spaces
    assert x.options == [conj, zwj]
    assert decomposed_nukta in x.question and precomposed in x.question
    assert unicodedata.normalize("NFC", x.question) != x.question  # proves no NFC was applied


def test_answer_text_match_uses_nfc_only_for_comparison():
    res = parse("1. q?\n(A) \u0958लम\n(B) ख\nउत्तर: क\u093cलम")
    assert res.questions[0].correct_index == 0
    assert res.questions[0].options[0] == "\u0958लम"            # stored form untouched


def test_broken_i_matra_order_is_repaired_and_reported():
    res = parse("1. िकताब िकसने लिखी?\n(A) राम\n(B) श्याम\nउत्तर: A")
    assert res.questions[0].question == "किताब किसने लिखी?"
    assert any("ि" in w and "2" in w for w in res.warnings) and res.review
    fixed, n = P.repair_devanagari("िक्षत्रिय और हिन्दी")
    assert fixed == "क्षित्रिय और हिन्दी" and n == 1           # valid text untouched
    assert P.repair_devanagari("किताब हिंदी दिल्ली")[1] == 0


def test_unreadable_glyphs_are_an_error_not_imported():
    res = parse("1. \ue001\ue002 abc?\n(A) a\n(B) b\nउत्तर: A")
    assert not res.questions and "अपठनीय" in str(res.errors[0]) and res.errors[0].number == 1


@needs_font
def test_hindi_pdf_text_layer_roundtrip_exact(tmp_path):
    stem = "निम्नलिखित में से कौन-सा क्षत्रिय राजवंश श्रावस्ती से संबंधित था?"
    opts = ["प्रतिहार", "चौहान", "गुहिल", "राठौड़"]
    pdf = tmp_path / "hi.pdf"
    G.make_text_pdf(pdf, q(1, stem, "C", opts))
    x = P.parse_pdf_result(pdf).questions[0]
    assert x.question == stem and x.options == opts and x.correct_index == 2


# ============================================================ 3. two columns
def _col(start, n, ans="B"):
    return "".join(q(i, f"प्रश्न संख्या {i} का text?", ans,
                     (f"विकल्प {i}a", f"विकल्प {i}b", f"विकल्प {i}c", f"विकल्प {i}d")) for i in range(start, start + n)).strip()


@needs_font
def test_two_column_pdf_read_column_by_column(tmp_path):
    pdf = tmp_path / "two.pdf"
    G.make_two_column_pdf(pdf, [("राजस्थान GK Test (भाग 1)", _col(1, 3), _col(4, 3)),
                                ("", _col(7, 3), _col(10, 3))],
                          header="RPSC Practice Paper 2026", footer_fmt="Page {n}")
    import fitz
    naive = fitz.open(str(pdf))[0].get_text()
    assert naive.index("4.") < naive.index("(A) विकल्प 1a")            # naive order interleaves
    res = P.parse_pdf_result(pdf)
    assert res.errors == [], res.errors
    assert [x.number for x in res.questions] == list(range(1, 13))
    for x in res.questions:
        assert x.options == [f"विकल्प {x.number}{c}" for c in "abcd"]
        assert x.question == f"प्रश्न संख्या {x.number} का text?"
    assert any("दो-column" in w for w in res.warnings)
    removed = [t for k, t in res.removed if k == "header/footer"]
    assert "RPSC Practice Paper 2026" in removed and "Page 1" in removed and "Page 2" in removed


@needs_font
def test_option_grid_and_list_table_are_not_split_as_columns(tmp_path):
    pdf = tmp_path / "grid.pdf"
    G.make_grid_pdf(pdf)
    ext = X.extract_pdf(pdf)
    assert ext.column_pages == []
    res = P.parse_pdf_result(pdf)
    assert res.errors == []
    q1, q2 = res.questions
    assert q1.options == ["ऊँट", "चिंकारा", "बाघ", "हिरण"] and q1.correct_index == 1
    assert "(A) रामदेवजी 1. रूणीचा" in q2.question and "(D) पाबूजी 4. कोलू" in q2.question
    assert q2.options[0] == "A-1, B-2, C-3, D-4"


def test_order_lines_unit():
    L = X.Line
    left = [L(40, 100 + 14 * i, 250, 110 + 14 * i, t) for i, t in
            enumerate(["1. q1?", "(A) a", "(B) b", "2. q2?", "(A) c", "(B) d"])]
    right = [L(310, 100 + 14 * i, 520, 110 + 14 * i, t) for i, t in
             enumerate(["3. q3?", "(A) e", "(B) f", "4. q4?", "(A) g", "(B) h"])]
    title = [L(40, 60, 520, 72, "Title spanning both columns")]
    out, cols = X.order_lines(title + right + left, 595)
    assert cols and [l.text for l in out][:4] == ["Title spanning both columns", "1. q1?", "(A) a", "(B) b"]
    assert [l.text for l in out][7] == "3. q3?"


# ============================================================ 4. numbering restarts
def test_restart_with_section_headings_and_inline_answers():
    txt = "भाग-अ\n" + q(1, "p1", "A") + q(2, "p2", "B") + "भाग-ब\n" + q(1, "r1", "C") + q(2, "r2", "D")
    res = parse(txt)
    assert res.errors == [] and key(res) == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert [x.correct_index for x in res.questions] == [0, 1, 2, 3]
    assert res.sections == ["भाग-अ", "भाग-ब"] and res.questions[2].section_label == "भाग-ब"
    assert ("section-heading", "भाग-ब") in res.removed


def test_restart_without_headings_and_combined_key_by_rank():
    txt = q(1, "p1", "A") + q(2, "p2", "B")
    txt = txt.replace("उत्तर: A\n", "").replace("उत्तर: B\n", "")
    txt += "1. r1?\n" + OPT4 + "2. r2?\n" + OPT4 + "उत्तरमाला\n1-A, 2-B, 1-C, 2-D"
    res = parse(txt)
    assert res.errors == [], res.errors
    assert key(res) == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert [x.correct_index for x in res.questions] == [0, 1, 2, 3]


def test_per_section_answer_keys_positional():
    sec = lambda n: "1. q?\n" + OPT4 + "2. q?\n" + OPT4
    txt = "Section A\n" + sec(1) + "Answer Key\n1-A 2-B\nSection B\n" + sec(2) + "Answer Key\n1-C 2-D"
    res = parse(txt)
    assert res.errors == [] and [x.correct_index for x in res.questions] == [0, 1, 2, 3]
    assert [x.section_label for x in res.questions] == ["Section A"] * 2 + ["Section B"] * 2


def test_page_restart_without_any_heading_chain_proven():
    txt = "".join(q(i, f"p{i}", "A") for i in range(1, 4)) + "".join(q(i, f"r{i}", "B") for i in range(1, 4))
    res = parse(txt)
    assert res.errors == [] and len(res.questions) == 6 and len(res.sections) == 2


def test_ambiguous_key_for_restarted_numbers_is_error_not_guess():
    txt = "1. q1?\n" + OPT4 + "2. q2?\n" + OPT4 + "1. r1?\n" + OPT4 + "2. r2?\n" + OPT4 + "उत्तरमाला\n1-A, 2-B, 1-C"
    res = parse(txt)
    assert key(res) == [(0, 1), (1, 1)]
    errs = [str(e) for e in res.errors]
    assert len(errs) == 2 and all("तय नहीं हो सका" in e for e in errs)


def test_list_two_numbers_do_not_count_as_restart():
    txt = (q(1, "पहला?", "A") + "2. सुमेलित कीजिए\nसूची-I\n(A) रामदेवजी\n(B) गोगाजी\n(C) तेजाजी\n(D) पाबूजी\n"
           "सूची-II\n1. कोलू\n2. गोगामेड़ी\n3. रामदेवरा\n4. खरनाल\nकूट:\n(A) A-3, B-2, C-4, D-1\n"
           "(B) A-2, B-3, C-1, D-4\n(C) A-4, B-1, C-2, D-3\n(D) A-1, B-4, C-3, D-2\nउत्तर: A\n" + q(3, "तीसरा?", "C"))
    res = parse(txt)
    assert res.errors == [] and res.sections == [] and [x.number for x in res.questions] == [1, 2, 3]
    assert "1. कोलू\n2. गोगामेड़ी\n3. रामदेवरा\n4. खरनाल" in res.questions[1].question


def test_numbered_explanation_points_do_not_restart_or_swallow_next_question():
    txt = "1. q1?\n" + OPT4 + "उत्तर: B\nव्याख्या:\n1. पहला कारण\n2. दूसरा कारण\n2. q2?\n" + OPT4 + "उत्तर: D"
    res = parse(txt)
    assert res.errors == [] and [x.number for x in res.questions] == [1, 2]
    assert res.questions[0].explanation == "1. पहला कारण\n2. दूसरा कारण"
    assert res.questions[1].question == "q2?"


def test_section_instructions_are_reported_and_statement_question_kept():
    txt = (q(1, "q1?", "A") + q(2, "q2?", "B") + "भाग - ब\nनिर्देश:\n1. सभी प्रश्न अनिवार्य हैं\n"
           "2. गलत उत्तर पर ऋणात्मक अंक\n1. निम्न कथनों पर विचार करें\n1. कथन एक\n2. कथन दो\n"
           "(A) केवल 1\n(B) केवल 2\n(C) दोनों\n(D) कोई नहीं\nउत्तर: C\n" + q(2, "r2?", "D"))
    res = parse(txt)
    assert res.errors == [] and key(res) == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert res.questions[2].question == "निम्न कथनों पर विचार करें\n1. कथन एक\n2. कथन दो"
    assert res.questions[1].options == ["a", "b", "c", "d"]
    assert any("निर्देश" in w for w in res.warnings)
    assert ("preamble", "1. सभी प्रश्न अनिवार्य हैं") in res.removed


def test_duplicate_number_within_section_still_reported():
    res = parse(q(1, "a", "A") + q(2, "b", "B") + q(2, "c", "C"))
    assert any(e.number == 2 for e in res.errors) and not res.ok
    assert res.sections == []                       # a repeated "2." is NOT a restart


def test_ocr_token_verification_unit():
    L = X.Line
    main = [L(40, 100, 300, 112, "(8) जयपुर"), L(40, 120, 300, 132, "प्रश्न 1 - 2"),
            L(40, 140, 300, 152, "उत्तर: (B)"), L(40, 160, 300, 172, "(A) अरावली")]
    eng = [L(40, 100, 300, 112, "(B) STEYR"), L(40, 120, 300, 132, "U1 -A"),
           L(40, 140, 300, 152, "SAX: (D)"), L(40, 160, 300, 172, "(A) Raat")]
    notes = []
    out = [l.text for l in X.verify_tokens(main, eng, notes)]
    assert out == ["(B) जयपुर", "प्रश्न 1 - A", "उत्तर: (?)", "(A) अरावली"]
    assert any(n.startswith("CONFLICT:") for n in notes)
    # the "?" answer is reported as an error, never guessed
    res = parse("1. q?\n(A) a\n(B) b\nउत्तर: (?)", ocr=True)
    assert not res.questions and res.errors


def test_ocr_digit_answer_for_letter_options_is_rejected():
    res = parse("1. q?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तरमाला\n1 - 2\n2 - 3", ocr=True)
    assert not res.questions and "OCR" in str(res.errors[0])
    # outside OCR the documented convention "2 = second option" still works
    assert parse("1. q?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: 2").questions[0].correct_index == 1


# ============================================================ 5. options 2–12, E kept
def test_e_option_is_kept_and_can_be_correct():
    res = parse("1. q?\n(A) a\n(B) b\n(C) c\n(D) d\n(E) इनमें से कोई नहीं\nउत्तर: E\n"
                "2. q?\n(A) a\n(B) b\n(C) c\n(D) d\n(E) उपर्युक्त में से एक से अधिक\nउत्तर: (B)")
    assert res.errors == []
    assert res.questions[0].options[4] == "इनमें से कोई नहीं" and res.questions[0].correct_index == 4
    assert len(res.questions[1].options) == 5


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 8, 12])
def test_any_option_count_2_to_12(n):
    opts = [f"विकल्प {i}" for i in range(n)]
    res = parse(q(1, "q?", P.LETTERS[n - 1], opts))
    assert res.errors == [] and res.questions[0].options == opts
    assert res.questions[0].correct_index == n - 1


def test_thirteen_options_is_an_error():
    lines = "\n".join(f"({c}) o{c}" for c in "ABCDEFGHIJKL") + "\n(M) extra"
    res = parse("1. q?\n" + lines + "\nउत्तर: A")
    # (M) is not a marker → joins option L; must not be silently merged either
    assert (res.errors and res.errors[0].number == 1) or res.questions[0].options[-1] == "oL (M) extra"


def test_marker_styles_hindi_circled_numeric_lowercase():
    res = parse("1. q?\n(क) एक\n(ख) दो\n(ग) तीन\n(घ) चार\n(ङ) पाँच\nउत्तर: (ङ)\n"
                "2. q?\nⒶ one\nⒷ two\nⒸ three\nउत्तर: Ⓑ\n"
                "3. q?\n① x\n② y\nउत्तर: ②\n"
                "4. q?\n(1) p\n(2) q\n(3) r\n(4) s\n(5) t\nउत्तर: 5\n"
                "5. q?\na) m\nb) n\nउत्तर: b")
    assert res.errors == [], res.errors
    assert [len(x.options) for x in res.questions] == [5, 3, 2, 5, 2]
    assert [x.correct_index for x in res.questions] == [4, 1, 1, 4, 1]


def test_roman_i_after_d_is_text_but_i_after_h_is_option():
    res = parse("1. सुमेल\n(A) a\n(B) b\n(C) c\n(D) d\nI. कोलू\nII. गोगामेड़ी\nकूट:\n(A) A-I\n(B) A-II\nउत्तर: A\n"
                "2. q?\n" + "\n".join(f"({c}) o{c}" for c in "ABCDEFGHI") + "\nउत्तर: I")
    assert res.errors == []
    assert "I. कोलू" in res.questions[0].question and res.questions[0].options == ["A-I", "A-II"]
    assert len(res.questions[1].options) == 9 and res.questions[1].correct_index == 8


def test_inline_option_grid_on_one_line():
    res = parse("1. q?\n(A) सत्य (B) असत्य\nउत्तर: B\n2. r?\n(A) 1 (B) 2 (C) 3 (D) 4 (E) 5\nउत्तर: E")
    assert [x.options for x in res.questions] == [["सत्य", "असत्य"], ["1", "2", "3", "4", "5"]]


def test_option_count_differs_from_majority_needs_review():
    res = parse(q(1, "a", "A") + q(2, "b", "A") + q(3, "c", "A", ("x", "y", "z")))
    assert res.errors == [] and len(res.questions[2].options) == 3
    assert res.review and "प्रश्न 3 (3)" in res.review[0]


def test_answer_beyond_available_options_is_error():
    res = parse("1. q?\n(A) a\n(B) b\n(C) c\nउत्तर: D")
    assert not res.questions and "केवल 3 options" in str(res.errors[0])


def test_manual_options_2_to_12_and_db_validation(tmp_path):
    assert engine.parse_manual_options("हाँ\nनहीं") == ["हाँ", "नहीं"]
    assert engine.parse_manual_options("(A) x\n(B) y\n(C) z\n(D) w\n(E) v") == ["x", "y", "z", "w", "v"]
    twelve = "\n".join(f"opt {i}" for i in range(12))
    assert len(engine.parse_manual_options(twelve)) == 12
    with pytest.raises(ValueError):
        engine.parse_manual_options("only one")
    with pytest.raises(ValueError):
        engine.parse_manual_options("\n".join(f"o{i}" for i in range(13)))
    # labels are kept when they are data (A-III …)
    assert engine.parse_manual_options("A-III, B-II\nA-II, B-III")[0] == "A-III, B-II"
    db.set_db_path(tmp_path / "q.db")
    db.init_db()
    try:
        db.ensure_user_row(1)
        qid = db.new_quiz(1, "t", "")
        db.add_question(qid, "q", ["a", "b", "c", "d", "e"], 4)
        db.add_question(qid, "q2", ["a", "b"], 1)
        with pytest.raises(ValueError):
            db.add_question(qid, "q", ["a"], 0)
        with pytest.raises(ValueError):
            db.add_question(qid, "q", [str(i) for i in range(13)], 0)
        with pytest.raises(ValueError):
            db.add_question(qid, "q", ["a", "b"], 2)
        assert [len(x["options"]) for x in db.get_questions(qid)] == [5, 2]
    finally:
        db.set_db_path(config.DB_PATH)


# ============================================================ 6. formats
def test_all_formats_fixture_still_parses_exactly():
    res = parse((FIX / "all_formats_hi.txt").read_text(encoding="utf-8"))
    assert res.errors == []
    types = {x.qtype for x in res.questions}
    assert {"statement", "list_matching", "assertion_reason", "ordering"} <= types


def test_a_to_d_data_inside_question_never_taken_as_choices():
    txt = ("1. निम्नलिखित कथनों पर विचार कीजिए:\nA. कथन एक\nB. कथन दो\nC. कथन तीन\nD. कथन चार\n"
           "सही कथन चुनिए:\n(A) केवल A और B\n(B) केवल C\n(C) A, B और C\n(D) सभी\nउत्तर: C\n"
           "2. कथन (A): सूर्य पूर्व में उगता है।\nकारण (R): पृथ्वी पश्चिम से पूर्व घूमती है।\n"
           "(A) (A) और (R) दोनों सही हैं तथा (R), (A) की सही व्याख्या है।\n(B) (A) सही, (R) गलत\n"
           "(C) (A) गलत, (R) सही\n(D) दोनों गलत\nउत्तर: A\n"
           "3. निम्न को सही क्रम में लगाइए:\n(A) प्लासी\n(B) बक्सर\n(C) पानीपत\n(D) हल्दीघाटी\n"
           "कूट:\n(A) C, D, A, B\n(B) D, C, B, A\n(C) A, B, C, D\n(D) B, A, D, C\nउत्तर: A\n"
           "4. कौन-से सही हैं? (एक से अधिक सही हो सकते हैं — कूट से एक चुनें)\n(A) केवल 1 और 2\n"
           "(B) केवल 2 और 3\n(C) केवल 1 और 3\n(D) 1, 2 और 3\nउत्तर: D")
    res = parse(txt)
    assert res.errors == []
    q1, q2, q3, q4 = res.questions
    assert "A. कथन एक" in q1.question and q1.options[0] == "केवल A और B"
    assert q2.options[0].startswith("(A) और (R) दोनों") and q2.qtype == "assertion_reason"
    assert "(D) हल्दीघाटी" in q3.question and q3.options[0] == "C, D, A, B" and q3.qtype == "ordering"
    assert q4.correct_index == 3


def test_multiple_correct_answers_line_is_error():
    for ans in ("उत्तर: A और C", "Answer: (A), (C)", "Ans: B & D", "उत्तर: A तथा B"):
        res = parse("1. q?\n" + OPT4 + ans)
        assert not res.questions, ans
        assert "एक से अधिक सही उत्तर" in str(res.errors[0]), ans


# ============================================================ 7. exact preservation
def test_trailing_answer_and_answer_echo_are_consumed_not_lost():
    res = parse("1. q?\n(A) गोगाजी\n(B) तेजाजी उत्तर - B\n2. r?\n(A) x\n(B) y\nउत्तर: (B) y\n"
                "3. s?\n(A) m\n(B) n\nउत्तर: (A) क्योंकि m सही है")
    assert res.errors == []
    a, b, c = res.questions
    assert a.options == ["गोगाजी", "तेजाजी"] and a.correct_index == 1
    assert b.explanation == "" and b.correct_index == 1
    assert c.explanation == "क्योंकि m सही है"


def test_answer_echo_of_other_option_is_error():
    res = parse("1. q?\n(A) x\n(B) y\nउत्तर: (B) x")
    assert not res.questions and "(A)" in str(res.errors[0])


def test_text_after_answer_goes_to_explanation_never_into_last_option():
    res = parse("1. q?\n(A) a\n(B) b\nउत्तर: A\nयह अतिरिक्त जानकारी है।\n2. r?\n(A) c\n(B) d\nउत्तर: B")
    assert res.questions[0].options == ["a", "b"]
    assert res.questions[0].explanation == "यह अतिरिक्त जानकारी है।"


def test_numbered_text_after_last_option_is_an_error():
    res = parse("1. q?\n(A) a\n(B) b\n(C) c\n(D) d\n7. कुछ और\nउत्तर: A")
    assert not res.questions and "अनपेक्षित text" in str(res.errors[0])


def test_coverage_safety_net_catches_a_dropped_character(monkeypatch):
    real = P._expand_lines

    def lossy(lines):
        return [ln.replace("ज़", "") for ln in real(lines)]
    monkeypatch.setattr(P, "_expand_lines", lossy)
    res = P.parse_source("1. ज़मीन?\n(A) a\n(B) b\nउत्तर: A")
    assert not res.questions and "आंतरिक जांच" in str(res.errors[0])


def test_every_source_character_is_in_output_or_reported():
    src = (FIX / "all_formats_hi.txt").read_text(encoding="utf-8")
    res = parse(src)
    produced = P._cc(*[x.question for x in res.questions], *[o for x in res.questions for o in x.options],
                     *[x.explanation for x in res.questions], *[t for _, t in res.removed])
    source = P._cc(src)
    # everything that is not in the output or in the reported removals must be
    # a structural label (question number, option label, answer/explanation tag)
    missing = source - produced
    allowed = set("प्रश्न0123456789.()ABCDEFGHIJKL:उत्तरव्याखAnswer-–—ा्") | set("Ansॉ")
    assert set(missing) <= allowed, set(missing) - allowed


def test_preamble_lines_are_reported():
    res = parse("राजस्थान GK टेस्ट\nसमय: 2 घंटे\n" + q(1, "q?", "A"))
    assert ("preamble", "समय: 2 घंटे") in res.removed
    assert any("समय: 2 घंटे" in w for w in res.warnings)


def test_long_question_and_options_preserved_exactly():
    long_q = "यह एक बहुत लंबा प्रश्न है जिसमें कई वाक्य हैं। " * 20
    long_o = "यह विकल्प भी बहुत लंबा है और इसमें सौ से अधिक अक्षर हैं ताकि सीमा पार हो जाए — 12,345.67 ₹ % @ # ! ? " * 2
    res = parse(f"1. {long_q.strip()}\n(A) {long_o.strip()}\n(B) छोटा\n(C) तीसरा\n(D) चौथा\n(E) पाँचवाँ\nउत्तर: A")
    x = res.questions[0]
    assert x.question == long_q.strip() and x.options[0] == long_o.strip() and len(x.options) == 5


# ============================================================ 8/9. keys & errors
def test_inline_and_key_section_mixed_and_conflicts():
    txt = q(1, "a", "A") + "2. b?\n" + OPT4 + "3. c?\n" + OPT4 + "Answer Key\n1. A\n2. (C)\n3. D"
    res = parse(txt)
    assert res.errors == [] and [x.correct_index for x in res.questions] == [0, 2, 3]
    res = parse(q(1, "a", "A") + "Answer Key\n1-B 2-C")
    assert any("अलग" in str(e) for e in res.errors)


def test_question_without_readable_number_is_reported():
    res = parse("प्रश्न . राजस्थान की राजधानी?\n(A) जोधपुर\n(B) जयपुर\nउत्तर: B\n" + q(2, "q?", "A"))
    assert any(e.number is None and "प्रश्न-क्रमांक नहीं पढ़ा" in e.message for e in res.errors)
    res = parse(q(1, "a?", "A") + "प्रश्न . b?\n(A) p\n(B) q\nउत्तर: B\n" + q(3, "c?", "A"))
    assert res.errors and not res.ok


def test_every_problem_has_number_and_reason():
    txt = (q(1, "ok", "A") + "2. no answer?\n" + OPT4 + "3. one option?\n(A) a\nउत्तर: A\n"
           "4. multi?\n" + OPT4 + "उत्तर: A और B\n" + q(5, "ok", "B"))
    res = parse(txt)
    by = {e.number: e.message for e in res.errors}
    assert set(by) == {2, 3, 4} and all(by.values())
    assert [x.number for x in res.questions] == [1, 5] and not res.ok


# ============================================================ 10. Telegram limits
@pytest.mark.parametrize("n", range(2, 13))
def test_poll_payload_never_exceeds_limits_for_any_option_count(n):
    rng = random.Random(n)
    for trial in range(30):
        opts = ["विकल्प " * rng.randint(1, 40) + str(i) for i in range(n)]
        qtext = "प्रश्न " * rng.randint(1, 120)
        question = {"question": qtext, "options": opts, "correct_index": rng.randrange(n),
                    "explanation": "व्याख्या " * rng.randint(0, 60)}
        perm = engine.make_permutation(n, True, rng)
        p = engine.build_poll_payload(question, perm, 0, 10)
        assert 1 <= len(p.question) <= config.POLL_QUESTION_MAX
        assert len(p.options) == n and len(set(p.options)) == n
        assert all(1 <= len(o) <= config.POLL_OPTION_MAX for o in p.options)
        assert opts[perm[p.correct_option_id]] == opts[question["correct_index"]]
        if any(len(o) > config.POLL_OPTION_MAX for o in opts):
            assert p.compact and p.options == [f"({c})" for c in P.LETTERS[:n]]
            full = "".join(p.full_text_chunks)
            assert all(o.strip() in full for o in opts)          # complete original text sent
        fb = engine.compact_fallback(question, perm, 0, 10)
        assert all(len(o) <= config.POLL_OPTION_MAX for o in fb.options)


# ============================================================ stress / regression
def _big_paper(seed, headings=True, expl=True, keys=None):
    """3 sections × 100 questions: statements, List-I/II with numbered List-II,
    numbered explanation points — numbering restarts in every section."""
    rng = random.Random(seed)
    parts, answers = [], []
    for s, sec in enumerate("अबस"):
        if headings:
            parts.append(f"भाग-{sec}")
        for n in range(1, 101):
            kind = n % 4
            if kind == 0:
                stem = f"{n}. निम्न कथनों पर विचार करें:\n1. कथन एक\n2. कथन दो\n3. कथन तीन\nसही कूट चुनें"
            elif kind == 1:
                stem = (f"{n}. सुमेलित करें\nसूची-I\n(A) क\n(B) ख\n(C) ग\n(D) घ\nसूची-II\n1. p\n2. q\n"
                        "3. r\n4. s\nकूट:")
            else:
                stem = f"प्रश्न {n}. साधारण प्रश्न संख्या {n} (भाग {sec})?"
            a = rng.choice("ABCD")
            answers.append((s, n, a))
            body = stem + "\n(A) a\n(B) b\n(C) c\n(D) d"
            if keys is None:
                body += "\nउत्तर: " + a
                if expl:
                    body += "\nव्याख्या:\n1. बिंदु एक\n2. बिंदु दो"
            parts.append(body)
        if keys == "per_section":
            parts.append("उत्तरमाला\n" + ", ".join(f"{n}-{a}" for ss, n, a in answers if ss == s))
    if keys == "end":
        parts.append("उत्तरमाला\n" + ", ".join(f"{n}-{a}" for _, n, a in answers))
    return "\n".join(parts), answers


@pytest.mark.parametrize("kw", [
    {}, {"headings": False}, {"headings": False, "expl": False},
    {"keys": "per_section"}, {"keys": "per_section", "headings": False}, {"keys": "end"},
    {"keys": "end", "headings": False},
])
def test_large_restarting_paper_every_answer_exact(kw):
    txt, answers = _big_paper(7, **kw)
    res = parse(txt)
    assert res.errors == [], [str(e) for e in res.errors[:5]]
    assert len(res.sections) == 3
    assert [(x.section, x.number, P.LETTERS[x.correct_index]) for x in res.questions] == answers


# ============================================================ UTF-16 lengths (Telegram counting)
def test_tg_len_and_split_message_count_utf16_units_losslessly():
    import quiz_engine as E
    assert E.tg_len("abc") == 3 and E.tg_len("प्रश्न") == len("प्रश्न")
    assert E.tg_len("𝑥🙂") == 4
    text = ("🙂" * 3000 + "\n") * 3 + "अंत"
    chunks = E.split_message(text, 4000)
    assert "".join(chunks) == text                                   # nothing lost
    assert all(1 <= E.tg_len(c) <= 4000 for c in chunks)            # Telegram-safe
    q = {"question": "🙂" * 150, "options": ["𝑥" * 50, "𝑥" * 51], "correct_index": 1}
    p = E.build_poll_payload(q, [0, 1], 0, 1)
    assert p.compact and p.full_text_chunks and E.tg_len(p.question) <= 300
    assert "🙂" * 150 in "".join(p.full_text_chunks) and "𝑥" * 51 in "".join(p.full_text_chunks)
