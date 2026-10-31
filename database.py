"""SQLite persistence layer.

Everything that matters (quizzes, questions, settings, live quiz sessions,
answers, wizard state) is stored here, so a bot restart never loses data.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

import config

log = logging.getLogger(__name__)

_DB_PATH: Path = config.DB_PATH
_lock = threading.RLock()

# Callbacks run *after* a transaction committed with at least one changed row.
# Used by backup.py to schedule the JSON export; a failing hook must never
# affect the database transaction it follows.
_write_hooks: list = []


class DatabaseError(Exception):
    """Raised for any database failure (wraps sqlite3 errors)."""


def add_write_hook(fn) -> None:
    if fn not in _write_hooks:
        _write_hooks.append(fn)


def clear_write_hooks() -> None:
    _write_hooks.clear()


def _notify_write_hooks(changed: int) -> None:
    for fn in list(_write_hooks):
        try:
            fn(f"db:{changed}")
        except Exception:  # noqa: BLE001 — a hook must never break the bot
            log.exception("Write hook %r failed", getattr(fn, "__name__", fn))


def set_db_path(path: str | Path) -> None:
    """Used by tests to point at a temporary database."""
    global _DB_PATH
    _DB_PATH = Path(path)


def get_db_path() -> Path:
    return _DB_PATH


@contextmanager
def connect():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        con = sqlite3.connect(_DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        changed = 0
        try:
            con.execute("PRAGMA foreign_keys=ON")
            yield con
            con.commit()
            changed = con.total_changes        # rows changed by this transaction
        except sqlite3.Error as exc:
            con.rollback()
            log.exception("Database error")
            raise DatabaseError(str(exc)) from exc
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        if changed:
            _notify_write_hooks(changed)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    last_name TEXT DEFAULT '',
    default_timer INTEGER DEFAULT 45,
    default_shuffle_questions INTEGER DEFAULT 0,
    default_shuffle_options INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    member_verified_ts REAL DEFAULT 0,
    member_checked_ts REAL DEFAULT 0,
    first_start_notified INTEGER DEFAULT 0,
    first_start_at TEXT DEFAULT '',
    batches_json TEXT DEFAULT '[]',
    is_banned INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quizzes (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    pre_text TEXT DEFAULT '',
    status TEXT DEFAULT 'ready',
    source TEXT DEFAULT 'manual',
    quiz_mode TEXT DEFAULT 'standard',
    access_tier TEXT DEFAULT 'free',
    batch_name TEXT DEFAULT '',
    allowed_chats_json TEXT DEFAULT '[]',
    section_timers_json TEXT DEFAULT '{}',
    negative_mark REAL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quiz_settings (
    quiz_id TEXT PRIMARY KEY,
    timer INTEGER DEFAULT 45,
    shuffle_questions INTEGER DEFAULT 0,
    shuffle_options INTEGER DEFAULT 0,
    FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    correct_index INTEGER NOT NULL,
    explanation TEXT DEFAULT '',
    qtype TEXT DEFAULT 'mcq',
    pre_text TEXT DEFAULT '',
    pre_media_type TEXT DEFAULT '',
    pre_media_id TEXT DEFAULT '',
    source_number INTEGER,
    source_ref TEXT DEFAULT '',
    section TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    status TEXT DEFAULT 'active',
    order_json TEXT DEFAULT '[]',
    current_index INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    correct INTEGER DEFAULT 0,
    wrong INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    score INTEGER DEFAULT 0,
    timer INTEGER DEFAULT 0,
    shuffle_options INTEGER DEFAULT 0,
    quiz_mode TEXT DEFAULT 'standard',
    section_scores_json TEXT DEFAULT '{}',
    negative_mark REAL DEFAULT 0,
    net_score REAL,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_ts REAL DEFAULT 0,
    finished_at TEXT,
    duration_sec REAL DEFAULT 0,
    cur_poll_id TEXT,
    cur_message_id INTEGER,
    cur_question_id INTEGER,
    cur_perm_json TEXT,
    cur_correct INTEGER,
    cur_sent_ts REAL,
    cur_post_explanation TEXT DEFAULT '',
    start_msg_id INTEGER
);

CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    position INTEGER DEFAULT 0,
    chosen_index INTEGER,
    correct INTEGER DEFAULT 0,
    status TEXT DEFAULT 'answered',
    elapsed REAL DEFAULT 0,
    answered_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
);

-- one quiz running inside a Telegram group (quiz_id + chat_id + session id);
-- any number of groups (and private chats) can run the same quiz at once.
CREATE TABLE IF NOT EXISTS group_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    started_by INTEGER,
    status TEXT DEFAULT 'active',
    order_json TEXT DEFAULT '[]',
    current_index INTEGER DEFAULT 0,
    asked INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    timer INTEGER DEFAULT 30,
    shuffle_options INTEGER DEFAULT 0,
    started_ts REAL DEFAULT 0,
    finished_ts REAL,
    cur_poll_id TEXT,
    cur_message_id INTEGER,
    cur_question_id INTEGER,
    cur_sent_ts REAL,
    cur_post_explanation TEXT DEFAULT '',
    negative_mark REAL DEFAULT 0
);

-- every poll sent in a group session (answers can be mapped back even later)
CREATE TABLE IF NOT EXISTS group_polls (
    poll_id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    q_index INTEGER NOT NULL,
    perm_json TEXT NOT NULL,
    correct_display INTEGER NOT NULL,
    message_id INTEGER,
    sent_ts REAL,
    FOREIGN KEY(session_id) REFERENCES group_sessions(id) ON DELETE CASCADE
);

-- one row per participant per question: A's answer can never overwrite B's
CREATE TABLE IF NOT EXISTS group_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    quiz_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    poll_id TEXT NOT NULL,
    question_id INTEGER NOT NULL,
    q_index INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    chosen_display INTEGER NOT NULL,
    chosen_index INTEGER NOT NULL,
    is_correct INTEGER NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,          -- points for this answer (1 correct / 0 wrong)
    elapsed REAL DEFAULT 0,
    answered_ts REAL,
    UNIQUE(session_id, question_id, user_id),
    FOREIGN KEY(session_id) REFERENCES group_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_states (
    user_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    data_json TEXT DEFAULT '{}',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- one row per delivered "complete review" PDF: prevents duplicate delivery.
-- attempt_id → private quiz attempt; (session_id,user_id) → group quiz participant.
CREATE TABLE IF NOT EXISTS review_pdfs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER,
    session_id INTEGER,
    user_id INTEGER NOT NULL,
    chat_id INTEGER,
    quiz_id TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',          -- pending|sent|failed|blocked
    tries INTEGER DEFAULT 0,
    bytes INTEGER DEFAULT 0,
    pages INTEGER DEFAULT 0,
    message_id INTEGER,
    error TEXT DEFAULT '',
    created_ts REAL DEFAULT 0,
    sent_ts REAL
);

CREATE TABLE IF NOT EXISTS batches (
    name TEXT PRIMARY KEY,
    description TEXT DEFAULT '',
    created_by INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS batch_members (
    batch_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    added_by INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0,
    PRIMARY KEY (batch_name, user_id)
);

CREATE TABLE IF NOT EXISTS authorized_chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT DEFAULT '',
    batch_name TEXT DEFAULT '',
    added_by INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_questions_quiz ON questions(quiz_id, position);
CREATE INDEX IF NOT EXISTS idx_attempts_quiz ON attempts(quiz_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_pdfs_attempt ON review_pdfs(attempt_id)
    WHERE attempt_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_pdfs_group ON review_pdfs(session_id, user_id)
    WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id, status);
CREATE INDEX IF NOT EXISTS idx_attempts_poll ON attempts(cur_poll_id);
CREATE INDEX IF NOT EXISTS idx_answers_attempt ON answers(attempt_id);
CREATE INDEX IF NOT EXISTS idx_quizzes_owner ON quizzes(owner_id);
CREATE INDEX IF NOT EXISTS idx_gsessions_chat ON group_sessions(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_gpolls_session ON group_polls(session_id);
CREATE INDEX IF NOT EXISTS idx_ganswers_session ON group_answers(session_id, user_id);
CREATE INDEX IF NOT EXISTS idx_batch_members_user ON batch_members(user_id);
"""

