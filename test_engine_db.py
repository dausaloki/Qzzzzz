"""quiz_engine + database unit tests."""
import random
import sqlite3

import pytest

import config
import database as db
import quiz_engine as E


# ------------------------------------------------------------ shuffle mapping
def test_shuffle_mapping_always_correct():
    rng = random.Random(7)
    q = {"question": "Q?", "options": ["w", "x", "y", "z"], "correct_index": 2}
    seen_positions = set()
    for _ in range(500):
        perm = E.make_permutation(4, True, rng)
        assert sorted(perm) == [0, 1, 2, 3]
        p = E.build_poll_payload(q, perm, 0, 1)
        # the option displayed at correct_option_id is the original correct option
        assert p.options[p.correct_option_id] == "y"
        assert E.original_choice(perm, p.correct_option_id) == 2
        seen_positions.add(p.correct_option_id)
    assert seen_positions == {0, 1, 2, 3}  # shuffle really moves the answer


def test_no_shuffle_keeps_order():
    assert E.make_permutation(4, False) == [0, 1, 2, 3]
    assert E.build_order([5, 6, 7], False) == [5, 6, 7]
    order = E.build_order(list(range(50)), True, random.Random(1))
    assert sorted(order) == list(range(50)) and order != list(range(50))


def test_legacy_poll_data():
    opts, c = E.poll_data({"options": ["a", "b", "c", "d"], "correct_index": 3}, True, random.Random(3))
    assert opts[c] == "d"


# ------------------------------------------------------------ long handling
LONG_Q = "यह प्रश्न बहुत लंबा है और इसमें कई कथन हैं। " * 20
LONG_O = "यह विकल्प एक सौ अक्षरों से कहीं अधिक लंबा है ताकि Telegram की सीमा पार हो जाए — " * 2


def test_short_question_native_poll():
    q = {"question": "राजधानी?", "options": ["a", "b", "c", "d"], "correct_index": 1, "explanation": "क्योंकि"}
    p = E.build_poll_payload(q, [0, 1, 2, 3], 2, 10)
    assert p.question == "[3/10] राजधानी?" and p.options == ["a", "b", "c", "d"]
    assert p.full_text_chunks == [] and not p.compact and p.explanation == "क्योंकि"


def test_long_question_sends_full_text_and_keeps_real_options():
    q = {"question": LONG_Q, "options": ["a", "b", "c", "d"], "correct_index": 0}
    p = E.build_poll_payload(q, [3, 2, 1, 0], 0, 5)
    full = "".join(p.full_text_chunks)
    assert LONG_Q.strip() in full                         # complete wording
    assert "(A) d" in full and "(D) a" in full           # displayed (shuffled) order
    assert len(p.question) <= 300 and p.options == ["d", "c", "b", "a"] and not p.compact
    assert p.options[p.correct_option_id] == "a"


def test_long_option_uses_compact_labels_with_correct_mapping():
    q = {"question": "कौन सा सही है?", "options": ["छोटा", LONG_O, "तीसरा", "चौथा"], "correct_index": 1}
    for perm in ([0, 1, 2, 3], [1, 0, 3, 2], [2, 3, 1, 0]):
        p = E.build_poll_payload(q, perm, 0, 1)
        assert p.compact and p.options == ["(A)", "(B)", "(C)", "(D)"]
        full = "".join(p.full_text_chunks)
        label = "ABCD"[p.correct_option_id]
        assert f"({label}) {LONG_O.strip()}" in full        # full option under the right label
        assert all(len(o) <= 100 for o in p.options)


def test_long_explanation_goes_to_followup_message():
    q = {"question": "Q", "options": ["a", "b", "c", "d"], "correct_index": 0, "explanation": "x" * 500}
    p = E.build_poll_payload(q, [0, 1, 2, 3], 0, 1)
    assert p.explanation is None and p.post_explanation == "x" * 500
    q["explanation"] = "line1\nline2\nline3\nline4"
    assert E.build_poll_payload(q, [0, 1, 2, 3], 0, 1).explanation is None


