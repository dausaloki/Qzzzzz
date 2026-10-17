"""HTML quiz import: extraction unit tests + end-to-end import through the bot.

Feature 1 of the round: an admin sends a .html/.htm quiz file, the bot extracts
every question/option/answer/explanation, shows the normal review/preview and
only creates the quiz after the usual confirmation.  Nothing here writes to
production data: every test runs on a tmp SQLite database.
"""
from pathlib import Path

import pytest

import config
import database as db
import html_quiz
from test_bot_flow import Harness, run

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never touch data/quiz.db — every test gets its own temporary database."""
    db.set_db_path(tmp_path / "quiz.db")
    db.init_db()
    monkeypatch.setattr(config, "NEXT_QUESTION_DELAY", 0.05)
    monkeypatch.setattr(config, "TIMER_GRACE", 0.2)
    yield
    db.set_db_path(config.DB_PATH)

# Three questions in three different HTML shapes; the first one is exactly the
# structure v1 of the extractor failed on (class containers holding <input> radios).
THREE_Q = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Rajasthan GK</title>
<style>.question{}</style><script>window.timer=30;</script></head>
<body>
<h1>राजस्थान भूगोल Quiz</h1>
<div class="question">
  <p class="question-text">1. राजस्थान का सबसे बड़ा जिला कौन-सा है?</p>
  <ul class="options">
    <li class="option"><input type="radio" name="q1"> (A) जैसलमेर</li>
    <li class="option"><input type="radio" name="q1" checked> (B) बीकानेर</li>
    <li class="option"><input type="radio" name="q1"> (C) जोधपुर</li>
    <li class="option"><input type="radio" name="q1"> (D) उदयपुर</li>
  </ul>
  <div class="solution">बीकानेर क्षेत्रफल में सबसे बड़ा जिला है।</div>
</div>
<div class="question">
  <p class="question-text">2. 'जय श्री श्याम' का संबंध किससे है?</p>
  <div class="option">A) खाटू श्याम जी</div>
  <div class="option correct">B) सालासर बालाजी</div>
  <div class="option">C) रामदेव जी</div>
  <div class="option">D) गोगा जी</div>
  <div class="answer">Answer: B</div>
  <p class="explanation">यह बालाजी का प्रसिद्ध मंदिर है।</p>
</div>
<div class="question">
  <p class="question-text">3. Which city is the capital of Rajasthan?</p>
  <label><input type="radio" name="q3" value="A"> Jaipur</label>
  <label><input type="radio" name="q3" value="B"> Jodhpur</label>
  <label><input type="radio" name="q3" value="C"> Udaipur</label>
  <label><input type="radio" name="q3" value="D"> Bikaner</label>
  <div class="answer">Answer: A</div>
</div>
</body></html>
"""


# ------------------------------------------------------------------ unit tests
def test_three_shapes_one_file_with_exact_text_and_answers():
    ex = html_quiz.extract(THREE_Q.encode("utf-8"))
    assert ex.ok and ex.structural is True
    assert ex.questions == 3 and ex.with_answer == 3
    qs = ex.result.questions
    assert [q.number for q in qs] == [1, 2, 3]

    # question text is kept verbatim (numbering stripped, wording untouched)
    assert qs[0].question == "राजस्थान का सबसे बड़ा जिला कौन-सा है?"
    assert qs[0].options == ["जैसलमेर", "बीकानेर", "जोधपुर", "उदयपुर"]
    assert qs[0].correct_index == 1                      # checked radio
    assert qs[0].explanation == "बीकानेर क्षेत्रफल में सबसे बड़ा जिला है।"

    assert qs[1].options == ["खाटू श्याम जी", "सालासर बालाजी", "रामदेव जी", "गोगा जी"]
    assert qs[1].correct_index == 1                      # class="correct"
    assert qs[1].explanation == "यह बालाजी का प्रसिद्ध मंदिर है।"

    assert qs[2].options == ["Jaipur", "Jodhpur", "Udaipur", "Bikaner"]
    assert qs[2].correct_index == 0                      # label text + "Answer: A"

    # every option keeps its original order and exact punctuation
    for q in qs:
        assert q.options == [o.strip() for o in q.options]
        assert all(q.options)


