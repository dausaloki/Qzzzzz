"""Universal PDF/TXT parser round: every question format, passages/tables,
promotion removal, shifts + section-wise keys, figure pages, OCR spacing,
import report, passage storage/playback and capacity (1200 → 3 parts).

Nothing here is tied to one PDF layout: sources are generated with varied
styles (a-d / A-D / (1)-(4), same-line / multi-line options, 1 or 2 columns)."""
from collections import Counter

import pytest

import config
import database as db
import pdf_extract as X
import pdf_parser as P
import quiz_creator as creator
import quiz_engine as engine
import pdfgen as G
from test_bot_flow import Harness, buttons, run

needs_font = pytest.mark.skipif(not G.have_font(), reason="Devanagari font not available")
OCR_OK, OCR_INFO = X.ocr_status()
needs_ocr = pytest.mark.skipif(not OCR_OK, reason=f"OCR data not available: {OCR_INFO}")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "quiz.db")
    db.init_db()
    monkeypatch.setattr(config, "NEXT_QUESTION_DELAY", 0.05)


def one(src):
    res = P.parse_source(src)
    assert res.errors == [], res.errors
    assert len(res.questions) == 1
    return res.questions[0]


# ------------------------------------------------------------------ formats
FORMATS = {
    "normal_mcq": ("""1. राजस्थान की राजधानी कौन सी है?
(a) जोधपुर (b) जयपुर (c) उदयपुर (d) अजमेर
उत्तर: (b)""", "mcq", 4, 1),
    "five_options": ("""1. राजस्थान का राज्य वृक्ष?
(A) नीम
(B) खेजड़ी
(C) आम
(D) पीपल
(E) इनमें से कोई नहीं
उत्तर: B""", "mcq", 5, 1),
    "six_options_inline": ("""1. Q?
(a) a (b) b (c) c (d) d (e) e (f) सभी
Ans. (f)""", "mcq", 6, 5),
    "statement": ("""1. निम्नलिखित कथनों पर विचार कीजिए:
1. माउंट आबू अरावली में है।
2. गुरु शिखर सर्वोच्च चोटी है।
उपर्युक्त में से कौन-सा/से कथन सही है/हैं?
(A) केवल 1 (B) केवल 2 (C) 1 और 2 दोनों (D) न तो 1 न ही 2
उत्तर: C""", "statement", 4, 2),
    "assertion_reason": ("""1. अभिकथन (A): थार मरुस्थल में वर्षा कम होती है।
कारण (R): अरावली मानसूनी पवनों के समानांतर है।
(A) A और R दोनों सही हैं तथा R, A की सही व्याख्या है
(B) A और R दोनों सही हैं परन्तु R, A की सही व्याख्या नहीं है
(C) A सही है परन्तु R गलत है
(D) A गलत है परन्तु R सही है
उत्तर: A""", "assertion_reason", 4, 0),
    "list_matching": ("""1. सूची-I को सूची-II से सुमेलित कीजिए:
सूची-I      सूची-II
A. सांभर    1. जयपुर
B. पिछोला   2. उदयपुर
कूट:
(a) A-1, B-2 (b) A-2, B-1 (c) A-1, B-1 (d) A-2, B-2
उत्तर: (a)""", "list_matching", 4, 0),
    "column_matching": ("""1. Column-I को Column-II से सुमेलित कीजिए:
Column-I      Column-II
A. Sambhar    1. Jaipur
B. Pichola    2. Udaipur
Code:
(a) A-1, B-2
(b) A-2, B-1
(c) A-1, B-1
(d) A-2, B-2
Answer: a""", "list_matching", 4, 0),
    "ordering": ("""1. निम्नलिखित को कालानुक्रम में व्यवस्थित कीजिए:
1. हल्दीघाटी 2. खानवा 3. तराइन
(A) 3, 2, 1 (B) 1, 2, 3 (C) 2, 3, 1 (D) 3, 1, 2
उत्तर: A""", "ordering", 4, 0),
    "true_false": ("""1. राजस्थान भारत का सबसे बड़ा राज्य है।
(A) सत्य
(B) असत्य
उत्तर: A""", "true_false", 2, 0),
    "except": ("""1. निम्नलिखित में से कौन-सा राजस्थान का जिला नहीं है?
(A) जयपुर (B) अजमेर (C) इंदौर (D) कोटा
उत्तर: C""", "negative", 4, 2),
    "all_of_the_above": ("""1. Q?
(A) x (B) y (C) z (D) उपर्युक्त सभी
उत्तर: D""", "mcq", 4, 3),
    "numeric_labels": ("""1. राजस्थान में कितने संभाग हैं?
(1) 6 (2) 7 (3) 10 (4) 33
उत्तर: (3)""", "mcq", 4, 2),
}