def test_duplicate_options_fallback_to_labels():
    q = {"question": "Q", "options": ["same", "same", "c", "d"], "correct_index": 2}
    p = E.build_poll_payload(q, [0, 1, 2, 3], 0, 1)
    assert p.compact


def test_split_message_is_lossless():
    rng = random.Random(0)
    for n in (0, 10, 3999, 4000, 4001, 12000, 25000):
        text = "".join(rng.choice("अआइ abc\n ") for _ in range(n))
        chunks = E.split_message(text)
        assert "".join(chunks) == text
        assert all(len(c) <= config.SAFE_MESSAGE_CHUNK for c in chunks)
    nospace = "क" * 9000
    assert "".join(E.split_message(nospace)) == nospace


# ------------------------------------------------------------ manual options
def test_parse_manual_options():
    assert E.parse_manual_options("a\nb\nc\nd") == ["a", "b", "c", "d"]
    assert E.parse_manual_options("(A) x\n(B) y\n(C) z\n(D) w") == ["x", "y", "z", "w"]
    assert E.parse_manual_options("A) x\nB) y\nC) z\nD) w") == ["x", "y", "z", "w"]
    # matching codes must not lose their "A-" prefix
    codes = "A-III, B-II, C-IV, D-I\nA-II, B-III, C-I, D-IV\nA-IV, B-I, C-II, D-III\nA-I, B-IV, C-III, D-II"
    assert E.parse_manual_options(codes)[0] == "A-III, B-II, C-IV, D-I"
    # multi-line options with labels
    assert E.parse_manual_options("(A) long\ncontinued\n(B) y\n(C) z\n(D) w")[0] == "long continued"
    assert E.parse_manual_options("a\nb\nc") == ["a", "b", "c"]          # 3 options are valid now
    with pytest.raises(ValueError):
        E.parse_manual_options("only one line")
    with pytest.raises(ValueError):
        E.parse_manual_options("")


def test_results_and_helpers():
    r = E.compute_result(10, 6, 2, 1, 75)
    assert (r.score, r.percentage, r.unanswered) == (6, 60.0, 1)
    assert E.format_duration(75) == "1m 15s" and E.format_duration(3725) == "1h 2m 5s"
    assert E.deep_link("MyBot", "abc123def0") == "https://t.me/MyBot?start=quiz_abc123def0"
    assert E.parse_start_payload("quiz_abc123def0") == "abc123def0"
    assert E.parse_start_payload("quiz_../../x") is None and E.parse_start_payload("hello") is None


# ------------------------------------------------------------ database
@pytest.fixture()
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "t.db")
    db.init_db()
    yield
    db.set_db_path(config.DB_PATH)


def test_database_tables_and_crud(fresh_db):
    with db.connect() as con:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "quizzes", "questions", "attempts", "answers", "quiz_settings", "user_states"} <= names
    db.update_user_settings(1, default_timer=60, default_shuffle_options=1)
    qid = db.new_quiz(1, "T", "D")
    quiz = db.get_quiz(qid)
    assert quiz["timer"] == 60 and quiz["shuffle_options"] == 1 and quiz["shuffle_questions"] == 0
    qsid = db.add_question(qid, "Q\nline2", ["a", "b", "c", "d"], 3, "e")
    assert db.get_question(qid, qsid)["options"] == ["a", "b", "c", "d"]
    with pytest.raises(ValueError):
        db.add_question(qid, "Q", ["a"], 0)
    with pytest.raises(ValueError):
        db.add_question(qid, "Q", ["a", "b", "c", "d"], 4)
    for i in range(99):
        db.add_question(qid, f"Q{i}", ["a", "b", "c", "d"], 0)
    with pytest.raises(ValueError, match="100"):
        db.add_question(qid, "Q101", ["a", "b", "c", "d"], 0)
    db.update_quiz(qid, title="New", timer=0, shuffle_questions=1)
    q2 = db.get_quiz(qid)
    assert (q2["title"], q2["timer"], q2["shuffle_questions"]) == ("New", 0, 1)
    assert db.delete_question(qsid, qid) and db.count_questions(qid) == 99
    assert [q["position"] for q in db.get_questions(qid)] == list(range(1, 100))
    db.set_state(1, "c_q", {"x": "हिंदी"})
    assert db.get_state(1) == ("c_q", {"x": "हिंदी"})
    db.clear_state(1)
    assert db.get_state(1) == (None, {})
    assert not db.delete_quiz(qid, owner_id=2)       # not owner
    assert db.delete_quiz(qid, owner_id=1) and db.get_quiz(qid) is None
    assert db.get_questions(qid) == []


