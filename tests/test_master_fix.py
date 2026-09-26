"""MASTER FIX round: PDF import accuracy (source of truth, Hindi orthography,
page numbers, answer keys, numbering) + question capacity (no length limit,
no total limit, automatic parts of QUESTIONS_PER_PART)."""
import re
from pathlib import Path

import pytest

import config
import database as db
import hindi_check as H
import keyboards as kb
import pdf_extract as X
import pdf_parser as P
import quiz_creator as creator
import quiz_engine as engine
import pdfgen as G
from test_bot_flow import Harness, buttons, run

needs_font = pytest.mark.skipif(not G.have_font(), reason="Devanagari font not available")
OCR_OK, OCR_INFO = X.ocr_status()
needs_ocr = pytest.mark.skipif(not OCR_OK, reason=f"OCR data not available: {OCR_INFO}")
L = "ABCD"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "quiz.db")
    db.init_db()
    monkeypatch.setattr(config, "NEXT_QUESTION_DELAY", 0.05)
    monkeypatch.setattr(config, "TIMER_GRACE", 0.2)
    yield
    db.set_db_path(config.DB_PATH)


# ------------------------------------------------------------------ helpers
def ans_of(i: int) -> int:
    """Deterministic, non-trivial answer pattern by ORIGINAL number."""
    return (i * 7 + i // 3) % 4


def gen_source(n: int, *, start: int = 1, key: bool = True, qlen: int = 0) -> str:
    """n numbered Hindi MCQs (+ a separate answer-key section)."""
    out = []
    for i in range(start, start + n):
        q = f"प्रश्न संख्या {i}: राजस्थान में {i}.5% क्षेत्र और ₹{i} का मान क्या है?"
        if qlen:
            q = (q + " " + "अरावली पर्वतमाला विश्व की प्राचीनतम पर्वतमाला है। " * (qlen // 40 + 1))[:qlen]
            q = q[:-1] + "?"                          # exact length, no trailing blank
        out.append(f"{i}. {q}")
        for j, l in enumerate(L):
            out.append(f"({l}) विकल्प {i}-{l}")
        if not key:
            out.append(f"उत्तर: ({L[ans_of(i)]})")
    if key:
        out.append("ANSWER KEY")
        out += [f"{i}. [{L[ans_of(i)].lower()}]" for i in range(start, start + n)]
    return "\n".join(out)


_RUN = re.compile(r"[\u0900-\u097F\u200c\u200d]+|[^\u0900-\u097F\u200c\u200d]+")


def _write(page, x, y, text, size):
    """Devanagari → Noto Devanagari, everything else → Noto Sans (has ₹ ₂ — ½ ° glyphs)."""
    import fitz
    deva, latin = fitz.Font(fontfile=str(G.FONT)), fitz.Font("notos")
    for run_ in _RUN.findall(text):
        is_deva = bool(re.match(r"[\u0900-\u097F]", run_))
        page.insert_text((x, y), run_, fontname="deva" if is_deva else "notos", fontsize=size)
        x += (deva if is_deva else latin).text_length(run_, fontsize=size)


def make_pdf(path: Path, pages: list[list[str]], header: str = "", footer: str = "") -> None:
    """Text-layer PDF, one list of lines per page, optional running header/footer."""
    import fitz
    doc = fitz.open()
    for pno, lines in enumerate(pages, 1):
        page = G.new_page(doc)
        page.insert_font(fontname="notos", fontbuffer=fitz.Font("notos").buffer)
        if header:
            _write(page, 40, 28, header, 9)
        y = 70
        for ln in lines:
            _write(page, 40, y, ln, 10)
            y += 15
        if footer:
            _write(page, 270, 828, footer.format(n=pno), 9)
    doc.save(str(path))


# ======================================================= 1. formats (text)
FORMATS = """1. राजस्थान का क्षेत्रफल 3,42,239 km² है, जो भारत का 10.41% है। DNA व H₂O में ₹ 500 और 25°C — सही?
(A) 10.41%
(b) 3.14 km
C. ½ भाग
D) 1,000 मी.
2. निम्नलिखित कथनों पर विचार कीजिए—
1. अरावली विश्व की प्राचीनतम पर्वतमाला है।
2. गुरु शिखर इसकी सबसे ऊँची चोटी है।
3. यह उत्तर-पूर्व से दक्षिण-पश्चिम फैली है।
4. इसकी लंबाई 692 किमी है।
कूट:
(A) केवल 1 और 2
(B) केवल 2 और 3
(C) केवल 1, 2 और 3
(D) 1, 2, 3 और 4
3. सूची-I को सूची-II से सुमेलित कीजिए—
सूची-I (झील)  सूची-II (जिला)
(A) सांभर  (I) जयपुर
(B) पिछोला  (II) उदयपुर
(C) आनासागर  (III) अजमेर
(D) नक्की  (IV) सिरोही
कूट:
(A) A-I, B-II, C-III, D-IV
(B) A-II, B-I, C-III, D-IV
(C) A-I, B-III, C-II, D-IV
(D) A-IV, B-II, C-III, D-I
4. कथन (A): राजस्थान में मरुस्थल का विस्तार हो रहा है।
कारण (R): यहाँ वर्षा कम होती है।
(a) A और R दोनों सही हैं तथा R, A की सही व्याख्या है।
(b) A और R दोनों सही हैं, परंतु R, A की सही व्याख्या नहीं है।
(c) A सही है, परंतु R गलत है।
(d) A गलत है, परंतु R सही है।
5. निम्नलिखित को उत्तर से दक्षिण क्रम में लगाइए—
(A) गंगानगर
(B) बीकानेर
(C) जोधपुर
(D) बाड़मेर
कूट:
(A) A, B, C, D
(B) B, A, C, D
(C) A, C, B, D
(D) D, C, B, A
ANSWER KEY
1. [a]  2. [c]  3. [a]  4. [b]  5. [d]"""


def _check_formats(res):
    assert res.errors == [], res.errors
    qs = {q.number: q for q in res.questions}
    assert sorted(qs) == [1, 2, 3, 4, 5]
    # exact text: numbers, decimals, %, symbols, English words, units, punctuation
    assert qs[1].question == ("राजस्थान का क्षेत्रफल 3,42,239 km² है, जो भारत का 10.41% है। "
                              "DNA व H₂O में ₹ 500 और 25°C — सही?")
    assert qs[1].options == ["10.41%", "3.14 km", "½ भाग", "1,000 मी."]          # (A) (b) C. D)
    # statements: 1–4 are part of the question; A–D after कूट are the options
    assert "\n1. अरावली" in qs[2].question and "\n4. इसकी लंबाई 692 किमी है।" in qs[2].question
    assert qs[2].options == ["केवल 1 और 2", "केवल 2 और 3", "केवल 1, 2 और 3", "1, 2, 3 और 4"]
    # List-I/II: A–D + I–IV are data (in the question), the A–D after कूट are options
    assert "(A) सांभर  (I) जयपुर" in qs[3].question and "(D) नक्की  (IV) सिरोही" in qs[3].question
    assert qs[3].options[0] == "A-I, B-II, C-III, D-IV" and len(qs[3].options) == 4
    # Assertion–Reason: कथन (A)/कारण (R) are not options
    assert qs[4].question.startswith("कथन (A):") and "कारण (R):" in qs[4].question
    assert len(qs[4].options) == 4 and qs[4].options[2] == "A सही है, परंतु R गलत है।"
    # ordering: first A–D block is data, second is options
    assert "(A) गंगानगर" in qs[5].question and qs[5].options == ["A, B, C, D", "B, A, C, D",
                                                                "A, C, B, D", "D, C, B, A"]
    assert [qs[i].correct_index for i in range(1, 6)] == [0, 2, 0, 1, 3]     # exact key mapping
    assert [qs[i].qtype for i in (3, 4, 5)] == ["list_matching", "assertion_reason", "ordering"]


def test_all_question_formats_text():
    _check_formats(P.parse_source(FORMATS))


@needs_font
def test_all_question_formats_pdf_with_separate_answer_key_page(tmp_path):
    lines = FORMATS.split("\n")
    k = lines.index("ANSWER KEY")
    pdf = tmp_path / "f.pdf"
    make_pdf(pdf, [lines[:25], lines[25:k], lines[k:]])
    res = P.parse_pdf_result(pdf)
    _check_formats(res)
    assert {q.number: q.page for q in res.questions} == {1: 1, 2: 1, 3: 1, 4: 2, 5: 2}


# ================================================= 2. layout / page numbers
@needs_font
def test_headers_footers_removed_page_break_and_pages(tmp_path):
    src = gen_source(12, key=False).split("\n")          # 6 lines per question
    # question 7 is split across the page break (question on page 1, options on page 2)
    pages = [src[:37], src[37:]]
    pdf = tmp_path / "h.pdf"
    make_pdf(pdf, pages, header="राजस्थान का भूगोल", footer="Page - {n}")
    res = P.parse_pdf_result(pdf)
    assert res.errors == [] and [q.number for q in res.questions] == list(range(1, 13))
    q7 = res.questions[6]
    assert q7.options == [f"विकल्प 7-{l}" for l in L] and q7.page == 1
    assert "राजस्थान का भूगोल" not in q7.question and "Page" not in " ".join(q7.options)
    for q in res.questions:
        assert "राजस्थान का भूगोल" not in q.question + "".join(q.options)
        assert q.correct_index == ans_of(q.number)
    assert any("राजस्थान का भूगोल" in t for kind, t in res.removed)
    assert res.questions[-1].page == 2


@needs_font
def test_two_column_reading_order(tmp_path):
    left = "\n".join(gen_source(3, key=False).split("\n"))
    right = "\n".join(gen_source(3, start=4, key=False).split("\n"))
    pdf = tmp_path / "c.pdf"
    G.make_two_column_pdf(pdf, [("", left, right)])
    res = P.parse_pdf_result(pdf)
    assert res.errors == [], res.errors
    assert [q.number for q in res.questions] == [1, 2, 3, 4, 5, 6]
    assert all(q.options == [f"विकल्प {q.number}-{l}" for l in L] for q in res.questions)
    assert all(q.correct_index == ans_of(q.number) for q in res.questions)


@needs_font
def test_unparsable_question_reported_with_number_and_page(tmp_path):
    src = gen_source(10, key=False).split("\n")
    # question 8 (page 2) loses its answer line → reported, never guessed/skipped silently
    i8 = src.index(next(ln for ln in src if ln.startswith("8. ")))
    del src[i8 + 5]
    pdf = tmp_path / "e.pdf"
    make_pdf(pdf, [src[:30], src[30:]])
    res = P.parse_pdf_result(pdf)
    assert [q.number for q in res.questions] == [1, 2, 3, 4, 5, 6, 7, 9, 10]
    err = [e for e in res.errors if e.number == 8]
    assert err and err[0].page == 2
    assert str(err[0]).startswith("प्रश्न 8 (पेज 2): Answer Not Found")


def test_original_numbering_preserved_and_4_digit_numbers():
    res = P.parse_source(gen_source(5, start=101))
    assert res.errors == [] and [q.number for q in res.questions] == [101, 102, 103, 104, 105]
    res = P.parse_source(gen_source(4, start=2376))
    assert res.errors == [] and [q.number for q in res.questions] == [2376, 2377, 2378, 2379]
    assert [q.correct_index for q in res.questions] == [ans_of(i) for i in range(2376, 2380)]


def test_missing_key_entry_is_answer_not_found_never_guessed():
    src = gen_source(6).replace("\n4. [", "\nX4. [")          # key row 4 unreadable
    res = P.parse_source(src)
    assert 4 not in [q.number for q in res.questions]
    assert any(e.number == 4 and "Answer Not Found" in e.message for e in res.errors)


# ================================================== 3. Hindi orthography
BROKEN = ["रािस्थाि", "निम्िलिखित", "निकल्पों", "चुिें"]


def test_orthography_validator_flags_broken_words_only():
    good = ("निम्नलिखित में से कौन-सा सही है? राजस्थान के विकल्पों में से चुनें। हैं, मैं, कृष्ण, ज़मीन, "
            "आँख, ऑफ़िस, श्रृंखला, द्वारा, 12.5% क्षेत्रफल, स्त्री, कर्त्तव्य, उन्होंने, दुःख, अंतःकरण")
    assert H.invalid_words(good) == []
    assert H.invalid_words(" ".join(BROKEN)) == BROKEN


def test_broken_hindi_is_verification_required_never_corrected():
    src = ("1. निम्िलिखित में से रािस्थाि की राजधानी कौन-सी है? सही निकल्पों में से चुिें।\n"
           "(A) जयपुर\n(B) अजमेर\n(C) जोधपुर\n(D) उदयपुर\nउत्तर: A\n"
           "2. राजस्थान का राज्य पशु?\n(A) ऊँट\n(B) चिंकारा\n(C) बाघ\n(D) गोडावण\nउत्तर: B")
    res = P.parse_source(src)
    q1, q2 = res.questions
    # the source text is kept EXACTLY (no auto-correction) …
    assert q1.question == ("निम्िलिखित में से रािस्थाि की राजधानी कौन-सी है? सही निकल्पों में से चुिें।")
    # … but it is flagged for verification against the page
    assert set(BROKEN) <= set(q1.verify) and q2.verify == []
    assert any(r.startswith("Verification Required — प्रश्न 1") for r in res.review)
    assert not any("प्रश्न 2" in r for r in res.review)


def test_repair_never_touches_i_matra_after_another_matra():
    assert P.repair_devanagari("रािस्थाि")[0] == "रािस्थाि"
    assert P.repair_devanagari("िकताब")[0] == "किताब"             # the unambiguous visual-order case


def test_broken_text_layer_is_detected_as_garbled():
    layer = ("निम्िलिखित रािस्थाि चुिें निकल्पों " * 3 + "राजस्थान की राजधानी जयपुर है और यहाँ बहुत किले हैं। " * 4)
    bad, why = X.text_quality(layer)
    assert bad and "मात्राएँ" in why
    assert X.text_quality("राजस्थान की राजधानी जयपुर है और यहाँ बहुत किले हैं। " * 10) == (False, "")


def test_uncertain_ocr_words_come_from_disagreement():
    unc = X.uncertain_words("राजस्थान का निकल्प 12.5%", "राजस्थान का विकल्प 12.5%")
    assert unc == {"निकल्प"}
    res = P.parse_source("1. राजस्थान का सही उत्तर?\n(A) विकल्प\n(B) दो\nउत्तर: A",
                         line_pages=[3, 3, 3, 3], uncertain={3: {"राजस्थान"}})
    assert res.questions[0].verify == ["राजस्थान"] and res.questions[0].page == 3
    assert res.review and res.review[-1].startswith("Verification Required — प्रश्न 1 (पेज 3)")


@needs_ocr
def test_scanned_hindi_pdf_ocr_matras_numbers_and_verification(tmp_path):
    text = ("1. राजस्थान का क्षेत्रफल कितना है?\n(A) 3,42,239 वर्ग किमी\n(B) 10.41% भाग\n"
            "(C) 2,50,000 वर्ग किमी\n(D) 1,00,000 वर्ग किमी\nउत्तर: (A)\n"
            "2. निम्नलिखित में से कौन-सा विकल्प सही है?\n(A) जयपुर\n(B) उदयपुर\n(C) जोधपुर\n(D) बीकानेर\n"
            "उत्तर: (C)")
    pdf = tmp_path / "scan.pdf"
    G.make_scanned_pdf(pdf, text, dpi=200)
    res = P.parse_pdf_result(pdf)
    assert res.ocr_pages == [1]
    assert [q.number for q in res.questions] == [1, 2], res.errors
    q1, q2 = res.questions
    assert q1.correct_index == 0 and q2.correct_index == 2
    assert "3,42,239" in q1.options[0] and "10.41%" in q1.options[1]
    assert "राजस्थान" in q1.question and "निम्नलिखित" in q2.question
    for q in res.questions:
        body = "\n".join([q.question, *q.options])
        for w in BROKEN:
            assert w not in body or w in q.verify               # never silently accepted
        assert set(H.invalid_words(body)) <= set(q.verify)
        assert q.page == 1
    assert res.review                                           # OCR always needs confirmation


# ============================================== 4. question length (no limit)
@pytest.mark.parametrize("length", [300, 301, 500, 1000, 1500, 5000])
def test_long_questions_stored_in_full_and_playable(length):
    src = gen_source(2, qlen=length)
    res = P.parse_source(src)
    assert res.errors == [] and len(res.questions) == 2
    q = res.questions[0]
    expected = src.split("\n")[0][3:]
    assert q.question == expected and len(q.question) == length              # no truncation
    db.ensure_user_row(1)
    qid = db.new_quiz(1, "Long", status="ready", source="import")
    db.add_questions_bulk(qid, [x.to_dict() for x in res.questions])
    stored = db.get_questions(qid)[0]
    assert stored["question"] == expected and stored["source_number"] == 1
    payload = engine.build_poll_payload(stored, [0, 1, 2, 3], 0, 2)
    assert engine.tg_len(payload.question) <= config.POLL_QUESTION_MAX
    if engine.tg_len(config.QUESTION_TAG + "\n" + expected) > config.POLL_QUESTION_MAX:
        full = "".join(payload.full_text_chunks)
        assert payload.full_text_chunks and all(engine.tg_len(c) <= 4096 for c in payload.full_text_chunks)
        assert expected in full.replace("\n", " ") or expected in full
        assert payload.options == [f"विकल्प 1-{l}" for l in L]
        assert payload.correct_option_id == ans_of(1)


def test_typed_1000_char_question_saved_in_full_in_creation():
    long_q = ("राजस्थान के भूगोल से संबंधित निम्नलिखित कथनों पर विचार कीजिए। " * 20)[:1000]

    async def t():
        async with Harness() as h:
            u = h.f.user(9101)
            await h.text(u, "/newquiz")
            await h.text(u, "Long Q")
            await h.text(u, "/skip")
            await h.text(u, long_q + "\n(A) एक\n(B) दो\n(C) तीन\n(D) चार\nउत्तर: C")
            qid = db.get_owner_quizzes(9101)[0]["id"]
            q = db.get_questions(qid)[0]
            assert q["question"] == long_q and len(q["question"]) == 1000 and q["correct_index"] == 2
            # the creation keyboard has no native poll-editor button (300-char "-57" counter)
            for m in h.api.sent("sendMessage", 9101):
                assert "request_poll" not in str(m.get("reply_markup") or "")
    run(t())


def test_native_poll_button_is_opt_in(monkeypatch):
    assert "request_poll" not in str(kb.creation_keyboard(True).to_dict())
    monkeypatch.setattr(config, "NATIVE_POLL_BUTTON", True)
    assert "request_poll" in str(kb.creation_keyboard(True).to_dict())


# ===================================================== 5. capacity / parts
@pytest.mark.parametrize("n,sizes", [
    (100, [100]), (456, [456]), (500, [500]), (501, [500, 1]), (850, [500, 350]),
    (1000, [500, 500]), (1001, [500, 500, 1]), (1200, [500, 500, 200]),
    (2378, [500, 500, 500, 500, 378]), (3000, [500] * 6), (5001, [500] * 10 + [1]),
])
def test_plan_parts_boundaries(n, sizes):
    qs = [{"number": i} for i in range(1, n + 1)]
    chunks = creator.plan_parts(qs)
    assert [len(c) for c in chunks] == sizes
    flat = [q["number"] for c in chunks for q in c]
    assert flat == list(range(1, n + 1))                                   # no loss, no dup, in order


def test_questions_per_part_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_PART", 200)
    assert [len(c) for c in creator.plan_parts(list(range(450)))] == [200, 200, 50]


async def _import_txt(h, u, body: str, name: str, title: str):
    fid = f"T{u['id']}"
    h.api.files[fid] = body.encode("utf-8")
    await h.send(h.f.document(u, fid, name, "text/plain", len(h.api.files[fid])))
    last = h.api.sent("sendMessage", u["id"])[-1]
    cbs = [b["callback_data"] for b in buttons(last["reply_markup"])]
    assert cbs[0] in ("i:create", "i:reviewed"), last["text"][-500:]
    await h.cb(u, cbs[0])
    await h.text(u, title)


@pytest.mark.parametrize("n", [100, 456, 500, 501, 850, 1000, 1001, 1200, 2378])
def test_import_capacity_end_to_end(n):
    per = config.QUESTIONS_PER_PART
    assert per == 500
    exp_parts = -(-n // per)

    async def t():
        async with Harness() as h:
            uid = 20000 + n
            u = h.f.user(uid)
            await h.cb(u, "m:imp")
            await _import_txt(h, u, gen_source(n), f"set{n}.txt", "राजस्थान GK")
            out = h.all_text(uid)
            assert "✅ <b>Import Complete</b>" in out or "✅ Import Complete" in out
            assert f"📚 Total Questions: {n}" in out and f"📦 Total Parts: {exp_parts}" in out
            quizzes = sorted(db.get_owner_quizzes(uid), key=lambda q: (q.get("part_no") or 0))
            assert len(quizzes) == exp_parts                                  # all in My Quizzes
            seen = []
            for k, qz in enumerate(quizzes, 1):
                quiz = db.get_quiz(qz["id"])
                qs = db.get_questions(qz["id"])
                lo, hi = (k - 1) * per + 1, min(k * per, n)
                assert len(qs) == hi - lo + 1
                assert [q["source_number"] for q in qs] == list(range(lo, hi + 1))
                assert all(q["correct_index"] == ans_of(q["source_number"]) for q in qs)
                assert all(q["options"] == [f"विकल्प {q['source_number']}-{l}" for l in L] for q in qs)
                assert all(q["question"].startswith(f"प्रश्न संख्या {q['source_number']}:") for q in qs)
                assert quiz["status"] == "ready"
                if exp_parts == 1:
                    assert quiz["title"] == "राजस्थान GK" and not quiz.get("series_id")
                else:
                    assert quiz["title"] == f"राजस्थान GK — Part {k}"
                    assert quiz["series_id"] == quizzes[0]["id"] and quiz["part_no"] == k
                    assert f"📝 Part {k}: Q{lo}–Q{hi}" in out or (lo == hi and f"📝 Part {k}: Q{lo}" in out)
                seen += [q["source_number"] for q in qs]
            assert seen == list(range(1, n + 1))                              # no missing / duplicate
            # full preview (.txt) has every question, its options and the correct answer
            up = [x for x in h.api.uploads if x[0] == "sendDocument"]
            assert up, "full preview document not sent"
            name, data = next(iter(up[-1][2].values()))
            txt = data.decode("utf-8")
            assert name == "full_preview.txt"
            for i in (1, n // 2 or 1, n):
                assert f"प्रश्न {i}\n" in txt
            assert txt.count("Correct Answer: ") == n
            assert f"Correct Answer: ({L[ans_of(n)]}) विकल्प {n}-{L[ans_of(n)]}" in txt
    run(t())


def test_3000_questions_no_total_limit():
    res = P.parse_source(gen_source(3000))
    assert res.errors == [] and len(res.questions) == 3000
    chunks = creator.plan_parts([q.to_dict() for q in res.questions])
    db.ensure_user_row(5)
    ids = []
    for k, c in enumerate(chunks, 1):
        qid = db.new_quiz(5, creator.part_title("Big", k), status="ready", source="import")
        db.set_part(qid, ids[0] if ids else qid, k)
        db.add_questions_bulk(qid, c)
        ids.append(qid)
    parts = db.series_parts(ids[0])
    assert [(p["part_no"], p["question_count"], p["first_number"], p["last_number"]) for p in parts] == \
        [(k, 500, (k - 1) * 500 + 1, k * 500) for k in range(1, 7)]


def test_answer_key_maps_by_original_number_into_the_right_part():
    body = gen_source(1000)
    res = P.parse_source(body)
    q501 = next(q for q in res.questions if q.number == 501)
    assert q501.correct_index == ans_of(501)
    chunks = creator.plan_parts([q.to_dict() for q in res.questions])
    assert chunks[1][0]["number"] == 501 and chunks[1][0]["correct_index"] == ans_of(501)


def test_part_title_never_exceeds_limit():
    t = creator.part_title("क" * 400, 12)
    assert t.endswith(" — Part 12") and len(t) <= creator.TITLE_MAX
    assert creator.part_title("GK — Part 1", 2) == "GK — Part 2"


# ================================================== 6. playback of parts
def test_parts_are_independent_and_next_part_button(monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_PART", 3)

    async def t():
        async with Harness() as h:
            u = h.f.user(9300)
            await h.cb(u, "m:imp")
            await _import_txt(h, u, gen_source(7), "p.txt", "Parts")
            quizzes = sorted(db.get_owner_quizzes(9300), key=lambda q: q["part_no"])
            assert [q["title"] for q in quizzes] == ["Parts — Part 1", "Parts — Part 2", "Parts — Part 3"]
            assert [q["question_count"] for q in quizzes] == [3, 3, 1]
            p1, p2, p3 = (q["id"] for q in quizzes)
            # every part has its own share link / Start button and a "Part k/N" line
            out = h.all_text(9300)
            for qid in (p1, p2, p3):
                assert f"start=quiz_{qid}" in out or f"quiz_{qid}" in out
            assert "📦 Part 2/3" in out
            # play Part 1 → result offers "Start Part 2"
            await h.start(u, p1)
            for i in range(3):
                await h.wait(lambda: len(h.polls_for(9300)) == i + 1)
                await h.answer_current(u, True)
            await h.wait(lambda: "🏁" in h.all_text(9300))
            res_msg = [m for m in h.api.sent("sendMessage", 9300) if "🏁" in m["text"]][-1]
            cbs = [b["callback_data"] for b in buttons(res_msg["reply_markup"])]
            assert f"q:run:{p2}" in cbs
            assert any(b["text"] == "➡️ Start Part 2" for b in buttons(res_msg["reply_markup"]))
            # Part 2 can be started directly; its polls keep the ORIGINAL numbers (Q4…)
            await h.start(u, p2)
            await h.wait(lambda: len(h.polls_for(9300)) == 4)
            _, poll = h.polls_for(9300)[-1]
            assert "[Q4 · 1/3]" in poll["question"]
            for i in range(3):
                await h.wait(lambda: len(h.polls_for(9300)) == 4 + i)
                await h.answer_current(u, True)
            await h.wait(lambda: h.all_text(9300).count("🏁") >= 2)
            last = [m for m in h.api.sent("sendMessage", 9300) if "🏁" in m["text"]][-1]
            assert f"q:run:{p3}" in str(last["reply_markup"])
            assert db.next_part(p3) is None
    run(t())


def test_poll_prefix_uses_source_number_only_when_different():
    q = {"question": "Q?", "options": ["a", "b"], "correct_index": 0, "source_number": 501}
    assert "[Q501 · 1/500]" in engine.build_poll_payload(q, [0, 1], 0, 500).question
    q["source_number"] = 1
    p = engine.build_poll_payload(q, [0, 1], 0, 500).question
    assert "[1/500]" in p and "Q1 ·" not in p


def test_manual_multi_question_message_rolls_over(monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_PART", 2)

    async def t():
        async with Harness() as h:
            u = h.f.user(9400)
            await h.text(u, "/newquiz")
            await h.text(u, "Roll")
            await h.text(u, "/skip")
            body = "\n".join(f"{i}. Q{i}?\n(A) a\n(B) b\nउत्तर: B" for i in range(1, 6))
            await h.text(u, body)
            await h.text(u, "/done")
            qz = sorted(db.get_owner_quizzes(9400), key=lambda q: q["part_no"])
            assert [q["question_count"] for q in qz] == [2, 2, 1]
            assert [q["title"] for q in qz] == ["Roll — Part 1", "Roll — Part 2", "Roll — Part 3"]
            assert [x["question"] for q in qz for x in db.get_questions(q["id"])] == \
                [f"Q{i}?" for i in range(1, 6)]
            assert all(db.get_quiz(q["id"])["status"] == "ready" for q in qz)
    run(t())
