"""Regression tests for the safety-audit fixes (issues 2-5).

* Issue 2 — backup/restore includes batches, batch members, authorized chats
  and batch/private access tiers; restores them safely and refuses outdated
  target schemas instead of dropping data.
* Issue 3 (payments / premium) no longer applies: the payment and premium
  feature was removed from the expansion — see ``tests/test_no_payments.py``.
* Issue 4 — negative marking: one label/number everywhere (result text, HTML,
  PDF, group result, statistics), disabled by default, reports use the value
  stored on the attempt / group session (fixes the HTML default-1/3 bug).
* Issue 5 — FREE_BOT removed and reported as ignored; the dashboard shows no
  payment / webhook settings at all; BOT_GROUP/CHANNEL_ID display only.

Everything runs on temporary SQLite files — never on a production database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import admin_tools
import backup
import config
import database as db
import group_runner
import html_report
import keyboards as kb
import quiz_modes
import quiz_runner
import review_pdf
import stats
from tests.test_bot_flow import isolated

_ = isolated
ROOT = Path(__file__).resolve().parents[1]
THIRD = 1.0 / 3.0


# ============================================================ helpers
class _U:
    def __init__(self, uid, name):
        self.id, self.username, self.first_name = uid, name.lower(), name


def _quiz(owner=700, n=3, title="Audit Quiz"):
    db.upsert_user(_U(owner, f"Owner{owner}"))
    qid = db.new_quiz(owner, title, "", timer=30, status="ready")
    for i in range(n):
        db.add_question(qid, f"प्रश्न {i + 1}?", ["A", "B", "C", "D"], 0, f"व्याख्या {i + 1}")
    return qid


def _attempt(qid, uid, statuses, duration=30.0, neg=None):
    """Finished private attempt; statuses like ('correct','wrong','skipped')."""
    db.upsert_user(_U(uid, f"User{uid}"))
    qs = db.get_questions(qid)
    att = db.create_attempt(qid, uid, uid, [q["id"] for q in qs], 30, False, 1_000_000.0)
    for i, st in enumerate(statuses):
        chosen = 0 if st == "correct" else (1 if st == "wrong" else None)
        db.record_answer(att, qs[i]["id"], i, chosen, st == "correct", st, 2.0)
    done = db.finish_attempt(att, "finished", duration)
    if neg is not None:
        net = quiz_modes.compute_net_score(done["correct"], done["wrong"], neg)
        db.update_attempt_metadata(att, negative_mark=neg, net_score=net)
    return db.get_attempt(att)


# ============================================================ issue 5 — config
def test_misleading_config_options_removed_and_reported(monkeypatch):
    assert not hasattr(config, "FREE_BOT")
    assert not hasattr(config, "RAZORPAY_WEBHOOK_SECRET")
    monkeypatch.setenv("FREE_BOT", "1")
    # a leftover payment variable is simply not read by the bot (feature removed)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec_should_never_be_shown")
    names = [n for n, _ in config.unsupported_env_settings()]
    assert names == ["FREE_BOT"]
    text = admin_tools.admin_dashboard_text()
    for word in ("Razorpay", "Webhook", "Premium", "refund", "/paycheck", "/grantpremium"):
        assert word.lower() not in text.lower(), word
    assert "whsec_should_never_be_shown" not in text
    assert "FREE_BOT" in text and "display only" in text
    monkeypatch.delenv("FREE_BOT")
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET")
    assert config.unsupported_env_settings() == []


def test_env_example_no_longer_offers_unimplemented_settings():
    lines = [ln.strip() for ln in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()]
    active = [ln for ln in lines if ln and not ln.startswith("#")]
    assert not any(ln.startswith(("FREE_BOT=", "RAZORPAY_", "PREMIUM_", "PARTIAL_REFUND_POLICY="))
                   for ln in active)


# ============================================================ issue 4 — negative marking
def test_negative_marking_is_disabled_by_default():
    qid = _quiz()
    assert float(db.get_quiz(qid)["negative_mark"] or 0) == 0
    sid = db.create_group_session(qid, -100500, 700, [q["id"] for q in db.get_questions(qid)],
                                  30, False, 1_000_000.0)
    assert float(db.get_group_session(sid)["negative_mark"] or 0) == 0


def test_labels_and_score_format_are_consistent():
    assert quiz_modes.negative_label(THIRD) == "1/3"
    assert quiz_modes.negative_label(round(THIRD, 6)) == "1/3"     # value stored by the editor
    assert quiz_modes.negative_label(0.25) == "1/4"
    assert quiz_modes.negative_label(0.5) == "1/2"
    assert quiz_modes.negative_label(0) == "" and quiz_modes.negative_label(None) == ""
    assert quiz_modes.format_score(2.0) == "2" and quiz_modes.format_score(5 / 3) == "1.67"
    qid = _quiz()
    db.update_quiz(qid, negative_mark=round(THIRD, 6))
    quiz = db.get_quiz(qid)
    assert "-1/3" in quiz_runner.ready_text(quiz) and "0.333333" not in quiz_runner.ready_text(quiz)
    labels = [b["text"] for row in kb.quiz_mode_menu(qid, quiz).to_dict()["inline_keyboard"] for b in row]
    assert any("-1/3" in lbl for lbl in labels)
    db.update_quiz(qid, negative_mark=0)
    assert "Negative" not in quiz_runner.ready_text(db.get_quiz(qid))


def test_private_result_text_unchanged_without_negative_and_labelled_with_it():
    qid = _quiz()
    att = _attempt(qid, 701, ("correct", "correct", "wrong"))
    plain = quiz_runner.result_text(att, "Audit Quiz", False)
    assert "Net Score" not in plain and "Negative" not in plain
    neg = quiz_runner.result_text(att, "Audit Quiz", False, negative_mark=THIRD,
                                  net_score=quiz_modes.compute_net_score(2, 1, THIRD))
    assert "Net Score: <b>1.67/3</b>" in neg and "-1/3" in neg and "0.333" not in neg


def test_html_report_without_negative_marking_does_not_deduct(monkeypatch):
    """Regression for the HTML default-1/3 bug: a normal quiz must show the plain score."""
    qid = _quiz()
    att = _attempt(qid, 702, ("correct", "correct", "wrong"))
    data, _name, _meta = html_report.build_html_report_for_attempt(att["id"])
    page = data.decode("utf-8")
    assert "🏆 Score" in page and "2 / 3" in page
    assert "Net Score" not in page and "1.67" not in page and "(-1/3)" not in page


def test_html_and_pdf_use_the_negative_mark_stored_on_the_attempt():
    qid = _quiz()
    db.update_quiz(qid, negative_mark=THIRD)
    att = _attempt(qid, 703, ("correct", "correct", "wrong"), neg=THIRD)
    db.update_quiz(qid, negative_mark=0.5)               # later edit must not change old reports
    page = html_report.build_html_report_for_attempt(att["id"])[0].decode("utf-8")
    assert "1.67 / 3" in page and "Net Score" in page and "-1/3 each" in page

    rep = review_pdf.report_for_attempt(att)
    assert rep.negative_mark == pytest.approx(THIRD) and rep.net_score == pytest.approx(1.67)
    assert "1/3" in rep.negative and rep.score == 2      # raw score field unchanged
    pdf, _pages = review_pdf.render_report(rep)
    import fitz
    text = "".join(p.get_text() for p in fitz.open(stream=pdf, filetype="pdf"))
    assert "Net score" in text and "1.67" in text

    plain = review_pdf.report_for_attempt(_attempt(qid, 704, ("correct", "wrong")))
    assert plain.net_score is None and plain.negative_mark == 0
    assert plain.negative == review_pdf.Report.__dataclass_fields__["negative"].default


def test_group_result_report_and_ranking_use_session_snapshot():
    qid = _quiz(n=3)
    qs = db.get_questions(qid)
    for uid in (711, 712):
        db.upsert_user(_U(uid, f"G{uid}"))

    def play(neg):
        sid = db.create_group_session(qid, -100600, 700, [q["id"] for q in qs], 30, False,
                                      1_000_000.0, negative_mark=neg)
        # 711: 2 correct + 1 wrong, fastest; 712: 2 correct, 0 wrong, slower
        for i, (ok, el) in enumerate(((True, 1.0), (True, 1.0), (False, 1.0))):
            db.record_group_answer(sid, qid, -100600, f"p{sid}{i}", qs[i]["id"], i, 711,
                                   0 if ok else 1, 0 if ok else 1, ok, el, 1_000_001.0 + i)
        for i in range(2):
            db.record_group_answer(sid, qid, -100600, f"p{sid}{i}", qs[i]["id"], i, 712,
                                   0, 0, True, 5.0, 1_000_010.0 + i)
        return db.finish_group_session(sid, "finished", 1_000_100.0)

    plain = group_runner.result_text(play(0.0), "G", False)
    assert "Net" not in plain and "Negative" not in plain
    assert plain.index("G711") < plain.index("G712")     # unchanged: correct, then speed

    done = play(THIRD)
    neg = group_runner.result_text(done, "G", False)
    assert "-1/3" in neg and "Net 2/3" in neg and "Net 1.67/3" in neg
    assert neg.index("G712") < neg.index("G711")         # ranked by net score
    rep = review_pdf.report_for_group(done, 711)
    assert rep.net_score == pytest.approx(1.67) and "1/3" in rep.negative


def test_statistics_rank_by_net_only_when_negative_marking_was_used():
    qid = _quiz()
    a = _attempt(qid, 721, ("correct", "correct", "wrong"), duration=10.0)
    b = _attempt(qid, 722, ("correct", "correct", "skipped"), duration=40.0)
    _, leaders = db.quiz_stats(qid)
    assert [r["user_id"] for r in leaders][:2] == [721, 722]           # legacy order kept
    assert db.user_rank(qid, 721)[0] == 1
    assert "Net" not in stats._history_line(a)

    for att, neg in ((a, THIRD), (b, THIRD)):
        db.update_attempt_metadata(att["id"], negative_mark=neg,
                                   net_score=quiz_modes.compute_net_score(att["correct"], att["wrong"], neg))
    _, leaders = db.quiz_stats(qid)
    assert [r["user_id"] for r in leaders][:2] == [722, 721]
    assert db.user_rank(qid, 722)[0] == 1 and db.user_rank(qid, 721)[0] == 2
    line = stats._history_line(db.get_attempt(a["id"]))
    assert "Net 1.67 (-1/3)" in line
    assert "Net" in stats.quiz_stats_text(qid, 721)


# ============================================================ issue 2 — backup/restore
def _seed_expansion():
    qid = _quiz(owner=800)
    db.upsert_user(_U(801, "Member"))
    db.create_batch("batch-A", "बैच", created_by=800)
    db.add_user_to_batch("batch-A", 801, added_by=800)
    db.add_authorized_chat(-100777, "Class Group", "batch-A", added_by=800)
    db.update_quiz(qid, access_tier="batch", batch_name="batch-A")
    return qid


def test_backup_round_trip_includes_expansion_tables(tmp_path):
    qid = _seed_expansion()
    data = backup.build_payload()
    for key, n in (("batches", 1), ("batch_members", 1), ("authorized_chats", 1)):
        assert len(data[key]) == n and data["counts"][key] == n
    assert "payments" not in data and "payments" not in data["counts"]   # feature removed
    assert backup.validate_payload(data) == []
    assert "secret" not in json.dumps(data).lower()

    target = tmp_path / "restored.db"
    res = backup.restore_payload(data, db_file=target, apply=True)
    assert res["ok"] and res["applied"], res
    assert res["inserted"]["batch_members"] == 1 and "payments" not in res["inserted"]
    con = sqlite3.connect(target)
    con.row_factory = sqlite3.Row
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='payments'").fetchone()
    assert con.execute("SELECT batch_name FROM authorized_chats WHERE chat_id=-100777").fetchone()[0] == "batch-A"
    assert con.execute("SELECT 1 FROM batch_members WHERE batch_name='batch-A' AND user_id=801").fetchone()
    row = con.execute("SELECT access_tier, batch_name FROM quizzes WHERE id=?", (qid,)).fetchone()
    assert (row["access_tier"], row["batch_name"]) == ("batch", "batch-A")
    con.close()
    # restoring the same file again creates nothing new
    again = backup.restore_payload(data, db_file=target, apply=True)
    assert again["ok"] and sum(again["inserted"].values()) == 0


def test_validation_rejects_bad_expansion_sections():
    _seed_expansion()
    data = backup.build_payload()
    bad = json.loads(json.dumps(data))
    bad["batch_members"].append(dict(bad["batch_members"][0]))          # duplicate member
    assert any("duplicate member" in p for p in backup.validate_payload(bad))
    bad = json.loads(json.dumps(data))
    bad["authorized_chats"].append(dict(bad["authorized_chats"][0]))    # duplicate chat
    assert any("duplicate chat" in p for p in backup.validate_payload(bad))
    bad = json.loads(json.dumps(data))
    bad["batch_members"] = {"not": "a list"}
    assert any("batch_members is not a list" in p for p in backup.validate_payload(bad))
    bad = json.loads(json.dumps(data))
    bad["counts"]["batches"] = 5
    assert any("counts.batches" in p for p in backup.validate_payload(bad))
    gh = backup.github_payload(data)
    assert all(c["chat_id"] is None for c in gh["authorized_chats"])
    assert all(m["user_id"] is None for m in gh["batch_members"])
    refused = backup.restore_payload(gh, apply=True)                    # redacted copy: never restored
    assert refused["ok"] is False and refused["applied"] is False
    assert any("REDACTED GitHub copy" in p for p in refused["problems"])


def test_old_backup_without_expansion_sections_still_restores():
    qid = _quiz(owner=810)
    data = backup.build_payload()
    for key in ("batches", "batch_members", "authorized_chats"):
        data.pop(key)
        data["counts"].pop(key)
    assert backup.validate_payload(data) == []
    db.delete_quiz(qid) if hasattr(db, "delete_quiz") else None
    res = backup.restore_payload(data, apply=True)
    assert res["ok"] and db.get_quiz(qid)


def _legacy_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '',
                            first_name TEXT DEFAULT '');
        CREATE TABLE quizzes (id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
                              title TEXT NOT NULL, description TEXT DEFAULT '');
    """)
    con.commit()
    con.close()


