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
                     "pdf_parser.py", "pdf_extract.py", "stats.py", "keyboards.py", "config.py"])
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
    assert rj["build"]["builder"] == "DOCKERFILE" and rj["deploy"]["overlapSeconds"] == 0
    df = (ROOT / "Dockerfile").read_text()
    assert "TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata" in df
    # the pinned standard models are installed; Debian's weaker "fast" packages are not
    assert "COPY scripts/fetch_tessdata.py" in df and "RUN python /tmp/fetch_tessdata.py" in df
    assert "apt-get install" not in df and "tesseract-ocr-hin" not in df
    assert "<<" not in df                                   # no BuildKit-only heredocs
    assert "ocr_status" in df                               # build fails if OCR can't work
    assert df.strip().splitlines()[-1] == 'CMD ["python", "bot.py"]'   # exec form → SIGTERM reaches python
    # railway.json startCommand is run by Railway in exec form too — must be the same command
    assert rj["deploy"]["startCommand"].split() == ["python", "bot.py"]
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert "tests/" in ignore and "scripts/" not in ignore and "data/" in ignore


def test_tests_use_exactly_the_ocr_models_pinned_for_the_image():
    """OCR accuracy depends on the model files: the files the tests run with
    must be byte-identical to what the Dockerfile installs."""
    import hashlib
    import pdf_extract as X
    td = X.find_tessdata()
    if not td:
        pytest.skip("no tessdata available")
    pinned = dict(re.findall(r'"(eng|hin)": "([0-9a-f]{64})"', (ROOT / "scripts/fetch_tessdata.py").read_text()))
    assert set(pinned) == {"eng", "hin"}
    for lang, sha in pinned.items():
        got = hashlib.sha256(Path(td, f"{lang}.traineddata").read_bytes()).hexdigest()
        assert got == sha, f"{td}/{lang}.traineddata differs from the Docker image's model"


_PERSIST = r"""
import sys, config, database as db
print("DB", config.DB_PATH); print("LOCK", config.LOCK_PATH)
db.init_db()
if sys.argv[1] == "write":
    db.ensure_user_row(42); qid = db.new_quiz(42, "Persist", "d"); db.add_question(qid, "Q?", ["a", "b", "c"], 2, None)
else:
    qs = db.get_owner_quizzes(42, include_drafts=True); print("QUIZ", qs[0]["title"], db.get_questions(qs[0]["id"])[0]["options"])
"""


def test_railway_volume_is_used_and_data_survives_a_redeploy(tmp_path):
    vol = tmp_path / "data"                                    # Railway mounts the volume here
    env = {k: v for k, v in os.environ.items() if k not in ("DATA_DIR", "DB_PATH", "BOT_TOKEN")}
    env.update(RAILWAY_VOLUME_MOUNT_PATH=str(vol), PYTHONPATH=str(ROOT))
    runs = [subprocess.run([sys.executable, "-c", _PERSIST, mode], cwd=tmp_path, env=env,
                           capture_output=True, text=True, timeout=60) for mode in ("write", "read")]
    for r in runs:
        assert r.returncode == 0, r.stderr
    out = runs[1].stdout
    assert f"DB {vol / 'quiz.db'}" in out and f"LOCK {vol / 'bot.lock'}" in out
    assert "QUIZ Persist ['a', 'b', 'c']" in out               # a fresh process sees the data


def test_missing_railway_volume_is_warned_loudly(tmp_path):
    env = _env(tmp_path, BOT_TOKEN="1:x", RAILWAY_ENVIRONMENT="production",
               TELEGRAM_API_BASE="http://127.0.0.1:9")          # unreachable: we only need startup logs
    p = subprocess.Popen([sys.executable, "bot.py"], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = p.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    assert "NO RAILWAY VOLUME ATTACHED" in out, out[-2000:]
