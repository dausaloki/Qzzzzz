"""Railway startup tests: env handling, single-instance lock, polling + Conflict."""
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("BOT_TOKEN", "RAILWAY_VOLUME_MOUNT_PATH")}
    env["DATA_DIR"] = str(tmp_path)
    env.update(extra)
    return env


def test_missing_token_exits_with_clear_message(tmp_path):
    p = subprocess.run([sys.executable, "bot.py"], cwd=ROOT, env=_env(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 1
    assert "BOT_TOKEN environment variable is missing" in p.stderr


def test_second_instance_is_refused(tmp_path):
    import fcntl
    lock = open(tmp_path / "bot.lock", "a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        p = subprocess.run([sys.executable, "bot.py"], cwd=ROOT, env=_env(tmp_path, BOT_TOKEN="1:x"),
                           capture_output=True, text=True, timeout=60)
    finally:
        lock.close()
    assert p.returncode == 1
    assert "refusing to start a second poller" in p.stderr


def test_invalid_token_against_real_telegram(tmp_path):
    """Real network start-up path (skipped if api.telegram.org is unreachable)."""
    try:
        import urllib.request
        urllib.request.urlopen("https://api.telegram.org", timeout=5)
    except Exception as exc:  # noqa: BLE001
        if "HTTP Error" not in str(exc):
            pytest.skip(f"no network: {exc}")
    p = subprocess.run([sys.executable, "bot.py"], cwd=ROOT,
                       env=_env(tmp_path, BOT_TOKEN="123456:invalid-token-for-test"),
                       capture_output=True, text=True, timeout=90)
    assert p.returncode != 0
    assert "InvalidToken" in p.stderr or "rejected by the server" in p.stderr
    assert (tmp_path / "quiz.db").exists()           # DB initialised on the Railway data dir


def test_run_polling_survives_conflict(tmp_path, caplog):
    sys.path.insert(0, str(Path(__file__).parent))
    from fake_telegram import FakeTelegram

    import bot as botmod
    import config
    import database as db

    db.set_db_path(tmp_path / "poll.db")
    api, updates = FakeTelegram(), FakeTelegram()
    updates.conflicts_left = 2
    app = botmod.build_application("123456:TEST", request=api, get_updates_request=updates, rate_limit=False)
    app.job_queue.run_once(lambda ctx: ctx.application.stop_running(), 8)
    with caplog.at_level(logging.WARNING):
        app.run_polling(allowed_updates=["message", "callback_query", "poll_answer"], close_loop=False)
    db.set_db_path(config.DB_PATH)
    assert updates.get_updates_calls > 3            # kept polling after the conflicts
    assert "Telegram Conflict" in caplog.text
    assert any(a == "deleteWebhook" for a, _ in api.calls)
    assert any(a == "setMyCommands" for a, _ in api.calls)   # post_init ran


def test_code_creates_exactly_one_application_and_poller():
    src = "\n".join((ROOT / f).read_text(encoding="utf-8") for f in
                    ["bot.py", "quiz_runner.py", "quiz_creator.py", "quiz_engine.py", "database.py",
                     "pdf_parser.py", "stats.py", "keyboards.py", "config.py"])
    assert len(re.findall(r"Application\.builder\(\)", src)) == 1
    assert len(re.findall(r"\.run_polling\(", src)) == 1
    assert "start_polling" not in src and "run_webhook" not in src


def test_deployment_files():
    req = (ROOT / "requirements.txt").read_text()
    assert "python-telegram-bot[job-queue,rate-limiter]==22.5" in req and "PyMuPDF" in req
    assert (ROOT / "Procfile").read_text().strip() == "worker: python bot.py"
    import json
    rj = json.loads((ROOT / "railway.json").read_text())
    assert rj["deploy"]["startCommand"] == "python bot.py" and rj["deploy"]["numReplicas"] == 1