def test_restore_refuses_outdated_target_instead_of_dropping_data(tmp_path):
    _seed_expansion()
    data = backup.build_payload()
    legacy = tmp_path / "legacy.db"
    _legacy_db(legacy)
    dry = backup.restore_payload(data, db_file=legacy, apply=False)
    assert dry["ok"] and "table batches" in dry["schema_outdated"] and "WARNING" in dry["note"]
    res = backup.restore_payload(data, db_file=legacy, apply=True)
    assert res["ok"] is False and res["applied"] is False
    assert "quizzes.access_tier" in res["schema_outdated"]
    con = sqlite3.connect(legacy)
    assert con.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 0   # nothing written
    con.close()
    # a legacy-only backup still restores into the legacy target (old behaviour)
    legacy_only = json.loads(json.dumps(data))
    for key in ("batches", "batch_members", "authorized_chats"):
        legacy_only[key] = []
        legacy_only["counts"][key] = 0
    for q in legacy_only["quizzes"]:
        q["access_tier"], q["batch_name"] = "free", ""
    for u in legacy_only["users"]:
        u["batches_json"] = "[]"
    ok = backup.restore_payload(legacy_only, db_file=legacy, apply=True)
    assert ok["ok"] and ok["applied"], ok


def test_cli_restore_migrate_upgrades_a_simulated_old_volume(tmp_path):
    _seed_expansion()
    data = backup.build_payload()
    vol = tmp_path / "volume"
    vol.mkdir()
    (vol / "quiz_backup.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    legacy = vol / "quiz.db"
    _legacy_db(legacy)
    env = dict(os.environ, DATA_DIR=str(vol), PYTHONPATH=str(ROOT))
    base = [sys.executable, "scripts/backup_tool.py", "restore", "--db", str(legacy),
            "--file", str(vol / "quiz_backup.json"), "--apply", "--yes"]
    refused = subprocess.run(base, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert refused.returncode == 1 and "--migrate" in refused.stdout
    done = subprocess.run(base + ["--migrate"], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert list(vol.glob("quiz.db.before-restore-*"))          # safety copy taken first
    con = sqlite3.connect(legacy)
    assert con.execute("SELECT 1 FROM batch_members WHERE batch_name='batch-A' AND user_id=801").fetchone()
    assert con.execute("SELECT access_tier, batch_name FROM quizzes").fetchone() == ("batch", "batch-A")
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='payments'").fetchone()
    con.close()


def test_fresh_restore_target_keeps_series_parts_and_timeouts(tmp_path):
    qid = _quiz(owner=820)
    with db.connect() as con:                     # no public setter for series fields
        con.execute("UPDATE quizzes SET series_id='series-x', part_no=2 WHERE id=?", (qid,))
    att = _attempt(qid, 821, ("correct", "timeout"))
    data = backup.build_payload()
    target = tmp_path / "fresh.db"
    assert backup.restore_payload(data, db_file=target, apply=True)["ok"]
    con = sqlite3.connect(target)
    assert con.execute("SELECT series_id, part_no FROM quizzes WHERE id=?", (qid,)).fetchone() == ("series-x", 2)
    assert con.execute("SELECT timeouts FROM attempts WHERE id=?", (att["id"],)).fetchone()[0] == \
        db.get_attempt(att["id"])["timeouts"]
    con.close()