@pytest.mark.parametrize("name", list(FORMATS))
def test_question_formats(name):
    src, qtype, nopt, ci = FORMATS[name]
    q = one(src)
    assert q.qtype == qtype, (name, q.qtype)
    assert len(q.options) == nopt and q.correct_index == ci
    # source wording kept exactly: every source line of the stem appears verbatim
    first = src.split("\n")[0].split(". ", 1)[1]
    assert q.question.startswith(first)


def test_q_answer_explanation_together():
    res = P.parse_source("""1. राजस्थान का राज्य पक्षी?
(A) गोडावण (B) मोर (C) तोता (D) कबूतर
उत्तर: A
व्याख्या: गोडावण को 1981 में राज्य पक्षी घोषित किया गया।
2. राज्य वृक्ष?
(A) खेजड़ी (B) नीम (C) पीपल (D) आम
उत्तर: A
व्याख्या: खेजड़ी 1983 में।""")
    assert res.errors == []
    assert [q.explanation for q in res.questions] == ["गोडावण को 1981 में राज्य पक्षी घोषित किया गया।",
                                                     "खेजड़ी 1983 में।"]
    assert all(q.answer_source == "inline" for q in res.questions)


def test_multiple_correct_is_reported_never_guessed():
    res = P.parse_source("""1. Q?
(A) a (B) b (C) c (D) d
उत्तर: A, C""")
    assert res.questions == []
    assert len(res.errors) == 1 and res.errors[0].number == 1
    msg = str(res.errors[0])
    assert "एक से अधिक" in msg and "अनुमान" in msg and "import नहीं" in msg


# -------------------------------------------------------- passages / tables
PASSAGE_SRC = """निम्नलिखित गद्यांश को पढ़कर प्रश्न 1-2 के उत्तर दीजिए:
राजस्थान क्षेत्रफल की दृष्टि से भारत का सबसे बड़ा राज्य है।
इसका क्षेत्रफल 3,42,239 वर्ग किमी है।
1. सबसे बड़ा राज्य?
(A) राजस्थान (B) गोवा
उत्तर: A
2. क्षेत्रफल?
(A) 3,42,239 वर्ग किमी (B) 2,00,000 वर्ग किमी
उत्तर: A
3. असंबद्ध प्रश्न?
(A) x (B) y
उत्तर: B
निम्नलिखित तालिका का अध्ययन कर प्रश्न 4-5 के उत्तर दीजिए:
वर्ष      उत्पादन
2020      40%
2021      55%
4. 2021 में उत्पादन?
(A) 40% (B) 55%
उत्तर: B
5. वृद्धि?
(A) 15% (B) 20%
उत्तर: A"""


def test_passage_and_table_attached_exactly_to_their_questions():
    res = P.parse_source(PASSAGE_SRC)
    assert res.errors == [], res.errors
    by = {q.number: q for q in res.questions}
    assert [q.number for q in res.questions] == [1, 2, 3, 4, 5]
    p1 = PASSAGE_SRC.split("\n1. ")[0]
    assert by[1].pre_text == p1 and by[2].pre_text == p1           # verbatim, before Q1
    assert by[3].pre_text == ""
    assert by[4].pre_text.startswith("निम्नलिखित तालिका") and "2021      55%" in by[4].pre_text
    assert by[5].pre_text == by[4].pre_text
    # the passage text is not merged into any question/explanation
    for q in res.questions:
        assert "गद्यांश" not in q.question + q.explanation and "तालिका" not in q.explanation
    assert by[3].explanation == ""


def test_passage_range_with_unreadable_question_is_reported():
    src = PASSAGE_SRC.replace("2. क्षेत्रफल?\n(A) 3,42,239 वर्ग किमी (B) 2,00,000 वर्ग किमी\nउत्तर: A\n", "")
    res = P.parse_source(src)
    assert any("1–2" in str(e) and "2" in str(e) for e in res.errors), res.errors


