import sqlite3
import json
import uuid
from pathlib import Path

DB_PATH = Path(__file__).with_name("quiz.db")

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS quizzes (
            id TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            pre_text TEXT DEFAULT '',
            timer INTEGER DEFAULT 30,
            shuffle_questions INTEGER DEFAULT 0,
            shuffle_options INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(owner_id) REFERENCES users(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            question TEXT NOT NULL,
            options_json TEXT NOT NULL,
            correct_index INTEGER NOT NULL,
            explanation TEXT DEFAULT '',
            FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            score INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            wrong INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            chosen_index INTEGER,
            correct INTEGER DEFAULT 0,
            elapsed REAL DEFAULT 0,
            FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
            FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_questions_quiz ON questions(quiz_id);
        CREATE INDEX IF NOT EXISTS idx_attempts_quiz ON attempts(quiz_id);
        """)
        con.commit()

def upsert_user(user):
    if not user:
        return
    with connect() as con:
        con.execute("""
            INSERT INTO users(user_id, username, first_name)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name
        """, (user.id, user.username or "", user.first_name or ""))
        con.commit()

def new_quiz(owner_id, title, description=""):
    qid = uuid.uuid4().hex[:10]
    with connect() as con:
        con.execute(
            "INSERT INTO quizzes(id,owner_id,title,description) VALUES(?,?,?,?)",
            (qid, owner_id, title, description)
        )
        con.commit()
    return qid

def get_quiz(qid):
    with connect() as con:
        return con.execute("SELECT * FROM quizzes WHERE id=?", (qid,)).fetchone()

def get_owner_quizzes(owner_id):
    with connect() as con:
        return con.execute(
            "SELECT * FROM quizzes WHERE owner_id=? ORDER BY updated_at DESC",
            (owner_id,)
        ).fetchall()

def update_quiz(qid, **fields):
    allowed = {
        "title", "description", "pre_text", "timer",
        "shuffle_questions", "shuffle_options"
    }
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = "CURRENT_TIMESTAMP"
    assignments = []
    values = []
    for k, v in fields.items():
        if v == "CURRENT_TIMESTAMP":
            assignments.append(f"{k}=CURRENT_TIMESTAMP")
        else:
            assignments.append(f"{k}=?")
            values.append(v)
    values.append(qid)
    with connect() as con:
        con.execute(
            f"UPDATE quizzes SET {', '.join(assignments)} WHERE id=?",
            values
        )
        con.commit()

def delete_quiz(qid, owner_id=None):
    with connect() as con:
        if owner_id is None:
            con.execute("DELETE FROM quizzes WHERE id=?", (qid,))
        else:
            con.execute("DELETE FROM quizzes WHERE id=? AND owner_id=?", (qid, owner_id))
        con.commit()

def add_question(qid, question, options, correct_index, explanation=""):
    with connect() as con:
        pos = con.execute(
            "SELECT COALESCE(MAX(position),0)+1 FROM questions WHERE quiz_id=?",
            (qid,)
        ).fetchone()[0]
        con.execute("""
            INSERT INTO questions
            (quiz_id,position,question,options_json,correct_index,explanation)
            VALUES(?,?,?,?,?,?)
        """, (qid, pos, question, json.dumps(options, ensure_ascii=False),
              correct_index, explanation))
        con.execute("UPDATE quizzes SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (qid,))
        con.commit()

def get_questions(qid):
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM questions WHERE quiz_id=? ORDER BY position,id", (qid,)
        ).fetchall()
    return [dict(r) | {"options": json.loads(r["options_json"])} for r in rows]

def get_question(qid, question_id):
    with connect() as con:
        row = con.execute(
            "SELECT * FROM questions WHERE quiz_id=? AND id=?", (qid, question_id)
        ).fetchone()
    if not row:
        return None
    return dict(row) | {"options": json.loads(row["options_json"])}

def delete_question(question_id, qid):
    with connect() as con:
        con.execute("DELETE FROM questions WHERE id=? AND quiz_id=?", (question_id, qid))
        rows = con.execute(
            "SELECT id FROM questions WHERE quiz_id=? ORDER BY position,id", (qid,)
        ).fetchall()
        for i, row in enumerate(rows, 1):
            con.execute("UPDATE questions SET position=? WHERE id=?", (i, row["id"]))
        con.commit()

def create_attempt(qid, user_id, chat_id, total):
    with connect() as con:
        cur = con.execute(
            "INSERT INTO attempts(quiz_id,user_id,chat_id,total) VALUES(?,?,?,?)",
            (qid,user_id,chat_id,total)
        )
        con.commit()
        return cur.lastrowid

def save_answer(attempt_id, question_id, chosen_index, correct, elapsed):
    with connect() as con:
        con.execute("""
            INSERT INTO answers(attempt_id,question_id,chosen_index,correct,elapsed)
            VALUES(?,?,?,?,?)
        """, (attempt_id, question_id, chosen_index, int(correct), elapsed))
        con.commit()

def finish_attempt(attempt_id, score, correct, wrong, skipped):
    with connect() as con:
        con.execute("""
            UPDATE attempts
            SET finished_at=CURRENT_TIMESTAMP, score=?, correct=?, wrong=?, skipped=?
            WHERE id=?
        """, (score,correct,wrong,skipped,attempt_id))
        con.commit()

def quiz_stats(qid):
    with connect() as con:
        row = con.execute("""
            SELECT COUNT(*) attempts,
                   COALESCE(SUM(correct),0) correct,
                   COALESCE(SUM(wrong),0) wrong,
                   COALESCE(SUM(skipped),0) skipped
            FROM attempts WHERE quiz_id=? AND finished_at IS NOT NULL
        """, (qid,)).fetchone()
        leaders = con.execute("""
            SELECT a.user_id, u.username, u.first_name, a.score, a.correct,
                   a.wrong, a.skipped, a.finished_at
            FROM attempts a
            LEFT JOIN users u ON u.user_id=a.user_id
            WHERE a.quiz_id=? AND a.finished_at IS NOT NULL
            ORDER BY a.correct DESC, a.score DESC, a.finished_at ASC
            LIMIT 10
        """, (qid,)).fetchall()
    return dict(row), [dict(x) for x in leaders]
