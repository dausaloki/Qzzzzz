"""Parser tests: every supported format, answer keys, error reporting, PDFs."""
from pathlib import Path

import pytest

import pdf_parser as P

FIX = Path(__file__).parent / "fixtures"
FONT = Path.home() / "fonts" / "NotoSansDevanagari.ttf"


def parse(text):
    return P.parse_source(text)


def by_num(res):
    return {q.number: q for q in res.questions}


# ------------------------------------------------------------ fixtures
def test_all_formats_hindi_fixture():
    res = parse((FIX / "all_formats_hi.txt").read_text(encoding="utf-8"))
    assert res.errors == []
    qs = by_num(res)
    assert sorted(qs) == [1, 2, 3, 4, 5, 6]
    # statement based: statements stay inside the question
    assert qs[1].qtype == "statement"
    assert "1. यह क्षेत्रफल की दृष्टि से भारत का सबसे बड़ा राज्य है।" in qs[1].question
    assert "4. इसका गठन 1 नवंबर 1956 को पूर्ण हुआ।" in qs[1].question
    assert qs[1].options == ["केवल 1, 2 और 4", "केवल 1 और 3", "केवल 2 और 3", "सभी कथन सही हैं"]
    assert qs[1].explanation.startswith("गुरु शिखर")
    # matching: List-I (A)-(D) data stays in question; real options after कूट
    assert "(A) रामदेवजी" in qs[2].question and "(D) पाबूजी" in qs[2].question
    assert qs[2].question.rstrip().endswith("कूट:")
    assert qs[2].options[0] == "A-III, B-II, C-IV, D-I"
    assert qs[2].correct_index == 0
    # assertion reason: options containing "(A)" text are preserved exactly
    assert qs[3].qtype == "assertion_reason"
    assert qs[3].options[0] == "(A) और (R) दोनों सही हैं तथा (R), (A) की सही व्याख्या है।"
    assert qs[3].options[3] == "(A) गलत है, परन्तु (R) सही है।"
    # ordering: A-D data in question, answer options after the prompt line
    assert "(C) उत्तर प्रदेश" in qs[4].question
    assert "नीचे दिए गए विकल्पों में से सबसे उपयुक्त उत्तर चुनें—" in qs[4].question
    assert qs[4].options == ["A, C, D, B", "A, D, C, B", "C, A, D, B", "A, C, B, D"]
    # inline options on one line + "उत्तर – A"
    assert qs[5].question == "हल्दीघाटी का युद्ध किस वर्ष हुआ था?"
    assert qs[5].options == ["1576", "1526", "1556", "1605"]
    # List-I/List-II with numbered List-II (must not become new questions)
    assert qs[6].qtype == "list_matching"
    assert "4. जयपुर" in qs[6].question
    assert qs[6].options[1] == "A-2, B-1, C-4, D-3"


def test_weak_numbering_and_answer_key_section():
    res = parse((FIX / "weak_numbers_key.txt").read_text(encoding="utf-8"))
    assert res.errors == []
    qs = by_num(res)
    assert sorted(qs) == [1, 2, 3, 4, 5]
    assert qs[1].question.startswith("निम्नलिखित कथनों पर विचार कीजिए:")
    assert "1. चंबल नदी" in qs[1].question and "3. बनास" in qs[1].question
    assert "4. बीकानेर" in qs[2].question
    assert qs[3].options == ["गोडावण", "मोर", "तीतर", "कबूतर"]
    assert qs[4].options == ["खेजड़ी", "रोहिड़ा", "नीम", "पीपल"]
    assert qs[4].explanation.startswith("खेजड़ी को राज्य वृक्ष")
    assert qs[5].options[3] == "सिंह"          # option text on the next line
    assert all(q.answer_source == "answer_key" for q in res.questions)


# ------------------------------------------------------------ markers
@pytest.mark.parametrize("marker", ["प्रश्न 1.", "1.", "1)", "Q1", "Q.1", "Q1.", "Q 1)", "Question 1:", "प्रश्न-1"])
def test_question_marker_styles(marker):
    text = f"""{marker} भारत की राजधानी क्या है?
(A) मुंबई
(B) नई दिल्ली
(C) कोलकाता
(D) चेन्नई
उत्तर: (B)
"""
    res = parse(text)
    assert res.errors == [], res.errors
    assert len(res.questions) == 1
    q = res.questions[0]
    assert q.number == 1 and q.question == "भारत की राजधानी क्या है?" and q.correct_index == 1


@pytest.mark.parametrize("ans,idx", [("उत्तर: (A)", 0), ("उत्तर: A", 0), ("Answer: B", 1), ("Ans. (C)", 2),
                                     ("उत्तर – D", 3), ("सही उत्तर: (C)", 2), ("Correct Answer: B", 1),
                                     ("उत्तर (D)", 3), ("answer - c", 2)])