# ------------------------------------------------------------- promotion
def test_promotion_lines_removed_and_reported():
    res = P.parse_source("""1. राजस्थान की राजधानी?
(A) जयपुर (B) अजमेर
Join Telegram @RajGKQuiz for more
उत्तर: B
2. Telegram के संस्थापक कौन हैं?
(A) Pavel Durov (B) Elon Musk
https://t.me/RajGKQuiz
उत्तर: A
3. www.rajasthan.gov.in किस राज्य की वेबसाइट है?
(A) राजस्थान (B) गुजरात
उत्तर: A""")
    assert res.errors == []
    q1, q2, q3 = res.questions
    assert q1.options == ["जयपुर", "अजमेर"] and q2.options == ["Pavel Durov", "Elon Musk"]
    assert q2.question == "Telegram के संस्थापक कौन हैं?"               # a question ABOUT Telegram stays
    assert q3.question.startswith("www.rajasthan.gov.in")                # a URL inside a question stays
    assert [t for k, t in res.removed if k == "promotion"] == ["Join Telegram @RajGKQuiz for more",
                                                                "https://t.me/RajGKQuiz"]
    assert any("promotion" in w for w in res.warnings)


# ---------------------------------------------- shifts + section-wise keys
def _shift_src(order=("शिफ्ट-1", "शिफ्ट-2")):
    keys = {"शिफ्ट-1": "1. A 2. B", "शिफ्ट-2": "1. D 2. C"}
    body = ""
    for s in ("शिफ्ट-1", "शिफ्ट-2"):
        body += s + "\n" + "".join(f"{n}. {s} Q{n}?\n(A) a (B) b (C) c (D) d\n" for n in (1, 2))
    body += "उत्तर कुंजी\n" + "".join(f"{s}\n{keys[s]}\n" for s in order)
    return body


@pytest.mark.parametrize("order", [("शिफ्ट-1", "शिफ्ट-2"), ("शिफ्ट-2", "शिफ्ट-1")])
def test_shift_sections_map_answer_key_by_label_and_number(order):
    res = P.parse_source(_shift_src(order))
    assert res.errors == [], res.errors
    got = [(q.section_label, q.number, q.correct_index) for q in res.questions]
    assert got == [("शिफ्ट-1", 1, 0), ("शिफ्ट-1", 2, 1), ("शिफ्ट-2", 1, 3), ("शिफ्ट-2", 2, 2)]
    for q in res.questions:
        assert "शिफ्ट" not in " ".join(q.options)


def test_repeated_numbering_ambiguous_key_is_not_guessed():
    src = ("1. Q1?\n(A) a (B) b\n2. Q2?\n(A) a (B) b\n1. R1?\n(A) a (B) b\n2. R2?\n(A) a (B) b\n"
           "3. R3?\n(A) a (B) b\nउत्तर कुंजी\n1. A 2. B 3. A\n")
    res = P.parse_source(src)
    # numbers 1 and 2 exist in both parts but the key has them once → never mapped by number alone
    bad = {(e.section, e.number) for e in res.errors}
    assert len(bad) >= 2
    for q in res.questions:
        assert not (q.number in (1, 2) and q.answer_source == "answer_key" and q.section_label == "भाग 1"
                    and any(e.number == q.number for e in res.errors))


# ------------------------------------------------------ 2-column + layout
@needs_font
def test_two_column_pdf_header_footer_promo_and_page_continuation(tmp_path):
    left1 = "\n".join(["1. राजस्थान की राजधानी?", "(A) जयपुर", "(B) अजमेर", "(C) कोटा", "(D) बीकानेर",
                       "उत्तर: A", "2. राज्य पशु (पशुधन श्रेणी)?", "(A) ऊँट", "(B) गाय"])
    right1 = "\n".join(["(C) भैंस", "(D) बकरी", "उत्तर: A", "Join Telegram @RajGKQuiz",
                        "3. सबसे लंबी नदी जो पूरी तरह", "राजस्थान में बहती है?"])
    left2 = "\n".join(["(A) लूनी", "(B) बनास", "(C) चंबल", "(D) माही", "उत्तर: B"])
    right2 = "\n".join(["4. थार मरुस्थल का विस्तार?", "(A) पश्चिम", "(B) पूर्व", "(C) उत्तर", "(D) दक्षिण",
                        "उत्तर: A"])
    pdf = tmp_path / "two.pdf"
    G.make_two_column_pdf(pdf, [("", left1, right1), ("", left2, right2)],
                          header="राजस्थान भूगोल MCQ", footer_fmt="Page {n}")
    res = P.parse_pdf_result(pdf)
    assert res.errors == [], res.errors
    assert [q.number for q in res.questions] == [1, 2, 3, 4]
    q2, q3 = res.questions[1], res.questions[2]
    assert q2.options == ["ऊँट", "गाय", "भैंस", "बकरी"]                    # continues into column 2
    assert q3.question == "सबसे लंबी नदी जो पूरी तरह\nराजस्थान में बहती है?"   # page break ≠ boundary
    assert q3.options == ["लूनी", "बनास", "चंबल", "माही"] and q3.correct_index == 1
    for q in res.questions:
        blob = q.question + " ".join(q.options)
        assert "Telegram" not in blob and "Page" not in blob and "भूगोल MCQ" not in blob