def test_attempt_lifecycle(fresh_db):
    qid = db.new_quiz(1, "T")
    ids = [db.add_question(qid, f"Q{i}", ["a", "b", "c", "d"], 0) for i in range(3)]
    aid = db.create_attempt(qid, 5, 5, ids, 30, True, 1000.0)
    db.set_current_poll(aid, "p1", 10, ids[0], [1, 0, 2, 3], 1, 1000.0)
    assert db.get_attempt_by_poll("p1")["id"] == aid
    db.record_answer(aid, ids[0], 0, 0, True, "correct", 2.5)
    a = db.get_attempt(aid)
    assert (a["correct"], a["current_index"], a["cur_poll_id"]) == (1, 1, None)
    db.record_answer(aid, ids[1], 1, 2, False, "wrong", 1)
    db.record_answer(aid, ids[2], 2, None, False, "skipped", 30)
    done = db.finish_attempt(aid, "finished", 40)
    assert (done["status"], done["correct"], done["wrong"], done["skipped"], done["score"]) == ("finished", 1, 1, 1, 1)
    s, leaders = db.quiz_stats(qid)
    assert s["attempts"] == 1 and leaders[0]["user_id"] == 5
    assert db.user_history(5)[0]["title"] == "T"


def test_migration_from_old_schema(tmp_path):
    """A quiz.db created by the previous bot version must keep working."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE quizzes (id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, title TEXT NOT NULL,
        description TEXT DEFAULT '', pre_text TEXT DEFAULT '', timer INTEGER DEFAULT 30,
        shuffle_questions INTEGER DEFAULT 0, shuffle_options INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE questions (id INTEGER PRIMARY KEY AUTOINCREMENT, quiz_id TEXT NOT NULL,
        position INTEGER NOT NULL, question TEXT NOT NULL, options_json TEXT NOT NULL,
        correct_index INTEGER NOT NULL, explanation TEXT DEFAULT '');
    CREATE TABLE attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, quiz_id TEXT NOT NULL,
        user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT, score INTEGER DEFAULT 0, total INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,
        wrong INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0);
    CREATE TABLE answers (id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL, chosen_index INTEGER, correct INTEGER DEFAULT 0, elapsed REAL DEFAULT 0);
    INSERT INTO users(user_id, username, first_name) VALUES (7, 'old', 'Old');
    INSERT INTO quizzes(id, owner_id, title, timer, shuffle_options) VALUES ('oldquiz001', 7, 'Old Quiz', 60, 1);
    INSERT INTO questions(quiz_id, position, question, options_json, correct_index)
        VALUES ('oldquiz001', 1, 'पुराना प्रश्न', '["a","b","c","d"]', 2);
    INSERT INTO attempts(quiz_id, user_id, chat_id, finished_at, total, correct) VALUES ('oldquiz001', 7, 7, '2025-01-01', 1, 1);
    INSERT INTO attempts(quiz_id, user_id, chat_id, total) VALUES ('oldquiz001', 7, 7, 1);
    """)
    con.commit()
    con.close()
    db.set_db_path(path)
    try:
        db.init_db()
        db.init_db()  # idempotent
        quiz = db.get_quiz("oldquiz001")
        assert quiz["title"] == "Old Quiz" and quiz["timer"] == 60 and quiz["shuffle_options"] == 1
        assert db.get_questions("oldquiz001")[0]["question"] == "पुराना प्रश्न"
        assert db.list_active_attempts() == []           # old unfinished attempt expired
        s, _ = db.quiz_stats("oldquiz001")
        assert s["attempts"] == 1
    finally:
        db.set_db_path(config.DB_PATH)
