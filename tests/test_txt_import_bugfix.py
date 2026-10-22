"""Regression tests for the TXT question-import bug fix.

Clearly distinguishes:
1. Tests on the user's actual uploaded file ``Topic_02_Varnamala_evam_Lipi_Full.txt``
   (50 Hindi MCQs using ``[प्रश्न 1]`` .. ``[प्रश्न 50]`` headers, section dividers,
   numbered points inside explanations, and ``[परीक्षा: ...]`` tags).
2. Tests on the Part-2 slice (``[प्रश्न 26]`` .. ``[प्रश्न 50]``) derived from the
   uploaded file.
3. Synthetic regression tests covering all supported question-number formats,
   option-label formats (including ``(अ)``–``(द)``), encodings/BOM/CRLF, answer
   mapping, missing-answer/missing-option error reporting, and zero-question
   content-aware diagnostics.
"""
from __future__ import annotations

from pathlib import Path

import database as db
import pdf_parser
from tests.test_bot_flow import Harness, buttons, isolated, run

_ = isolated


FIXTURE_FULL_TXT = (
    Path(__file__).resolve().parent / "fixtures" / "Topic_02_Varnamala_evam_Lipi_Full.txt"
)


# ---------------------------------------------------------------------------
# 1. Actual uploaded TXT file tests
# ---------------------------------------------------------------------------
def test_actual_uploaded_topic02_full_txt_parses_all_50_questions():
    assert FIXTURE_FULL_TXT.exists(), "Fixture Topic_02_Varnamala_evam_Lipi_Full.txt missing"
    raw_text = pdf_parser.decode_text_bytes(FIXTURE_FULL_TXT.read_bytes())
    res = pdf_parser.parse_source(raw_text)

    assert len(res.detected_numbers) == 50
    assert res.detected_numbers == list(range(1, 51))
    assert len(res.questions) == 50
    assert res.errors == []

    # Also verify legacy parse_txt(path) returns all 50 question dicts without raising
    qs_dicts = pdf_parser.parse_txt(str(FIXTURE_FULL_TXT))
    assert len(qs_dicts) == 50

    # Verify import_report counts
    rep = pdf_parser.import_report(res)
    assert rep["total_detected"] == 50
    assert rep["parsed"] == 50
    assert rep["failed"] == 0
    assert rep["other_errors"] == 0
    assert rep["answer_inline"] == 50
    assert rep["answer_key"] == 0

    # Verify first, second, middle, and last questions preserve exact Hindi wording, options, and answers
    q1 = res.questions[0]
    assert q1.number == 1
    assert q1.question == "हिन्दी वर्णमाला में स्वरों की संख्या कितनी मानी गई है?"
    assert q1.options == ["10", "11", "13", "14"]
    assert q1.correct_index == 1  # (B)
    assert q1.answer_source == "inline"
    assert "[परीक्षा: CET / REET / Patwar / LDC]" in q1.explanation

    # Explanation in Q4 contains "1. ह्रस्व स्वर ...", "2. दीर्घ स्वर ...", "3. प्लुत स्वर ..."
    # and "[परीक्षा: RPSC LDC / VDO 2021]" — must stay inside Q4's explanation, never split as MCQs
    q4 = res.questions[3]
    assert q4.number == 4
    assert "1. ह्रस्व स्वर" in q4.explanation
    assert "2. दीर्घ स्वर" in q4.explanation
    assert "3. प्लुत स्वर" in q4.explanation
    assert "[परीक्षा: RPSC LDC / VDO 2021]" in q4.explanation

    q26 = res.questions[25]
    assert q26.number == 26
    assert q26.question == "'र' व्यंजन किस प्रकार का ध्वनि भेद है?"
    assert q26.options == ["प्रकंपी / लुंठित", "पार्श्विक", "संघर्षहीन", "उत्क्षिप्त"]
    assert q26.correct_index == 0  # (A) प्रकंपी / लुंठित

    q50 = res.questions[49]
    assert q50.number == 50
    assert q50.question == "वर्ण किसे कहते हैं?"
    assert q50.options[1] == "वह मूल ध्वनि जिसके खंड न हो सकें"
    assert q50.correct_index == 1  # (B) वह मूल ध्वनि जिसके खंड न हो सकें
    assert "[परीक्षा: Police Constable 2020 / REET]" in q50.explanation

    # Every question must have 4 options and an inline-mapped answer in 0..3
    for idx, q in enumerate(res.questions, start=1):
        assert q.number == idx
        assert len(q.options) == 4
        assert 0 <= q.correct_index < 4
        assert q.answer_source == "inline"


