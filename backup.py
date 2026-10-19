"""Automatic, permanent backup of every quiz and quiz-related record.

Design rules (why this module is safe):

* **The SQLite database is the only source of truth.**  This module reads the
  database and *rewrites* ``quiz_backup.json`` from scratch every time — it
  never appends, never merges and never writes to the database.  Restoring is a
  separate, explicit, admin-only tool (``scripts/backup_tool.py``).
* **Every committed change schedules a backup.**  ``database.connect()`` calls
  the hook registered here after a transaction has committed, so a new quiz, an
  edited question, a changed answer, a finished attempt or a new leaderboard
  row all mark the backup dirty.  A background supervisor rewrites the file
  after a short quiet period (default 5 s, at most one write per 30 s while the
  bot is busy, and always within ``BACKUP_MAX_DELAY``) so nothing is ever left
  unwritten.
* **Atomic + crash safe.**  The JSON is written to a temporary file in the same
  directory, fsynced, then ``os.replace()``d over the old file.  The previous
  good file is kept as ``quiz_backup.json.prev``.
* **Validated before it is published.**  A backup that fails validation (broken
  references, missing quiz codes, an answer index out of range, a secret in the
  payload) is never written over the last good file; the error is logged and
  retried.
* **No secrets, no leakage.**  The payload is built from a fixed column list, so
  ``BOT_TOKEN`` and friends can never appear in it; a value scan is the last
  check before writing.  The GitHub copy is additionally redacted (no user ids,
  no chat ids, no owner ids, no usernames).
* **Storage.**  The default location is next to the database (``DATA_DIR`` →
  ``RAILWAY_VOLUME_MOUNT_PATH`` on Railway), i.e. on the persistent volume, and
  it is verified/regenerated on every startup.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import config
import database as db

log = logging.getLogger("quizbot.backup")

BACKUP_FORMAT = "qzzzzz-quiz-backup"
BACKUP_VERSION = 1

# Tables exported, in restore-safe order (parents before children).  ``ORDER BY``
# makes the JSON deterministic, so an unchanged database produces identical
# output and "has anything changed?" is answered with a hash.
_TABLES: dict[str, str] = {
    "users": "ORDER BY created_at, user_id",
    "quizzes": "ORDER BY created_at, id",
    "quiz_settings": "ORDER BY quiz_id",
    "questions": "ORDER BY quiz_id, position, id",
    "attempts": "ORDER BY id",
    "answers": "ORDER BY attempt_id, id",
    "group_sessions": "ORDER BY id",
    "group_polls": "ORDER BY session_id, q_index",
    "group_answers": "ORDER BY session_id, q_index, id",
    "user_states": "ORDER BY user_id",          # wizard + pending-review state
    "review_pdfs": "ORDER BY id",               # review-PDF delivery records (dedupe/retry)
}
_RESTORE_ORDER = ["users", "quizzes", "quiz_settings", "questions", "attempts",
                  "answers", "group_sessions", "group_polls", "group_answers", "user_states",
                  "review_pdfs"]
# Never exported, whatever happens (secrets / credentials).
_FORBIDDEN_KEYS = {"token", "bot_token", "password", "secret", "api_key", "api_hash", "github_token"}

_EXPORT_LOCK = threading.RLock()             # one writer thread per process
_dirty = threading.Event()
_dirty_reason = {"text": ""}
_dirty_first = {"ts": 0.0}
_dirty_last = {"ts": 0.0}
_last: dict[str, Any] = {}                   # last export result
_last_hash: dict[str, str] = {}              # content hash of the last local write
_last_gh: dict[str, Any] = {}                # last GitHub sync result
_last_gh_hash: dict[str, str] = {}           # hash of the last uploaded payload
_safety = {"ts": 0.0}
_db_sig: dict[str, Any] = {"sig": None}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_hook_registered = False
_gh_disabled_logged = False


# --------------------------------------------------------------------- helpers
def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def db_path() -> Path:
    return Path(db.get_db_path())


def backup_path() -> Path:
    return Path(config.BACKUP_PATH)


def prev_path() -> Path:
    return Path(f"{backup_path()}.prev")


def lock_path() -> Path:
    return Path(f"{backup_path()}.lock")


def db_signature() -> list:
    """(mtime, size) of the database and its WAL file — cheap drift detector."""
    parts = []
    for path in (db_path(), Path(f"{db_path()}-wal")):
        try:
            st = path.stat()
            parts.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((str(path), 0, 0))
    return parts


def _external_change(now: float) -> bool:
    """True when the database changed without our write hook seeing it."""
    interval = float(getattr(config, "BACKUP_SAFETY_INTERVAL", 300))
    if now - _safety["ts"] < interval:
        return False
    _safety["ts"] = now
    if _db_sig["sig"] is None:
        _db_sig["sig"] = db_signature()          # first tick: nothing to compare yet
        return False
    if db_signature() != _db_sig["sig"]:
        log.info("Database changed outside this process — scheduling a backup")
        return True
    return False


def _sleep(seconds: float) -> None:
    """Retry back-off; short in tests so a failure path stays fast."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        seconds = min(seconds, 0.05)
    if seconds > 0:
        time.sleep(seconds)


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]


