"""Deterministic regression test for the start-up SIGTERM race.

Root cause of the intermittent ``test_backup_live`` failure (``assert -15 == 0``):
``bot.main()`` runs the migration, writes the start-up backup, verifies it and
starts the backup thread *before* ``app.run_polling()``.  python-telegram-bot
installs its SIGTERM handler only inside ``run_polling()``.  A SIGTERM in that
window (Railway redeploy/stop right after a start, or the test stopping the bot
as soon as ``quiz_backup.json`` appears) hit Python's default action: the
process was killed instantly (exit -15) and no shutdown backup was written.

Instead of relying on timing luck, the real ``python bot.py`` start-up code is
run in a subprocess and SIGTERM is delivered at an exact point of the window.
"""
import json
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from test_backup_live import _env
from test_real_startup import FakeBotAPI

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signals")

WRAPPER = textwrap.dedent('''
    import os, signal, sys
    sys.path.insert(0, os.getcwd())
    point = sys.argv[1]

    def _sigterm_self():
        os.kill(os.getpid(), signal.SIGTERM)       # Railway's SIGTERM, exactly here

    def _hook(obj, name):
        orig = getattr(obj, name)
        def hooked(*a, **k):
            _sigterm_self()
            return orig(*a, **k)
        setattr(obj, name, hooked)

    import backup, bot, database
    from telegram.ext import Application
    if point == "during-migration":
        _hook(database, "init_db")
    elif point == "after-startup-backup":
        _hook(backup, "verify_file")               # backup file already written
    elif point == "before-ptb-handlers":
        _hook(Application, "run_polling")          # past every start-up step
    bot.main()
''')


@pytest.mark.parametrize("point", ["during-migration", "after-startup-backup", "before-ptb-handlers"])
def test_sigterm_during_startup_exits_cleanly_with_final_backup(tmp_path, point):
    wrapper = tmp_path / "start_with_sigterm.py"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    log_path = tmp_path / "bot.log"
    with FakeBotAPI(conflicts=0) as api:
        with open(log_path, "a") as logf:
            proc = subprocess.run([sys.executable, str(wrapper), point], cwd=ROOT,
                                  env=_env(tmp_path, api.port), stdout=logf,
                                  stderr=subprocess.STDOUT, timeout=60)
        calls = list(api.calls)
    log = log_path.read_text()
    assert proc.returncode == 0, f"exit {proc.returncode} (-15 = killed by SIGTERM)\n{log[-2500:]}"
    assert "Stop signal" in log and "Shutdown backup" in log, log[-2500:]
    assert "getUpdates" not in calls                     # polling never started
    data = json.loads((tmp_path / "quiz_backup.json").read_text(encoding="utf-8"))
    assert data["format"] == "qzzzzz-quiz-backup" and data["version"] == 1


def test_sigterm_after_polling_started_still_uses_ptb_shutdown(tmp_path):
    """Unchanged behaviour once polling runs: PTB's own handler → post_shutdown."""
    log_path = tmp_path / "bot.log"
    with FakeBotAPI(conflicts=0) as api:
        with open(log_path, "a") as logf:
            proc = subprocess.Popen([sys.executable, "bot.py"], cwd=ROOT, env=_env(tmp_path, api.port),
                                    stdout=logf, stderr=subprocess.STDOUT)
            try:
                import time
                end = time.time() + 45
                while time.time() < end and api.get_updates_calls < 1:
                    time.sleep(0.05)
                assert api.get_updates_calls >= 1, log_path.read_text()[-2000:]
            finally:
                proc.send_signal(signal.SIGTERM)
                rc = proc.wait(timeout=30)
    log = log_path.read_text()
    assert rc == 0
    assert "Shutdown backup" in log and "Stop signal" not in log
