"""Final master pass: readable PDFs and immutable per-attempt scoring rules.
All fixtures are original/synthetic; temporary SQLite only.
"""
from __future__ import annotations

import re
from pathlib import Path

import fitz
import pytest

import database as db
import review_pdf as pdf
import quiz_modes
import quiz_runner
from scripts.generate_pdf_samples import sample_report
from tests.test_bot_flow import Harness, isolated, make_quiz, run

_ = isolated


@pytest.mark.parametrize("count", [0, 1, 8, 50])
def test_readable_pdf_has_all_content_and_branding_on_every_page(count):
    rep = sample_report(count)
    raw, pages = pdf.render_report(rep)
    doc = fitz.open(stream=raw, filetype="pdf")
    assert len(doc) == pages
    assert doc[0].rect.width == 420
    text = "\n".join(page.get_text() for page in doc)
    assert "????" not in text
    for i in range(1, count + 1):
        assert f"मौलिक अभ्यास {i}:" in text
    if count:
        assert "24 cm²" in text
        assert "x² + y² = r²" in text
        assert "√16 = 4" in text
        assert "Section-wise Score" in text
    for page in doc:
        assert "LOKESH DAUSA" in page.get_text()
        assert "@LOKESH_DAUSA" in page.get_text()
        assert page.get_images(full=True)
        assert any(l.get("uri") == pdf.OFFICIAL_GROUP_URL and l["from"].y0 > 800 for l in page.get_links())
        assert any("devanagari" in f[3].lower() for f in page.get_fonts())
        for trace in page.get_texttrace():
            if trace["type"] == 3:  # exact Unicode invisible layer is checked separately
                continue
            x0, y0, x1, y1 = trace["bbox"]
            assert x0 >= 24 and x1 <= pdf.PAGE_W - 20
            assert y0 >= 0 and y1 <= pdf.PAGE_H - 8
    doc.close()


def test_source_text_font_sizes_and_positions_never_shrink(monkeypatch):
    captured = []
    original = pdf._Canvas._draw_runs

    def record(self, x, baseline, text, size, color, bold=False, **kw):
        captured.append((self._cur_page_idx, x, baseline, text, size, bold))
        return original(self, x, baseline, text, size, color, bold, **kw)

    monkeypatch.setattr(pdf._Canvas, "_draw_runs", record)
    raw, _ = pdf.render_report(sample_report(8))
    assert raw.startswith(b"%PDF")
    assert pdf.QUESTION_PT >= 12 and pdf.BODY_PT >= 11
    assert 20 <= pdf.TITLE_PT <= 24
    assert 15 <= pdf.SECTION_PT <= 18
    assert 9 <= pdf.FOOTER_PT <= 10
    assert 1.3 <= pdf.LINE_SPACING <= 1.4
    assert any(t == "Complete Quiz Review" and sz == pdf.TITLE_PT for _, _, _, t, sz, _ in captured)
    assert any(t.startswith("प्रश्न 1") and sz >= 12 and bold for _, _, _, t, sz, bold in captured)
    for _, x, baseline, text, size, _ in captured:
        assert baseline > 0 and baseline < pdf.PAGE_H - 8
        assert x >= pdf.MARGIN_L - 0.1
        # Every visible source line is within the printable horizontal bounds.
        assert x + pdf.fonts().width(text, size) <= pdf.PAGE_W - pdf.MARGIN_R + 2, text
        if "मौलिक अभ्यास" in text:
            assert size >= 12
        if "Explanation:" in text:
            assert size >= 11