# Columns that may be missing in databases created by the previous version.
_MIGRATION_COLUMNS = {
    "users": {
        "last_name": "TEXT DEFAULT ''",
        "default_timer": "INTEGER DEFAULT 45",
        "default_shuffle_questions": "INTEGER DEFAULT 0",
        "default_shuffle_options": "INTEGER DEFAULT 0",
        "created_at": "TEXT",
        "last_seen": "TEXT",
        "member_verified_ts": "REAL DEFAULT 0",
        "member_checked_ts": "REAL DEFAULT 0",
        "first_start_notified": "INTEGER DEFAULT 0",
        "first_start_at": "TEXT DEFAULT ''",
        "batches_json": "TEXT DEFAULT '[]'",
        "is_banned": "INTEGER DEFAULT 0",
    },
    "quizzes": {
        "pre_text": "TEXT DEFAULT ''",
        "status": "TEXT DEFAULT 'ready'",
        "source": "TEXT DEFAULT 'manual'",
        "created_at": "TEXT",
        "updated_at": "TEXT",
        "series_id": "TEXT",
        "part_no": "INTEGER",
        "quiz_mode": "TEXT DEFAULT 'standard'",
        "access_tier": "TEXT DEFAULT 'free'",
        "batch_name": "TEXT DEFAULT ''",
        "allowed_chats_json": "TEXT DEFAULT '[]'",
        "section_timers_json": "TEXT DEFAULT '{}'",
        "negative_mark": "REAL DEFAULT 0",
    },
    "questions": {
        "qtype": "TEXT DEFAULT 'mcq'",
        "pre_text": "TEXT DEFAULT ''",
        "pre_media_type": "TEXT DEFAULT ''",
        "pre_media_id": "TEXT DEFAULT ''",
        "source_number": "INTEGER",
        "source_ref": "TEXT DEFAULT ''",
        "section": "TEXT DEFAULT ''",
        "created_at": "TEXT",
    },
    "attempts": {
        "timeouts": "INTEGER DEFAULT 0",
        "status": "TEXT DEFAULT 'active'",
        "order_json": "TEXT DEFAULT '[]'",
        "current_index": "INTEGER DEFAULT 0",
        "timer": "INTEGER DEFAULT 0",
        "shuffle_options": "INTEGER DEFAULT 0",
        "quiz_mode": "TEXT DEFAULT 'standard'",
        "section_scores_json": "TEXT DEFAULT '{}'",
        "negative_mark": "REAL DEFAULT 0",
        "net_score": "REAL",
        "started_ts": "REAL DEFAULT 0",
        "duration_sec": "REAL DEFAULT 0",
        "cur_poll_id": "TEXT",
        "start_msg_id": "INTEGER",
        "cur_message_id": "INTEGER",
        "cur_question_id": "INTEGER",
        "cur_perm_json": "TEXT",
        "cur_correct": "INTEGER",
        "cur_sent_ts": "REAL",
        "cur_post_explanation": "TEXT DEFAULT ''",
    },
    "answers": {
        "position": "INTEGER DEFAULT 0",
        "status": "TEXT DEFAULT 'answered'",
        "answered_at": "TEXT",
    },
    # negative mark snapshot taken when a group quiz starts (0 = disabled, the default)
    "group_sessions": {
        "negative_mark": "REAL DEFAULT 0",
    },
}


def _columns(con, table) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        had_users_table = bool(
            con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        )
        had_first_start_col = ("first_start_notified" in _columns(con, "users")) if had_users_table else True
        con.executescript(SCHEMA)
        # --- migrate older schema in place ---
        for table, cols in _MIGRATION_COLUMNS.items():
            existing = _columns(con, table)
            for col, decl in cols.items():
                if col not in existing:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        if had_users_table and not had_first_start_col:
            # Pre-existing users in a migrated database already started the bot earlier;
            # mark them notified so a redeploy/upgrade never spams duplicate notifications.
            con.execute("""
                UPDATE users
                SET first_start_notified=1,
                    first_start_at=COALESCE(NULLIF(first_start_at, ''), created_at, CURRENT_TIMESTAMP)
                WHERE first_start_notified=0
            """)
        qcols = _columns(con, "quizzes")
        if {"timer", "shuffle_questions", "shuffle_options"} <= qcols:
            # old version kept settings on the quizzes table
            con.execute("""
                INSERT OR IGNORE INTO quiz_settings(quiz_id,timer,shuffle_questions,shuffle_options)
                SELECT id, COALESCE(timer,30), COALESCE(shuffle_questions,0),
                       COALESCE(shuffle_options,0) FROM quizzes
            """)
        con.execute("""
            INSERT OR IGNORE INTO quiz_settings(quiz_id) SELECT id FROM quizzes
        """)
        # old attempts: finished_at set -> finished, else abandoned
        con.execute("""
            UPDATE attempts SET status='finished'
            WHERE finished_at IS NOT NULL AND (status IS NULL OR status='active')
              AND (order_json IS NULL OR order_json='[]')
        """)
        con.execute("""
            UPDATE attempts SET status='expired'
            WHERE finished_at IS NULL AND status='active'
              AND (order_json IS NULL OR order_json='[]')
        """)
        con.executescript(INDEXES)


# ----------------------------------------------------------------- users
def upsert_user(user) -> None:
    if user is None:
        return
    last_name = getattr(user, "last_name", None) or ""
    with connect() as con:
        con.execute("""
            INSERT INTO users(user_id, username, first_name, last_name, default_timer, last_seen)
            VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_name=excluded.last_name,
              last_seen=CURRENT_TIMESTAMP
        """, (user.id, user.username or "", user.first_name or "", last_name, config.DEFAULT_TIMER))


def get_user(user_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def ensure_user_row(user_id: int) -> dict:
    with connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO users(user_id, default_timer) VALUES(?, ?)",
            (user_id, config.DEFAULT_TIMER),
        )
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row)


def prepare_first_start_notification(user, now_str: str) -> tuple[bool, str]:
    """Ensure the user row exists and check whether first-start notification is needed.

    Returns ``(should_notify, first_start_at)``.  ``first_start_notified`` is only
    flipped to ``1`` by :func:`mark_first_start_notified` after the message to the
    support group succeeds, so temporary delivery failures can retry safely while
    keeping the original ``first_start_at`` timestamp.
    """
    if user is None:
        return False, now_str
    upsert_user(user)
    with connect() as con:
        row = con.execute(
            "SELECT first_start_notified, first_start_at FROM users WHERE user_id=?",
            (user.id,),
        ).fetchone()
        if not row:
            return False, now_str
        if int(row["first_start_notified"] or 0):
            return False, row["first_start_at"] or now_str
        ts = (row["first_start_at"] or "").strip() or now_str
        if not (row["first_start_at"] or "").strip():
            con.execute("UPDATE users SET first_start_at=? WHERE user_id=?", (ts, user.id))
        return True, ts