def test_actual_uploaded_topic02_full_txt_bot_upload_creates_50_question_quiz():
    async def t():
        async with Harness() as h:
            u = h.f.user(101)
            raw_bytes = FIXTURE_FULL_TXT.read_bytes()
            h.api.files["TOPIC02_FULL"] = raw_bytes
            await h.send(
                h.f.document(
                    u,
                    "TOPIC02_FULL",
                    "Topic_02_Varnamala_evam_Lipi_Full.txt",
                    "text/plain",
                    len(raw_bytes),
                )
            )

            all_txt = h.all_text(101)
            assert "✅ सही parse हुए प्रश्न: <b>50</b>" in all_txt
            assert "❌ समस्या वाले प्रश्न/हिस्से: <b>0</b>" in all_txt
            assert "• Total detected: 50" in all_txt
            assert "• Parsed: 50" in all_txt
            assert "• Failed: 0" in all_txt
            assert "• Answer mapped: 50" in all_txt

            last_msg = h.api.sent("sendMessage", 101)[-1]
            cbs = [b.get("callback_data") for b in buttons(last_msg.get("reply_markup"))]
            confirm_cb = "i:reviewed" if "i:reviewed" in cbs else "i:create"
            assert confirm_cb in cbs
            await h.cb(u, confirm_cb)
            await h.cb(u, "i:usedefault")

            quizzes = db.get_owner_quizzes(101)
            assert len(quizzes) == 1
            assert quizzes[0]["title"] == "Topic_02_Varnamala_evam_Lipi_Full"
            qs = db.get_questions(quizzes[0]["id"])
            assert len(qs) == 50
            assert qs[0]["question"] == "हिन्दी वर्णमाला में स्वरों की संख्या कितनी मानी गई है?"
            assert qs[0]["options"] == ["10", "11", "13", "14"]
            assert qs[49]["question"] == "वर्ण किसे कहते हैं?"

    run(t())


def test_part2_slice_q26_to_q50_parses_all_25_questions(tmp_path):
    full_text = FIXTURE_FULL_TXT.read_text(encoding="utf-8")
    idx = full_text.index("[प्रश्न 26]")
    part2_text = (
        "====================================================================\n"
        "अध्याय 2: वर्णमाला एवं लिपि — भाग 2 (प्रश्न 26 से 50)\n"
        "====================================================================\n\n"
        + full_text[idx:]
    )
    p2_file = tmp_path / "Topic_02_Varnamala_evam_Lipi_Part2.txt"
    p2_file.write_text(part2_text, encoding="utf-8")

    res = pdf_parser.parse_source(pdf_parser.decode_text_bytes(p2_file.read_bytes()))
    assert res.detected_numbers == list(range(26, 51))
    assert len(res.questions) == 25
    assert res.errors == []
    assert res.questions[0].number == 26
    assert res.questions[-1].number == 50

    qs_dicts = pdf_parser.parse_txt(str(p2_file))
    assert len(qs_dicts) == 25


# ---------------------------------------------------------------------------
# 2. Synthetic tests for question-number, option-label, encoding & error cases
# ---------------------------------------------------------------------------
def test_supported_question_number_formats():
    headers = [
        "[प्रश्न 1]",
        "(प्रश्न 2)",
        "प्रश्न 3.",
        "प्रश्न 4:",
        "प्रश्न संख्या 5",
        "6.",
        "7)",
        "(8)",
        "[9]",
        "Q10",
        "Q.11",
        "Question 12:",
    ]
    blocks = []
    for idx, hdr in enumerate(headers, start=1):
        blocks.append(
            f"{hdr} राजस्थान का प्रश्न क्रमांक {idx} कौन-सा है?\n"
            f"(A) विकल्प एक\n"
            f"(B) विकल्प दो\n"
            f"(C) विकल्प तीन\n"
            f"(D) विकल्प चार\n"
            f"उत्तर: (B)\n"
        )
    text = "\n".join(blocks)
    res = pdf_parser.parse_source(text)
    assert res.errors == []
    assert [q.number for q in res.questions] == list(range(1, 13))
    for idx, q in enumerate(res.questions, start=1):
        assert q.question == f"राजस्थान का प्रश्न क्रमांक {idx} कौन-सा है?"
        assert q.options == ["विकल्प एक", "विकल्प दो", "विकल्प तीन", "विकल्प चार"]
        assert q.correct_index == 1