# ------------------------------------------------------------ figure pages
@needs_font
def test_figure_question_on_image_page_needs_verification(tmp_path):
    import fitz
    src = ["1. दिए गए मानचित्र में चिह्नित जिला कौन सा है?", "(A) जयपुर", "(B) अजमेर", "(C) कोटा",
           "(D) टोंक", "उत्तर: A", "2. राजस्थान की राजधानी?", "(A) जयपुर", "(B) अजमेर", "उत्तर: A"]
    for with_img in (True, False):
        pdf = tmp_path / f"fig{with_img}.pdf"
        G.make_text_pdf(pdf, "\n".join(src))
        if with_img:
            doc = fitz.open(pdf)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 150), 0)
            pix.clear_with(200)
            doc[0].insert_image(fitz.Rect(300, 300, 500, 450), stream=pix.tobytes("jpeg"))
            doc.saveIncr()
            doc.close()
        res = P.parse_pdf_result(pdf)
        assert res.errors == [] and len(res.questions) == 2
        q1, q2 = res.questions
        assert bool(q1.flags) is with_img and q2.flags == []
        assert any("चित्र" in r for r in res.review) is with_img


# ------------------------------------------------------------- OCR spacing
def test_ocr_spacing_joins_only_provable_breaks():
    lex = Counter({"राजस्थान": 5, "की": 9, "राजधानी": 3, "जयपुर": 4, "है": 8})
    txt, fx = X.normalize_ocr_spacing("रा जस्था न की राजधानी जयपुर है", lex)
    assert txt == "राजस्थान की राजधानी जयपुर है" and fx == [("रा जस्था न", "राजस्थान")]
    # a fragment starting with a vowel sign can never start a word
    txt, fx = X.normalize_ocr_spacing("राजस्था न ि", Counter())
    assert txt == "राजस्था नि"
    txt, fx = X.normalize_ocr_spacing("भार त", Counter())
    assert txt == "भार त" and fx == []                                  # unknown word → untouched
    # two real words are never glued, English/numbers untouched
    txt, fx = X.normalize_ocr_spacing("की है Raj asthan 12 34", Counter({"कीहै": 5, "की": 5, "है": 5}))
    assert txt == "की है Raj asthan 12 34" and fx == []


@needs_font
@needs_ocr
def test_scanned_pdf_ocr_keeps_clean_text_and_asks_for_verification(tmp_path):
    src = ("1. राजस्थान की राजधानी कौन सी है?\n(A) जयपुर\n(B) अजमेर\n(C) कोटा\n(D) उदयपुर\nउत्तर: A\n"
           "2. राजस्थान का राज्य वृक्ष कौन सा है?\n(A) नीम\n(B) खेजड़ी\n(C) आम\n(D) पीपल\nउत्तर: B")
    pdf = tmp_path / "scan.pdf"
    G.make_scanned_pdf(pdf, src)
    res = P.parse_pdf_result(pdf)
    assert res.ocr_pages == [1]
    assert any("OCR" in r for r in res.review)
    # every question is either parsed with the RIGHT answer or reported — never guessed
    right = {1: 0, 2: 1}
    parsed = {q.number: q for q in res.questions}
    failed = {e.number for e in res.errors}
    assert set(parsed) | failed == {1, 2} and not (set(parsed) & failed)
    assert all(q.correct_index == right[n] for n, q in parsed.items())
    assert 1 in parsed and parsed[1].options == ["जयपुर", "अजमेर", "कोटा", "उदयपुर"]
    rep = P.import_report(res)
    assert rep["verification_required"] == len(parsed) and rep["failed"] == len(failed)
    assert X.extract_pdf(pdf).space_fixes == []            # clean scan: no word was re-joined