def _select(con: sqlite3.Connection, table: str, order: str) -> list[dict]:
    """Read a whole table using only the columns that exist in this database."""
    cols = [c for c in _columns(con, table) if c.lower() not in _FORBIDDEN_KEYS]
    if not cols:
        return []
    return [dict(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM {table} {order}")]


def _strip_secrets(value: Any) -> Any:
    """Drop any dict key that looks like a credential (recursive)."""
    if isinstance(value, dict):
        return {k: _strip_secrets(v) for k, v in value.items() if k.lower() not in _FORBIDDEN_KEYS}
    if isinstance(value, list):
        return [_strip_secrets(v) for v in value]
    return value


def _plain(row: dict) -> dict:
    """sqlite3.Row → plain dict (also normalises non-JSON column types)."""
    return {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
            for k, v in dict(row).items()}


# --------------------------------------------------------------------- build
def build_payload(now: Optional[float] = None) -> dict:
    """Read the *whole* production database into one JSON-serialisable dict.

    A single read transaction (``BEGIN``) is used, so the export is a consistent
    snapshot even while the bot writes (WAL mode keeps the bot running).
    """
    now = time.time() if now is None else now
    with db.connect() as con:
        con.execute("BEGIN")
        try:
            tables = {name: [_plain(r) for r in _select(con, name, order)]
                      for name, order in _TABLES.items()}
        finally:
            try:
                con.execute("ROLLBACK")             # read-only snapshot
            except sqlite3.Error:                   # pragma: no cover - defensive
                pass

    quizzes_by_id: dict[str, dict] = {}
    for row in tables["quizzes"]:
        row["questions"] = []
        row["settings"] = None
        quizzes_by_id[row["id"]] = row

    for st in tables["quiz_settings"]:
        quiz = quizzes_by_id.get(st["quiz_id"])
        if quiz is not None:
            quiz["settings"] = {k: v for k, v in st.items() if k != "quiz_id"}
    settings_orphans = sum(1 for st in tables["quiz_settings"] if st["quiz_id"] not in quizzes_by_id)

    orphans = settings_orphans
    for row in tables["questions"]:
        quiz = quizzes_by_id.get(row.get("quiz_id"))
        if quiz is None:
            orphans += 1
            continue
        quiz["questions"].append(row)
    for quiz in quizzes_by_id.values():
        quiz["questions"].sort(key=lambda q: (q.get("position") is None, q.get("position"), q.get("id")))

    attempts: list[dict] = tables["attempts"]
    by_attempt = {a["id"]: a for a in attempts}
    for a in attempts:
        a["answers"] = []
    for row in tables["answers"]:
        att = by_attempt.get(row.get("attempt_id"))
        if att is None:
            orphans += 1
            continue
        att["answers"].append(row)

    sessions: list[dict] = tables["group_sessions"]
    by_session = {s["id"]: s for s in sessions}
    for s in sessions:
        s["polls"] = []
        s["answers"] = []
    for row in tables["group_polls"]:
        sess = by_session.get(row.get("session_id"))
        if sess is None:
            orphans += 1
            continue
        sess["polls"].append(row)
    for row in tables["group_answers"]:
        sess = by_session.get(row.get("session_id"))
        if sess is None:
            orphans += 1
            continue
        sess["answers"].append(row)

    try:
        stat = db_path().stat()
        db_info = {"path": str(db_path()), "size_bytes": stat.st_size, "mtime": _iso(stat.st_mtime)}
    except OSError:                                  # pragma: no cover - defensive
        db_info = {"path": str(db_path()), "size_bytes": 0, "mtime": ""}

    tables = {k: _strip_secrets(v) for k, v in tables.items()}
    counts = {k: len(v) for k, v in tables.items()}
    counts["answers"] = sum(len(a["answers"]) for a in attempts)
    counts["group_polls"] = sum(len(s["polls"]) for s in sessions)
    counts["group_answers"] = sum(len(s["answers"]) for s in sessions)
    counts["questions"] = sum(len(q["questions"]) for q in quizzes_by_id.values())
    counts["quiz_settings"] = sum(1 for q in quizzes_by_id.values() if q.get("settings"))
    counts["orphans"] = orphans

    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "generated_at": _iso(now),
        "generated_ts": round(float(now), 3),
        "source": "sqlite",
        "source_note": "full export of the production database; never edited by hand",
        "db": db_info,
        "counts": counts,
        "quizzes": sorted(quizzes_by_id.values(), key=lambda q: (q.get("created_at") or "", q["id"])),
        "attempts": attempts,
        "group_sessions": sessions,
        "users": tables["users"],
        "pending_states": tables["user_states"],
        "review_pdfs": tables["review_pdfs"],
    }


def content_hash(data: dict) -> str:
    """Stable hash of the *data*, ignoring the volatile export timestamp.

    Used to decide "nothing changed → do not rewrite the file / do not commit".
    """
    copy = {k: v for k, v in data.items() if k not in ("generated_at", "generated_ts")}
    if isinstance(copy.get("db"), dict):
        copy["db"] = {k: v for k, v in copy["db"].items() if k not in ("size_bytes", "mtime")}
    return hashlib.sha256(_serialise(copy).encode("utf-8")).hexdigest()


def _secret_scan(text: str) -> list[str]:
    """Refuse to publish a payload that contains a live credential."""
    bad = []
    token = os.getenv("BOT_TOKEN", "").strip()
    if token and token in text:
        bad.append("payload contains BOT_TOKEN")
    if config.GITHUB_TOKEN and config.GITHUB_TOKEN in text:
        bad.append("payload contains GITHUB_TOKEN")
    for key in ("password", "api_key", "api_hash"):
        if f'"{key}"' in text:
            bad.append(f"payload contains a {key!r} field")
    return bad


# --------------------------------------------------------------------- validate
def validate_payload(data: dict) -> list[str]:
    """JSON-level + structural + referential integrity check → list of problems."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["backup is not a JSON object"]
    if data.get("format") != BACKUP_FORMAT:
        problems.append(f"unexpected format {data.get('format')!r}")
    if data.get("version") != BACKUP_VERSION:
        problems.append(f"unexpected version {data.get('version')!r} (expected {BACKUP_VERSION})")
    if not data.get("generated_at"):
        problems.append("missing generated_at")

    quizzes = data.get("quizzes")
    if not isinstance(quizzes, list):
        return problems + ["quizzes is not a list"]

    seen_ids: set[str] = set()
    q_ids: set[str] = set()
    per_quiz: dict[str, int] = {}
    for i, quiz in enumerate(quizzes):
        where = f"quizzes[{i}]"
        qid = quiz.get("id")
        if not isinstance(qid, str) or not qid.strip():
            problems.append(f"{where}: missing quiz code/id")
            continue
        if qid in seen_ids:
            problems.append(f"{where}: duplicate quiz code {qid!r}")
        seen_ids.add(qid)
        q_ids.add(qid)
        if not isinstance(quiz.get("title"), str) or not quiz["title"].strip():
            problems.append(f"{where} ({qid}): missing title")
        questions = quiz.get("questions")
        if not isinstance(questions, list):
            problems.append(f"{where} ({qid}): questions is not a list")
            questions = []
        per_quiz[qid] = len(questions)
        seen_pos: set[int] = set()
        for j, q in enumerate(questions):
            qwhere = f"quiz {qid} questions[{j}]"
            if q.get("quiz_id") not in (None, qid):
                problems.append(f"{qwhere}: quiz_id {q.get('quiz_id')!r} != {qid!r}")
            pos = q.get("position")
            if isinstance(pos, int):
                if pos in seen_pos:
                    problems.append(f"{qwhere}: duplicate position {pos}")
                seen_pos.add(pos)
            opts = q.get("options", q.get("options_json"))   # DB column is options_json
            if isinstance(opts, str):
                try:
                    opts = json.loads(opts)
                except (TypeError, ValueError):
                    problems.append(f"{qwhere}: options_json is not valid JSON")
                    opts = None
            if not isinstance(opts, list) or len(opts) < 2:
                problems.append(f"{qwhere}: fewer than 2 options")
            ci = q.get("correct_index")
            if not isinstance(ci, int) or (isinstance(opts, list) and not 0 <= ci < len(opts)):
                problems.append(f"{qwhere}: correct_index {ci!r} out of range for "
                                f"{len(opts) if isinstance(opts, list) else '?'} options")
            if not str(q.get("question") or "").strip():
                problems.append(f"{qwhere}: empty question text")

    for i, att in enumerate(data.get("attempts") or []):
        where = f"attempts[{i}]"
        if not isinstance(att.get("id"), int):
            problems.append(f"{where}: missing attempt id")
            continue
        if att.get("quiz_id") not in q_ids:
            problems.append(f"{where} (id {att['id']}): unknown quiz_id {att.get('quiz_id')!r}")
        total = len(att.get("answers") or [])
        if isinstance(att.get("total"), int) and att["total"] > 0 and total > att["total"]:
            problems.append(f"{where}: {total} answers for a {att['total']}-question attempt")
        for j, a in enumerate(att.get("answers") or []):
            if a.get("attempt_id") != att["id"]:
                problems.append(f"{where}.answers[{j}]: attempt_id {a.get('attempt_id')!r} != {att['id']}")
            if a.get("status") not in (None, "correct", "wrong", "skipped", "timeout"):
                problems.append(f"{where}.answers[{j}]: unknown status {a.get('status')!r}")

    for i, sess in enumerate(data.get("group_sessions") or []):
        where = f"group_sessions[{i}]"
        if not isinstance(sess.get("id"), int):
            problems.append(f"{where}: missing session id")
            continue
        if sess.get("quiz_id") not in q_ids:
            problems.append(f"{where} (id {sess['id']}): unknown quiz_id {sess.get('quiz_id')!r}")
        for j, a in enumerate(sess.get("answers") or []):
            if a.get("session_id") != sess["id"]:
                problems.append(f"{where}.answers[{j}]: session_id {a.get('session_id')!r} != {sess['id']}")
            if a.get("quiz_id") not in (None, sess.get("quiz_id")):
                problems.append(f"{where}.answers[{j}]: quiz_id mismatch")

    counts = data.get("counts")
    if isinstance(counts, dict):
        checks = [("quizzes", len(quizzes)),
                  ("questions", sum(len(q.get("questions") or []) for q in quizzes)),
                  ("attempts", len(data.get("attempts") or [])),
                  ("answers", sum(len(a.get("answers") or []) for a in data.get("attempts") or [])),
                  ("group_sessions", len(data.get("group_sessions") or [])),
                  ("review_pdfs", len(data.get("review_pdfs") or [])),
                  ("users", len(data.get("users") or []))]
        for key, actual in checks:
            if counts.get(key) != actual:
                problems.append(f"counts.{key} {counts.get(key)} != {actual} actual")
    else:
        problems.append("missing counts")

    try:
        text = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        problems.append(f"payload is not JSON serialisable: {exc}")
        text = ""
    problems.extend(_secret_scan(text))
    return problems


# --------------------------------------------------------------------- write
def _atomic_write(path: Path, text: str) -> int:
    """Write ``text`` atomically; keep the previous file as ``.prev``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if path.exists():
        try:
            old = path.read_bytes()
            with open(prev_path(), "wb") as fh:
                fh.write(old)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:                       # never fail an export over .prev
            log.warning("Could not keep the previous backup copy: %s", exc)
    os.replace(tmp, path)
    try:                                             # make the rename durable
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:                                  # pragma: no cover - some FS
        pass
    return len(text.encode("utf-8"))


def _serialise(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)


def _take_process_lock() -> Optional[int]:
    """Cross-process guard so two containers can never write at the same time."""
    try:
        import fcntl
    except ImportError:                              # pragma: no cover - Windows
        return None
    try:
        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:                                  # pragma: no cover - defensive
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        log.warning("Another process is writing %s — skipping this backup tick", path.name)
        return None
    return fd


def _drop_process_lock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except (ImportError, OSError):                   # pragma: no cover - defensive
        pass
    try:
        os.close(fd)
    except OSError:                                  # pragma: no cover - defensive
        pass


# --------------------------------------------------------------------- export
def export_now(reason: str = "manual", force: bool = False) -> dict:
    """Build → validate → atomically write the backup.  Never raises.

    With ``force=False`` the file is left untouched when the exported content is
    byte-identical to the last write (no timestamp churn, no needless commit).
    """
    out: dict[str, Any] = {"ok": False, "reason": reason, "path": str(backup_path()),
                           "generated_at": _iso(), "ts": time.time(), "attempts": 0, "errors": []}
    attempts = max(1, int(getattr(config, "BACKUP_RETRIES", 5)))
    delay = max(0.0, float(getattr(config, "BACKUP_RETRY_DELAY", 3)))
    with _EXPORT_LOCK:
        fd = _take_process_lock()
        if fd is None:
            out["errors"] = ["another process holds the backup lock"]
            return out
        try:
            for attempt in range(1, attempts + 1):
                out["attempts"] = attempt
                try:
                    data = build_payload()
                    problems = validate_payload(data)
                    if problems:
                        out["errors"] = problems
                        log.error("Backup NOT written (%d problem(s)): %s",
                                  len(problems), "; ".join(problems[:5]))
                        if attempt < attempts:
                            _sleep(delay)
                            continue
                        break
                    text = _serialise(data)
                    digest = content_hash(data)
                    path = backup_path()
                    if not force and digest == _last_hash.get("local") and path.exists():
                        out.update(ok=True, skipped="unchanged", bytes=path.stat().st_size,
                                   hash=digest, counts=data["counts"], generated_ts=data["generated_ts"])
                        _dirty.clear()
                        _last.update(out)
                        return out
                    size = _atomic_write(path, text)
                    out.update(ok=True, skipped=False, bytes=size, hash=digest,
                               counts=data["counts"], generated_ts=data["generated_ts"],
                               prev=str(prev_path()) if prev_path().exists() else "")
                    _last_hash["local"] = digest
                    _db_sig["sig"] = db_signature()
                    _dirty.clear()
                    _dirty_reason["text"] = ""
                    _last.update(out)
                    log.info("Backup written: %s (%s bytes, %d quizzes, %d questions, %d attempts, "
                             "reason=%s)", path.name, f"{size:,}", data["counts"].get("quizzes", 0),
                             data["counts"].get("questions", 0), data["counts"].get("attempts", 0), reason)
                    return out
                except (db.DatabaseError, sqlite3.Error, OSError, ValueError, TypeError) as exc:
                    out["errors"] = [f"{type(exc).__name__}: {exc}"]
                    log.error("Backup attempt %d/%d failed (%s): %s", attempt, attempts, reason, exc)
                    if attempt < attempts:
                        _sleep(delay)
            out["ok"] = False
            if not out["errors"]:
                out["errors"] = ["validation failed"]
            _last.update(out)
            return out
        finally:
            _drop_process_lock(fd)


def last_result() -> dict:
    return dict(_last)


def reset_state() -> None:
    """Forget cached hashes/results (tests; also handy after a manual restore)."""
    _last.clear()
    _last_hash.clear()
    _last_gh.clear()
    _last_gh_hash.clear()
    _dirty.clear()
    _dirty_reason["text"] = ""
    _dirty_first["ts"] = 0.0
    _dirty_last["ts"] = 0.0
    _safety["ts"] = 0.0
    _db_sig["sig"] = None


# --------------------------------------------------------------------- dirty tracking
def mark_dirty(reason: str = "db") -> None:
    """Called after a committed database transaction (see ``database.py``)."""
    now = time.time()
    if not _dirty.is_set():
        _dirty_first["ts"] = now
    _dirty_last["ts"] = now
    _dirty_reason["text"] = reason or "db"
    _dirty.set()


def pending() -> bool:
    return _dirty.is_set()


def register_db_hook() -> None:
    """Ask ``database`` to notify us after every committed write."""
    global _hook_registered
    db.add_write_hook(mark_dirty)          # add_write_hook ignores duplicates
    _hook_registered = True


def install(do_startup_export: bool = True) -> dict:
    """Wire the module into the running bot: hook + startup export."""
    register_db_hook()
    return {"hook": True,
            "startup": export_now(reason="startup", force=True) if do_startup_export else None}


# --------------------------------------------------------------------- github
def github_payload(data: dict) -> dict:
    """Redacted copy for GitHub: no user/chat/owner ids, no usernames."""
    private = ("user_id", "chat_id", "owner_id", "started_by", "username", "first_name", "last_name")

    def scrub(row: dict) -> dict:
        return {k: v for k, v in row.items() if k not in private}

    out = json.loads(json.dumps(data, ensure_ascii=False))
    out["redacted"] = ("this copy is for GitHub: user ids, chat ids, owner ids and names are "
                       "removed; the complete file stays on the persistent volume")
    out["users"] = [dict(scrub(u), user_ref=f"u{i}") for i, u in enumerate(out.get("users") or [])]
    out["pending_states"] = [scrub(u) for u in out.get("pending_states") or []]
    for a in out.get("attempts") or []:
        a.update({k: None for k in private if k in a})     # keep the keys, drop the values
        a["redacted"] = True
    for s in out.get("group_sessions") or []:
        s.update({k: None for k in private if k in s})
        s["redacted"] = True
        for ans in s.get("answers") or []:
            ans.update({k: None for k in private if k in ans})
            ans["redacted"] = True
    for q in out.get("quizzes") or []:
        q["owner_id"] = None
    return out


def _gh_request(method: str, url: str, body: Optional[dict] = None) -> tuple[int, dict, dict]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {config.GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "qzzzzz-quizbot-backup")
    if payload:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=config.GITHUB_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            try:
                return resp.status, json.loads(raw), dict(resp.headers)
            except ValueError:
                return resp.status, {}, dict(resp.headers)
    except urllib.error.HTTPError as exc:                # 401/403/404/409/422/429/5xx
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"message": raw}
        return exc.code, parsed, dict(exc.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"message": f"network error: {exc}"}, {}


