"""Regression tests for the second review round.

* ``Fonts/`` vs ``fonts/``: GitHub's main branch has ``Fonts/`` (browser upload);
  Linux is case-sensitive, so the old lookup silently fell back to MuPDF's
  built-in fonts.  The bundled fonts must load for either spelling.
* The redacted GitHub copy used to pass validation and then crash the restore
  with ``KeyError: 'user_id'`` (after creating an empty schema).  It is now
  refused cleanly and nothing is written.
* Rows without their natural key are refused before anything is written.
* An old-format backup (written by production 0e62d92, no expansion sections or
  columns) restores with safe defaults: free quizzes, no negative marking,
  empty batch tables.  (The payment checks of this round were removed together
  with the payment / premium feature.)
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import backup
import database as db
import review_pdf
from tests.test_bot_flow import isolated

_ = isolated
ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ fonts
def test_bundled_font_dir_is_found_case_insensitively(tmp_path):
    (tmp_path / "Fonts").mkdir()
    assert review_pdf.bundled_font_dirs(tmp_path) == [tmp_path / "Fonts"]
    (tmp_path / "fonts").mkdir()                       # both present: exact name first
    assert review_pdf.bundled_font_dirs(tmp_path)[0] == tmp_path / "fonts"
    empty = tmp_path / "none"
    empty.mkdir()
    assert review_pdf.bundled_font_dirs(empty) == [empty / "fonts"]


def _repo_copy(dst: Path, font_dir_name: str) -> Path:
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not files:                                      # running from an extracted ZIP (no .git)
        files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                 if p.is_file() and not any(part in ("data", "__pycache__", ".git") for part in p.parts)]
    for rel in files:
        if not (ROOT / rel).is_file():
            continue
        target = dst / rel
        if rel.startswith("fonts/"):
            target = dst / font_dir_name / rel.split("/", 1)[1]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    return dst


def test_pdf_uses_bundled_fonts_when_folder_is_capitalised(tmp_path):
    tree = _repo_copy(tmp_path / "app", "Fonts")
    (tree / "Fonts" / "Fonts").write_text("\n")        # the stray 1-byte file on GitHub
    probe = ("import review_pdf as R; ok, info = R.pdf_font_status(); fb = R.fonts(); "
             "print(ok, fb.source, fb.dev_file, fb.lat_file, fb.latb_file, sep='|')")
    out = subprocess.run([sys.executable, "-c", probe], cwd=tree, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PDF_FONT_DIR": ""},
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    ok, source, dev, lat, latb = out.stdout.strip().splitlines()[-1].split("|")
    assert ok == "True" and source == "bundled"
    for f in (dev, lat, latb):
        assert f.startswith(str(tree / "Fonts")), (f, out.stdout)


def test_font_status_reports_the_font_source():
    ok, info = review_pdf.pdf_font_status()
    assert ok and "source=bundled" in info, info


# ------------------------------------------------------------------ backup
def _seed():
    db.ensure_user_row(950)
    qid = db.new_quiz(950, "राउंड-2 क्विज़", "", timer=30, status="ready")
    db.add_question(qid, "प्रश्न?", ["क", "ख"], 0, "")
    return qid


def test_github_redacted_copy_is_refused_without_writing(tmp_path):
    _seed()
    gh = backup.github_payload(backup.build_payload())
    target = tmp_path / "fresh.db"
    res = backup.restore_payload(gh, db_file=target, apply=True)
    assert res["ok"] is False and res["applied"] is False
    assert any("REDACTED GitHub copy" in p for p in res["problems"])
    assert not target.exists() or not sqlite3.connect(target).execute(
        "select count(*) from sqlite_master where type='table'").fetchone()[0]


def test_github_copy_without_payments_no_longer_crashes(tmp_path):
    db.ensure_user_row(951)
    qid = db.new_quiz(951, "t", "", timer=30, status="ready")
    db.add_question(qid, "q?", ["a", "b"], 0, "")
    gh = backup.github_payload(backup.build_payload())
    target = tmp_path / "fresh.db"
    res = backup.restore_payload(gh, db_file=target, apply=True)      # used to raise KeyError
    assert res["ok"] is False and not res["applied"]
    assert not target.exists() or not sqlite3.connect(target).execute(
        "select count(*) from sqlite_master where type='table'").fetchone()[0]


def test_rows_without_key_are_refused_before_any_write(tmp_path):
    _seed()
    data = backup.build_payload()
    data["users"][0].pop("user_id")
    target = tmp_path / "fresh.db"
    res = backup.restore_payload(data, db_file=target, apply=True)
    assert res["ok"] is False and any("without their key" in p for p in res["problems"])
    assert not target.exists() or not sqlite3.connect(target).execute(
        "select count(*) from sqlite_master where type='table'").fetchone()[0]


def test_old_format_backup_restores_with_safe_defaults(tmp_path):
    qid = _seed()
    data = backup.build_payload()
    # make it look exactly like a 0e62d92 file: no expansion sections, no expansion columns
    for key in ("batches", "batch_members", "authorized_chats"):
        data.pop(key)
        data["counts"].pop(key)
    for u in data["users"]:
        for col in ("batches_json", "is_banned"):
            u.pop(col, None)
    for q in data["quizzes"]:
        for col in ("quiz_mode", "access_tier", "batch_name", "allowed_chats_json",
                    "section_timers_json", "negative_mark"):
            q.pop(col, None)
    assert backup.validate_payload(data) == []
    target = tmp_path / "fresh.db"
    res = backup.restore_payload(data, db_file=target, apply=True)
    assert res["ok"] and res["applied"], res
    c = sqlite3.connect(target)
    assert c.execute("select access_tier, negative_mark, quiz_mode from quizzes where id=?",
                     (qid,)).fetchone() == ("free", 0, "standard")
    assert c.execute("select count(*) from batches").fetchone()[0] == 0
    assert not c.execute("select 1 from sqlite_master where name='payments'").fetchone()


def test_restore_report_is_stable_for_a_second_identical_restore(tmp_path):
    _seed()
    data = backup.build_payload()
    target = tmp_path / "fresh.db"
    assert backup.restore_payload(data, db_file=target, apply=True)["ok"]
    again = backup.restore_payload(json.loads(json.dumps(data)), db_file=target, apply=True)
    assert again["ok"] and sum(again["inserted"].values()) == 0