def test_inline_answer_styles(ans, idx):
    text = f"1. Capital of Rajasthan?\n(A) Jaipur\n(B) Jodhpur\n(C) Udaipur\n(D) Ajmer\n{ans}\n"
    res = parse(text)
    assert res.errors == [], res.errors
    assert res.questions[0].correct_index == idx


def test_option_styles_latin_lower_and_hindi():
    text = """1. Q one?
a) w
b) x
c) y
d) z
Answer: c
2. प्रश्न दो?
(क) एक
(ख) दो
(ग) तीन
(घ) चार
उत्तर: (ख)
3. Q three?
A. alpha
B. beta
C. gamma
D. delta
Answer: D
"""
    res = parse(text)
    assert res.errors == []
    qs = by_num(res)
    assert qs[1].options == ["w", "x", "y", "z"] and qs[1].correct_index == 2
    assert qs[2].options == ["एक", "दो", "तीन", "चार"] and qs[2].correct_index == 1
    assert qs[3].correct_index == 3


def test_numeric_options_fallback():
    text = """1. सबसे बड़ा जिला?
(1) जैसलमेर
(2) बाड़मेर
(3) बीकानेर
(4) जोधपुर
उत्तर: (1)
"""
    res = parse(text)
    assert res.errors == []
    assert res.questions[0].options[0] == "जैसलमेर" and res.questions[0].correct_index == 0


def test_multiline_options_are_joined_not_lost():
    text = """1. Choose the correct statement.
(A) The Aravalli range is one of the oldest fold
mountains in the world
(B) Thar desert
(C) Chambal
(D) Luni river flows into
the Rann of Kutch
Answer: A
"""
    res = parse(text)
    assert res.errors == []
    q = res.questions[0]
    assert q.options[0] == "The Aravalli range is one of the oldest fold mountains in the world"
    assert q.options[3] == "Luni river flows into the Rann of Kutch"


def test_trailing_answer_on_option_line_and_options_after_lead_in():
    text = """1. राज्य पुष्प?
कूट: (A) रोहिड़ा (B) गुलाब (C) कमल (D) गेंदा उत्तर: A
"""
    res = parse(text)
    assert res.errors == [], res.errors
    q = res.questions[0]
    assert q.options == ["रोहिड़ा", "गुलाब", "कमल", "गेंदा"] and q.correct_index == 0
    assert q.question.endswith("कूट:")


def test_answer_given_as_option_text():
    text = "1. Year of Haldighati?\n(A) 1576\n(B) 1526\n(C) 1556\n(D) 1605\nउत्तर: 1576\n"
    res = parse(text)
    assert res.errors == [] and res.questions[0].correct_index == 0


# ------------------------------------------------------------ answer keys
def test_answer_key_variants():
    body = "".join(f"{i}. Question {i}?\n(A) a{i}\n(B) b{i}\n(C) c{i}\n(D) d{i}\n" for i in range(1, 7))
    for key in ["Answer Key\n1-A, 2-B, 3-C\n4-D 5-A 6-B",
                "उत्तर कुंजी:\n1. (A)\n2. (B)\n3. (C)\n4. (D)\n5. (A)\n6. (B)",
                "ANSWERS\n1 A\n2 B\n3 C\n4 D\n5 A\n6 B",
                "उत्तरमाला\n1-क 2-ख 3-ग 4-घ 5-क 6-ख",
                "Answer Key\n1\nA\n2\nB\n3\nC\n4\nD\n5\nA\n6\nB",  # table extracted vertically
                "1-A 2-B 3-C 4-D 5-A 6-B"]:                         # headerless trailing key
        res = parse(body + "\n" + key)
        assert res.errors == [], (key, res.errors)
        assert [q.correct_index for q in res.questions] == [0, 1, 2, 3, 0, 1], key


def test_old_style_numbered_answer_lines():
    text = "1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\n2. R?\n(A) a\n(B) b\n(C) c\n(D) d\n1. उत्तर: C\n2. Answer: (D)\n"
    res = parse(text)
    assert res.errors == []
    assert [q.correct_index for q in res.questions] == [2, 3]


# ------------------------------------------------------------ never guess / errors
def test_missing_answer_is_error_not_guess():
    text = "1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\n2. R?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: B\n"
    res = parse(text)
    assert [q.number for q in res.questions] == [2]
    assert len(res.errors) == 1 and res.errors[0].number == 1
    assert "सही उत्तर नहीं मिला" in res.errors[0].message