def test_option_text_is_not_duplicated_from_the_input_value():
    """<label><input value="A"> Jaipur</label> must not become "A Jaipur"."""
    ex = html_quiz.extract(THREE_Q.encode())
    assert ex.result.questions[2].options[0] == "Jaipur"


def test_checked_input_in_plain_div_wrappers_without_classes():
    html = """<body><form>
    <p>1. Capital of India?</p>
    <div><input type="radio" name="a1"> New Delhi</div>
    <div><input type="radio" name="a1" checked> Mumbai</div>
    <div><input type="radio" name="a1"> Chennai</div>
    <p>2. Largest planet?</p>
    <div><input type="radio" name="a2"> Earth</div>
    <div><input type="radio" name="a2"> Mars</div>
    <div><input type="radio" name="a2" checked> Jupiter</div>
    </form></body>"""
    ex = html_quiz.extract(html.encode())
    assert ex.ok and ex.questions == 2
    qs = ex.result.questions
    assert qs[0].question == "Capital of India?"
    assert qs[0].options == ["New Delhi", "Mumbai", "Chennai"] and qs[0].correct_index == 1
    assert qs[1].question == "Largest planet?"
    assert qs[1].correct_index == 2


def test_table_rows_become_questions():
    html = """<table>
    <tr><th>प्रश्न</th><th>A</th><th>B</th><th>C</th><th>D</th><th>उत्तर</th></tr>
    <tr><td>1. राजस्थान की राजधानी?</td><td>जयपुर</td><td>जोधपुर</td><td>बीकानेर</td>
        <td>अजमेर</td><td>Answer: A</td></tr>
    <tr><td>2. सबसे बड़ा महासागर?</td><td>हिंद</td><td>अटलांटिक</td><td>प्रशांत</td>
        <td>आर्कटिक</td><td>Answer: C</td></tr>
    </table>"""
    ex = html_quiz.extract(html.encode())
    assert ex.ok and ex.questions == 2 and ex.with_answer == 2
    qs = ex.result.questions
    assert qs[0].question == "राजस्थान की राजधानी?" and qs[0].correct_index == 0
    assert qs[0].options == ["जयपुर", "जोधपुर", "बीकानेर", "अजमेर"]
    assert qs[1].correct_index == 2
    # the header row must never become a question
    assert all("प्रश्न" not in q.question for q in qs)


def test_section_headings_are_kept_with_their_questions():
    html = """<body><h1>Big Quiz</h1><h2>भाग-अ</h2>
    <div class="question"><p>1. Q one?</p><div class="option correct">a</div>
        <div class="option">b</div></div>
    <div class="question"><p>2. Q two?</p><div class="option">a</div>
        <div class="option correct">b</div></div>
    <h2>भाग-ब</h2>
    <div class="question"><p>3. Q three?</p><div class="option">a</div>
        <div class="option">b</div><div class="option correct">c</div></div>
    </body>"""
    ex = html_quiz.extract(html.encode())
    labels = [q.section_label for q in ex.result.questions]
    assert labels == ["भाग-अ", "भाग-अ", "भाग-ब"]


def test_missing_answer_is_an_error_never_a_guess():
    html = """<body>
    <div class="question"><p>1. Q with answer?</p>
      <div class="option">a</div><div class="option correct">b</div></div>
    <div class="question"><p>2. Q without answer?</p>
      <div class="option">a</div><div class="option">b</div></div>
    </body>"""
    ex = html_quiz.extract(html.encode())
    assert ex.questions == 2 and ex.with_answer == 1
    assert len(ex.result.errors) == 1
    err = ex.result.errors[0]
    assert err.number == 2 and "Answer Not Found" in err.message
    assert [q.number for q in ex.result.questions] == [1]
    assert any("उत्तर स्पष्ट नहीं" in n for n in ex.notes)