def mark_first_start_notified(user_id: int, ts_str: str = "") -> None:
    with connect() as con:
        if ts_str:
            con.execute(
                "UPDATE users SET first_start_notified=1, "
                "first_start_at=COALESCE(NULLIF(first_start_at, ''), ?) WHERE user_id=?",
                (ts_str, user_id),
            )
        else:
            con.execute("UPDATE users SET first_start_notified=1 WHERE user_id=?", (user_id,))


_USER_SETTING_FIELDS = {"default_timer", "default_shuffle_questions", "default_shuffle_options"}


def update_user_settings(user_id: int, **fields) -> None:
    bad = set(fields) - _USER_SETTING_FIELDS
    if bad:
        raise ValueError(f"Invalid user setting(s): {bad}")
    ensure_user_row(user_id)
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*fields.values(), user_id))


# ---------------------------------------------------------------- quizzes
def new_quiz(owner_id: int, title: str, description: str = "", *,
             status: str = "ready", source: str = "manual",
             timer: Optional[int] = None, shuffle_questions: Optional[int] = None,
             shuffle_options: Optional[int] = None) -> str:
    user = ensure_user_row(owner_id)
    timer = user.get("default_timer", config.DEFAULT_TIMER) if timer is None else timer
    if timer is None:
        timer = config.DEFAULT_TIMER
    sq = user.get("default_shuffle_questions", 0) if shuffle_questions is None else shuffle_questions
    so = user.get("default_shuffle_options", 0) if shuffle_options is None else shuffle_options
    with connect() as con:
        for _ in range(5):
            qid = uuid.uuid4().hex[:10]
            if not con.execute("SELECT 1 FROM quizzes WHERE id=?", (qid,)).fetchone():
                break
        con.execute(
            "INSERT INTO quizzes(id,owner_id,title,description,status,source) VALUES(?,?,?,?,?,?)",
            (qid, owner_id, title, description or "", status, source),
        )
        con.execute(
            "INSERT INTO quiz_settings(quiz_id,timer,shuffle_questions,shuffle_options) VALUES(?,?,?,?)",
            (qid, int(timer or 0), int(bool(sq)), int(bool(so))),
        )
    return qid


def get_quiz(qid: str) -> Optional[dict]:
    if not qid:
        return None
    with connect() as con:
        row = con.execute("""
            SELECT q.*, COALESCE(s.timer, 30) AS timer,
                   COALESCE(s.shuffle_questions, 0) AS shuffle_questions,
                   COALESCE(s.shuffle_options, 0) AS shuffle_options,
                   (SELECT COUNT(*) FROM questions WHERE quiz_id=q.id) AS question_count
            FROM quizzes q LEFT JOIN quiz_settings s ON s.quiz_id=q.id
            WHERE q.id=?
        """, (qid,)).fetchone()
    return dict(row) if row else None


def get_owner_quizzes(owner_id: int, include_drafts: bool = True) -> list[dict]:
    sql = """
        SELECT q.*, (SELECT COUNT(*) FROM questions WHERE quiz_id=q.id) AS question_count
        FROM quizzes q WHERE owner_id=?
    """
    if not include_drafts:
        sql += " AND status='ready'"
    sql += " ORDER BY q.updated_at DESC, q.created_at DESC"
    with connect() as con:
        return [dict(r) for r in con.execute(sql, (owner_id,)).fetchall()]


def get_recent_played_quizzes(user_id: int, limit: int = 10) -> list[dict]:
    with connect() as con:
        rows = con.execute("""
            SELECT q.id, q.title, q.owner_id, MAX(a.id) AS last_attempt,
                   (SELECT COUNT(*) FROM questions WHERE quiz_id=q.id) AS question_count
            FROM attempts a JOIN quizzes q ON q.id=a.quiz_id
            WHERE a.user_id=? AND q.owner_id<>?
            GROUP BY q.id ORDER BY last_attempt DESC LIMIT ?
        """, (user_id, user_id, limit)).fetchall()
    return [dict(r) for r in rows]


_QUIZ_FIELDS = {
    "title", "description", "pre_text", "status", "source",
    "quiz_mode", "access_tier", "batch_name", "allowed_chats_json",
    "section_timers_json", "negative_mark",
}
_SETTING_FIELDS = {"timer", "shuffle_questions", "shuffle_options"}