def test_incomplete_options_reported_with_number():
    # a question with only A-C is a valid 3-option question now, but in a
    # 4-option paper it must be flagged for review; if its answer is D it is
    # an error with the question number.
    text = ("1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: A\n"
            "2. R?\n(A) a\n(B) b\n(C) c\nउत्तर: A\n"
            "3. S?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: C\n"
            "4. T?\n(A) a\n(B) b\n(C) c\nउत्तर: D\n")
    res = parse(text)
    assert [e.number for e in res.errors] == [4] and "केवल 3 options" in res.errors[0].message
    assert [q.number for q in res.questions] == [1, 2, 3]
    assert len(res.questions[1].options) == 3
    assert res.review and "प्रश्न 2 (3)" in res.review[0]


def test_conflicting_inline_and_key():
    text = "1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: A\n\nAnswer Key\n1-B\n"
    res = parse(text)
    assert res.questions == [] and res.errors[0].number == 1 and "अलग" in res.errors[0].message


def test_conflicting_key_entries():
    text = "1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\n\nAnswer Key\n1-B\n1-C\n"
    res = parse(text)
    assert res.questions == [] and res.errors[0].number == 1


def test_five_options_are_kept_never_dropped():
    text = ("1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\n(E) e\nउत्तर: A\n"
            "2. R?\n(A) a\n(B) b\n(C) c\n(D) d\n(E) अनुत्तरित प्रश्न\nउत्तर: B\n")
    res = parse(text)
    assert res.errors == []
    assert res.questions[0].options == ["a", "b", "c", "d", "e"]
    assert res.questions[1].options == ["a", "b", "c", "d", "अनुत्तरित प्रश्न"]


def test_gap_in_numbering_reported():
    text = ("1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: A\n"
            "3. S?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: C\n")
    res = parse(text)
    assert any(e.number == 2 for e in res.errors)


def test_answer_key_number_without_question_is_error():
    text = "1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\n\nAnswer Key\n1-A 2-B 3-C\n"
    res = parse(text)
    assert {e.number for e in res.errors} == {2, 3}


def test_empty_and_garbage_input():
    assert parse("").errors
    res = parse("यह सिर्फ एक साधारण वाक्य है।\nकोई प्रश्न नहीं।")
    assert res.questions == [] and res.errors and res.errors[0].number is None


# ------------------------------------------------------------ robustness
def test_explanation_with_numbered_points_does_not_split():
    text = """1. प्रश्न एक?
(A) a
(B) b
(C) c
(D) d
उत्तर: A
व्याख्या:
2. यह बिंदु दो है
3. यह बिंदु तीन है
2. प्रश्न दो?
(A) w
(B) x
(C) y
(D) z
उत्तर: D
"""
    res = parse(text)
    assert res.errors == [], res.errors
    qs = by_num(res)
    assert "2. यह बिंदु दो है" in qs[1].explanation
    assert qs[2].question == "प्रश्न दो?" and qs[2].correct_index == 3


def test_instruction_preamble_numbers_ignored():
    text = """निर्देश:
1. सभी प्रश्न अनिवार्य हैं।
2. प्रत्येक प्रश्न 1 अंक का है।
1. पहला प्रश्न?
(A) a
(B) b
(C) c
(D) d
2. दूसरा प्रश्न?
(A) a
(B) b
(C) c
(D) d
Answer Key: 
1-A 2-B
"""
    res = parse(text)
    assert res.errors == [], res.errors
    assert [q.question for q in res.questions] == ["पहला प्रश्न?", "दूसरा प्रश्न?"]


def test_paper_starting_mid_way_with_statements():
    text = """51. निम्न कथनों पर विचार करें:
1. कथन एक
2. कथन दो
(A) केवल 1
(B) केवल 2
(C) दोनों
(D) कोई नहीं
उत्तर: C
52. दूसरा?
(A) a
(B) b
(C) c
(D) d
उत्तर: A
"""
    res = parse(text)
    assert res.errors == [], res.errors
    qs = by_num(res)
    assert sorted(qs) == [51, 52]
    assert "1. कथन एक" in qs[51].question


def test_decimal_numbers_and_question_starting_with_uttar():
    text = """1. उत्तर प्रदेश का क्षेत्रफल कितना है?
(A) 2.40 लाख वर्ग किमी
(B) 3.42 लाख वर्ग किमी
(C) 1.5 लाख वर्ग किमी
(D) 3.08 लाख वर्ग किमी
उत्तर: (A)
2. उत्तर-पश्चिम भारत का सबसे बड़ा राज्य?
(A) राजस्थान
(B) पंजाब
(C) हरियाणा
(D) गुजरात
उत्तर: A
"""
    res = parse(text)
    assert res.errors == [], res.errors
    qs = by_num(res)
    assert qs[1].question == "उत्तर प्रदेश का क्षेत्रफल कितना है?"
    assert qs[2].question == "उत्तर-पश्चिम भारत का सबसे बड़ा राज्य?"
    assert qs[1].options[2] == "1.5 लाख वर्ग किमी"


