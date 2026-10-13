"""Automatic/permanent backup system tests (quiz_backup.json).

Covers the eight verification points of the requirement:

1. export of everything that is in the database,
2. automatic backup of a new quiz / new question / recorded attempt,
3. consistency: rewrite-from-database (never append), atomic replace, retries,
4. restart behaviour (fresh application on the same data dir),
5. GitHub sync against a fake GitHub API (create, update, conflict, rate limit,
   auth error, redaction, no needless commits),
6. validation + restore without duplicating records,
7. secrets never appear in the backup,
8. the admin-only /backup command.
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import backup
import config
import database as db
from test_bot_flow import Harness, make_quiz, run

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def bk(tmp_path, monkeypatch):
    """Isolated database + backup file per test, with production-like timing."""
    db.set_db_path(tmp_path / "quiz.db")
    db.init_db()
    monkeypatch.setattr(config, "BACKUP_PATH", tmp_path / "quiz_backup.json")
    monkeypatch.setattr(config, "BACKUP_DEBOUNCE", 0.05)
    monkeypatch.setattr(config, "BACKUP_MIN_INTERVAL", 0.05)
    monkeypatch.setattr(config, "BACKUP_MAX_DELAY", 5.0)
    monkeypatch.setattr(config, "BACKUP_RETRY_DELAY", 0.01)
    monkeypatch.setattr(config, "BACKUP_RETRIES", 3)
    monkeypatch.setattr(config, "GROUP_DEFAULT_TIMER", 1)
    monkeypatch.setattr(config, "GROUP_NEXT_DELAY", 0.05)
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    monkeypatch.setattr(config, "GITHUB_REPO", "")
    backup.reset_state()
    backup.register_db_hook()            # the hook production registers at startup
    yield
    backup.stop_background()
    backup.reset_state()
    db.clear_write_hooks()
    db.set_db_path(config.DB_PATH)


def seed(n_quizzes=2, n_questions=3, with_attempt=True, with_group=True):
    """A small but complete production-like database (quiz + attempt + group run)."""
    owner = 501
    db.ensure_user_row(owner)
    ids = []
    for k in range(n_quizzes):
        qid = db.new_quiz(owner, f"राजस्थान GK {k + 1}", "desc", timer=45, shuffle_questions=1)
        db.update_quiz(qid, status="ready", source="pdf")
        for i in range(n_questions):
            db.add_question(qid, f"प्रश्न {k + 1}.{i + 1}?",
                            [f"opt{i}a", f"opt{i}b", f"opt{i}c", f"opt{i}d"], i % 4,
                            f"व्याख्या {i}", qtype="mcq", source_number=i + 1)
        ids.append(qid)
    qid = ids[0]
    q = db.get_questions(qid)
    if with_attempt:
        att = db.create_attempt(qid, owner, owner, [x["id"] for x in q], 45, False, time.time() - 20)
        db.record_answer(att, q[0]["id"], 0, 0, True, "correct", 2.0)
        db.record_answer(att, q[1]["id"], 1, 2, False, "wrong", 3.5)
        db.finish_attempt(att, "finished", 12.0)
    if with_group:
        sess = db.create_group_session(qid, -1001, owner, [x["id"] for x in q], 45, False,
                                       time.time() - 15)
        db.set_group_current(sess, "poll1", 7, q[0]["id"], 0, [0, 1, 2, 3], 0, time.time() - 10, "")
        db.record_group_answer(sess, qid, -1001, "poll1", q[0]["id"], 0, owner, 0, 0, True,
                               2.5, time.time() - 9)
        db.finish_group_session(sess, "finished", time.time())
    db.set_state(owner, "imp_review", {"review": [{"text": "pending प्रश्न", "page": 4}]})
    return ids


def read_backup(path=None):
    path = Path(path or config.BACKUP_PATH)
    return json.loads(path.read_text(encoding="utf-8"))


# ============================================================ 1. full export
def test_export_contains_every_record_type():
    (qid,) = seed(n_quizzes=1)
    res = backup.export_now(reason="test", force=True)
    assert res["ok"] and not res["skipped"], res
    data = read_backup()
    assert data["format"] == backup.BACKUP_FORMAT and data["version"] == backup.BACKUP_VERSION
    assert data["generated_at"] and data["counts"]["quizzes"] == 1
    quiz = data["quizzes"][0]
    assert quiz["id"] == qid and quiz["title"] == "राजस्थान GK 1"
    assert quiz["source"] == "pdf" and quiz["status"] == "ready"
    assert quiz["settings"] == {"timer": 45, "shuffle_questions": 1, "shuffle_options": 0}
    q = quiz["questions"]
    assert [x["position"] for x in q] == [1, 2, 3]                 # original order kept
    assert q[0]["source_number"] == 1 and q[0]["explanation"] == "व्याख्या 0"
    assert json.loads(q[0]["options_json"])[q[0]["correct_index"]] == "opt0a"
    att = data["attempts"][0]
    assert att["quiz_id"] == qid and att["status"] == "finished" and att["score"] == 1
    assert len(att["answers"]) == 2 and {a["status"] for a in att["answers"]} == {"correct", "wrong"}
    sess = data["group_sessions"][0]
    assert sess["chat_id"] == -1001 and sess["polls"][0]["poll_id"] == "poll1"
    assert sess["answers"][0]["is_correct"] == 1 and sess["answers"][0]["score"] == 1   # leaderboard
    assert data["users"][0]["user_id"] == 501
    assert data["pending_states"][0]["state"] == "imp_review"       # pending review
    assert backup.validate_payload(data) == []
    assert "questions" in data["counts"] and data["counts"]["attempts"] == 1


def test_export_is_idempotent_and_never_appends():
    seed(n_quizzes=2)
    first = backup.export_now(reason="test", force=True)
    h1 = backup.content_hash(read_backup())
    second = backup.export_now(reason="test", force=True)
    assert first["ok"] and second["ok"]
    assert backup.content_hash(read_backup()) == h1      # same data, rewritten (never appended)
    data = read_backup()
    assert len(data["quizzes"]) == 2 and len({q["id"] for q in data["quizzes"]}) == 2
    assert backup.last_result()["counts"] == second["counts"]


def test_unchanged_data_is_not_rewritten():
    seed(n_quizzes=1)
    backup.export_now(reason="test", force=True)
    stamp = Path(config.BACKUP_PATH).stat().st_mtime_ns
    res = backup.export_now(reason="test", force=False)
    assert res["ok"] and res["skipped"] == "unchanged"
    assert Path(config.BACKUP_PATH).stat().st_mtime_ns == stamp
    assert res["bytes"] == Path(config.BACKUP_PATH).stat().st_size


def test_atomic_write_keeps_previous_copy_and_no_temp_file():
    (qid,) = seed(n_quizzes=1)
    backup.export_now(reason="test", force=True)
    old = read_backup()
    db.add_question(qid, "नया प्रश्न?", ["a", "b", "c", "d"], 0, "")
    backup.export_now(reason="test", force=True)
    new = read_backup()
    assert len(new["quizzes"][0]["questions"]) == 4
    prev = json.loads(Path(f"{config.BACKUP_PATH}.prev").read_text(encoding="utf-8"))
    assert len(prev["quizzes"][0]["questions"]) == 3                 # last good copy
    assert prev["generated_ts"] == old["generated_ts"]
    assert not list(Path(config.BACKUP_PATH).parent.glob("*.tmp"))   # temp file cleaned up


# ============================================================ 2. automatic update
def test_new_quiz_is_backed_up_automatically():
    """The real production path: supervisor thread + database hook."""
    backup.export_now(reason="startup", force=True)
    assert backup.start_background(poll_seconds=0.02) is True
    try:
        qid = make_quiz(owner=777, n=2, title="Auto Quiz")
        deadline = time.time() + 10
        while time.time() < deadline:
            data = read_backup()
            if any(q["id"] == qid for q in data["quizzes"]):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"new quiz {qid} was not backed up automatically: "
                                 f"{[q['id'] for q in read_backup()['quizzes']]}")
        quiz = [q for q in read_backup()["quizzes"] if q["id"] == qid][0]
        assert quiz["title"] == "Auto Quiz" and len(quiz["questions"]) == 2
    finally:
        backup.stop_background()
    assert backup.pending() is False                     # nothing left unwritten


def test_question_edit_and_settings_change_are_backed_up():
    (qid,) = seed(n_quizzes=1)
    backup.export_now(reason="test", force=True)
    q = db.get_questions(qid)[0]
    db.update_question(q["id"], question="संशोधित प्रश्न?", correct_index=3, explanation="नई व्याख्या")
    db.update_quiz(qid, status="draft")
    assert backup.pending() is True                       # commit marked it dirty
    backup.run_once(now=time.time() + 1)
    data = read_backup()
    quiz = data["quizzes"][0]
    assert quiz["status"] == "draft"
    edited = [x for x in quiz["questions"] if x["id"] == q["id"]][0]
    assert edited["question"] == "संशोधित प्रश्न?" and edited["correct_index"] == 3
    assert edited["explanation"] == "नई व्याख्या"
    ok, problems, info = backup.verify_file()
    assert ok, problems


def test_attempt_and_leaderboard_are_backed_up():
    (qid,) = seed(n_quizzes=1, with_attempt=False, with_group=False)
    backup.export_now(reason="test", force=True)
    assert read_backup()["counts"]["attempts"] == 0
    q = db.get_questions(qid)
    att = db.create_attempt(qid, 501, 501, [x["id"] for x in q], 30, False, time.time())
    db.record_answer(att, q[0]["id"], 0, 0, True, "correct", 1.5)
    db.finish_attempt(att, "finished", duration_sec=4.0)
    backup.run_once(now=time.time() + 1)
    data = read_backup()
    assert data["counts"]["attempts"] == 1 and data["counts"]["answers"] == 1
    assert data["attempts"][0]["answers"][0]["correct"] == 1
    assert data["attempts"][0]["score"] == 1


# ============================================================ 3. consistency
def test_write_hook_only_fires_for_real_changes():
    backup.reset_state()
    seed(n_quizzes=1)
    assert backup.pending() is True
    backup.export_now(reason="test", force=True)
    assert backup.pending() is False
    db.get_questions(db.get_owner_quizzes(501)[0]["id"])   # read only
    db.get_quiz("nope")
    assert backup.pending() is False
    db.ensure_user_row(501)                                # no-op write (INSERT OR IGNORE)
    assert backup.pending() is False
    db.add_question(db.get_owner_quizzes(501)[0]["id"], "q", ["a", "b"], 0, "")
    assert backup.pending() is True


def test_scheduler_debounce_rate_limit_and_max_delay(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DEBOUNCE", 5.0)
    monkeypatch.setattr(config, "BACKUP_MIN_INTERVAL", 600.0)
    monkeypatch.setattr(config, "BACKUP_MAX_DELAY", 3600.0)
    t0 = time.time()
    backup.export_now(reason="startup", force=True)          # baseline (empty database)
    seed(n_quizzes=1)                                        # a real change → dirty
    before = Path(config.BACKUP_PATH).read_bytes()
    assert backup.run_once(now=t0 + 0.01)["exported"] is False       # still settling
    assert Path(config.BACKUP_PATH).read_bytes() == before
    backup._dirty_last["ts"] = t0 - 10                              # quiet period over…
    assert backup.run_once(now=t0 + 1)["exported"] is False         # …but rate limited
    assert Path(config.BACKUP_PATH).read_bytes() == before
    assert backup.run_once(now=t0 + 3601)["exported"] is True       # hard deadline wins
    assert read_backup()["counts"]["quizzes"] == 1 and backup.pending() is False


def test_export_retries_and_keeps_the_old_file_on_failure(monkeypatch):
    (qid,) = seed(n_quizzes=1)
    backup.export_now(reason="test", force=True)
    good = Path(config.BACKUP_PATH).read_bytes()
    calls = {"n": 0}
    real = backup.build_payload

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("disk hiccup")
        return real()

    monkeypatch.setattr(backup, "build_payload", flaky)
    res = backup.export_now(reason="test", force=True)
    assert res["ok"] and res["attempts"] == 3 and calls["n"] == 3
    good = Path(config.BACKUP_PATH).read_bytes()          # the file the retry produced

    db.add_question(qid, "नया?", ["a", "b"], 0, "")       # a change waits for the next export
    monkeypatch.setattr(backup, "build_payload",
                        lambda: (_ for _ in ()).throw(OSError("disk gone")))
    res = backup.export_now(reason="test", force=True)
    assert res["ok"] is False and res["attempts"] == config.BACKUP_RETRIES
    assert res["errors"] and "disk gone" in res["errors"][0]
    assert Path(config.BACKUP_PATH).read_bytes() == good            # last good file intact
    assert backup.pending() is True                                  # retried later


def test_invalid_payload_is_never_published(monkeypatch):
    seed(n_quizzes=1)
    backup.export_now(reason="test", force=True)
    good = Path(config.BACKUP_PATH).read_bytes()
    data = read_backup()
    data["quizzes"][0]["questions"][0]["correct_index"] = 99          # out of range
    monkeypatch.setattr(backup, "build_payload", lambda: data)
    res = backup.export_now(reason="test", force=True)
    assert res["ok"] is False
    assert any("correct_index" in p for p in res["errors"])
    assert Path(config.BACKUP_PATH).read_bytes() == good


def test_validator_catches_broken_records():
    seed(n_quizzes=1)
    data = backup.build_payload()
    assert backup.validate_payload(data) == []
    bad = json.loads(json.dumps(data))
    bad["quizzes"][0]["id"] = ""
    assert any("missing quiz code" in p for p in backup.validate_payload(bad))
    bad = json.loads(json.dumps(data))
    q = bad["quizzes"][0]["questions"][0]
    q["options_json"] = "{not json"
    assert any("not valid JSON" in p for p in backup.validate_payload(bad))
    bad = json.loads(json.dumps(data))
    bad["attempts"][0]["quiz_id"] = "ghost"
    assert any("unknown quiz_id" in p for p in backup.validate_payload(bad))
    bad = json.loads(json.dumps(data))
    bad["counts"]["quizzes"] = 42
    assert any("counts.quizzes" in p for p in backup.validate_payload(bad))
    bad = json.loads(json.dumps(data))
    bad["attempts"][0]["answers"][0]["attempt_id"] = 123456
    assert any("attempt_id" in p for p in backup.validate_payload(bad))
    assert backup.validate_payload({"format": "x"}) != []


# ============================================================ 4. restart
def test_restart_keeps_data_and_regenerates_the_backup():
    """A redeploy: fresh Application + startup pass over the same persistent files."""
    (qid,) = seed(n_quizzes=1)
    backup.export_now(reason="test", force=True)
    before = read_backup()

    async def redeploy():
        async with Harness():                     # new Application, new DB connection
            assert len(db.get_owner_quizzes(501)) == 1     # data survived
            assert len(db.get_questions(qid)) == 3
            info = backup.install()                        # what bot.main() does at startup
            assert info["startup"]["ok"], info

    run(redeploy())
    after = read_backup()
    assert after["quizzes"] == before["quizzes"] and after["attempts"] == before["attempts"]
    ok, problems, v = backup.verify_file()
    assert ok, problems
    assert v["quizzes"] == 1


def test_supervisor_survives_database_errors(monkeypatch):
    """A broken read must not kill the supervisor: it logs, retries, recovers."""
    seed(n_quizzes=1)
    backup.export_now(reason="test", force=True)
    calls = {"n": 0}
    real = backup.build_payload

    def broken():
        calls["n"] += 1
        if calls["n"] <= config.BACKUP_RETRIES:
            raise OSError("temporary database problem")
        return real()

    monkeypatch.setattr(backup, "build_payload", broken)
    db.add_question(db.get_owner_quizzes(501)[0]["id"], "नया?", ["a", "b"], 0, "")
    first = backup.run_once(now=time.time() + 1)
    assert first["exported"] is False and backup.pending() is True    # still pending
    second = backup.run_once(now=time.time() + 2)
    assert second["exported"] is True and backup.pending() is False   # recovered
    assert read_backup()["counts"]["questions"] == 4


def test_install_on_startup_exports_and_verifies(monkeypatch):
    seed(n_quizzes=1)
    Path(config.BACKUP_PATH).unlink(missing_ok=True)
    info = backup.install()
    assert info["hook"] is True and info["startup"]["ok"] is True
    ok, problems, v = backup.verify_file()
    assert ok, problems
    assert v["counts"]["quizzes"] == 1 and v["generated_at"]
    data = read_backup()
    assert data["db"]["path"].endswith("quiz.db") and data["source"] == "sqlite"


# ============================================================ 5. github
class FakeGitHub:
    """Minimal GitHub contents API: GET/PUT one file, sha conflicts, failures."""

    def __init__(self):
        self.lock = threading.Lock()
        self.file: bytes | None = None
        self.sha = "sha-1"
        self.commits = 0
        self.requests: list[tuple[str, str, str | None]] = []   # method, path, auth
        self.fail_puts = 0
        self.fail_status = 500
        self.conflict_puts = 0
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _params(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def do_GET(self):
                api.requests.append(("GET", self.path, self.headers.get("Authorization")))
                if api.fail_puts:
                    api.fail_puts -= 1
                    return self._send(api.fail_status, {"message": "server error"})
                with api.lock:
                    if api.file is None:
                        return self._send(404, {"message": "Not Found"})
                    return self._send(200, {"name": "quiz_backup.json", "sha": api.sha,
                                            "content": base64.b64encode(api.file).decode()})

            def do_PUT(self):
                api.requests.append(("PUT", self.path, self.headers.get("Authorization")))
                body = self._params()
                if api.fail_puts:
                    api.fail_puts -= 1
                    return self._send(api.fail_status, {"message": "server error"})
                if api.conflict_puts:
                    api.conflict_puts -= 1
                    return self._send(409, {"message": "sha does not match"})
                with api.lock:
                    expected = api.sha
                    if api.file is not None and body.get("sha") != expected:
                        return self._send(409, {"message": "sha does not match"})
                    api.file = base64.b64decode(body["content"])
                    api.sha = f"sha-{api.commits + 2}"
                    api.commits += 1
                    return self._send(201, {"content": {"sha": api.sha},
                                            "commit": {"sha": f"c0ffee{api.commits:04d}"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def uploaded(self):
        return json.loads(self.file.decode("utf-8"))


@pytest.fixture
def gh(monkeypatch):
    server = FakeGitHub()
    monkeypatch.setattr(config, "GITHUB_API", f"http://127.0.0.1:{server.port}")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "ghp_test_token_not_real")
    monkeypatch.setattr(config, "GITHUB_REPO", "dausaloki/Qzzzzz-private")
    monkeypatch.setattr(config, "GITHUB_BACKUP_PATH", "quiz_backup.json")
    yield server
    server.close()


def test_github_sync_disabled_without_token(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    monkeypatch.setattr(config, "GITHUB_REPO", "")
    res = backup.sync_github(force=True)
    assert res["ok"] is False and res["skipped"] == "not-configured"


def test_github_sync_creates_updates_and_skips_unchanged(gh):
    (qid,) = seed(n_quizzes=1)
    res = backup.sync_github(force=True)
    assert res["ok"] and res["commit"] and gh.commits == 1, res
    uploaded = gh.uploaded()
    assert uploaded["format"] == backup.BACKUP_FORMAT
    assert [q["id"] for q in uploaded["quizzes"]] == [qid]
    assert all(r[2] == "Bearer ghp_test_token_not_real" for r in gh.requests)   # token from env

    # no data change → no new commit
    assert backup.sync_github(force=False)["skipped"] == "unchanged"
    assert gh.commits == 1

    # a change is uploaded in place (sha of the existing file is used)
    db.add_question(qid, "नया प्रश्न?", ["a", "b", "c", "d"], 0, "")
    res = backup.sync_github(force=True)
    assert res["ok"] and gh.commits == 2
    assert len(gh.uploaded()["quizzes"][0]["questions"]) == 4
    puts = [r for r in gh.requests if r[0] == "PUT"]
    assert len(puts) == 2 and gh.uploaded()["version"] == backup.BACKUP_VERSION


def test_github_sync_redacts_user_data(gh):
    seed(n_quizzes=1)
    assert backup.sync_github(force=True)["ok"]
    raw = gh.file.decode("utf-8")
    uploaded = gh.uploaded()
    assert "501" not in raw and "-1001" not in raw          # no user/chat ids
    assert uploaded["attempts"][0]["user_id"] is None
    assert uploaded["attempts"][0]["chat_id"] is None
    assert uploaded["group_sessions"][0]["chat_id"] is None
    assert uploaded["users"][0].get("username") is None
    assert uploaded["quizzes"][0]["title"] == db.get_quiz(db.get_owner_quizzes(501)[0]["id"])["title"]
    assert "redacted" in uploaded


def test_github_sync_retries_conflict_and_rate_limit(gh):
    seed(n_quizzes=1)
    gh.conflict_puts = 1
    res = backup.sync_github(force=True)
    assert res["ok"] and res["attempts"] >= 2 and gh.commits == 1, res

    db.add_question(db.get_owner_quizzes(501)[0]["id"], "q2", ["a", "b"], 0, "")
    gh.fail_puts = 1
    gh.fail_status = 429
    res = backup.sync_github(force=True)
    assert res["ok"] and res["attempts"] >= 2 and gh.commits == 2


def test_github_sync_auth_error_does_not_crash(gh):
    seed(n_quizzes=1)

    def unauthorized(method, url, body=None):
        return 401, {"message": "Bad credentials"}, {}

    original = backup._gh_request
    backup._gh_request = unauthorized
    try:
        res = backup.sync_github(force=True)
    finally:
        backup._gh_request = original
    assert res["ok"] is False and "401" in res["errors"][0] and gh.commits == 0


def test_status_text_reports_state(gh):
    seed(n_quizzes=1)
    backup.export_now(reason="test", force=True)
    text = backup.status_text()
    assert "Backup status" in text and "quizzes=1" in text and "✅ valid" in text
    assert str(config.BACKUP_PATH) in text


# ============================================================ 6. restore
def test_restore_is_dry_run_by_default_and_idempotent(tmp_path):
    seed(n_quizzes=2)
    backup.export_now(reason="test", force=True)
    other = tmp_path / "other.db"
    db.set_db_path(other)
    db.init_db()
    assert db.get_owner_quizzes(501) == []
    plan = backup.restore_from_file(config.BACKUP_PATH, db_file=other, apply=False)
    assert plan["ok"] and plan["applied"] is False
    assert plan["inserted"]["quizzes"] == 2 and plan["inserted"]["questions"] == 6
    assert db.get_owner_quizzes(501) == []                      # nothing written

    applied = backup.restore_from_file(config.BACKUP_PATH, db_file=other, apply=True)
    assert applied["ok"] and applied["applied"] is True
    quizzes = db.get_owner_quizzes(501)
    assert len(quizzes) == 2 and all(len(db.get_questions(q["id"])) == 3 for q in quizzes)
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) c FROM group_answers").fetchone()["c"] == 1
        assert con.execute("SELECT COUNT(*) c FROM answers").fetchone()["c"] == 2

    again = backup.restore_from_file(config.BACKUP_PATH, db_file=other, apply=True)
    assert again["inserted"]["quizzes"] == 0 and again["updated"]["quizzes"] == 2
    assert len(db.get_owner_quizzes(501)) == 2                  # never duplicated
    assert db.get_user(501) is not None
    assert len(db.get_questions(quizzes[0]["id"])) == 3


def test_restore_brings_back_a_deleted_quiz_without_touching_others(tmp_path):
    ids = seed(n_quizzes=2)
    backup.export_now(reason="test", force=True)
    assert db.delete_quiz(ids[0]) is True
    assert len(db.get_owner_quizzes(501)) == 1
    plan = backup.restore_from_file(config.BACKUP_PATH, apply=True)
    assert plan["ok"]
    assert {q["id"] for q in db.get_owner_quizzes(501)} == set(ids)
    assert len(db.get_questions(ids[0])) == 3


# ============================================================ 7. secrets
def test_no_secrets_in_the_backup(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:SECRET-TOKEN-XYZ")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "ghp_anothersecret")
    seed(n_quizzes=1)
    res = backup.export_now(reason="test", force=True)
    assert res["ok"] is True
    raw = Path(config.BACKUP_PATH).read_text(encoding="utf-8")
    assert "SECRET-TOKEN-XYZ" not in raw and "ghp_anothersecret" not in raw
    assert "BOT_TOKEN" not in raw and "GITHUB_TOKEN" not in raw
    assert not any(k in raw for k in ('"password"', '"api_key"', '"api_hash"'))


def test_secret_inside_a_record_blocks_the_export(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "123456:LEAKED")
    qid = seed(n_quizzes=1)[0]
    backup.export_now(reason="test", force=True)
    good = Path(config.BACKUP_PATH).read_bytes()
    db.add_question(qid, "token 123456:LEAKED ?", ["a", "b"], 0, "")
    res = backup.export_now(reason="test", force=True)
    assert res["ok"] is False and any("BOT_TOKEN" in e for e in res["errors"])
    assert Path(config.BACKUP_PATH).read_bytes() == good


def test_forbidden_columns_are_never_selected():
    seed(n_quizzes=1)
    with db.connect() as con:
        con.execute("ALTER TABLE users ADD COLUMN token TEXT DEFAULT ''")
        con.execute("UPDATE users SET token='abcdef-secret'")
    res = backup.export_now(reason="test", force=True)
    assert res["ok"], res["errors"]
    assert "abcdef-secret" not in Path(config.BACKUP_PATH).read_text(encoding="utf-8")
    assert "token" not in read_backup()["users"][0]


# ============================================================ 8. bot integration
def test_backup_command_is_admin_only_and_reports():
    async def t():
        async with Harness() as h:
            owner, stranger = h.f.user(601), h.f.user(602)
            await h.text(stranger, "/backup")
            assert "सेट करें" in h.all_text(602)              # locked: no admin configured
            config.BACKUP_ADMIN_IDS = (601,)
            try:
                await h.text(stranger, "/backup")
                assert "केवल administrator" in h.all_text(602)
                await h.text(owner, "/backup")
                assert "Backup status" in h.all_text(601)
                await h.text(owner, "/backup now")
                assert "✅ Backup पूरा" in h.all_text(601)
                assert Path(config.BACKUP_PATH).exists()
                await h.text(owner, "/backup verify")
                assert "Verify" in h.all_text(601)
                await h.text(owner, "/backup sync")
                assert "GitHub sync सेट नहीं है" in h.all_text(601)
                await h.text(owner, "/backup download")           # the file itself is sent
                uploads = [u for u in h.api.uploads if u[0] == "sendDocument"]
                assert uploads, h.api.calls[-5:]
                _api, params, files = uploads[-1]
                name, blob = files["document"]
                assert name.endswith("quiz_backup.json"), name
                sent = json.loads(blob.decode("utf-8"))            # a real, valid backup
                assert sent["format"] == backup.BACKUP_FORMAT
                assert "quiz_backup.json" in params["caption"]
                await h.text(owner, "/backup restore")
                assert "dry-run" in h.all_text(601)
                await h.text(owner, "/backup help")
                assert "/backup now" in h.all_text(601)
            finally:
                config.BACKUP_ADMIN_IDS = ()
    run(t())


def test_end_to_end_quiz_created_in_the_bot_is_backed_up():
    """Create a quiz through the real handlers, then look at the JSON."""
    async def t():
        async with Harness() as h:
            u = h.f.user(701, "Prem")
            await h.cb(u, "m:new")
            await h.text(u, "बैकअप टेस्ट")
            await h.text(u, "/skip")
            await h.text(u, "प्रश्न एक?\n(A) अ\n(B) ब\n(C) स\n(D) द\nउत्तर: C\nव्याख्या: कारण")
            assert "प्रश्न 1 जुड़ गया" in h.all_text(701)
            await h.text(u, "/done")
            assert "Quiz तैयार है!" in h.all_text(701)
            qid = db.get_owner_quizzes(701)[0]["id"]
            backup.run_once(now=time.time() + 1)
            data = read_backup()
            quiz = [q for q in data["quizzes"] if q["id"] == qid]
            assert quiz and quiz[0]["title"] == "बैकअप टेस्ट"
            assert quiz[0]["questions"][0]["question"] == "प्रश्न एक?"
            assert quiz[0]["questions"][0]["correct_index"] == 2      # (C)
            assert quiz[0]["questions"][0]["explanation"] == "कारण"
            assert quiz[0]["owner_id"] == 701
    run(t())


def test_recorded_attempt_of_a_real_run_is_saved():
    async def t():
        async with Harness() as h:
            u = h.f.user(702)
            qid = make_quiz(owner=702, n=2)
            await h.start(u, qid)
            await h.wait(lambda: h.polls_for(702), msg="first poll")
            await h.answer_current(u, correct=True)
            await h.wait(lambda: len(h.polls_for(702)) == 2, msg="second poll")
            await h.answer_current(u, correct=False)
            await h.wait(lambda: db.user_history(702), msg="attempt finished")
            backup.run_once(now=time.time() + 1)
            data = read_backup()
            att = [a for a in data["attempts"] if a["quiz_id"] == qid]
            assert att and att[0]["status"] in ("finished", "expired")
            assert att[0]["correct"] == 1 and att[0]["wrong"] == 1
            assert len(att[0]["answers"]) == 2
            assert {a["correct"] for a in att[0]["answers"]} == {0, 1}
    run(t())


def test_group_quiz_answers_are_saved():
    from test_round5 import G1, answer, group_start, gpolls
    async def t():
        async with Harness() as h:
            starter = h.f.user(703)
            qid = make_quiz(owner=703, n=2)
            await group_start(h, starter, qid, chat=G1)
            await h.wait(lambda: gpolls(h, G1), msg="group poll")
            await answer(h, h.f.user(704), G1, correct=True)
            await h.wait(lambda: len(db.get_group_answers(db.list_active_group_sessions()[-1]["id"])) >= 1,
                         msg="group answer")
            backup.run_once(now=time.time() + 1)
            data = read_backup()
            sess = [s for s in data["group_sessions"] if s["quiz_id"] == qid]
            assert sess and sess[0]["answers"], "group answers missing from the backup"
            assert sess[0]["answers"][0]["is_correct"] == 1
    run(t())


# ============================================================ 9. cli tool
def test_cli_export_verify_restore(tmp_path):
    seed(n_quizzes=2)
    db_file = db.get_db_path()
    out = tmp_path / "cli_backup.json"
    env = {**os.environ, "DATA_DIR": str(tmp_path), "PYTHONPATH": str(ROOT)}
    env.pop("BOT_TOKEN", None)

    def cli(*args):
        return subprocess.run([sys.executable, "scripts/backup_tool.py", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)

    p = cli("export", "--db", str(db_file), "--out", str(out))
    assert p.returncode == 0, p.stdout + p.stderr
    assert out.exists() and "records:" in p.stdout
    data = read_backup(out)
    assert len(data["quizzes"]) == 2

    p = cli("verify", "--file", str(out), "--db", str(db_file))
    assert p.returncode == 0 and "✅" in p.stdout, p.stdout

    p = cli("status", "--db", str(db_file), "--out", str(out))
    assert p.returncode == 0 and "Backup status" in p.stdout

    fresh = tmp_path / "fresh.db"
    db.init_db()
    p = cli("restore", "--file", str(out), "--db", str(fresh), "--no-backup")
    assert p.returncode == 0 and "DRY RUN" in p.stdout
    p = cli("restore", "--file", str(out), "--db", str(fresh), "--apply", "--yes")
    assert p.returncode == 0 and "restored" in p.stdout, p.stdout
    db2 = db.get_db_path()
    db.set_db_path(fresh)
    db.init_db()
    assert len(db.get_owner_quizzes(501)) == 2
    db.set_db_path(db2)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    p = cli("verify", "--file", str(broken), "--no-db")
    assert p.returncode == 1 and "JSON" in p.stdout


def test_backup_defaults_to_the_persistent_data_dir():
    """Without BACKUP_PATH the file lives next to the database (Railway volume)."""
    env = {k: v for k, v in os.environ.items() if k not in ("BACKUP_PATH", "BOT_TOKEN")}
    env["DATA_DIR"] = "/tmp/volume-sim"
    out = subprocess.run([sys.executable, "-c",
                          "import config; print(config.BACKUP_PATH); print(config.DB_PATH)"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines == ["/tmp/volume-sim/quiz_backup.json", "/tmp/volume-sim/quiz.db"], out
    # and in-process: the file sits beside the database
    assert Path(config.BACKUP_PATH).parent == db.get_db_path().parent
    assert backup.prev_path().name == "quiz_backup.json.prev"
