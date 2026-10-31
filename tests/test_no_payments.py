"""Regression tests for the decision to REMOVE payments and premium membership.

The production bot (GitHub ``0e62d92``) never had payments, premium membership,
paid quizzes or a ``payments`` table.  The feature expansion had added them; they
are removed again.  These tests make sure that:

* no payment module, command, setting, button or deployment variable is left;
* every quiz is free — including a stored ``premium`` / ``paid`` value left by an
  earlier development build — while the non-payment controls (batch, private,
  authorized groups, mandatory join gate) keep working;
* nothing is dropped or rewritten in an existing database (no destructive
  migration), even if it contains payment data from a development build;
* backups no longer carry payment / premium data, and an older development
  backup that still contains it restores normally with that data ignored and
  reported — never written, never deleted.

Everything runs on temporary SQLite files — never on a production database.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import access_control
import backup
import bot as botmod
import config
import database as db
import keyboards as kb
from telegram.ext import CommandHandler
from tests.test_bot_flow import Harness, buttons, isolated, make_quiz, run

_ = isolated
ROOT = Path(__file__).resolve().parents[1]

REMOVED_COMMANDS = {"pay", "premium", "verify", "grantpremium", "revokepremium", "paycheck"}
REMOVED_VARIABLES = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET",
                     "PREMIUM_PRICE_INR", "PREMIUM_DURATION_DAYS", "PARTIAL_REFUND_POLICY")
_TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".example", ".cfg", ".ini", ".yml",
                  ".yaml", ".sh", ""}
_SKIP_DIRS = {".git", "tests", "data", "__pycache__", ".venv", "node_modules"}


def _project_files():
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in _SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.suffix.lower() in _TEXT_SUFFIXES or path.name in ("Dockerfile", "Procfile",
                                                                 ".env.example", ".dockerignore"):
            yield rel, path


def _legacy_payment_artifacts(path: Path) -> None:
    """What a database touched by the removed development build could contain."""
    con = sqlite3.connect(path)
    con.executescript("""
        ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0;
        ALTER TABLE users ADD COLUMN premium_until REAL DEFAULT 0;
        CREATE TABLE payments (order_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                               amount_paise INTEGER DEFAULT 0, status TEXT DEFAULT 'created',
                               payment_id TEXT, granted INTEGER DEFAULT 0);
        INSERT INTO users(user_id, first_name, is_premium, premium_until)
               VALUES (4242, 'Legacy', 1, 1999999999.0);
        INSERT INTO payments VALUES ('order_LEGACY1', 4242, 9900, 'paid', 'pay_LEGACY1', 1);
        INSERT INTO payments VALUES ('order_LEGACY2', 4242, 9900, 'created', NULL, 0);
    """)
    con.commit()
    con.close()


def _dump(path: Path) -> dict:
    con = sqlite3.connect(path)
    out = {
        "payments": con.execute("SELECT * FROM payments ORDER BY order_id").fetchall(),
        "premium": con.execute("SELECT user_id, is_premium, premium_until FROM users "
                               "ORDER BY user_id").fetchall(),
        "quizzes": con.execute("SELECT * FROM quizzes ORDER BY id").fetchall(),
        "integrity": con.execute("PRAGMA integrity_check").fetchone()[0],
    }
    con.close()
    return out


# ---------------------------------------------------------------- code & config
def test_payment_module_settings_and_db_helpers_are_gone():
    assert not (ROOT / "payments.py").exists()
    assert importlib.util.find_spec("payments") is None
    for name in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "PREMIUM_PRICE_INR",
                 "PREMIUM_DURATION_DAYS", "PARTIAL_REFUND_POLICIES", "partial_refund_policy"):
        assert not hasattr(config, name), name
    for name in ("is_user_premium", "grant_premium", "revoke_premium", "list_premium_users",
                 "create_payment_order", "verify_and_complete_payment_atomic",
                 "apply_payment_refund_atomic", "mark_payment_refunded_atomic"):
        assert not hasattr(db, name), name
    assert "premium" not in access_control.VALID_TIERS
    assert "payments" not in db.SCHEMA and "premium" not in db.SCHEMA


def test_no_payment_variable_in_code_docs_or_deployment_files():
    hits = []
    for rel, path in _project_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for var in REMOVED_VARIABLES + ("api.razorpay.com", "/paycheck", "/grantpremium"):
            if var in text:
                hits.append(f"{rel}: {var}")
    assert hits == []
    env_active = [ln.split("=", 1)[0] for ln in (ROOT / ".env.example").read_text("utf-8").splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
    assert not [v for v in env_active if "RAZORPAY" in v or "PREMIUM" in v or "REFUND" in v]


# ---------------------------------------------------------------- commands & UI
def test_payment_commands_buttons_and_texts_are_gone(monkeypatch):
    monkeypatch.setenv("OWNER_ID", "990")
    monkeypatch.setattr(config, "ADMIN_IDS", frozenset({990}))

    async def t():
        async with Harness() as h:
            registered = {c for group in h.app.handlers.values() for hd in group
                          if isinstance(hd, CommandHandler) for c in hd.commands}
            assert not (registered & REMOVED_COMMANDS)
            assert {"start", "help", "access", "batch", "authchat", "admin", "broadcast",
                    "backup"} <= registered                       # other tools kept
            assert not [c for c in botmod.BOT_COMMANDS if c.command in REMOVED_COMMANDS]

            user, owner = h.f.user(951, "User"), h.f.user(990, "Owner")
            for cmd in ("/premium", "/pay order", "/pay paid", "/verify order_X pay_Y"):
                await h.text(user, cmd)
            for cmd in ("/grantpremium 951 30", "/revokepremium 951", "/paycheck order_X"):
                await h.text(owner, cmd)
            assert h.all_text(951) == "" and h.all_text(990) == ""   # unknown → no reply
            # an old "Create Order" button pressed after the update
            await h.cb(user, "pay:order")
            alerts = [p.get("text") or "" for a, p in h.api.calls if a == "answerCallbackQuery"]
            assert any("valid नहीं" in a for a in alerts)
            assert h.all_text(951) == ""

            await h.text(user, "/help")
            help_text = h.all_text(951)
            assert "/access" in help_text
            for word in ("Premium", "/pay", "/premium", "भुगतान", "Razorpay"):
                assert word not in help_text, word
            await h.text(owner, "/admin")
            dash = h.all_text(990)
            assert "Admin & Owner Control Panel" in dash
            for word in ("Razorpay", "Premium", "Webhook", "refund", "/grantpremium", "/paycheck"):
                assert word not in dash, word
    run(t())
    with db.connect() as con:
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='payments'").fetchone()


def test_access_menu_offers_only_non_payment_options():
    qid = make_quiz(owner=960)
    for stored in ("free", "premium", "paid", None):
        db.update_quiz(qid, access_tier=stored or "free")
        markup = kb.quiz_access_menu(qid, db.get_quiz(qid))
        labels = [b["text"] for b in buttons(markup.to_dict())]
        data = [b.get("callback_data") for b in buttons(markup.to_dict())]
        assert not any("Premium" in lb or "Paid" in lb for lb in labels)
        assert not any(d and d.endswith(":premium") for d in data)
        assert [lb for lb in labels if lb.startswith("✅")] == ["✅ 🌐 Free (Open to All)"]


# ---------------------------------------------------------------- access rules
def test_every_quiz_is_free_including_legacy_premium_values():
    qid = make_quiz(owner=961)
    for legacy in ("premium", "paid", "PREMIUM", "gold"):
        db.update_quiz(qid, access_tier=legacy)                 # as an old dev build might leave it
        quiz = db.get_quiz(qid)
        assert access_control.normalize_tier(legacy) == "free"
        assert access_control.tier_label(legacy) == "🌐 Free (Open to All)"
        assert access_control.check_quiz_access(7001, 7001, quiz) == (True, "")        # private chat
        assert access_control.check_quiz_access(7001, -100123, quiz) == (True, "")     # group
        assert access_control.can_share_inline(7001, quiz) is True
        assert db.get_quiz(qid)["access_tier"] == legacy        # row not rewritten


def test_non_payment_access_controls_still_work():
    qid = make_quiz(owner=962)
    db.update_quiz(qid, access_tier="private")
    ok, msg = access_control.check_quiz_access(7002, 7002, db.get_quiz(qid))
    assert ok is False and "Private Quiz" in msg
    assert access_control.check_quiz_access(962, 962, db.get_quiz(qid)) == (True, "")
    db.create_batch("B1", created_by=962)
    db.update_quiz(qid, access_tier="batch", batch_name="B1")
    ok, msg = access_control.check_quiz_access(7002, 7002, db.get_quiz(qid))
    assert ok is False and "Batch Restricted" in msg
    db.add_user_to_batch("B1", 7002)
    assert access_control.check_quiz_access(7002, 7002, db.get_quiz(qid)) == (True, "")
    db.update_quiz(qid, access_tier="free", batch_name="")
    access_control.set_allowed_chats(qid, [-1001])
    assert access_control.check_quiz_access(7003, -1002, db.get_quiz(qid))[0] is False
    assert access_control.check_quiz_access(7003, -1001, db.get_quiz(qid)) == (True, "")


def test_new_quiz_defaults_are_free_with_negative_marking_off():
    qid = make_quiz(owner=963)
    quiz = db.get_quiz(qid)
    assert quiz["access_tier"] == "free" and float(quiz.get("negative_mark") or 0) == 0
    assert access_control.check_quiz_access(7004, 7004, quiz) == (True, "")


# ---------------------------------------------------------------- database safety
def test_fresh_database_has_no_payment_schema(tmp_path):
    db.set_db_path(tmp_path / "fresh.db")
    db.init_db()
    con = sqlite3.connect(tmp_path / "fresh.db")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    users = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    con.close()
    assert "payments" not in tables
    assert not ({"is_premium", "premium_until"} & users)
    assert {"batches", "batch_members", "authorized_chats", "quizzes", "users"} <= tables


def test_existing_payment_data_is_never_dropped_or_altered(tmp_path):
    path = tmp_path / "volume.db"
    db.set_db_path(path)
    db.init_db()
    make_quiz(owner=964)
    _legacy_payment_artifacts(path)
    before = _dump(path)
    assert len(before["payments"]) == 2
    for _ in range(2):                                   # start-up migration, twice
        db.init_db()
    after = _dump(path)
    assert after == before and after["integrity"] == "ok"


# ---------------------------------------------------------------- backup / restore
def test_backup_export_has_no_payment_data_and_leaves_the_db_alone(tmp_path):
    path = tmp_path / "volume.db"
    db.set_db_path(path)
    db.init_db()
    make_quiz(owner=965)
    _legacy_payment_artifacts(path)
    before = _dump(path)
    data = backup.build_payload()
    assert "payments" not in data and "payments" not in data["counts"]
    assert all("is_premium" not in u and "premium_until" not in u for u in data["users"])
    assert backup.validate_payload(data) == []
    assert _dump(path) == before                         # export never writes


def _old_dev_backup() -> dict:
    """A backup as the removed development build wrote it (with payments)."""
    make_quiz(owner=966, title="पुराना बैकअप")
    data = backup.build_payload()
    for u in data["users"]:
        u.update(is_premium=1, premium_until=1999999999.0)
    data["payments"] = [{"order_id": "order_OLD1", "user_id": 966, "amount_paise": 9900,
                         "status": "paid", "payment_id": "pay_OLD1", "granted": 1}]
    data["counts"]["payments"] = 1
    return json.loads(json.dumps(data))


def test_old_dev_backup_with_payments_restores_and_ignores_that_data(tmp_path):
    data = _old_dev_backup()
    assert backup.validate_payload(data) == []
    # (a) fresh target: everything else restored, no payment table/columns created
    fresh = tmp_path / "fresh.db"
    res = backup.restore_payload(data, db_file=fresh, apply=True)
    assert res["ok"] and res["applied"], res
    assert res["ignored_removed_feature"] == {"payments": 1, "users.is_premium": 1,
                                              "users.premium_until": 1}
    con = sqlite3.connect(fresh)
    assert con.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 1
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='payments'").fetchone()
    assert "is_premium" not in {r[1] for r in con.execute("PRAGMA table_info(users)")}
    con.close()
    # (b) a target that still holds development payment data: left exactly as it was
    old = tmp_path / "old_dev.db"
    db.set_db_path(old)
    db.init_db()
    _legacy_payment_artifacts(old)
    con = sqlite3.connect(old)
    con.execute("INSERT INTO users(user_id, first_name, is_premium, premium_until) "
                "VALUES (966, 'Owner966', 0, 0)")
    con.commit()
    con.close()
    before = _dump(old)
    res = backup.restore_payload(data, db_file=old, apply=True)
    assert res["ok"] and res["applied"], res
    after = _dump(old)
    assert after["payments"] == before["payments"]        # nothing inserted / changed / deleted
    assert after["premium"] == before["premium"]          # premium flags never written
    assert after["integrity"] == "ok"


def test_cli_dry_run_reports_ignored_payment_data(tmp_path):
    data = _old_dev_backup()
    vol = tmp_path / "volume"
    vol.mkdir()
    f = vol / "quiz_backup.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, DATA_DIR=str(vol), PYTHONPATH=str(ROOT))
    out = subprocess.run([sys.executable, "scripts/backup_tool.py", "restore", "--db",
                          str(vol / "quiz.db"), "--file", str(f)], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "ignored data of the removed payments/premium feature" in out.stdout
    assert "payments=1" in out.stdout and "DRY RUN" in out.stdout