def sync_github(force: bool = False) -> dict:
    """Upload the redacted backup through the GitHub contents API.  Never raises.

    * no token → disabled (``skipped: "not-configured"``)
    * content unchanged → no commit (``skipped: "unchanged"``)
    * SHA race / 409 / 422 → re-read the file and retry (up to BACKUP_RETRIES)
    * 401 / 403 / 429 / 5xx → logged; retried on the next scheduled sync
    """
    global _gh_disabled_logged
    out: dict[str, Any] = {"ok": False, "attempts": 0, "errors": [], "ts": time.time()}
    if not config.github_enabled():
        out["skipped"] = "not-configured"
        if not _gh_disabled_logged:
            log.info("GitHub backup sync disabled (set GITHUB_TOKEN + GITHUB_REPO to enable it)")
            _gh_disabled_logged = True
        _last_gh.update(out)
        return out
    if pending():                                        # never upload a stale file
        local = export_now(reason="github-pre-sync")
        if not local.get("ok"):
            out["errors"] = ["local backup is stale (export failed); upload skipped"]
            _last_gh.update(out)
            return out
    else:
        local = _last
    payload = github_payload(build_payload())
    text = _serialise(payload)
    digest = content_hash(payload)
    if not force and digest == _last_gh_hash.get("github"):
        out.update(ok=True, skipped="unchanged", hash=digest)
        _last_gh.update(out)
        return out

    url = f"{config.GITHUB_API}/repos/{config.GITHUB_REPO}/contents/{config.GITHUB_BACKUP_PATH}"
    if config.GITHUB_BRANCH:
        url += f"?ref={config.GITHUB_BRANCH}"
    counts = payload.get("counts") or {}
    body: dict[str, Any] = {
        "message": (f"quiz backup {payload['generated_at']} (v{BACKUP_VERSION}, "
                    f"{counts.get('quizzes', 0)} quizzes, {counts.get('questions', 0)} questions, "
                    f"{counts.get('attempts', 0)} attempts)"),
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    if config.GITHUB_BRANCH:
        body["branch"] = config.GITHUB_BRANCH

    attempts = max(1, int(getattr(config, "BACKUP_RETRIES", 5)))
    delay = max(0.0, float(getattr(config, "BACKUP_RETRY_DELAY", 3)))
    for attempt in range(1, attempts + 1):
        out["attempts"] = attempt
        status, resp, _h = _gh_request("GET", url)
        if status == 200 and resp.get("sha"):
            body["sha"] = resp["sha"]                     # update the file, never replace it
        elif status in (401, 403):
            out["errors"] = [f"GitHub auth/permission error {status}: {resp.get('message')} "
                             "(needs a token with contents:write)"]
            log.error("GitHub sync: %s", out["errors"][0])
            break
        elif status == 404:
            body.pop("sha", None)                         # first run → create the file
        elif status == 0 or status == 429 or status >= 500:
            out["errors"] = [f"GitHub temporarily unavailable ({status}: {resp.get('message')})"]
            log.warning("GitHub sync: %s", out["errors"][0])
            if attempt < attempts:
                _sleep(delay)
                continue
            break
        elif status != 200:
            out["errors"] = [f"GitHub error {status}: {resp.get('message')}"]
            break

        status, resp, _h = _gh_request("PUT", url, body)
        if status in (200, 201):
            out.update(ok=True, skipped=False, hash=digest, bytes=len(text.encode("utf-8")),
                       commit=(resp.get("commit") or {}).get("sha", "")[:12],
                       local_bytes=(local or {}).get("bytes", 0))
            _last_gh_hash["github"] = digest
            _last_gh.update(out)
            log.info("GitHub backup pushed: %s/%s (%s bytes, commit %s)", config.GITHUB_REPO,
                     config.GITHUB_BACKUP_PATH, f"{out['bytes']:,}", out["commit"])
            return out
        if status in (409, 422, 404):                     # someone else committed → re-read sha
            out["errors"] = [f"GitHub conflict ({status}) — re-reading the file and retrying"]
            log.warning("GitHub sync: %s", out["errors"][0])
            if attempt < attempts:
                _sleep(delay)
                continue
            break
        if status == 0 or status == 429 or status >= 500:
            out["errors"] = [f"GitHub temporarily unavailable ({status})"]
            if attempt < attempts:
                _sleep(delay)
                continue
            break
        out["errors"] = [f"GitHub error {status}: {resp.get('message')}"]
        break
    _last_gh.update(out)
    return out


def last_github_result() -> dict:
    return dict(_last_gh)


# --------------------------------------------------------------------- verify
def verify_file(path: Optional[Path] = None, compare_db: bool = True) -> tuple[bool, list[str], dict]:
    """Validate the backup file on disk: JSON syntax, structure, DB cross-check."""
    path = Path(path or backup_path())
    problems: list[str] = []
    info: dict[str, Any] = {"path": str(path), "bytes": 0, "generated_at": "", "counts": {}, "stale": []}
    if not path.exists():
        return False, [f"{path} does not exist"], info
    try:
        raw = path.read_text(encoding="utf-8")
        info["bytes"] = len(raw.encode("utf-8"))
        data = json.loads(raw)                        # JSON syntax check
    except (OSError, ValueError) as exc:
        return False, [f"unreadable/invalid JSON: {exc}"], info
    info["generated_at"] = data.get("generated_at", "")
    info["counts"] = data.get("counts") or {}
    info["quizzes"] = len(data.get("quizzes") or [])
    problems.extend(validate_payload(data))
    if compare_db:
        try:
            live = build_payload().get("counts") or {}
            for key in ("quizzes", "questions", "attempts", "answers", "users"):
                a = (data.get("counts") or {}).get(key)
                b = live.get(key)
                if a is not None and b is not None and a != b:
                    info["stale"].append(f"{key}: file={a} database={b}")
        except (db.DatabaseError, sqlite3.Error, OSError) as exc:
            problems.append(f"could not read the database for comparison: {exc}")
    return (not problems), problems, info


# --------------------------------------------------------------------- restore
def restore_payload(data: dict, db_file: Optional[Path] = None, apply: bool = False) -> dict:
    """Restore a backup **without duplicating existing records**.

    Every table is upserted on its natural key (``INSERT`` when the row is gone,
    ``UPDATE`` when it exists), so restoring twice is a no-op the second time and
    a live database keeps rows that are newer than the backup.  With
    ``apply=False`` (the default) nothing is written — the payload is only
    checked against the current database and a plan is returned.
    """
    problems = validate_payload(data)
    result: dict[str, Any] = {"ok": not problems, "problems": problems, "applied": False,
                              "inserted": {}, "updated": {}, "skipped": {}, "target": str(db_file or db_path())}
    if problems:
        return result
    target = Path(db_file) if db_file else db_path()
    con = sqlite3.connect(target, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        rows: dict[str, list[dict]] = {t: [] for t in _RESTORE_ORDER}
        rows["users"] = list(data.get("users") or [])
        rows["quizzes"] = [dict({k: v for k, v in q.items() if k not in ("questions", "settings")})
                           for q in data.get("quizzes") or []]
        rows["quiz_settings"] = [dict(q["settings"], quiz_id=q["id"])
                                 for q in data.get("quizzes") or [] if q.get("settings")]
        rows["questions"] = [q for quiz in data.get("quizzes") or [] for q in quiz.get("questions") or []]
        rows["attempts"] = [dict({k: v for k, v in a.items() if k != "answers"})
                            for a in data.get("attempts") or []]
        rows["answers"] = [a for att in data.get("attempts") or [] for a in att.get("answers") or []]
        rows["group_sessions"] = [dict({k: v for k, v in s.items() if k not in ("polls", "answers")})
                                  for s in data.get("group_sessions") or []]
        rows["group_polls"] = [p for s in data.get("group_sessions") or [] for p in s.get("polls") or []]
        rows["group_answers"] = [a for s in data.get("group_sessions") or [] for a in s.get("answers") or []]
        rows["user_states"] = list(data.get("pending_states") or [])
        rows["review_pdfs"] = list(data.get("review_pdfs") or [])

        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        schema_missing = "quizzes" not in names
        if schema_missing and not apply:
            result["note"] = ("target database has no schema yet: --apply creates it and inserts "
                              "every record")
            for table in _RESTORE_ORDER:
                result["inserted"][table] = len(rows[table])
                result["updated"][table] = 0
            con.rollback()
            return result
        if schema_missing:                            # empty/new database → create the schema
            con.executescript(db.SCHEMA)
            con.executescript(db.INDEXES)

        for table in _RESTORE_ORDER:
            table_rows = rows[table]
            if not table_rows:
                result["inserted"][table] = result["updated"][table] = 0
                continue
            cols = _columns(con, table)
            if not cols:
                result.setdefault("skipped", {})[table] = "table missing"
                continue
            pk = "user_id" if table in ("users", "user_states") else (
                "quiz_id" if table == "quiz_settings" else "poll_id" if table == "group_polls" else "id")
            if pk not in cols:
                result["skipped"][table] = f"no {pk} column"
                continue
            ins = upd = 0
            for row in table_rows:
                values = {k: v for k, v in row.items() if k in cols}
                if not values:
                    continue
                exists = con.execute(f"SELECT 1 FROM {table} WHERE {pk}=?", (values[pk],)).fetchone()
                if exists:
                    if apply:
                        sets = ", ".join(f"{k}=?" for k in values if k != pk)
                        if sets:
                            con.execute(f"UPDATE {table} SET {sets} WHERE {pk}=?",
                                        [v for k, v in values.items() if k != pk] + [values[pk]])
                    upd += 1
                else:
                    if apply:
                        con.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES "
                                    f"({', '.join('?' for _ in values)})", list(values.values()))
                    ins += 1
            result["inserted"][table] = ins
            result["updated"][table] = upd
        if apply:
            con.commit()
            result["applied"] = True
            mark_dirty("restore")
        else:
            con.rollback()
    except sqlite3.Error as exc:
        con.rollback()
        result["ok"] = False
        result["problems"] = problems + [f"database error: {exc}"]
    finally:
        con.close()
    return result


def restore_from_file(path: Path, db_file: Optional[Path] = None, apply: bool = False) -> dict:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "problems": [f"cannot read {path}: {exc}"], "applied": False,
                "inserted": {}, "updated": {}, "skipped": {}}
    return restore_payload(data, db_file=db_file, apply=apply)