def test_supported_option_label_formats_including_hindi_absd():
    text = """
[प्रश्न 1] प्रथम प्रश्न (अ/ब/स/द कोष्ठक में)?
(अ) जयपुर
(ब) जोधपुर
(स) उदयपुर
(द) कोटा
उत्तर: (अ)

[प्रश्न 2] द्वितीय प्रश्न (अ) ब) स) द) रूप में)?
अ) अजमेर
ब) बीकानेर
स) भरतपुर
द) पाली
उत्तर: ब

[प्रश्न 3] तृतीय प्रश्न (A) B) C) D) रूप में)?
A) एक
B) दो
C) तीन
D) चार
Answer: C

[प्रश्न 4] चतुर्थ प्रश्न (A. B. C. D. रूप में)?
A. लाल
B. हरा
C. नीला
D. पीला
Answer: D

[प्रश्न 5] पंचम प्रश्न ((a) (b) (c) (d) रूप में)?
(a) पूर्व
(b) पश्चिम
(c) उत्तर
(d) दक्षिण
उत्तर: (c)
"""
    res = pdf_parser.parse_source(text)
    assert res.errors == []
    assert len(res.questions) == 5
    assert [q.correct_index for q in res.questions] == [0, 1, 2, 3, 2]
    assert res.questions[0].options == ["जयपुर", "जोधपुर", "उदयपुर", "कोटा"]
    assert res.questions[1].options == ["अजमेर", "बीकानेर", "भरतपुर", "पाली"]


def test_utf8_bom_crlf_and_multiline_questions_and_options(tmp_path):
    content = (
        "\ufeff[प्रश्न 1] निम्नलिखित कथनों पर विचार कीजिए:\r\n"
        "कथन I: हिन्दी की लिपि देवनागरी है।\r\n"
        "कथन II: देवनागरी बाएँ से दाएँ लिखी जाती है।\r\n"
        "(A) केवल कथन I सही है\r\n"
        "परन्तु कथन II गलत है\r\n"
        "(B) केवल कथन II सही है\r\n"
        "(C) कथन I और कथन II\r\n"
        "दोनों सही हैं\r\n"
        "(D) दोनों गलत हैं\r\n"
        "उत्तर: (C)\r\n"
        "व्याख्या: देवनागरी लिपि बाएँ से दाएँ लिखी जाती है।\r\n"
    )
    f = tmp_path / "bom_crlf.txt"
    f.write_bytes(content.encode("utf-8"))

    res = pdf_parser.parse_source(pdf_parser.decode_text_bytes(f.read_bytes()))
    assert res.errors == []
    assert len(res.questions) == 1
    q = res.questions[0]
    assert "कथन I: हिन्दी की लिपि देवनागरी है।" in q.question
    assert "कथन II: देवनागरी बाएँ से दाएँ लिखी जाती है।" in q.question
    assert q.options[2] == "कथन I और कथन II दोनों सही हैं"
    assert q.correct_index == 2


def test_missing_options_and_missing_answer_reported_accurately():
    text = """
[प्रश्न 1] पहला सही प्रश्न कौन-सा है?
(A) विकल्प 1
(B) विकल्प 2
(C) विकल्प 3
(D) विकल्प 4
उत्तर: (A)

[प्रश्न 2] इस प्रश्न में विकल्प नहीं दिए गए हैं?

[प्रश्न 3] इस प्रश्न में उत्तर नहीं दिया गया है?
(A) एक
(B) दो
(C) तीन
(D) चार
"""
    res = pdf_parser.parse_source(text)
    assert res.detected_numbers == [1, 2, 3]
    assert [q.number for q in res.questions] == [1]
    err_by_num = {e.number: e.message for e in res.errors}
    assert 2 in err_by_num and "options" in err_by_num[2]
    assert 3 in err_by_num and "Answer Not Found" in err_by_num[3]


def test_zero_questions_diagnostics_reflect_actual_content():
    # Case 1: question headers present, but no options anywhere
    res_no_opts = pdf_parser.parse_source(
        "[प्रश्न 1] पहला प्रश्न बिना विकल्प के?\n[प्रश्न 2] दूसरा प्रश्न बिना विकल्प के?\n"
    )
    assert len(res_no_opts.questions) == 0
    assert len(res_no_opts.errors) == 1
    assert "कोई प्रश्न नहीं मिला" in res_no_opts.errors[0].message
    assert "2 प्रश्न-क्रमांक मिले" in res_no_opts.errors[0].message

    # Case 2: option sets present, but no question headers
    res_no_qnums = pdf_parser.parse_source(
        "बिना क्रमांक का प्रश्न\n(A) एक\n(B) दो\n(C) तीन\n(D) चार\nउत्तर: (A)\n"
    )
    assert len(res_no_qnums.questions) == 0
    assert len(res_no_qnums.errors) == 1
    assert "कोई प्रश्न नहीं मिला" in res_no_qnums.errors[0].message
    assert "1 विकल्प-समूह मिले" in res_no_qnums.errors[0].message

    # Case 3: plain notes with neither question headers nor option sets
    res_prose = pdf_parser.parse_source("यह केवल सामान्य नोट्स का पैराग्राफ है।\nइसमें कोई प्रश्न नहीं है।\n")
    assert len(res_prose.questions) == 0
    assert len(res_prose.errors) == 1
    assert "कोई प्रश्न नहीं मिला" in res_prose.errors[0].message
    assert "0 प्रश्न-क्रमांक और 0 विकल्प-समूह" in res_prose.errors[0].message