def test_haldighati_not_treated_as_explanation():
    text = "1. हल्दीघाटी युद्ध?\n(A) 1576\n(B) 1526\n(C) 1556\n(D) 1605\nउत्तर: A\n"
    res = parse(text)
    assert res.questions[0].question == "हल्दीघाटी युद्ध?"


def test_long_question_and_long_options_preserved_exactly():
    long_q = "यह एक बहुत लंबा प्रश्न है। " * 40
    long_o = "यह एक बहुत लंबा विकल्प है जो सौ अक्षरों से अधिक है " * 4
    text = f"1. {long_q.strip()}\n(A) {long_o.strip()}\n(B) छोटा\n(C) छोटा दो\n(D) छोटा तीन\nउत्तर: A\n"
    res = parse(text)
    assert res.errors == []
    assert res.questions[0].question == long_q.strip()
    assert res.questions[0].options[0] == long_o.strip()
    assert len(res.questions[0].options[0]) > 100


def test_more_than_100_questions_warns():
    body = "".join(f"{i}. Q{i}?\n(A) a\n(B) b\n(C) c\n(D) d\nAnswer: A\n" for i in range(1, 106))
    res = parse(body)
    assert res.errors == [] and len(res.questions) == 105
    assert any("अधिकतम" in w for w in res.warnings)


def test_backward_compatible_parse_text_api():
    good = "1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: A\n"
    out = P.parse_text(good)
    assert out[0]["correct_index"] == 0 and out[0]["options"] == ["a", "b", "c", "d"]
    with pytest.raises(ValueError):
        P.parse_text("1. Q?\n(A) a\n(B) b\n(C) c\n(D) d\n")


def test_split_question_and_options_for_manual_input():
    q, opts = P.split_question_and_options("सुमेलित कीजिए\n(A) x\n(B) y\n(C) z\n(D) w\nकूट:\n"
                                           "(A) A-1, B-2\n(B) A-2, B-1\n(C) A-3\n(D) A-4")
    assert q.endswith("कूट:") and "(A) x" in q
    assert opts == ["A-1, B-2", "A-2, B-1", "A-3", "A-4"]


# ------------------------------------------------------------ files
def _make_pdf(path: Path, text: str):
    """Write text with per-script font runs (Devanagari → Noto, rest → Helvetica)."""
    import re as _re
    import fitz
    deva = fitz.Font(fontfile=str(FONT))
    helv = fitz.Font("helv")
    doc = fitz.open()
    lines = text.split("\n")
    per_page = 45
    for i in range(0, len(lines), per_page):
        page = doc.new_page()
        page.insert_font(fontname="deva", fontfile=str(FONT))
        y = 40
        for ln in lines[i:i + per_page]:
            x = 40
            for run in _re.findall(r"[\u0900-\u097F\u200c\u200d]+|[^\u0900-\u097F]+", ln):
                is_deva = bool(_re.match(r"[\u0900-\u097F]", run))
                page.insert_text((x, y), run, fontname="deva" if is_deva else "helv", fontsize=10)
                x += (deva if is_deva else helv).text_length(run, fontsize=10)
            y += 16
    doc.save(str(path))


@pytest.mark.skipif(not FONT.exists(), reason="Devanagari font not available")
def test_pdf_english_and_hindi_roundtrip(tmp_path):
    src = (FIX / "all_formats_hi.txt").read_text(encoding="utf-8")
    pdf = tmp_path / "paper.pdf"
    _make_pdf(pdf, src)
    res = P.parse_pdf_result(pdf)
    assert res.errors == [], res.errors
    assert len(res.questions) == 6
    # bytes input too
    res2 = P.parse_pdf_result(pdf.read_bytes())
    assert len(res2.questions) == 6
    assert by_num(res2)[2].options[0] == "A-III, B-II, C-IV, D-I"


def test_pdf_errors(tmp_path):
    import fitz
    blank = tmp_path / "blank.pdf"
    d = fitz.open()
    d.new_page()
    d.save(str(blank))
    with pytest.raises(P.SourceError, match="selectable text"):
        P.extract_pdf_text(blank)
    with pytest.raises(P.SourceError):
        P.extract_pdf_text(b"this is not a pdf")


def test_text_decoding():
    assert P.decode_text_bytes("प्रश्न".encode("utf-8-sig")) == "प्रश्न"
    assert P.decode_text_bytes("प्रश्न".encode("utf-16")) == "प्रश्न"
    with pytest.raises(P.SourceError):
        P.decode_text_bytes(b"\xff\xfe\xfa" + b"\x80" * 3 if False else b"\xc3\x28\xa0\xa1")