# --------------------------------------------------------------------- scheduler
def run_once(now: Optional[float] = None, github_due: Optional[bool] = None) -> dict:
    """One scheduler tick: export when dirty (debounce + rate limit + max delay).

    The background supervisor calls this every second; tests call it directly
    with a fake ``now`` so the timing rules are verified without sleeping.
    """
    now = time.time() if now is None else now
    result: dict[str, Any] = {"exported": False, "github": None, "reason": ""}
    if pending():
        reason = _dirty_reason["text"] or "db"
        debounce = float(getattr(config, "BACKUP_DEBOUNCE", 5))
        min_interval = float(getattr(config, "BACKUP_MIN_INTERVAL", 30))
        max_delay = float(getattr(config, "BACKUP_MAX_DELAY", 120))
        settled = now - _dirty_last["ts"] >= debounce
        overdue = now - _dirty_first["ts"] >= max_delay
        if not (settled or overdue):
            return result                                # a write burst, still settling
        last_ts = float(_last.get("generated_ts") or 0)
        if last_ts and now - last_ts < min_interval and not overdue:
            return result                                # rate limited; next tick takes it
    elif _external_change(now):
        reason = "external-db-change"                    # e.g. a CLI restore/import
    else:
        return result
    result["reason"] = reason
    out = export_now(reason=reason)
    result["export"] = out
    result["exported"] = bool(out.get("ok") and not out.get("skipped"))
    if out.get("ok") and config.github_enabled():
        due = github_due if github_due is not None else (
            now - float(_last_gh.get("ts") or 0) >= float(getattr(config, "GITHUB_SYNC_INTERVAL", 300)))
        if due:
            result["github"] = sync_github()
    return result