def update_quiz(qid: str, **fields) -> None:
    bad = set(fields) - _QUIZ_FIELDS - _SETTING_FIELDS
    if bad:
        raise ValueError(f"Invalid quiz field(s): {bad}")
    qf = {k: v for k, v in fields.items() if k in _QUIZ_FIELDS}
    sf = {k: v for k, v in fields.items() if k in _SETTING_FIELDS}
    with connect() as con:
        if qf:
            sets = ", ".join(f"{k}=?" for k in qf)
            con.execute(f"UPDATE quizzes SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (*qf.values(), qid))
        if sf:
            con.execute("INSERT OR IGNORE INTO quiz_settings(quiz_id) VALUES(?)", (qid,))
            sets = ", ".join(f"{k}=?" for k in sf)
            con.execute(f"UPDATE quiz_settings SET {sets} WHERE quiz_id=?",
                        (*[int(v) for v in sf.values()], qid))
            con.execute("UPDATE quizzes SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (qid,))


def delete_quiz(qid: str, owner_id: Optional[int] = None) -> bool:
    with connect() as con:
        if owner_id is None:
            cur = con.execute("DELETE FROM quizzes WHERE id=?", (qid,))
        else:
            cur = con.execute("DELETE FROM quizzes WHERE id=? AND owner_id=?", (qid, owner_id))
        deleted = cur.rowcount > 0
        if deleted:
            con.execute("DELETE FROM quiz_settings WHERE quiz_id=?", (qid,))
            con.execute("DELETE FROM questions WHERE quiz_id=?", (qid,))
            con.execute("""UPDATE attempts SET status='stopped', cur_poll_id=NULL
                           WHERE quiz_id=? AND status='active'""", (qid,))
    return deleted


# -------------------------------------------------------------- questions
def _q_row(row) -> dict:
    d = dict(row)
    d["options"] = json.loads(d["options_json"])
    return d


def count_questions(qid: str) -> int:
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM questions WHERE quiz_id=?", (qid,)).fetchone()[0]


def add_question(qid: str, question: str, options: list[str], correct_index: int,
                 explanation: str = "", *, qtype: str = "mcq", pre_text: str = "",
                 pre_media_type: str = "", pre_media_id: str = "",
                 source_number: Optional[int] = None, source_ref: str = "",
                 section: str = "") -> int:
    _check_question(question, options, correct_index)
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM questions WHERE quiz_id=?", (qid,)).fetchone()[0]
        if n >= config.QUESTIONS_PER_PART:
            raise ValueError(f"A quiz part can have at most {config.QUESTIONS_PER_PART} questions "
                             "(the next questions go into the next Part)")
        pos = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM questions WHERE quiz_id=?",
                          (qid,)).fetchone()[0]
        cur = con.execute("""
            INSERT INTO questions(quiz_id,position,question,options_json,correct_index,
                                  explanation,qtype,pre_text,pre_media_type,pre_media_id,source_number,
                                  source_ref,section)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (qid, pos, question, json.dumps(list(options), ensure_ascii=False),
              int(correct_index), explanation or "", qtype, pre_text or "",
              pre_media_type or "", pre_media_id or "", source_number, source_ref or "",
              (section or "").strip()))
        con.execute("UPDATE quizzes SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (qid,))
        return cur.lastrowid


def _check_question(question: str, options, correct_index) -> None:
    if not isinstance(options, (list, tuple)) or not (
            config.MIN_OPTIONS <= len(options) <= config.MAX_OPTIONS):
        raise ValueError(f"{config.MIN_OPTIONS}-{config.MAX_OPTIONS} options are required")
    if not question or not str(question).strip():
        raise ValueError("Question text is empty")
    if any(not str(o).strip() for o in options):
        raise ValueError("An option is empty")
    if not 0 <= int(correct_index) < len(options):
        raise ValueError(f"correct_index must be 0..{len(options) - 1}")


def add_questions_bulk(qid: str, questions: Iterable[dict], source: str = "import") -> int:
    """Insert many questions in ONE transaction (all or nothing), in order.

    The question text is stored exactly as given (no length limit; SQLite TEXT
    is Unicode-safe); ``number`` is the original source numbering."""
    rows = []
    for q in questions:
        _check_question(q["question"], q["options"], q["correct_index"])
        rows.append(q)
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM questions WHERE quiz_id=?", (qid,)).fetchone()[0]
        if n + len(rows) > config.QUESTIONS_PER_PART:
            raise ValueError(f"A quiz part can have at most {config.QUESTIONS_PER_PART} questions")
        pos = con.execute("SELECT COALESCE(MAX(position),0) FROM questions WHERE quiz_id=?", (qid,)).fetchone()[0]
        con.executemany("""
            INSERT INTO questions(quiz_id,position,question,options_json,correct_index,explanation,qtype,
                                  pre_text,pre_media_type,pre_media_id,source_number,source_ref,section)
            VALUES(?,?,?,?,?,?,?,?,'','',?,?,?)
        """, [(qid, pos + i + 1, q["question"], json.dumps(list(q["options"]), ensure_ascii=False),
               int(q["correct_index"]), q.get("explanation", "") or "", q.get("qtype", "mcq") or "mcq",
               q.get("pre_text", "") or "", q.get("number"), q.get("source_ref", "") or "",
               (q.get("section") or q.get("section_label") or "").strip()) for i, q in enumerate(rows)])
        con.execute("UPDATE quizzes SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (qid,))
    return len(rows)


# -------------------------------------------------------------- series / parts
def set_part(qid: str, series_id: str, part_no: int) -> None:
    with connect() as con:
        con.execute("UPDATE quizzes SET series_id=?, part_no=? WHERE id=?", (series_id, int(part_no), qid))


def series_parts(series_id: Optional[str]) -> list[dict]:
    """All parts of an import series, Part 1 first."""
    if not series_id:
        return []
    with connect() as con:
        rows = con.execute("""
            SELECT q.*, (SELECT COUNT(*) FROM questions WHERE quiz_id=q.id) AS question_count,
                   (SELECT MIN(source_number) FROM questions WHERE quiz_id=q.id) AS first_number,
                   (SELECT MAX(source_number) FROM questions WHERE quiz_id=q.id) AS last_number
            FROM quizzes q WHERE series_id=? ORDER BY part_no, created_at
        """, (series_id,)).fetchall()
    return [dict(r) for r in rows]


def next_part(qid: str) -> Optional[dict]:
    quiz = get_quiz(qid)
    if not quiz or not quiz.get("series_id"):
        return None
    with connect() as con:
        row = con.execute("""SELECT id FROM quizzes WHERE series_id=? AND part_no>? AND status='ready'
                             ORDER BY part_no LIMIT 1""", (quiz["series_id"], quiz["part_no"] or 0)).fetchone()
    return get_quiz(row["id"]) if row else None


def get_questions(qid: str) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM questions WHERE quiz_id=? ORDER BY position,id",
                           (qid,)).fetchall()
    return [_q_row(r) for r in rows]


def get_question(qid: Optional[str], question_id: int) -> Optional[dict]:
    with connect() as con:
        if qid is None:
            row = con.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
        else:
            row = con.execute("SELECT * FROM questions WHERE quiz_id=? AND id=?",
                              (qid, question_id)).fetchone()
    return _q_row(row) if row else None


def update_question(question_id: int, **fields) -> None:
    """Update a question.  ``options`` and ``correct_index`` are validated
    together inside one transaction, so an edit can never leave a question
    whose correct answer points outside its options."""
    allowed = {"question", "explanation", "correct_index", "pre_text", "qtype", "options"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Invalid question field(s): {bad}")
    if not fields:
        return
    if "question" in fields and not str(fields["question"] or "").strip():
        raise ValueError("Question text is empty")
    with connect() as con:
        row = con.execute("SELECT options_json, correct_index, quiz_id FROM questions WHERE id=?",
                          (question_id,)).fetchone()
        if row is None:
            raise ValueError("Question not found")
        options = fields.pop("options", None)
        if options is not None:
            if not isinstance(options, (list, tuple)) or not (
                    config.MIN_OPTIONS <= len(options) <= config.MAX_OPTIONS):
                raise ValueError(f"{config.MIN_OPTIONS}-{config.MAX_OPTIONS} options are required")
            if any(not str(o).strip() for o in options):
                raise ValueError("An option is empty")
            fields["options_json"] = json.dumps(list(options), ensure_ascii=False)
        n_opts = len(options) if options is not None else len(json.loads(row["options_json"]))
        correct = int(fields.get("correct_index", row["correct_index"]))
        if not 0 <= correct < n_opts:
            raise ValueError(f"correct_index must be 0..{n_opts - 1}")
        if "correct_index" in fields:
            fields["correct_index"] = correct
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE questions SET {sets} WHERE id=?", (*fields.values(), question_id))
        con.execute("UPDATE quizzes SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["quiz_id"],))


def move_question(qid: str, question_id: int, delta: int) -> bool:
    """Move a question up (delta=-1) or down (+1).  Positions are renumbered
    1..n in the same transaction, so the order stays dense and consistent."""
    with connect() as con:
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM questions WHERE quiz_id=? ORDER BY position,id", (qid,)).fetchall()]
        if question_id not in ids:
            return False
        i = ids.index(question_id)
        j = i + (1 if delta > 0 else -1)
        if not 0 <= j < len(ids):
            return False
        ids[i], ids[j] = ids[j], ids[i]
        for pos, x in enumerate(ids, 1):
            con.execute("UPDATE questions SET position=? WHERE id=?", (pos, x))
        con.execute("UPDATE quizzes SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (qid,))
        return True


def user_rank(qid: str, user_id: int) -> tuple[int, int]:
    """(rank, participants) by each user's best finished attempt:
    higher net score first (net == correct unless the attempt used negative
    marking), then faster.  (0, n) if the user has none."""
    with connect() as con:
        rows = con.execute("""
            SELECT user_id, correct, duration_sec,
                   (correct - wrong * COALESCE(negative_mark, 0)) AS net FROM attempts a
            WHERE quiz_id=? AND status='finished' AND id = (
                SELECT b.id FROM attempts b WHERE b.quiz_id=a.quiz_id AND b.user_id=a.user_id
                  AND b.status='finished'
                ORDER BY (b.correct - b.wrong * COALESCE(b.negative_mark, 0)) DESC,
                         b.duration_sec ASC, b.id ASC LIMIT 1)
            ORDER BY net DESC, duration_sec ASC, id ASC
        """, (qid,)).fetchall()
    for i, r in enumerate(rows, 1):
        if r["user_id"] == user_id:
            return i, len(rows)
    return 0, len(rows)


def delete_question(question_id: int, qid: str) -> bool:
    with connect() as con:
        cur = con.execute("DELETE FROM questions WHERE id=? AND quiz_id=?", (question_id, qid))
        rows = con.execute("SELECT id FROM questions WHERE quiz_id=? ORDER BY position,id",
                           (qid,)).fetchall()
        for i, row in enumerate(rows, 1):
            con.execute("UPDATE questions SET position=? WHERE id=?", (i, row["id"]))
        con.execute("UPDATE quizzes SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (qid,))
        return cur.rowcount > 0


# --------------------------------------------------------------- attempts
def create_attempt(qid: str, user_id: int, chat_id: int, order: list[int],
                   timer: int, shuffle_options: bool, started_ts: float,
                   start_msg_id: Optional[int] = None, *, snapshot_rules: bool = False) -> int:
    with connect() as con:
        cur = con.execute("""
            INSERT INTO attempts(quiz_id,user_id,chat_id,status,order_json,current_index,total,
                                 timer,shuffle_options,started_ts,start_msg_id)
            VALUES(?,?,?,'active',?,0,?,?,?,?,?)
        """, (qid, user_id, chat_id, json.dumps(order), len(order), int(timer or 0),
              int(bool(shuffle_options)), started_ts, start_msg_id))
        if snapshot_rules:
            # Snapshot mode and scoring before the first poll, in the same transaction.
            # Later quiz edits must not change a running or completed attempt's rules.
            con.execute("""UPDATE attempts SET
                quiz_mode=COALESCE((SELECT quiz_mode FROM quizzes WHERE id=?), 'standard'),
                negative_mark=COALESCE((SELECT negative_mark FROM quizzes WHERE id=?), 0)
                WHERE id=?""", (qid, qid, cur.lastrowid))
        return cur.lastrowid


def _attempt_row(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    d["order"] = json.loads(d.get("order_json") or "[]")
    d["cur_perm"] = json.loads(d["cur_perm_json"]) if d.get("cur_perm_json") else None
    return d


def get_attempt(attempt_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    return _attempt_row(row)


def get_active_attempt(user_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("""SELECT * FROM attempts WHERE user_id=? AND status='active'
                             ORDER BY id DESC LIMIT 1""", (user_id,)).fetchone()
    return _attempt_row(row)


def get_attempt_by_poll(poll_id: str) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM attempts WHERE cur_poll_id=? AND status='active'",
                          (poll_id,)).fetchone()
    return _attempt_row(row)


def list_active_attempts() -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM attempts WHERE status='active'").fetchall()
    return [_attempt_row(r) for r in rows]


def set_current_poll(attempt_id: int, poll_id: str, message_id: int, question_id: int,
                     perm: list[int], correct: int, sent_ts: float,
                     post_explanation: str = "") -> None:
    with connect() as con:
        con.execute("""
            UPDATE attempts SET cur_poll_id=?, cur_message_id=?, cur_question_id=?,
                   cur_perm_json=?, cur_correct=?, cur_sent_ts=?, cur_post_explanation=?
            WHERE id=?
        """, (poll_id, message_id, question_id, json.dumps(perm), correct, sent_ts,
              post_explanation or "", attempt_id))


def record_answer(attempt_id: int, question_id: int, position: int,
                  chosen_index: Optional[int], is_correct: bool, status: str,
                  elapsed: float) -> None:
    """Save one answer and advance the attempt atomically.

    ``timeout`` (timer ran out) counts as skipped AND is counted separately in
    ``attempts.timeouts``."""
    if status not in {"correct", "wrong", "skipped", "timeout"}:
        raise ValueError("bad status")
    sets = {"correct": "correct=correct+1", "wrong": "wrong=wrong+1", "skipped": "skipped=skipped+1",
            "timeout": "skipped=skipped+1, timeouts=timeouts+1"}[status]
    with connect() as con:
        con.execute("""
            INSERT INTO answers(attempt_id,question_id,position,chosen_index,correct,status,elapsed)
            VALUES(?,?,?,?,?,?,?)
        """, (attempt_id, question_id, position, chosen_index, int(bool(is_correct)), status,
              float(elapsed)))
        con.execute(f"""
            UPDATE attempts SET {sets}, score=correct+?, current_index=current_index+1,
                   cur_poll_id=NULL, cur_message_id=NULL, cur_question_id=NULL,
                   cur_perm_json=NULL, cur_correct=NULL, cur_sent_ts=NULL,
                   cur_post_explanation=''
            WHERE id=?
        """, (1 if status == "correct" else 0, attempt_id))


def finish_attempt(attempt_id: int, status: str, duration_sec: float) -> Optional[dict]:
    if status not in {"finished", "stopped", "expired"}:
        raise ValueError("bad status")
    with connect() as con:
        con.execute("""
            UPDATE attempts SET status=?, finished_at=CURRENT_TIMESTAMP, duration_sec=?,
                   score=correct, cur_poll_id=NULL
            WHERE id=? AND status='active'
        """, (status, float(duration_sec), attempt_id))
        row = con.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    return _attempt_row(row)


def get_answers(attempt_id: int) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM answers WHERE attempt_id=? ORDER BY id", (attempt_id,)).fetchall()]


# ------------------------------------------------------------------ stats
def quiz_stats(qid: str) -> tuple[dict, list[dict]]:
    with connect() as con:
        row = con.execute("""
            SELECT COUNT(*) AS attempts,
                   COUNT(DISTINCT user_id) AS users,
                   COALESCE(SUM(correct),0) AS correct,
                   COALESCE(SUM(wrong),0) AS wrong,
                   COALESCE(SUM(skipped),0) AS skipped, COALESCE(SUM(timeouts),0) AS timeouts,
                   COALESCE(SUM(total),0) AS total_questions,
                   COALESCE(AVG(CASE WHEN total>0 THEN 100.0*correct/total END),0) AS avg_pct
            FROM attempts WHERE quiz_id=? AND status IN ('finished','stopped')
        """, (qid,)).fetchone()
        # best attempt per user
        leaders = con.execute("""
            SELECT a.user_id, u.username, u.first_name, a.correct, a.wrong, a.skipped,
                   a.total, a.duration_sec, a.finished_at, a.status,
                   COALESCE(a.negative_mark, 0) AS negative_mark,
                   (a.correct - a.wrong * COALESCE(a.negative_mark, 0)) AS net
            FROM attempts a LEFT JOIN users u ON u.user_id=a.user_id
            WHERE a.quiz_id=? AND a.status IN ('finished','stopped')
              AND a.id = (
                SELECT b.id FROM attempts b
                WHERE b.quiz_id=a.quiz_id AND b.user_id=a.user_id
                  AND b.status IN ('finished','stopped')
                ORDER BY (b.correct - b.wrong * COALESCE(b.negative_mark, 0)) DESC,
                         b.duration_sec ASC, b.id ASC LIMIT 1)
            -- net == correct when negative marking is off (the default), so the
            -- ranking is unchanged for every quiz that does not use it
            ORDER BY net DESC, a.duration_sec ASC
            LIMIT 10
        """, (qid,)).fetchall()
    return dict(row), [dict(x) for x in leaders]


def user_history(user_id: int, qid: Optional[str] = None, limit: int = 15) -> list[dict]:
    sql = """
        SELECT a.*, q.title FROM attempts a LEFT JOIN quizzes q ON q.id=a.quiz_id
        WHERE a.user_id=? AND a.status IN ('finished','stopped')
    """
    params: list[Any] = [user_id]
    if qid:
        sql += " AND a.quiz_id=?"
        params.append(qid)
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(limit)
    with connect() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def user_totals(user_id: int) -> dict:
    with connect() as con:
        row = con.execute("""
            SELECT COUNT(*) AS attempts, COALESCE(SUM(correct),0) AS correct,
                   COALESCE(SUM(wrong),0) AS wrong, COALESCE(SUM(skipped),0) AS skipped,
                   COALESCE(SUM(timeouts),0) AS timeouts,
                   COALESCE(SUM(total),0) AS total
            FROM attempts WHERE user_id=? AND status IN ('finished','stopped')
        """, (user_id,)).fetchone()
    return dict(row)


# ------------------------------------------------------------ user states
def set_state(user_id: int, state: str, data: Optional[dict] = None) -> None:
    with connect() as con:
        con.execute("""
            INSERT INTO user_states(user_id,state,data_json,updated_at)
            VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,
                data_json=excluded.data_json, updated_at=CURRENT_TIMESTAMP
        """, (user_id, state, json.dumps(data or {}, ensure_ascii=False)))


def get_state(user_id: int) -> tuple[Optional[str], dict]:
    with connect() as con:
        row = con.execute("SELECT state,data_json FROM user_states WHERE user_id=?",
                          (user_id,)).fetchone()
    if not row:
        return None, {}
    try:
        data = json.loads(row["data_json"] or "{}")
    except json.JSONDecodeError:
        data = {}
    return row["state"], data


def clear_state(user_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM user_states WHERE user_id=?", (user_id,))


# --------------------------------------------------------------- membership (join gate)
def get_membership(user_id: int) -> tuple[float, float]:
    """(verified_ts, checked_ts); verified_ts == 0 → never verified / left."""
    row = ensure_user_row(user_id)
    return float(row.get("member_verified_ts") or 0), float(row.get("member_checked_ts") or 0)


def set_member_verified(user_id: int, ts: float) -> None:
    ensure_user_row(user_id)
    with connect() as con:
        con.execute("""UPDATE users SET member_verified_ts=CASE WHEN member_verified_ts>0
                       THEN member_verified_ts ELSE ? END, member_checked_ts=? WHERE user_id=?""",
                    (ts, ts, user_id))


def clear_member(user_id: int) -> None:
    ensure_user_row(user_id)
    with connect() as con:
        con.execute("UPDATE users SET member_verified_ts=0, member_checked_ts=0 WHERE user_id=?", (user_id,))


# --------------------------------------------------------------- group sessions
def _gs_row(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    d["order"] = json.loads(d.get("order_json") or "[]")
    return d


def create_group_session(qid: str, chat_id: int, started_by: int, order: list[int], timer: int,
                         shuffle_options: bool, started_ts: float,
                         negative_mark: float = 0.0) -> Optional[int]:
    """New session for this group; None if the group already runs a quiz.

    ``negative_mark`` is a snapshot of the quiz setting at start time (0 = disabled,
    the default), so later edits to the quiz never change a finished session's score."""
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        busy = con.execute("SELECT id FROM group_sessions WHERE chat_id=? AND status='active'",
                           (chat_id,)).fetchone()
        if busy:
            return None
        cur = con.execute("""
            INSERT INTO group_sessions(quiz_id,chat_id,started_by,status,order_json,current_index,total,
                                       timer,shuffle_options,started_ts,negative_mark)
            VALUES(?,?,?,'active',?,0,?,?,?,?,?)
        """, (qid, chat_id, started_by, json.dumps(order), len(order), int(timer),
              int(bool(shuffle_options)), started_ts, max(0.0, float(negative_mark or 0.0))))
        return cur.lastrowid


def get_group_session(session_id: int) -> Optional[dict]:
    with connect() as con:
        return _gs_row(con.execute("SELECT * FROM group_sessions WHERE id=?", (session_id,)).fetchone())


def get_active_group_session(chat_id: int) -> Optional[dict]:
    with connect() as con:
        return _gs_row(con.execute("SELECT * FROM group_sessions WHERE chat_id=? AND status='active' "
                                   "ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone())


def list_active_group_sessions() -> list[dict]:
    with connect() as con:
        return [_gs_row(r) for r in con.execute("SELECT * FROM group_sessions WHERE status='active'")]


def set_group_current(session_id: int, poll_id: str, message_id: int, question_id: int, q_index: int,
                      perm: list[int], correct_display: int, sent_ts: float, post_explanation: str) -> None:
    with connect() as con:
        con.execute("""UPDATE group_sessions SET cur_poll_id=?, cur_message_id=?, cur_question_id=?,
                       cur_sent_ts=?, cur_post_explanation=?, asked=MAX(asked, ?) WHERE id=?""",
                    (poll_id, message_id, question_id, sent_ts, post_explanation or "", q_index + 1, session_id))
        con.execute("""INSERT OR REPLACE INTO group_polls(poll_id,session_id,question_id,q_index,perm_json,
                       correct_display,message_id,sent_ts) VALUES(?,?,?,?,?,?,?,?)""",
                    (poll_id, session_id, question_id, q_index, json.dumps(list(perm)), int(correct_display),
                     message_id, sent_ts))


def close_group_question(session_id: int, poll_id: Optional[str]) -> bool:
    """Close the current question and advance.  Atomic: returns False if the
    question was already closed (timer and /skip racing)."""
    with connect() as con:
        if poll_id is None:
            cur = con.execute("""UPDATE group_sessions SET current_index=current_index+1
                                 WHERE id=? AND status='active' AND cur_poll_id IS NULL""", (session_id,))
        else:
            cur = con.execute("""UPDATE group_sessions SET current_index=current_index+1, cur_poll_id=NULL,
                                 cur_message_id=NULL, cur_question_id=NULL, cur_post_explanation=''
                                 WHERE id=? AND status='active' AND cur_poll_id=?""", (session_id, poll_id))
        return cur.rowcount > 0


def get_group_poll(poll_id: str) -> Optional[dict]:
    with connect() as con:
        row = con.execute("""SELECT p.*, s.quiz_id, s.chat_id, s.status AS session_status, s.cur_poll_id
                             FROM group_polls p JOIN group_sessions s ON s.id=p.session_id
                             WHERE p.poll_id=?""", (poll_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["perm"] = json.loads(d["perm_json"])
    return d


def record_group_answer(session_id: int, quiz_id: str, chat_id: int, poll_id: str, question_id: int,
                        q_index: int, user_id: int, chosen_display: int, chosen_index: int,
                        is_correct: bool, elapsed: float, ts: float) -> bool:
    """False if this participant already answered this question (never overwritten)."""
    with connect() as con:
        cur = con.execute("""
            INSERT OR IGNORE INTO group_answers(session_id,quiz_id,chat_id,poll_id,question_id,q_index,user_id,
                                                chosen_display,chosen_index,is_correct,score,elapsed,answered_ts)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (session_id, quiz_id, chat_id, poll_id, question_id, q_index, user_id, chosen_display,
              chosen_index, int(bool(is_correct)), 1 if is_correct else 0, elapsed, ts))
        return cur.rowcount > 0


def finish_group_session(session_id: int, status: str, ts: float) -> Optional[dict]:
    """Mark finished/stopped exactly once; returns the session or None if already done."""
    with connect() as con:
        cur = con.execute("""UPDATE group_sessions SET status=?, finished_ts=?, cur_poll_id=NULL
                             WHERE id=? AND status='active'""", (status, ts, session_id))
        if cur.rowcount == 0:
            return None
        return _gs_row(con.execute("SELECT * FROM group_sessions WHERE id=?", (session_id,)).fetchone())


def group_leaderboard(session_id: int) -> list[dict]:
    """Per-participant totals from the stored answers (best first)."""
    with connect() as con:
        rows = con.execute("""
            SELECT a.user_id, u.username, u.first_name,
                   SUM(a.is_correct) AS correct, SUM(1 - a.is_correct) AS wrong, SUM(a.score) AS score,
                   COUNT(*) AS answered, COALESCE(SUM(a.elapsed), 0) AS elapsed
            FROM group_answers a LEFT JOIN users u ON u.user_id=a.user_id
            WHERE a.session_id=?
            GROUP BY a.user_id
            ORDER BY correct DESC, elapsed ASC, a.user_id ASC
        """, (session_id,)).fetchall()
    return [dict(r) for r in rows]


def get_group_answers(session_id: int) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM group_answers WHERE session_id=? ORDER BY q_index, user_id", (session_id,))]


def latest_answered_attempt(user_id: int, quiz_id: Optional[str] = None) -> Optional[dict]:
    """Most recent finished/stopped private attempt for ``user_id`` (optionally
    for ``quiz_id``) that actually has recorded user answers."""
    with connect() as con:
        if quiz_id:
            row = con.execute("""
                SELECT a.* FROM attempts a
                WHERE a.user_id=? AND a.quiz_id=? AND a.status IN ('finished','stopped')
                  AND (a.correct + a.wrong > 0
                       OR EXISTS (SELECT 1 FROM answers ans
                                  WHERE ans.attempt_id=a.id AND ans.chosen_index IS NOT NULL))
                ORDER BY a.id DESC LIMIT 1
            """, (int(user_id), str(quiz_id))).fetchone()
        else:
            row = con.execute("""
                SELECT a.* FROM attempts a
                WHERE a.user_id=? AND a.status IN ('finished','stopped')
                  AND (a.correct + a.wrong > 0
                       OR EXISTS (SELECT 1 FROM answers ans
                                  WHERE ans.attempt_id=a.id AND ans.chosen_index IS NOT NULL))
                ORDER BY a.id DESC LIMIT 1
            """, (int(user_id),)).fetchone()
    return _attempt_row(row)


def latest_answered_group_session(user_id: int, quiz_id: Optional[str] = None) -> Optional[dict]:
    """Most recent group session where ``user_id`` has recorded answers in ``group_answers``."""
    with connect() as con:
        if quiz_id:
            row = con.execute("""
                SELECT s.* FROM group_sessions s
                WHERE s.quiz_id=? AND EXISTS (
                    SELECT 1 FROM group_answers ga
                    WHERE ga.session_id=s.id AND ga.user_id=?
                )
                ORDER BY CASE WHEN s.status IN ('finished','stopped') THEN 0 ELSE 1 END, s.id DESC
                LIMIT 1
            """, (str(quiz_id), int(user_id))).fetchone()
        else:
            row = con.execute("""
                SELECT s.* FROM group_sessions s
                WHERE s.status IN ('finished','stopped') AND EXISTS (
                    SELECT 1 FROM group_answers ga
                    WHERE ga.session_id=s.id AND ga.user_id=?
                )
                ORDER BY s.id DESC LIMIT 1
            """, (int(user_id),)).fetchone()
    return _gs_row(row)


def user_group_history(user_id: int, limit: int = 10) -> list[dict]:
    """Finished/stopped group sessions where ``user_id`` participated or started."""
    with connect() as con:
        rows = con.execute("""
            SELECT s.*, q.title AS quiz_title,
                   (SELECT COUNT(*) FROM group_answers ga
                    WHERE ga.session_id=s.id AND ga.user_id=?) AS user_answered
            FROM group_sessions s
            LEFT JOIN quizzes q ON q.id=s.quiz_id
            WHERE s.status IN ('finished','stopped')
              AND (s.started_by=? OR EXISTS (
                  SELECT 1 FROM group_answers ga WHERE ga.session_id=s.id AND ga.user_id=?
              ))
            ORDER BY CASE WHEN user_answered > 0 THEN 0 ELSE 1 END, s.id DESC
            LIMIT ?
        """, (int(user_id), int(user_id), int(user_id), int(limit))).fetchall()
    return [_gs_row(r) for r in rows if r]


def group_quiz_stats(qid: str) -> dict:
    with connect() as con:
        row = con.execute("""
            SELECT COUNT(DISTINCT s.id) AS sessions, COUNT(DISTINCT s.chat_id) AS groups,
                   COUNT(DISTINCT a.user_id) AS participants
            FROM group_sessions s LEFT JOIN group_answers a ON a.session_id=s.id
            WHERE s.quiz_id=? AND s.status IN ('finished','stopped')
        """, (qid,)).fetchone()
    return dict(row)


# --------------------------------------------------------------- review PDFs
def review_pdf_get(*, attempt_id: Optional[int] = None, session_id: Optional[int] = None,
                   user_id: Optional[int] = None) -> Optional[dict]:
    """The delivery record for a private attempt or a group participant."""
    with connect() as con:
        if attempt_id is not None:
            row = con.execute("SELECT * FROM review_pdfs WHERE attempt_id=?", (int(attempt_id),)).fetchone()
        elif session_id is not None and user_id is not None:
            row = con.execute("SELECT * FROM review_pdfs WHERE session_id=? AND user_id=?",
                              (int(session_id), int(user_id))).fetchone()
        else:
            row = None
    return dict(row) if row else None


def review_pdf_save(*, status: str, attempt_id: Optional[int] = None, session_id: Optional[int] = None,
                    user_id: int, chat_id: Optional[int] = None, quiz_id: str = "",
                    bytes_: int = 0, pages: int = 0, message_id: Optional[int] = None,
                    error: str = "", bump: bool = False) -> dict:
    """Insert/update the delivery record (never raises for a missing row)."""
    now = time.time()
    with connect() as con:
        if attempt_id is not None:
            row = con.execute("SELECT * FROM review_pdfs WHERE attempt_id=?", (int(attempt_id),)).fetchone()
        elif session_id is not None:
            row = con.execute("SELECT * FROM review_pdfs WHERE session_id=? AND user_id=?",
                              (int(session_id), int(user_id))).fetchone()
        else:
            row = None
        if row is None:
            cur = con.execute("""
                INSERT INTO review_pdfs(attempt_id,session_id,user_id,chat_id,quiz_id,status,tries,
                                        bytes,pages,message_id,error,created_ts,sent_ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (attempt_id, session_id, int(user_id), chat_id, quiz_id or "", status,
                  1 if bump else 0, int(bytes_ or 0), int(pages or 0), message_id, error or "",
                  now, now if status == "sent" else None))
            rid = cur.lastrowid
        else:
            rid = row["id"]
            con.execute("""
                UPDATE review_pdfs SET status=?, chat_id=COALESCE(?,chat_id),
                       quiz_id=CASE WHEN ?<>'' THEN ? ELSE quiz_id END,
                       bytes=CASE WHEN ?>0 THEN ? ELSE bytes END,
                       pages=CASE WHEN ?>0 THEN ? ELSE pages END,
                       message_id=COALESCE(?,message_id), error=?,
                       tries=tries+?, sent_ts=CASE WHEN ?='sent' THEN ? ELSE sent_ts END
                WHERE id=?
            """, (status, chat_id, quiz_id or "", quiz_id or "", int(bytes_ or 0), int(bytes_ or 0),
                  int(pages or 0), int(pages or 0), message_id, error or "", 1 if bump else 0,
                  status, now, rid))
        row = con.execute("SELECT * FROM review_pdfs WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else {}


# ------------------------------------ expansion: attempts metadata & sections
def get_latest_completed_attempt(user_id: int, quiz_id: Optional[str] = None) -> Optional[dict]:
    with connect() as con:
        if quiz_id:
            row = con.execute("""
                SELECT * FROM attempts
                WHERE user_id=? AND quiz_id=? AND status IN ('finished','stopped')
                ORDER BY id DESC LIMIT 1
            """, (int(user_id), str(quiz_id))).fetchone()
        else:
            row = con.execute("""
                SELECT * FROM attempts
                WHERE user_id=? AND status IN ('finished','stopped')
                ORDER BY id DESC LIMIT 1
            """, (int(user_id),)).fetchone()
    return dict(row) if row else None


def get_attempt_answers(attempt_id: int) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM answers WHERE attempt_id=? ORDER BY position, id",
                           (int(attempt_id),)).fetchall()
    return [dict(r) for r in rows]


def update_attempt_metadata(attempt_id: int, *, quiz_mode: Optional[str] = None,
                            section_scores: Optional[dict] = None,
                            negative_mark: Optional[float] = None,
                            net_score: Optional[float] = None) -> None:
    sets = []
    vals: list = []
    if quiz_mode is not None:
        sets.append("quiz_mode=?")
        vals.append(str(quiz_mode))
    if section_scores is not None:
        sets.append("section_scores_json=?")
        vals.append(json.dumps(section_scores, ensure_ascii=False))
    if negative_mark is not None:
        sets.append("negative_mark=?")
        vals.append(float(negative_mark))
    if net_score is not None:
        sets.append("net_score=?")
        vals.append(float(net_score))
    if not sets:
        return
    vals.append(int(attempt_id))
    with connect() as con:
        con.execute(f"UPDATE attempts SET {', '.join(sets)} WHERE id=?", tuple(vals))


# ------------------------------------ expansion: batches & authorized chats
def create_batch(name: str, description: str = "", created_by: int = 0) -> dict:
    bname = (name or "").strip()
    if not bname:
        raise ValueError("Batch name is empty")
    now_ts = time.time()
    with connect() as con:
        con.execute("""
            INSERT INTO batches(name, description, created_by, created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET description=excluded.description
        """, (bname, description or "", int(created_by or 0), now_ts))
        row = con.execute("SELECT * FROM batches WHERE name=?", (bname,)).fetchone()
    return dict(row) if row else {}


def delete_batch(name: str) -> bool:
    bname = (name or "").strip()
    with connect() as con:
        cur = con.execute("DELETE FROM batches WHERE name=?", (bname,))
        con.execute("DELETE FROM batch_members WHERE batch_name=?", (bname,))
    return cur.rowcount > 0


def list_batches() -> list[dict]:
    with connect() as con:
        rows = con.execute("""
            SELECT b.*, (SELECT COUNT(*) FROM batch_members m WHERE m.batch_name=b.name) AS member_count
              FROM batches b ORDER BY b.name
        """).fetchall()
    return [dict(r) for r in rows]


def add_user_to_batch(batch_name: str, user_id: int, added_by: int = 0) -> None:
    bname = (batch_name or "").strip()
    if not bname:
        raise ValueError("Batch name is empty")
    uid = int(user_id)
    now_ts = time.time()
    ensure_user_row(uid)
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO batches(name, created_by, created_at) VALUES(?,?,?)",
                    (bname, int(added_by or 0), now_ts))
        con.execute("""
            INSERT OR REPLACE INTO batch_members(batch_name, user_id, added_by, created_at)
            VALUES(?,?,?,?)
        """, (bname, uid, int(added_by or 0), now_ts))
        mrows = con.execute("SELECT batch_name FROM batch_members WHERE user_id=? ORDER BY batch_name",
                            (uid,)).fetchall()
        con.execute("UPDATE users SET batches_json=? WHERE user_id=?",
                    (json.dumps([r["batch_name"] for r in mrows], ensure_ascii=False), uid))


def remove_user_from_batch(batch_name: str, user_id: int) -> bool:
    bname = (batch_name or "").strip()
    uid = int(user_id)
    with connect() as con:
        cur = con.execute("DELETE FROM batch_members WHERE batch_name=? AND user_id=?", (bname, uid))
        mrows = con.execute("SELECT batch_name FROM batch_members WHERE user_id=? ORDER BY batch_name",
                            (uid,)).fetchall()
        con.execute("UPDATE users SET batches_json=? WHERE user_id=?",
                    (json.dumps([r["batch_name"] for r in mrows], ensure_ascii=False), uid))
    return cur.rowcount > 0


def get_user_batches(user_id: int) -> list[str]:
    if not user_id:
        return []
    with connect() as con:
        rows = con.execute("SELECT batch_name FROM batch_members WHERE user_id=? ORDER BY batch_name",
                           (int(user_id),)).fetchall()
    return [r["batch_name"] for r in rows]


def user_in_batch(user_id: int, batch_name: str) -> bool:
    bname = (batch_name or "").strip()
    if not bname or not user_id:
        return False
    with connect() as con:
        row = con.execute(
            "SELECT 1 FROM batch_members WHERE LOWER(batch_name)=LOWER(?) AND user_id=?",
            (bname, int(user_id)),
        ).fetchone()
    return bool(row)


def list_batch_members(batch_name: str) -> list[int]:
    bname = (batch_name or "").strip()
    with connect() as con:
        rows = con.execute("SELECT user_id FROM batch_members WHERE LOWER(batch_name)=LOWER(?) ORDER BY user_id",
                           (bname,)).fetchall()
    return [int(r["user_id"]) for r in rows]


def add_authorized_chat(chat_id: int, title: str = "", batch_name: str = "", added_by: int = 0) -> dict:
    cid = int(chat_id)
    now_ts = time.time()
    with connect() as con:
        con.execute("""
            INSERT INTO authorized_chats(chat_id, title, batch_name, added_by, created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE
               SET title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE authorized_chats.title END,
                   batch_name=excluded.batch_name
        """, (cid, title or "", (batch_name or "").strip(), int(added_by or 0), now_ts))
        row = con.execute("SELECT * FROM authorized_chats WHERE chat_id=?", (cid,)).fetchone()
    return dict(row) if row else {}


def remove_authorized_chat(chat_id: int) -> bool:
    with connect() as con:
        cur = con.execute("DELETE FROM authorized_chats WHERE chat_id=?", (int(chat_id),))
    return cur.rowcount > 0


def get_authorized_chat(chat_id: int) -> Optional[dict]:
    if not chat_id:
        return None
    with connect() as con:
        row = con.execute("SELECT * FROM authorized_chats WHERE chat_id=?", (int(chat_id),)).fetchone()
    return dict(row) if row else None


def list_authorized_chats() -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM authorized_chats ORDER BY chat_id").fetchall()
    return [dict(r) for r in rows]


def chat_has_batch(chat_id: int, batch_name: str) -> bool:
    bname = (batch_name or "").strip()
    if not chat_id or not bname:
        return False
    row = get_authorized_chat(int(chat_id))
    if not row:
        return False
    return (row.get("batch_name") or "").strip().lower() == bname.lower()


def list_all_user_ids() -> list[int]:
    with connect() as con:
        rows = con.execute("SELECT user_id FROM users WHERE COALESCE(is_banned, 0)=0 ORDER BY user_id").fetchall()
    return [int(r["user_id"]) for r in rows]

