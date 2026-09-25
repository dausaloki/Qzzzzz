"""The REAL startup path: ``python bot.py`` as a subprocess against a local
fake Bot API server (TELEGRAM_API_BASE).  Verifies:

* exactly one getUpdates request is ever in flight (single poller),
* 409 Conflict responses do not crash the process, it keeps polling,
* /start is answered,
* a second process with the same data dir refuses to start (lock),
* SIGTERM gives a clean, zero exit code.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456:REAL-STARTUP-TEST"


class FakeBotAPI:
    def __init__(self, conflicts: int = 2):
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.get_updates_calls = 0
        self.conflicts_left = conflicts
        self.start_delivered = False
        self.calls: list[str] = []
        self.sent: list[dict] = []
        self.msg_id = 100
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type", "")
                params = {}
                if "json" in ctype and raw:
                    params = json.loads(raw)
                elif raw:
                    params = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                method = self.path.rsplit("/", 1)[-1]
                status, body = api.handle(method, params)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client abandoned a long poll (normal during shutdown)

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    # --------------------------------------------------------------- API
    def handle(self, method: str, params: dict):
        with self.lock:
            self.calls.append(method)
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 999, "is_bot": True, "first_name": "Quiz",
                                                "username": "real_quiz_bot"}}
        if method == "getUpdates":
            with self.lock:
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
                self.get_updates_calls += 1
                conflict = self.conflicts_left > 0
                if conflict:
                    self.conflicts_left -= 1
            try:
                if conflict:
                    time.sleep(0.05)
                    return 409, {"ok": False, "error_code": 409,
                                 "description": "Conflict: terminated by other getUpdates request; "
                                                "make sure that only one bot instance is running"}
                with self.lock:
                    deliver = not self.start_delivered
                    self.start_delivered = True
                if deliver:
                    upd = {"update_id": 1, "message": {
                        "message_id": 1, "date": int(time.time()),
                        "chat": {"id": 4242, "type": "private", "first_name": "Ravi"},
                        "from": {"id": 4242, "is_bot": False, "first_name": "Ravi"},
                        "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}}
                    return 200, {"ok": True, "result": [upd]}
                time.sleep(min(float(params.get("timeout", 1) or 1), 0.5))
                return 200, {"ok": True, "result": []}
            finally:
                with self.lock:
                    self.inflight -= 1
        if method == "sendMessage":
            with self.lock:
                self.msg_id += 1
                self.sent.append(params)
                mid = self.msg_id
            return 200, {"ok": True, "result": {
                "message_id": mid, "date": int(time.time()),
                "chat": {"id": int(params.get("chat_id", 0)), "type": "private"},
                "text": params.get("text", "")}}
        return 200, {"ok": True, "result": True}


def _env(tmp_path: Path, port: int) -> dict:
    env = dict(os.environ)
    env.update({"BOT_TOKEN": TOKEN, "TELEGRAM_API_BASE": f"http://127.0.0.1:{port}",
                "DATA_DIR": str(tmp_path), "PYTHONUNBUFFERED": "1"})
    env.pop("DB_PATH", None)
    env.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
    return env


def _wait(cond, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signals/locks")
def test_real_bot_process_single_poller_conflict_start_and_sigterm(tmp_path):
    with FakeBotAPI(conflicts=2) as api:
        env = _env(tmp_path, api.port)
        log_path = tmp_path / "bot.log"
        with open(log_path, "w") as logf:
            proc = subprocess.Popen([sys.executable, "bot.py"], cwd=ROOT, env=env,
                                    stdout=logf, stderr=subprocess.STDOUT)
            try:
                ok = _wait(lambda: any(str(m.get("chat_id")) == "4242" for m in api.sent), 45)
                log = log_path.read_text()
                assert ok, f"/start was never answered. calls={api.calls[-20:]}\n{log[-3000:]}"
                assert proc.poll() is None, "bot process died after 409 Conflict"
                reply = next(m for m in api.sent if str(m.get("chat_id")) == "4242")
                assert "Quiz" in reply.get("text", "") or "quiz" in reply.get("text", "")
                assert api.conflicts_left == 0          # both 409s were served…
                assert "Conflict" in log                # …and logged, not fatal
                assert "OCR ready" in log or "OCR disabled" in log   # OCR probe ran, didn't block

                # a second process with the same data dir must refuse to poll
                before = api.get_updates_calls
                second = subprocess.run([sys.executable, "bot.py"], cwd=ROOT, env=env,
                                        capture_output=True, text=True, timeout=60)
                assert second.returncode == 1, second.stdout + second.stderr
                assert "refusing to start a second poller" in (second.stdout + second.stderr)

                # keep polling a little, then check the single-poller invariant
                _wait(lambda: api.get_updates_calls >= before + 2, 10)
                # measured BEFORE shutdown: on SIGTERM PTB abandons the pending long
                # poll and sends one final getUpdates(timeout=0) to commit the offset,
                # while this fake may still be sleeping on the dead connection.
                assert api.max_inflight == 1, f"{api.max_inflight} concurrent getUpdates"
                assert api.get_updates_calls >= 4
                assert api.calls.count("getMe") >= 1
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                try:
                    rc = proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    raise AssertionError("bot did not exit after SIGTERM")
        log = log_path.read_text()
        assert rc == 0, f"exit code {rc}\n{log[-3000:]}"
        assert "Traceback" not in log.split("Conflict")[-1] or "Unhandled" not in log, log[-3000:]