def test_multi_answer_question_is_flagged_not_mapped():
    html = """<body><div class="question"><p>1. कौन-से राज्य राजस्थान से लगे हैं?</p>
      <div class="option correct">पंजाब</div><div class="option correct">गुजरात</div>
      <div class="option">केरल</div></div></body>"""
    ex = html_quiz.extract(html.encode())
    assert ex.questions == 1 and ex.with_answer == 0
    assert "multi-answer" in ex.result.errors[0].message


def test_answer_only_by_colour_is_flagged_for_review():
    html = """<body><div class="q"><p>1. पृथ्वी का उपग्रह?</p>
      <div class="opt">सूर्य</div><div class="opt" style="color: green">चंद्रमा</div>
      <div class="opt">मंगल</div></div></body>"""
    ex = html_quiz.extract(html.encode())
    assert ex.questions == 1 and ex.with_answer == 1
    assert ex.result.questions[0].correct_index == 1
    assert any("रंग" in f or "हाइलाइट" in f for f in ex.result.questions[0].flags)


def test_plain_text_inside_html_uses_the_normal_text_parser():
    html = """<html><body><pre>
1. भारत की राजधानी कहाँ है?
(A) दिल्ली
(B) मुंबई
(C) चेन्नई
(D) कोलकाता
उत्तर: A
</pre></body></html>"""
    ex = html_quiz.extract(html.encode())
    assert ex.structural is False                       # no DOM structure → text fallback
    assert ex.questions == 1 and ex.with_answer == 1
    assert ex.result.questions[0].options == ["दिल्ली", "मुंबई", "चेन्नई", "कोलकाता"]
    assert ex.result.questions[0].correct_index == 0
    assert any("text की तरह parse" in n for n in ex.notes)


def test_same_line_options_are_split_by_the_text_parser():
    html = """<body><div class="question">1. 2 + 2 = ?
    <div class="options">(A) 3 (B) 4 (C) 5 (D) 6</div>
    <div class="answer">Answer: (B)</div></div></body>"""
    ex = html_quiz.extract(html.encode())
    assert ex.questions == 1 and ex.with_answer == 1
    assert ex.result.questions[0].options == ["3", "4", "5", "6"]
    assert ex.result.questions[0].correct_index == 1


def test_malformed_markup_never_crashes():
    html = ("<div class='question'><p>1. टूटा हुआ markup?<div class='option'>a"
            "<div class='option correct'>b<td>c</tr><b><i>unclosed"
            "<script>if (a < b) { }</script>")
    ex = html_quiz.extract(html.encode())               # returns, never raises
    assert isinstance(ex.questions, int)
    assert isinstance(ex.result.errors, list)


def test_empty_and_non_html_inputs_are_rejected_clearly():
    with pytest.raises(html_quiz.HtmlError):
        html_quiz.extract(b"")
    with pytest.raises(html_quiz.HtmlError):
        html_quiz.extract(b"%PDF-1.7\n% not html")
    with pytest.raises(html_quiz.HtmlError):
        html_quiz.extract(b"PK\x03\x04zip")


def test_size_cap_uses_config(monkeypatch):
    monkeypatch.setattr(config, "HTML_MAX_BYTES", 200)
    with pytest.raises(html_quiz.HtmlError):
        html_quiz.extract(("<body>" + "x" * 400 + "</body>").encode())


def test_bom_and_meta_charset_are_decoded():
    body = "<body><div class='question'><p>1. सवाल?</p>" \
           "<div class='option'>क</div><div class='option correct'>ख</div></div></body>"
    meta = "<meta charset='utf-8'>".encode()
    ex = html_quiz.extract(b"\xef\xbb\xbf" + b"<html><head>" + meta + b"</head>" + body.encode())
    assert ex.result.questions[0].question == "सवाल?"


