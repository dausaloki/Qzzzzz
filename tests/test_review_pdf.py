"""Complete quiz review PDF (feature 2): report building, rendering, delivery.

The PDF itself is inspected through PyMuPDF's text layer, so these tests check
what a reader actually sees: every question, every option in order, the tick /
cross marks, the labels, the explanations and the summary numbers.
"""
import re

import pytest

import config
import database as db
import review_pdf
from review_pdf import Report, ReportQuestion

from test_bot_flow import Harness, run, buttons, make_quiz

G1 = -1001


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never touch data/quiz.db — every test gets its own temporary database."""
    db.set_db_path(tmp_path / "quiz.db")
    db.init_db()
    monkeypatch.setattr(config, "NEXT_QUESTION_DELAY", 0.05)
    monkeypatch.setattr(config, "TIMER_GRACE", 0.2)
    yield
    db.set_db_path(config.DB_PATH)


def _pdf_text(pdf: bytes) -> str:
    import fitz
    doc = fitz.open(stream=pdf, filetype="pdf")
    return "\n".join(page.get_text() for page in doc)


def _pdf_pages(pdf: bytes) -> int:
    import fitz
    return fitz.open(stream=pdf, filetype="pdf").page_count


def _quiz_with_questions(owner=401, n=5, title="राजस्थान भूगोल — Complete Test"):
    db.ensure_user_row(owner)
    qid = db.new_quiz(owner, title, "desc", timer=30)
    rows = [
        ("निम्नलिखित कथनों पर विचार कीजिए:\n1. राजस्थान क्षेत्रफल में सबसे बड़ा राज्य है।\n"
         "2. राजस्थान की राजधानी जयपुर है।\nउपर्युक्त में से कौन सही है?",
         ["केवल 1", "केवल 2", "1 और 2 दोनों", "कोई नहीं"], 2, "दोनों कथन सही हैं।"),
        ("'जय श्री श्याम' का संबंध किस मंदिर से है?",
         ["खाटू श्याम जी", "सालासर बालाजी", "रामदेव जी", "गोगा जी"], 0, ""),
        ("Which is the largest desert in India?",
         ["Thar", "Sahara", "Gobi", "Kalahari"], 0, "The Thar Desert lies in Rajasthan."),
        ("राजस्थान में सर्वाधिक वर्षा कहाँ होती है?",
         ["जैसलमेर", "माउंट आबू", "बीकानेर", "बाड़मेर"], 1, "माउंट आबू में सर्वाधिक वर्षा होती है।"),
        ("2 + 2 = ?  (punctuation: \"quotes\", —dash—, 100%)",
         ["3", "4", "5", "6"], 1, "साधारण जोड़।"),
    ][:n]
    for q, opts, ci, exp in rows:
        db.add_question(qid, q, opts, ci, exp)
    return qid


def _finished_attempt(qid, uid=401, answers=(("correct", 2), ("wrong", 1), ("skipped", None),
                                             ("timeout", None))):
    """Answer the first questions as given; the rest stay unanswered."""
    qs = db.get_questions(qid)
    att = db.create_attempt(qid, uid, uid, [q["id"] for q in qs], 30, False, 1_000_000.0)
    for i, (status, idx) in enumerate(answers):
        if i >= len(qs):
            break
        chosen = idx if idx is not None else None
        db.record_answer(att, qs[i]["id"], i, chosen, status == "correct", status, 2.0)
    return db.finish_attempt(att, "finished", 40.0)


# ------------------------------------------------------------- report building
def test_report_counts_and_order_for_correct_wrong_skipped_unanswered():
    qid = _quiz_with_questions()
    att = _finished_attempt(qid)
    rep = review_pdf.report_for_attempt(att)

    assert (rep.total, rep.correct, rep.incorrect, rep.unanswered) == (5, 1, 1, 3)
    assert rep.attempted == 2 and rep.skipped == 2 and rep.consistent
    assert rep.score == 1 and rep.max_score == 5 and rep.percentage == 20.0
    assert [q.number for q in rep.questions] == [1, 2, 3, 4, 5]
    assert [q.status for q in rep.questions] == [
        "correct", "wrong", "unanswered", "unanswered", "unanswered"]
    assert rep.questions[0].chosen == 2 and rep.questions[1].chosen == 1
    assert rep.questions[0].correct == [2] and rep.questions[1].correct == [0]
    # the skipped/timed-out questions are marked as such but still count as not attempted
    assert rep.questions[2].skipped and rep.questions[3].skipped


def test_report_keeps_source_text_option_order_and_explanations():
    qid = _quiz_with_questions(n=5)
    att = _finished_attempt(qid, answers=())
    rep = review_pdf.report_for_attempt(att)
    qs = db.get_questions(qid)
    for row, src in zip(rep.questions, qs):
        assert row.text == src["question"]
        assert row.options == src["options"]
        assert row.explanation == src["explanation"]


def test_report_for_group_uses_that_users_own_answers_only():
    qid = _quiz_with_questions(owner=402, n=3)
    qs = db.get_questions(qid)
    class U:
        def __init__(self, uid, name):
            self.id, self.username, self.first_name = uid, name.lower(), name

    db.upsert_user(U(402, "Suresh"))
    db.upsert_user(U(403, "Meena"))
    sid = db.create_group_session(qid, G1, 402, [q["id"] for q in qs], 30, False, 1_000_000.0)
    db.record_group_answer(sid, qid, G1, "poll1", qs[0]["id"], 0, 402, 0, 0, True, 1.0, 1_000_001.0)
    db.record_group_answer(sid, qid, G1, "poll1", qs[0]["id"], 0, 403, 1, 1, False, 1.5, 1_000_002.0)
    db.record_group_answer(sid, qid, G1, "poll2", qs[1]["id"], 1, 403, 0, 0, True, 1.0, 1_000_003.0)
    done = db.finish_group_session(sid, "finished", 1_000_100.0)

    a = review_pdf.report_for_group(done, 402)
    assert (a.total, a.correct, a.incorrect, a.unanswered) == (3, 1, 0, 2)
    assert a.questions[0].status == "correct" and a.questions[0].chosen == 0
    assert a.questions[1].status == "unanswered" and a.questions[1].chosen is None
    assert a.mode == "group" and "ID 402" in a.user_label

    b = review_pdf.report_for_group(done, 403)
    assert (b.correct, b.incorrect, b.unanswered) == (1, 1, 1)
    assert b.questions[0].status == "wrong" and b.questions[0].chosen == 1
    assert b.questions[1].status == "correct"


# ------------------------------------------------------------------- rendering
def test_pdf_contains_every_question_option_mark_and_explanation():
    qid = _quiz_with_questions()
    att = _finished_attempt(qid)
    rep = review_pdf.report_for_attempt(att)
    pdf, pages = review_pdf.render_report(rep)
    assert pages >= 1 and len(pdf) > 20_000
    text = _pdf_text(pdf)

    # 1. every question and every option, in the original order
    for row in rep.questions:
        assert f"प्रश्न {row.number}" in text
        for opt in row.options:
            assert opt in text
    pos = -1
    for n in range(1, 6):
        nxt = text.find(f"प्रश्न {n}")
        assert nxt > pos, f"question {n} is out of order"
        pos = nxt
    first_q = rep.questions[0]
    opt_positions = [text.find(o, text.find("प्रश्न 1")) for o in first_q.options]
    assert opt_positions == sorted(opt_positions) and -1 not in opt_positions

    # 2. ticks / crosses and the text labels (colour is never the only signal)
    assert "✓" in text and "✗" in text
    assert "CORRECT" in text and "INCORRECT" in text and "NOT ATTEMPTED" in text
    assert "सही" in text and "गलत" in text and "अनुत्तरित" in text
    assert "YOUR ANSWER" in text and "CORRECT ANSWER" in text

    # 3. explanations are shown verbatim
    assert "दोनों कथन सही हैं।" in text
    assert "The Thar Desert lies in Rajasthan." in text

    # 4. summary block: every required number, and the totals add up
    assert "Complete Quiz Review" in text
    assert rep.quiz_title in text
    assert "Lokesh" in text or "User 401" in text
    assert "ID 401" in text
    assert "Total questions" in text and "Attempted" in text and "Percentage" in text
    assert "20.00%" in text and "1 / 5" in text
    assert "Negative marking" in text
    assert "सभी प्रश्नों की संख्या" in text or "सही + गलत + अनुत्तरित" in text

    # 5. Hindī text really goes through the embedded Devanagari font
    assert "राजस्थान" in text and "जयपुर" in text


def test_pdf_marks_the_users_answer_and_the_correct_answer():
    rep = Report("T", "U (@u) · ID 1", "private", "2026-10-17 05:10 UTC", "2026-10-17 10:40 IST",
                 1, 1, 0, 1, 0, 0, 0, 1, 0.0,
                 [ReportQuestion(1, "प्रश्न?", ["क", "ख", "ग", "घ"], 3, [1], "wrong", "क्योंकि ख")])
    text = _pdf_text(review_pdf.render_report(rep)[0])
    # the chosen wrong option carries ✗ + the "your answer" label on the next line,
    # the correct option carries ✓ + the "correct answer" label
    lines = text.split("\n")
    iw = next(i for i, ln in enumerate(lines) if "(D) घ" in ln)
    ir = next(i for i, ln in enumerate(lines) if "(B) ख" in ln)
    assert "✗" in lines[iw] and "✓" not in lines[iw]
    assert "YOUR ANSWER" in lines[iw + 1] and "INCORRECT" in lines[iw + 1]
    assert "✓" in lines[ir] and "✗" not in lines[ir]
    assert "CORRECT ANSWER" in lines[ir + 1]


def test_pdf_unanswered_question_shows_options_and_verified_answer_only():
    rep = Report("T", "U · ID 1", "private", "2026-10-17 05:10 UTC", "2026-10-17 10:40 IST",
                 1, 0, 0, 0, 1, 0, 0, 1, 0.0,
                 [ReportQuestion(1, "अनुत्तरित प्रश्न?", ["क", "ख", "ग"], None, [2], "unanswered")])
    text = _pdf_text(review_pdf.render_report(rep)[0])
    assert "प्रश्न 1" in text and "अनुत्तरित प्रश्न?" in text
    for opt in ("क", "ख", "ग"):
        assert opt in text
    assert "NOT ATTEMPTED" in text
    assert "CORRECT ANSWER" in text and "✓" in text
    assert "YOUR ANSWER" not in text                       # no invented selection


def test_pdf_unverified_answer_is_labelled_not_guessed():
    rep = Report("T", "U · ID 1", "private", "2026-10-17 05:10 UTC", "2026-10-17 10:40 IST",
                 1, 0, 0, 0, 1, 0, 0, 1, 0.0,
                 [ReportQuestion(1, "उत्तर नहीं पता?", ["क", "ख"], None, [], "unanswered")])
    pdf, pages = review_pdf.render_report(rep)              # must not raise
    text = _pdf_text(pdf)
    assert "NOT VERIFIED" in text and "सत्यापित नहीं" in text


def test_pdf_marks_every_answer_of_a_multi_answer_set():
    rep = Report("T", "U · ID 1", "private", "2026-10-17 05:10 UTC", "2026-10-17 10:40 IST",
                 1, 1, 1, 0, 0, 0, 1, 1, 100.0,
                 [ReportQuestion(1, "दो सही विकल्प?", ["क", "ख", "ग", "घ"], 0, [0, 2], "correct")])
    text = _pdf_text(review_pdf.render_report(rep)[0])
    marked = [ln for ln in text.split("\n") if "✓" in ln and re.search(r"\((A|C)\)", ln)]
    assert len(marked) == 2


def test_large_quiz_pdf_is_complete_and_paginated():
    qs = [ReportQuestion(i, f"{i}. लंबा प्रश्न — " + "विवरण " * 8,
                         ["विकल्प एक " + "x" * 40, "विकल्प दो", "विकल्प तीन", "विकल्प चार"],
                         (i - 1) % 4, [(i + 1) % 4], "correct" if i % 2 else "wrong",
                         "व्याख्या " * 10, section="भाग-अ" if i <= 25 else "भाग-ब")
          for i in range(1, 61)]
    rep = Report("बड़ा टेस्ट", "U · ID 1", "private", "2026-10-17 05:10 UTC", "2026-10-17 10:40 IST",
                 60, 60, 30, 30, 0, 0, 30, 60, 50.0, qs)
    pdf, pages = review_pdf.render_report(rep)
    assert pages >= 5
    text = _pdf_text(pdf)
    assert [i for i in range(1, 61) if f"प्रश्न {i}" not in text] == []
    assert f"Page 1/{pages}" in text and f"Page {pages}/{pages}" in text
    first = text.find("प्रश्न 1")
    last = text.rfind("प्रश्न 60")
    assert 0 < first < last


def test_pdf_exact_text_preservation():
    weird = "  \"Quoted\" — dash — 'single' 50% & <tag> ₹100 ???  "
    rep = Report("टेस्ट — \"name\"", "U · ID 1", "private", "2026-10-17 05:10 UTC",
                 "2026-10-17 10:40 IST", 1, 1, 1, 0, 0, 0, 1, 1, 100.0,
                 [ReportQuestion(1, weird, [weird, "second"], 0, [0], "correct", weird)])
    text = _pdf_text(review_pdf.render_report(rep)[0])
    flat = " ".join(text.split())
    assert "\"Quoted\"" in flat and "50%" in flat and "₹100" in flat


def test_filename_and_caption_are_telegram_safe():
    rep = Report("राजस्थान / GK: टेस्ट #1", "Lokesh (@loki) · ID 7", "private",
                 "2026-10-17 05:10 UTC", "2026-10-17 10:40 IST", 3, 2, 1, 1, 1, 0, 1, 3, 33.33, [])
    name = review_pdf.pdf_filename(rep)
    assert name.startswith("review_") and name.endswith(".pdf")
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name)
    cap = review_pdf.summary_caption(rep)
    assert "Complete Review PDF" in cap and "1/3" in cap and "33.33%" in cap
    assert len(cap) <= 1024


# ------------------------------------------------------------------- delivery
def test_auto_pdf_is_sent_after_the_result_and_only_once():
    async def t():
        async with Harness() as h:
            u = h.f.user(501, "Lokesh", "loki")
            qid = make_quiz(owner=501, n=3, timer=30, title="Auto PDF Quiz")
            await h.start(u, qid)
            for i in range(3):
                await h.wait(lambda: len(h.polls_for(501)) == i + 1)
                await h.answer_current(u, True)
            await h.wait(lambda: h.api.sent("sendDocument", 501), msg="review PDF")
            await h.wait(lambda: "🏁" in h.all_text(501), msg="result")

            docs = h.api.sent("sendDocument", 501)
            assert len(docs) == 1
            assert "Complete Review PDF" in docs[0]["caption"]
            assert "3/3" in docs[0]["caption"]
            upload = [f for a, p, f in h.api.uploads if a == "sendDocument"][-1]
            name, data = upload["document"]
            assert name.startswith("review_") and name.endswith(".pdf")
            text = _pdf_text(data)
            for i in range(1, 4):
                assert f"प्रश्न {i}" in text
                assert f"o{i - 1}a" in text
            rec = db.review_pdf_get(attempt_id=db.user_history(501)[0]["id"])
            assert rec["status"] == "sent" and rec["bytes"] > 0 and rec["pages"] >= 1

            # a second call must not duplicate the document
            att = db.get_attempt(rec["attempt_id"])
            res = await review_pdf.deliver_attempt(h.app.bot, att)
            assert res["skipped"] == "already-sent"
            assert len(h.api.sent("sendDocument", 501)) == 1

    run(t())


def test_explicit_review_command_resends_the_pdf():
    async def t():
        async with Harness() as h:
            u = h.f.user(502, "Meena", "meena")
            qid = make_quiz(owner=502, n=2, timer=30, title="Cmd PDF")
            await h.start(u, qid)
            for i in range(2):
                await h.wait(lambda: len(h.polls_for(502)) == i + 1)
                await h.answer_current(u, True)
            await h.wait(lambda: h.api.sent("sendDocument", 502), msg="auto PDF")
            await h.text(u, "/review")
            await h.wait(lambda: len(h.api.sent("sendDocument", 502)) == 2, msg="resent PDF")
            assert "3/3" not in h.api.sent("sendDocument", 502)[-1]["caption"]

    run(t())


def test_review_command_without_finished_quiz_is_explained():
    async def t():
        async with Harness() as h:
            u = h.f.user(503)
            await h.text(u, "/review")
            assert "कोई पूरा हुआ quiz नहीं" in h.api.texts(503)[-1]
            assert h.api.sent("sendDocument", 503) == []

    run(t())


def test_user_who_never_started_the_bot_gets_instructions_not_a_crash():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=504, n=2, timer=30)
            att = _finished_attempt(qid, uid=504, answers=())
            att = db.get_attempt(att["id"])
            h.api.fail_next["sendDocument"] = "Forbidden: bot can't initiate conversation with a user"
            h.api.fail_code["sendDocument"] = 403
            res = await review_pdf.deliver_attempt(h.app.bot, att)
            assert res["ok"] is False and res["blocked"] is True
            rec = db.review_pdf_get(attempt_id=att["id"])
            assert rec["status"] == "blocked" and "Forbidden" in rec["error"]
            # the failure did not corrupt the attempt or the result flow
            assert db.get_attempt(att["id"])["status"] == "finished"

    run(t())


def test_failed_delivery_is_retried_and_stops_after_success():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=505, n=2, timer=30)
            att = _finished_attempt(qid, uid=505)

            calls = {"n": 0}
            real_send = review_pdf._send

            async def flaky(bot, chat_id, pdf, filename, caption):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("network down")
                return await real_send(bot, chat_id, pdf, filename, caption)

            review_pdf._send = flaky
            try:
                res = await review_pdf.auto_after_finish(h.app, att)
                assert not res.get("ok")
                assert db.review_pdf_get(attempt_id=att["id"])["status"] == "failed"
                jobs = [j for j in h.app.job_queue.jobs() if j.name and "review_pdf_retry" in j.name]
                assert len(jobs) == 1                  # a retry was queued

                # run the retry job by hand, exactly like the JobQueue would
                job = jobs[0]
                ctx = type("C", (), {"job": job, "bot": h.app.bot, "application": h.app})()
                job.data["tries"] = 1
                await review_pdf._retry_job(ctx)
                assert h.api.sent("sendDocument", 505)
                assert db.review_pdf_get(attempt_id=att["id"])["status"] == "sent"

                await review_pdf._retry_job(ctx)       # already sent → nothing more happens
                assert len(h.api.sent("sendDocument", 505)) == 1
            finally:
                review_pdf._send = real_send

    run(t())


def test_group_button_sends_the_pdf_privately_to_the_asking_member():
    async def t():
        async with Harness() as h:
            qid = _quiz_with_questions(owner=506, n=3)
            qs = db.get_questions(qid)
            sid = db.create_group_session(qid, G1, 506, [q["id"] for q in qs], 30, False,
                                          1_000_000.0)
            for i, correct in ((0, True), (1, False)):
                db.record_group_answer(sid, qid, G1, f"p{i}", qs[i]["id"], i, 507, 0, 0, correct,
                                       1.0, 1_000_001.0 + i)
            db.finish_group_session(sid, "finished", 1_000_100.0)
            u = h.f.user(507, "Suresh", "suresh")
            await h.cb(u, f"p:g:{sid}", chat_id=G1)
            await h.wait(lambda: h.api.sent("sendDocument", 507), msg="private PDF")
            assert h.api.sent("sendDocument", G1) == []      # never posted into the group
            doc = h.api.sent("sendDocument", 507)[0]
            assert "Complete Review PDF" in doc["caption"]
            text = _pdf_text([f for a, p, f in h.api.uploads if a == "sendDocument"][-1]["document"][1])
            assert "प्रश्न 1" in text and "प्रश्न 3" in text
            assert "3" in text
            # pressing the button again re-sends on explicit request (never silently twice)
            await h.cb(u, f"p:g:{sid}", chat_id=G1)
            await h.wait(lambda: len(h.api.sent("sendDocument", 507)) == 2, msg="second PDF")

    run(t())


def test_group_auto_off_by_default_but_button_is_always_there(monkeypatch):
    # a group question closes when its timer runs out, so keep it short like the
    # existing group tests do (the timer itself is covered by test_round5)
    monkeypatch.setattr(config, "GROUP_DEFAULT_TIMER", 1)
    monkeypatch.setattr(config, "GROUP_NEXT_DELAY", 0.05)

    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=510, n=2, timer=0, title="Group Result")
            u = h.f.user(510)
            await h.text(u, f"/start@TestQuizBot quiz_{qid}", chat_id=G1)
            await h.cb(u, f"g:go:{qid}", chat_id=G1)
            for i in range(2):
                await h.wait(lambda: len(h.polls_for(G1)) == i + 1)
                pid, poll = h.polls_for(G1)[-1]
                await h.send(h.f.poll_answer(u, pid, poll["correct_option_id"]))
            await h.wait(lambda: "🏆" in h.all_text(G1), msg="group result")
            with db.connect() as con:
                sid = con.execute("SELECT id FROM group_sessions WHERE chat_id=? ORDER BY id DESC",
                                  (G1,)).fetchone()[0]
            marked = [m for m in h.api.sent("sendMessage", G1) if m.get("reply_markup")]
            cbs = [b.get("callback_data") for m in marked for b in buttons(m["reply_markup"])]
            assert f"p:g:{sid}" in cbs
            assert h.api.sent("sendDocument") == []          # REVIEW_PDF_GROUP_AUTO default 0

    run(t())


def test_group_button_without_private_start_explains_start():
    async def t():
        async with Harness() as h:
            qid = _quiz_with_questions(owner=511, n=2)
            qs = db.get_questions(qid)
            sid = db.create_group_session(qid, G1, 511, [q["id"] for q in qs], 30, False,
                                          1_000_000.0)
            db.finish_group_session(sid, "finished", 1_000_100.0)
            u = h.f.user(512)
            h.api.fail_next["sendDocument"] = "Forbidden: bot can't initiate conversation with a user"
            h.api.fail_code["sendDocument"] = 403
            await h.cb(u, f"p:g:{sid}", chat_id=G1)
            alerts = [p.get("text") for a, p in h.api.calls if a == "answerCallbackQuery"]
            assert any("start" in (t or "").lower() for t in alerts), alerts
            assert h.api.sent("sendDocument", G1) == []

    run(t())


def test_review_command_is_refused_in_a_group_and_points_to_private_chat():
    async def t():
        async with Harness() as h:
            u = h.f.user(513)
            await h.text(u, "/review", chat_id=G1)
            assert "private" in h.api.texts(G1)[-1]
            assert h.api.sent("sendDocument") == []

    run(t())


def test_disabled_by_config_sends_nothing(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_PDF_ENABLED", False)

    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=514, n=2, timer=30)
            att = _finished_attempt(qid, uid=514)
            res = await review_pdf.deliver_attempt(h.app.bot, att, force=True)
            assert res["skipped"] == "disabled"
            assert h.api.sent("sendDocument") == []

    run(t())


def test_pdf_hindi_unicode_matras_conjuncts_and_no_question_marks():
    """Verify Hindi matras, conjuncts, headers, footers, labels and explanations
    across multiple pages with zero '????' corruption."""
    import fitz

    rep = Report(
        quiz_title="राजस्थान का सामान्य ज्ञान — परीक्षा श्रृंखला",
        user_label="लोकेश कुमार मीणा (राजस्थान प्रशासनिक सेवा अभ्यर्थी) (@lokesh) · ID 99",
        mode="group",
        when_utc="2026-10-18 07:25 UTC",
        when_local="2026-10-18 12:55 IST",
        total=4, attempted=2, correct=1, incorrect=1, unanswered=2, skipped=1,
        score=1, max_score=4, percentage=25.0,
        stopped=True,
        questions=[
            ReportQuestion(
                1,
                "निम्नलिखित में से राजस्थान की चित्रकला की शैलियों और उनके प्रमुख चित्रकारों का सही मिलान कीजिए:\n"
                "कथन I: किशनगढ़ शैली का स्वर्णकाल सावंत सिंह (नागरीदास) का शासनकाल माना जाता है।\n"
                "कथन II: नाथद्वारा शैली में 'पिछवाई चित्रण' प्रमुख विशेषता है, जिसमें कमला और इलायची प्रसिद्ध हैं।\n"
                "कथन III: 'चावंड शैली' के प्रमुख चित्रकार नसीरुद्दीन (निसारदी) थे।\n"
                "उपर्युक्त कथनों में से कौन-सा/से सही है/हैं?",
                ["केवल I तथा II", "केवल II तथा III", "केवल I तथा III", "I, II तथा III सभी"],
                1, [3], "wrong",
                explanation="तीनों कथन राजस्थान की चित्रशैलियों (किशनगढ़, नाथद्वारा, चावंड) के संदर्भ में पूर्णतः सत्य हैं।",
                section="भाग-अ: राजस्थान की कला एवं संस्कृति",
            ),
            ReportQuestion(
                2,
                "क्षेत्रफल की दृष्टि से भारत का सबसे बड़ा राज्य कौन-सा है?",
                ["राजस्थान", "मध्य प्रदेश", "महाराष्ट्र", "उत्तर प्रदेश"],
                0, [0], "correct",
                explanation="क्षेत्रफल की दृष्टि से राजस्थान भारत का सबसे बड़ा राज्य है।",
                section="भाग-अ: राजस्थान की कला एवं संस्कृति",
            ),
            ReportQuestion(
                3,
                "सुमेलित कीजिए:\nसूची-I (संप्रदाय) / सूची-II (प्रमुख पीठ)\n"
                "(A) जसनाथी संप्रदाय — (1) कतरियासर, बीकानेर\n"
                "(B) विश्नोई संप्रदाय — (2) मुकाम तालवा, नोखा\n"
                "(C) रामस्नेही संप्रदाय — (3) शाहपुरा, भीलवाड़ा\n"
                "(D) दादूपंथी संप्रदाय — (4) नरेना (नारायणा), जयपुर",
                ["A-4, B-2, C-3, D-1", "A-3, B-2, C-4, D-1", "A-1, B-2, C-3, D-4", "A-4, B-3, C-2, D-1"],
                None, [2], "unanswered",
                explanation="जसनाथी संप्रदाय की प्रमुख पीठ कतरियासर (बीकानेर) तथा दादूपंथी की नरेना (जयपुर) में स्थित है।",
                section="भाग-ब: भारतीय संविधान एवं राजव्यवस्था",
                skipped=True,
            ),
            ReportQuestion(
                4,
                "भारतीय संविधान की प्रस्तावना एवं मूल अधिकारों के संदर्भ में विचार कीजिए:\n"
                "कथन I: 42वें संविधान संशोधन अधिनियम, 1976 द्वारा प्रस्तावना में 'समाजवादी', 'पंथनिरपेक्ष' और 'अखंडता' शब्द जोड़े गए।\n"
                "कथन II: अनुच्छेद 32 को डॉ. भीमराव अंबेडकर ने 'संविधान की आत्मा और हृदय' कहा है।\n"
                "कथन III: संपत्ति के अधिकार को 44वें संशोधन द्वारा मूल अधिकारों की श्रेणी से हटाकर विधिक अधिकार बनाया गया।",
                ["केवल I तथा II", "केवल II तथा III", "केवल I तथा III", "I, II तथा III सभी"],
                None, [3], "unanswered",
                explanation="तीनों कथन भारतीय संविधान के संदर्भ में पूर्णतः सत्य हैं।",
                section="भाग-ब: भारतीय संविधान एवं राजव्यवस्था",
            ),
        ],
    )
    pdf, pages = review_pdf.render_report(rep)
    assert pages >= 2
    doc = fitz.open(stream=pdf, filetype="pdf")
    full_text = "\n".join(page.get_text() for page in doc)

    assert "????" not in full_text
    for word in (
        "राजस्थान का सामान्य ज्ञान — परीक्षा श्रृंखला",
        "लोकेश कुमार मीणा",
        "परिणाम सारांश",
        "उपयोगकर्ता",
        "दिनांक-समय",
        "कुल प्रश्न",
        "प्रयास किए",
        "अनुत्तरित",
        "प्राप्तांक",
        "प्रतिशत",
        "ऋणात्मक अंकन",
        "लागू नहीं (हर सही उत्तर पर 1 अंक)",
        "सभी प्रश्नों की संख्या = सही + गलत + अनुत्तरित ✓",
        "किशनगढ़",
        "स्वर्णकाल",
        "नाथद्वारा",
        "प्रसिद्ध",
        "नसीरुद्दीन",
        "उपर्युक्त",
        "क्षेत्रफल",
        "दृष्टि",
        "विश्नोई",
        "रामस्नेही",
        "पंथनिरपेक्ष",
        "अनुच्छेद",
        "हृदय",
        "संपत्ति",
        "श्रेणी",
        "पूर्णतः",
        "← आपका उत्तर · गलत (YOUR ANSWER · INCORRECT)",
        "← आपका उत्तर · सही (YOUR ANSWER · CORRECT)",
        "← सही उत्तर (CORRECT ANSWER)",
        f"Page 1/{pages}",
        f"Page {pages}/{pages}",
    ):
        assert word in full_text, f"missing Hindi/label text: {word!r}"

    for pno, page in enumerate(doc, 1):
        pfonts = [f[3] for f in page.get_fonts()]
        assert any("devanagari" in (fn or "").lower() for fn in pfonts), f"page {pno} fonts: {pfonts}"
    doc.close()


def test_pdf_renders_hindi_without_external_font_files(monkeypatch, tmp_path):
    """Even if the fonts/ directory is missing on the server, built-in MuPDF
    Devanagari fonts must be extracted and embedded with zero '????' output."""
    import fitz

    monkeypatch.setattr(review_pdf, "_BUILTIN_CACHE_DIR", tmp_path / "builtin_fonts")
    monkeypatch.setattr(review_pdf.FontBook, "_candidates", lambda self, name: [])
    review_pdf._FONTS.clear()
    try:
        fb = review_pdf.fonts()
        assert not fb.degraded
        rep = Report(
            "राजस्थान सामान्य ज्ञान",
            "राहुल शर्मा (@rahul) · ID 5",
            "private",
            "2026-10-18 07:25 UTC",
            "2026-10-18 12:55 IST",
            2, 1, 0, 1, 1, 0, 0, 2, 0.0,
            [
                ReportQuestion(1, "भारत की राजधानी क्या है?", ["मुंबई", "नई दिल्ली", "जयपुर", "कोलकाता"],
                               0, [1], "wrong", "नई दिल्ली भारत की राजधानी है।"),
                ReportQuestion(2, "राजस्थान की राजधानी कौन-सी है?", ["जोधपुर", "जयपुर", "उदयपुर", "कोटा"],
                               None, [1], "unanswered"),
            ],
        )
        pdf, pages = review_pdf.render_report(rep)
        assert pages >= 1
        doc = fitz.open(stream=pdf, filetype="pdf")
        txt = "\n".join(page.get_text() for page in doc)
        font_names = [f[3] for page in doc for f in page.get_fonts()]
        doc.close()
        assert "????" not in txt
        for w in ("राजस्थान सामान्य ज्ञान", "राहुल शर्मा", "परिणाम सारांश", "नई दिल्ली", "जयपुर", "✓", "✗"):
            assert w in txt
        assert any("devanagari" in (fn or "").lower() for fn in font_names), font_names
    finally:
        review_pdf._FONTS.clear()


def test_pdf_font_status_self_check():
    ok, info = review_pdf.pdf_font_status()
    assert ok, info
    assert "dev=" in info and "embedded=" in info