# ------------------------------------------------------------ import report
def test_import_report_counts_and_problem_list():
    src = ("1. Q1?\n(A) a (B) b\nउत्तर: A\n2. Q2?\n(A) a (B) b\n3. Q3?\n(A) a (B) b\n"
           "उत्तर कुंजी\n3. B\n")
    res = P.parse_source(src)
    rep = P.import_report(res)
    assert rep["parsed"] == 2 and rep["failed"] == 1 and rep["total_detected"] == 3
    assert rep["answer_inline"] == 1 and rep["answer_key"] == 1
    assert any(ref == "प्रश्न 2" and "Answer Not Found" in why for _pg, ref, why in rep["problems"])
    lines = creator.report_lines(rep, 1)
    for k in ("Total detected: 3", "Parsed: 2", "Verification Required: 0", "Failed: 1",
              "Answer mapped: 2", "Parts created: 1"):
        assert any(k in ln for ln in lines), k


# --------------------------------------- end to end: passage stored + sent
async def _import(h, u, body, title):
    fid = f"U{u['id']}"
    h.api.files[fid] = body.encode("utf-8")
    await h.send(h.f.document(u, fid, "set.txt", "text/plain", len(h.api.files[fid])))
    last = h.api.sent("sendMessage", u["id"])[-1]
    cbs = [b["callback_data"] for b in buttons(last["reply_markup"])]
    assert cbs[0] in ("i:create", "i:reviewed"), last["text"][-600:]
    await h.cb(u, cbs[0])
    await h.text(u, title)


def test_passage_is_stored_and_sent_before_its_questions():
    async def t():
        async with Harness() as h:
            u = h.f.user(7101)
            await h.cb(u, "m:imp")
            await _import(h, u, PASSAGE_SRC, "गद्यांश टेस्ट")
            out = h.all_text(7101)
            assert "Total detected: 5" in out and "Parts created: 1" in out
            up = [x for x in h.api.uploads if x[0] == "sendDocument"]
            _name, data = next(iter(up[-1][2].values()))
            prev = data.decode("utf-8")
            assert "गद्यांश / तालिका / निर्देश" in prev and "इसका क्षेत्रफल 3,42,239 वर्ग किमी है।" in prev
            qz = db.get_owner_quizzes(7101)[0]
            qs = db.get_questions(qz["id"])
            assert qs[0]["pre_text"] == PASSAGE_SRC.split("\n1. ")[0] and qs[2]["pre_text"] == ""
            await h.start(u, qz["id"])
            await h.wait(lambda: h.polls_for(7101), msg="first poll")
            txt = h.all_text(7101)
            assert "राजस्थान क्षेत्रफल की दृष्टि से भारत का सबसे बड़ा राज्य है।" in txt
    run(t())


def test_1200_questions_make_three_parts_with_original_numbers():
    body = "".join(f"{n}. प्रश्न संख्या {n}: राजस्थान?\n(A) क{n} (B) ख{n} (C) ग{n} (D) घ{n}\nउत्तर: {'ABCD'[n % 4]}\n"
                   for n in range(1, 1201))

    async def t():
        async with Harness() as h:
            u = h.f.user(7102)
            await h.cb(u, "m:imp")
            await _import(h, u, body, "बड़ा सेट")
            out = h.all_text(7102)
            assert "Parts created: 3" in out and "Total detected: 1200" in out
            parts = sorted(db.get_owner_quizzes(7102), key=lambda q: q.get("part_no") or 0)
            got = [[q["source_number"] for q in db.get_questions(p["id"])] for p in parts]
            assert [(g[0], g[-1], len(g)) for g in got] == [(1, 500, 500), (501, 1000, 500), (1001, 1200, 200)]
            for p in parts:
                for q in db.get_questions(p["id"]):
                    assert q["correct_index"] == q["source_number"] % 4
    run(t())


def test_1000_plus_char_question_stored_full_and_sent_with_compact_poll():
    stem = "राजस्थान " * 150 + "कौन सा सही है?"
    assert len(stem) > 1000
    res = P.parse_source(f"1. {stem}\n(A) a (B) b (C) c (D) d\nउत्तर: B")
    assert res.errors == [] and res.questions[0].question == stem          # no truncation
    pl = engine.build_poll_payload(res.questions[0].to_dict(), [0, 1, 2, 3], 0, 1)
    assert pl.long_question and stem.split()[-1] in "".join(pl.full_text_chunks)
    assert "".join(pl.full_text_chunks).count("राजस्थान") >= 150
    assert engine.tg_len(pl.question) <= config.POLL_QUESTION_MAX
    assert not any(ch.isdigit() and "-" in pl.question for ch in pl.question if ch == "-")