def test_looks_like_html_and_import_document():
    class D:
        def __init__(self, name, mime):
            self.file_name, self.mime_type = name, mime

    assert html_quiz.looks_like_html("quiz.HTML", "")
    assert html_quiz.looks_like_html("x.htm", "application/octet-stream")
    assert html_quiz.looks_like_html("x.bin", "text/html")
    assert not html_quiz.looks_like_html("quiz.pdf", "application/pdf")
    import quiz_creator
    assert quiz_creator.is_import_document(D("a.html", "text/html"))
    assert quiz_creator.is_import_document(D("a.xhtml", ""))
    assert quiz_creator.is_import_document(D("a.pdf", "application/pdf"))
    assert quiz_creator.is_import_document(D("a.txt", "text/plain"))
    assert not quiz_creator.is_import_document(D("a.png", "image/png"))


def test_large_html_is_processed_completely():
    parts = ["<html><body>"]
    for i in range(1, 151):
        parts.append(f'<div class="question"><p>{i}. प्रश्न {i}?</p>'
                     f'<div class="option">विकल्प अ</div><div class="option correct">विकल्प ब</div>'
                     f'<div class="option">विकल्प स</div><div class="option">विकल्प द</div></div>')
    parts.append("</body></html>")
    ex = html_quiz.extract("".join(parts).encode())
    assert ex.questions == 150 and ex.with_answer == 150
    assert [q.number for q in ex.result.questions] == list(range(1, 151))


# ------------------------------------------------------------ bot integration
def _send_html(h, user, html: str, name="rajasthan_gk.html", caption=None):
    fid = f"HTML{abs(hash(html)) % 100000}"
    h.api.files[fid] = html.encode("utf-8")
    return h.send(h.f.document(user, fid, name, "text/html", len(h.api.files[fid]), caption=caption))


async def _confirm_import(h, user, chat_id=None):
    """Press whatever confirm button the review offers (create / reviewed / partial)."""
    chat_id = chat_id or user["id"]
    data = str(h.api.sent("sendMessage", chat_id)[-1]["reply_markup"])
    for action in ("i:create", "i:reviewed", "i:partial"):
        if f"'{action}'" in data:
            await h.cb(user, action, chat_id=chat_id)
            break
    await h.cb(user, "i:usedefault", chat_id=chat_id)


def test_html_document_preview_then_create_quiz():
    async def t():
        async with Harness() as h:
            u = h.f.user(3101, "Asha", "asha")
            await _send_html(h, u, THREE_Q)
            review = h.api.sent("sendMessage", 3101)[-1]
            assert "सही parse हुए प्रश्न: <b>3</b>" in review["text"]
            assert "HTML: 3 question blocks मिले" in review["text"]
            # Q3's answer came from an "Answer: A" line → the review asks for one confirmation
            assert "i:reviewed" in str(review["reply_markup"])
            await h.cb(u, "i:reviewed")
            await h.cb(u, "i:usedefault")
            quizzes = db.get_owner_quizzes(3101)
            assert len(quizzes) == 1
            assert quizzes[0]["title"] == "rajasthan_gk"          # file name, never invented
            qs = db.get_questions(quizzes[0]["id"])
            assert [q["correct_index"] for q in qs] == [1, 1, 0]
            assert qs[0]["options"] == ["जैसलमेर", "बीकानेर", "जोधपुर", "उदयपुर"]
            assert qs[0]["explanation"] == "बीकानेर क्षेत्रफल में सबसे बड़ा जिला है।"
            assert [q["position"] for q in qs] == [1, 2, 3]

            # the imported quiz really runs as native Telegram quiz polls
            await h.start(u, quizzes[0]["id"])
            for i in range(3):
                await h.wait(lambda: len(h.polls_for(3101)) == i + 1)
                await h.answer_current(u, True)
            await h.wait(lambda: "🏁" in h.all_text(3101))
            assert "Correct: 3" in h.all_text(3101)

    run(t())