def _loop(poll_seconds: float = 1.0) -> None:
    log.info("Backup supervisor started (file=%s, debounce=%ss, min interval=%ss)",
             backup_path(), config.BACKUP_DEBOUNCE, config.BACKUP_MIN_INTERVAL)
    while not _stop.is_set():
        try:
            run_once()
        except Exception as exc:                          # noqa: BLE001 — never die
            log.error("Backup tick failed: %s", exc)
        _stop.wait(poll_seconds)
    log.info("Backup supervisor stopped")


def start_background(poll_seconds: float = 1.0) -> bool:
    """Start the supervisor thread (idempotent; production path only)."""
    global _thread
    if not config.BACKUP_ENABLED:
        log.warning("Automatic backups are DISABLED (BACKUP_ENABLED=0)")
        return False
    if _thread and _thread.is_alive():
        return False
    register_db_hook()
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(poll_seconds,), name="quiz-backup", daemon=True)
    _thread.start()
    return True


def stop_background(timeout: float = 5.0) -> None:
    _stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout)


def shutdown_export() -> dict:
    """SIGTERM/shutdown: write and upload whatever is still pending."""
    stop_background()
    out = export_now(reason="shutdown", force=True)
    if config.github_enabled():
        out["github"] = sync_github()
    return out


# --------------------------------------------------------------------- status
def status_text() -> str:
    """Admin-facing status (Hindi flavour), used by the /backup command."""
    path = backup_path()
    lines = ["🗄️ <b>Backup status</b>", f"File: <code>{path}</code>"]
    if path.exists():
        st = path.stat()
        ok, problems, info = verify_file(path)
        lines.append(f"Size: {info['bytes']:,} bytes · written {_iso(st.st_mtime)}")
        lines.append(f"Generated: {info.get('generated_at') or '—'}")
        counts = info.get("counts") or {}
        lines.append("Records: " + ", ".join(f"{k}={counts.get(k, 0)}" for k in
                                            ("quizzes", "questions", "attempts", "answers", "users")))
        lines.append("Integrity: " + ("✅ valid" if ok else "❌ " + "; ".join(problems[:3])))
        if info.get("stale"):
            lines.append("⚠️ DB drift: " + "; ".join(info["stale"][:3]))
    else:
        lines.append("File: ❌ not created yet")
    lines.append(f"Pending changes: {'yes' if pending() else 'no'}")
    last = _last or {}
    if last:
        lines.append(f"Last export: {'✅' if last.get('ok') else '❌'} {last.get('reason', '')} "
                     f"{last.get('generated_at', '')} (tries {last.get('attempts', 0)})")
        if last.get("errors"):
            lines.append("Error: " + "; ".join(str(e) for e in last["errors"][:2]))
    if config.github_enabled():
        gh = _last_gh or {}
        state = "✅ pushed" if gh.get("ok") and not gh.get("skipped") else (
            f"⏳ {gh.get('skipped')}" if gh.get("skipped") else
            f"❌ {gh.get('errors') or 'not synced yet'}")
        lines.append(f"GitHub: {config.GITHUB_REPO}/{config.GITHUB_BACKUP_PATH} — {state}")
    else:
        lines.append("GitHub: ❌ not configured (set GITHUB_TOKEN + GITHUB_REPO)")
    return "\n".join(lines)