def test_huge_explanation_flows_without_clipping_or_scaling():
    rep = sample_report(1)
    rep.questions[0].explanation = "\n".join(f"व्याख्या पंक्ति {i}: गणना क्रम सुरक्षित है।" for i in range(160))
    raw, pages = pdf.render_report(rep)
    doc = fitz.open(stream=raw, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    assert pages > 3
    for i in range(160):
        assert f"व्याख्या पंक्ति {i}:" in text
    doc.close()


def test_wrap_preserves_devanagari_clusters_and_all_characters():
    source = "क्षेत्रज्ञान" * 80
    lines = pdf._wrap(source, pdf.QUESTION_PT, 120)
    assert "".join(lines) == source
    for line in lines:
        assert not line.endswith("्")
        assert pdf.fonts().width(line, pdf.QUESTION_PT) <= 120


def test_attempt_snapshots_mode_and_negative_mark_before_first_poll(tmp_path):
    db.set_db_path(tmp_path / "snapshot.db")
    db.init_db()
    qid = db.new_quiz(888, "Original rules", timer=45)
    db.update_quiz(qid, quiz_mode="exam", negative_mark=1 / 3)
    aid = db.create_attempt(qid, 888, 888, [], 45, False, 1000, snapshot_rules=True)
    db.update_quiz(qid, quiz_mode="practice", negative_mark=0)
    att = db.get_attempt(aid)
    assert att["quiz_mode"] == "exam"
    assert att["negative_mark"] == pytest.approx(1 / 3)
    assert quiz_modes.compute_net_score(4, 3, att["negative_mark"]) == 3


def test_running_exam_keeps_answers_hidden_when_owner_changes_mode(monkeypatch):
    monkeypatch.setattr(pdf, "auto_after_finish", _no_pdf)

    async def t():
        async with Harness() as h:
            u = h.f.user(889, "Exam user")
            qid = make_quiz(owner=889, n=2, timer=45)
            db.update_quiz(qid, quiz_mode="exam", negative_mark=1 / 3)
            from types import SimpleNamespace
            aid = await quiz_runner.start_quiz(h.app, SimpleNamespace(**u), 889, qid)
            att = db.get_attempt(aid)
            first = h.api.sent("sendPoll", 889)[-1]
            assert first["type"] == "regular" and "correct_option_id" not in first
            db.update_quiz(qid, quiz_mode="practice", negative_mark=0)
            # Skip is a real path that used to re-read the edited quiz mode.
            await quiz_runner._skip_current_locked(h.app, att, "Skipped", "skipped")
            assert "सही उत्तर:" not in h.all_text(889)
            await quiz_runner.send_current(h.app, aid)
            second = h.api.sent("sendPoll", 889)[-1]
            assert second["type"] == "regular" and "correct_option_ids" not in second
            current = db.get_attempt(aid)
            db.record_answer(aid, current["cur_question_id"], current["current_index"],
                             0, False, "wrong", 1.0)
            # Finish under the original rules, not the edited settings.
            current = db.get_attempt(aid)
            await quiz_runner._finish_locked(h.app, current, "stopped", auto_pdf=False)
            done = db.get_attempt(aid)
            assert done["quiz_mode"] == "exam"
            assert done["negative_mark"] == pytest.approx(1 / 3)
            assert done["net_score"] == pytest.approx(-0.33)
            assert "Net Score" in h.all_text(889) and "-0.33/2" in h.all_text(889)
    run(t())


async def _no_pdf(*args, **kwargs):
    return None


def test_original_fixture_structure_and_roundtrip_for_all_50_questions():
    import pdf_parser
    path = Path(__file__).parent / "fixtures" / "Topic_02_Varnamala_evam_Lipi_Full.txt"
    source = path.read_text(encoding="utf-8")
    assert "स्वयं रचित 50 प्रश्न" in source
    blocks = re.split(r"\[प्रश्न \d+\] ", source)[1:]
    result = pdf_parser.parse_source(source)
    assert result.errors == [] and len(blocks) == len(result.questions) == 50
    for i, (block, q) in enumerate(zip(blocks, result.questions), 1):
        assert q.number == i
        assert q.question == block.splitlines()[0]
        opts = re.findall(r"^\([A-D]\) (.+)$", block, re.M)
        answer = re.search(r"उत्तर: \(([A-D])\)", block).group(1)
        assert q.options == opts
        assert q.correct_index == ord(answer) - ord("A")
        assert q.explanation == block.split("व्याख्या: ", 1)[1].strip()
