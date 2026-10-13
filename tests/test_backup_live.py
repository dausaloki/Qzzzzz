"""Live backup tests: the REAL ``python bot.py`` process (fake Bot API).

These tests prove the parts that only exist in a running process:

* the backup is written on startup and is valid JSON,
* a database change made *while the bot runs* is picked up and written,
* SIGTERM produces a final export (Railway redeploy / stop),
* restarting the application keeps the quizzes and the backup (persistent dir).
"""
import json
import os
import signal
import subprocess
import sys

from pathlib import Path

import pytest

from test_real_startup import FakeBotAPI, TOKEN, _wait

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signals")


def _env(tmp_path: Path, port: int, **extra) -> dict:
    env = dict(os.environ)
    env.update({"BOT_TOKEN": TOKEN, "TELEGRAM_API_BASE": f"http://127.0.0.1:{port}",
                "DATA_DIR": str(tmp_path), "PYTHONUNBUFFERED": "1",
                "BACKUP_SAFETY_INTERVAL": "2", "BACKUP_MIN_INTERVAL": "1",
                "BACKUP_DEBOUNCE": "0.5"})
    env.pop("DB_PATH", None)
    env.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
    env.update(extra)
    return env


def _valid(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == "qzzzzz-quiz-backup" and data["version"] == 1
    return data


def _start(tmp_path: Path, port: int, **extra):
    log_path = tmp_path / "bot.log"
    logf = open(log_path, "a")
    proc = subprocess.Popen([sys.executable, "bot.py"], cwd=ROOT, env=_env(tmp_path, port, **extra),
                            stdout=logf, stderr=subprocess.STDOUT)
    return proc, logf, log_path


def _stop(proc, logf, timeout: float = 30):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("bot did not exit after SIGTERM")
    logf.close()
    return rc


def _add_quiz_directly(db_file: Path, n: int = 2) -> str:
    """Write to the production database from *another* process (like the CLI does)."""
    sys.path.insert(0, str(ROOT))
    import database as db
    previous = db.get_db_path()
    db.set_db_path(db_file)
    try:
        db.init_db()
        qid = db.new_quiz(9001, "बाहरी प्रश्न quiz", "बैकअप टेस्ट")
        for i in range(n):
            db.add_question(qid, f"बाहरी प्रश्न {i + 1}?", [f"o{i}a", f"o{i}b", f"o{i}c", f"o{i}d"],
                            i % 4, f"व्याख्या {i}")
        return qid
    finally:
        db.set_db_path(previous)


def test_live_bot_backs_up_on_startup_and_after_an_external_change(tmp_path):
    db_file = tmp_path / "quiz.db"
    backup_file = tmp_path / "quiz_backup.json"
    with FakeBotAPI(conflicts=0) as api:
        proc, logf, log_path = _start(tmp_path, api.port)
        try:
            assert _wait(lambda: backup_file.exists(), 45), log_path.read_text()[-2000:]
            data = _valid(backup_file)                      # written before polling started
            assert data["counts"]["quizzes"] == 0
            assert (tmp_path / "quiz_backup.json.prev") is not None
            assert "Backup file:" in log_path.read_text()
            assert _wait(lambda: "Backup supervisor started" in log_path.read_text(), 20)

            # someone else writes to the database while the bot is running
            qid = _add_quiz_directly(db_file, n=3)
            ok = _wait(lambda: qid in backup_file.read_text(encoding="utf-8"), 30)
            assert ok, "external database change was not picked up:\n" + log_path.read_text()[-2500:]
            data = _valid(backup_file)
            quiz = [q for q in data["quizzes"] if q["id"] == qid][0]
            assert len(quiz["questions"]) == 3 and quiz["title"] == "बाहरी प्रश्न quiz"

            # a corrupt/partial file must never be produced: parse the .prev too
            prev = json.loads(Path(f"{backup_file}.prev").read_text(encoding="utf-8"))
            assert prev["format"] == "qzzzzz-quiz-backup"
        finally:
            rc = _stop(proc, logf)
        log = log_path.read_text()
    assert rc == 0
    assert "Shutdown backup" in log                        # final export on SIGTERM
    data = _valid(backup_file)
    assert data["counts"]["quizzes"] == 1 and data["counts"]["questions"] == 3


def test_live_restart_keeps_quizzes_and_backup(tmp_path):
    db_file = tmp_path / "quiz.db"
    backup_file = tmp_path / "quiz_backup.json"
    qid = _add_quiz_directly(db_file, n=2)                 # quizzes that existed "before"
    with FakeBotAPI(conflicts=0) as api:
        proc, logf, log_path = _start(tmp_path, api.port)
        try:
            assert _wait(lambda: backup_file.exists(), 45)
            first = _valid(backup_file)
            assert [q["id"] for q in first["quizzes"]] == [qid]
        finally:
            assert _stop(proc, logf) == 0

        # ---- redeploy: same DATA_DIR (Railway volume), brand new container
        proc, logf, log_path = _start(tmp_path, api.port)
        try:
            assert _wait(lambda: "Backup verified:" in log_path.read_text(), 45), \
                log_path.read_text()[-2000:]
            log = log_path.read_text()
            assert "Backup file:" in log and "quizzes, " in log
            second = _valid(backup_file)
            assert [q["id"] for q in second["quizzes"]] == [qid]      # data survived
            assert len(second["quizzes"][0]["questions"]) == 2

            # and a new quiz created after the restart is backed up too
            new_qid = _add_quiz_directly(db_file, n=1)
            assert _wait(lambda: new_qid in backup_file.read_text(encoding="utf-8"), 30), \
                log_path.read_text()[-2000:]
            assert len(_valid(backup_file)["quizzes"]) == 2
        finally:
            assert _stop(proc, logf) == 0


def test_live_cli_verify_against_the_running_database(tmp_path):
    db_file = tmp_path / "quiz.db"
    _add_quiz_directly(db_file, n=2)
    env = _env(tmp_path, 1)                                # port unused: no server needed
    out = tmp_path / "cli.json"
    p = subprocess.run([sys.executable, "scripts/backup_tool.py", "export",
                        "--db", str(db_file), "--out", str(out)],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    p = subprocess.run([sys.executable, "scripts/backup_tool.py", "verify", "--file", str(out),
                        "--db", str(db_file)], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "✅" in p.stdout
    assert len(_valid(out)["quizzes"]) == 1