def test_html_caption_becomes_the_quiz_title():
    async def t():
        async with Harness() as h:
            u = h.f.user(3102)
            await _send_html(h, u, THREE_Q, name="whatever.html", caption="मेरा टेस्ट")
            await _confirm_import(h, u)
            assert db.get_owner_quizzes(3102)[0]["title"] == "मेरा टेस्ट"

    run(t())


def test_html_with_missing_answers_needs_explicit_partial_confirmation():
    html = THREE_Q.replace('<div class="answer">Answer: A</div>', "")
    async def t():
        async with Harness() as h:
            u = h.f.user(3103)
            await _send_html(h, u, html)
            out = h.all_text(3103)
            assert "Answer Not Found" in out
            last = h.api.sent("sendMessage", 3103)[-1]
            assert "i:partial" in str(last["reply_markup"])
            assert 'i:create"' not in str(last["reply_markup"])
            await h.cb(u, "i:create")                     # forged/old button must not bypass
            await h.cb(u, "i:usedefault")
            assert db.get_owner_quizzes(3103) == []       # nothing silently imported
            await h.cb(u, "i:partial")
            await h.text(u, "Partial HTML")
            qz = db.get_owner_quizzes(3103)
            assert len(qz) == 1
            assert len(db.get_questions(qz[0]["id"])) == 2

    run(t())


def test_broken_html_shows_a_clear_error_and_creates_nothing():
    async def t():
        async with Harness() as h:
            u = h.f.user(3104)
            await _send_html(h, u, "<html><body><p>no questions at all</p></body></html>")
            out = h.all_text(3104)
            assert "कोई प्रश्न नहीं मिला" in out
            assert db.get_owner_quizzes(3104) == []
            # a PDF renamed to .html is rejected instead of crashing
            h.api.files["BADPDF"] = b"%PDF-1.7\n"
            await h.send(h.f.document(u, "BADPDF", "fake.html", "text/html", 8))
            assert "❌" in h.api.texts(3104)[-1]

    run(t())


def test_html_admin_gate(monkeypatch):
    monkeypatch.setattr(config, "HTML_IMPORT_ADMIN_ONLY", True)
    monkeypatch.setattr(config, "BACKUP_ADMIN_IDS", (3105,))

    async def t():
        async with Harness() as h:
            normal = h.f.user(9999)
            await _send_html(h, normal, THREE_Q)
            assert "केवल administrator" in h.api.texts(9999)[-1]
            assert db.get_owner_quizzes(9999) == []
            admin = h.f.user(3105)
            await _send_html(h, admin, THREE_Q)
            assert "सही parse हुए प्रश्न: <b>3</b>" in h.api.texts(3105)[-1]

    run(t())


def test_existing_pdf_and_txt_import_paths_are_untouched():
    """The HTML branch must not break the old document types."""
    async def t():
        async with Harness() as h:
            u = h.f.user(3106)
            h.api.files["TXT2"] = (FIX / "weak_numbers_key.txt").read_bytes()
            await h.send(h.f.document(u, "TXT2", "old.txt", "text/plain",
                                      len(h.api.files["TXT2"])))
            assert "i:create" in str(h.api.sent("sendMessage", 3106)[-1]["reply_markup"])
            await h.cb(u, "i:create")
            await h.cb(u, "i:usedefault")
            assert len(db.get_owner_quizzes(3106)) == 1

    run(t())


def test_buttons_on_preview_still_work_after_an_html_import_error():
    """An error must not leave behind a stuck import session."""
    html = THREE_Q.replace('<div class="answer">Answer: A</div>', "")
    async def t():
        async with Harness() as h:
            u = h.f.user(3107)
            await _send_html(h, u, html)
            await h.cb(u, "i:cancel")
            assert db.get_owner_quizzes(3107) == []
            # after cancelling, a normal text import starts from scratch
            await h.text(u, "/import")
            await h.text(u, "1. नया?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: B")
            await h.cb(u, "i:create")
            await h.text(u, "Fresh")
            assert len(db.get_questions(db.get_owner_quizzes(3107)[0]["id"])) == 1

    run(t())
