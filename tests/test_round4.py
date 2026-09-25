"""Round 4: conversational creation (QuizBot-style), native poll capture,
/undo, ready screen, rank, inline sharing, question editing and migration."""
import asyncio
import sqlite3

import pytest

import config
import database as db
import quiz_creator as creator
import quiz_engine as engine
from test_bot_flow import Harness, buttons, make_quiz, run


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "quiz.db")
    db.init_db()
    monkeypatch.setattr(config, "NEXT_QUESTION_DELAY", 0.05)
    monkeypatch.setattr(config, "TIMER_GRACE", 0.2)
    yield
    db.set_db_path(config.DB_PATH)


def qs_of(owner):
    quizzes = db.get_owner_quizzes(owner)
    return quizzes[0]["id"], db.get_questions(quizzes[0]["id"]) if quizzes else []


# ---------------------------------------------------------------- typed parser
def test_parse_typed_question_formats():
    t = engine.parse_typed_question("Q?\n(A) x\n(B) y\n(C) z\nउत्तर: (C)\nव्याख्या: क्योंकि")
    assert (t.question, t.options, t.correct, t.explanation) == ("Q?", ["x", "y", "z"], 2, "क्योंकि")
    t = engine.parse_typed_question("Q?\n(A) x\n(B) y")
    assert t.correct is None and t.explanation == ""
    # the answer line is never glued onto the last option; extra tail lines become explanation
    t = engine.parse_typed_question("Q?\n(A) x\n(B) y\nAnswer: B\nपहली line\nदूसरी line")
    assert t.options == ["x", "y"] and t.correct == 1 and t.explanation == "पहली line\nदूसरी line"
    # 12 options
    opts = "\n".join(f"({config.OPTION_LABELS[i]}) विकल्प {i}" for i in range(12))
    t = engine.parse_typed_question("बारह?\n" + opts + "\nउत्तर: L")
    assert len(t.options) == 12 and t.correct == 11
    with pytest.raises(engine.NoOptionsError):
        engine.parse_typed_question("सिर्फ़ एक सूचना वाला text")
    with pytest.raises(ValueError):
        engine.parse_typed_question("Q?\n(A) x\n(B) y\nउत्तर: D")
    with pytest.raises(ValueError) as ei:
        engine.parse_typed_question("Q?\n(A) अकेला")
    assert not isinstance(ei.value, engine.NoOptionsError)


def test_parse_typed_question_assertion_reason_ordering_and_list():
    ar = ("अभिकथन (A): सूर्य पूर्व में उगता है।\nकारण (R): पृथ्वी पश्चिम से पूर्व घूमती है।\n"
          "(A) A और R दोनों सही हैं तथा R, A की सही व्याख्या है\n(B) A और R दोनों सही हैं परन्तु R, A की "
          "सही व्याख्या नहीं है\n(C) A सही है परन्तु R गलत है\n(D) A गलत है परन्तु R सही है\nउत्तर: A")
    t = engine.parse_typed_question(ar)
    assert t.question.startswith("अभिकथन (A)") and "कारण (R)" in t.question and t.correct == 0
    assert engine.detect_qtype(t.question) == "assertion_reason"
    lists = ("सूची-I को सूची-II से सुमेलित कीजिए\nसूची-I\n(A) घूमर\n(B) गैर\n(C) चरी\n(D) तेरहताली\n"
             "सूची-II\n1. मारवाड़\n2. किशनगढ़\n3. कामड़ समुदाय\n4. मेवाड़\nकूट:\n"
             "(A) A-1, B-4, C-2, D-3\n(B) A-4, B-1, C-3, D-2\n(C) A-2, B-3, C-4, D-1\n(D) A-3, B-2, C-1, D-4")
    t = engine.parse_typed_question(lists)
    assert t.options[0] == "A-1, B-4, C-2, D-3" and "(D) तेरहताली" in t.question and t.correct is None
    order = ("सही क्रम चुनिए\n1. अजमेर\n2. जयपुर\n3. कोटा\n(A) 1-2-3\n(B) 3-2-1\n(C) 2-1-3\n(D) 1-3-2\nउत्तर: C")
    t = engine.parse_typed_question(order)
    assert t.options == ["1-2-3", "3-2-1", "2-1-3", "1-3-2"] and "3. कोटा" in t.question


# ---------------------------------------------------------------- database
def test_move_question_and_atomic_options_update():
    qid = make_quiz(n=3)
    a, b, c = [q["id"] for q in db.get_questions(qid)]
    assert db.move_question(qid, c, -1) and [q["id"] for q in db.get_questions(qid)] == [a, c, b]
    assert not db.move_question(qid, a, -1)
    assert not db.move_question("nope", a, 1)
    assert [q["position"] for q in db.get_questions(qid)] == [1, 2, 3]
    # options + correct answer validated together: an invalid combination changes nothing
    with pytest.raises(ValueError):
        db.update_question(a, options=["x", "y"], correct_index=3)
    with pytest.raises(ValueError):
        db.update_question(a, options=["only"], correct_index=0)
    with pytest.raises(ValueError):
        db.update_question(a, options=["x", "  "], correct_index=0)
    with pytest.raises(ValueError):
        db.update_question(a, question="   ")
    assert db.get_question(qid, a)["options"] == ["o0a", "o0b", "o0c", "o0d"]
    db.update_question(a, options=["x", "y"], correct_index=1)
    assert db.get_question(qid, a)["options"] == ["x", "y"] and db.get_question(qid, a)["correct_index"] == 1


def test_user_rank_best_attempt():
    qid = make_quiz(n=2)
    for uid, correct, dur in ((1, 1, 10), (2, 2, 30), (3, 2, 20), (1, 2, 50)):
        att = db.create_attempt(qid, uid, uid, [1, 2], 0, False, 0)
        with db.connect() as con:
            con.execute("UPDATE attempts SET correct=? WHERE id=?", (correct, att))
        db.finish_attempt(att, "finished", dur)
        with db.connect() as con:
            con.execute("UPDATE attempts SET correct=?, duration_sec=? WHERE id=?", (correct, dur, att))
    assert db.user_rank(qid, 3) == (1, 3)      # 2 correct in 20 s
    assert db.user_rank(qid, 2) == (2, 3)
    assert db.user_rank(qid, 1) == (3, 3)      # best attempt: 2 correct in 50 s
    assert db.user_rank(qid, 99) == (0, 3)


def test_migration_adds_start_msg_id_without_data_loss(tmp_path):
    path = tmp_path / "r3.db"
    db.set_db_path(path)
    db.init_db()
    qid = make_quiz(n=2)
    att = db.create_attempt(qid, 111, 111, [1, 2], 0, False, 1.0)
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE attempts DROP COLUMN start_msg_id")          # = round-3 schema
    con.commit()
    con.close()
    db.init_db()
    assert db.get_attempt(att)["start_msg_id"] is None
    assert len(db.get_questions(qid)) == 2
    assert db.create_attempt(qid, 111, 111, [1], 0, False, 1.0, start_msg_id=5)


# ---------------------------------------------------------------- creation via native polls
def test_create_quiz_from_native_quiz_polls_and_regular_poll():
    async def t():
        async with Harness() as h:
            u = h.f.user(4001)
            await h.text(u, "/newquiz")
            await h.text(u, "Poll Quiz")
            await h.text(u, "/skip")                                     # no description
            assert db.get_quiz(qs_of(4001)[0])["description"] == ""
            # quiz poll made with the 📝 button: question, options, answer, explanation captured
            await h.send(h.f.user_poll(u, "राजधानी?", ["जयपुर", "जोधपुर", "कोटा"], correct=0,
                                       explanation="जयपुर 1949 से"))
            assert "प्रश्न 1 जुड़ गया" in h.all_text(4001)
            # a regular poll has no correct answer → the bot asks (never invents), then explanation
            await h.send(h.f.user_poll(u, "सबसे बड़ा ज़िला?", ["जैसलमेर", "बीकानेर"], quiz=False))
            assert "सही उत्तर कौन-सा है" in h.all_text(4001)
            await h.cb(u, "c:correct:0")
            await h.text(u, "क्षेत्रफल में")
            # a forwarded quiz whose answer Telegram hides (no correct_option_id) → asked too
            await h.send(h.f.user_poll(u, "छिपा उत्तर?", ["a", "b", "c", "d"], correct=None))
            assert db.get_state(4001)[0] == "c_correct"
            await h.cb(u, "c:correct:3")
            assert db.get_state(4001)[0] == "c_expl"
            await h.text(u, "/skip")
            # 12-option poll
            await h.send(h.f.user_poll(u, "बारह?", [f"v{i}" for i in range(12)], correct=11))
            await h.text(u, "/done")
            qid, qs = qs_of(4001)
            assert [q["question"] for q in qs] == ["राजधानी?", "सबसे बड़ा ज़िला?", "छिपा उत्तर?", "बारह?"]
            assert qs[0]["options"] == ["जयपुर", "जोधपुर", "कोटा"] and qs[0]["correct_index"] == 0
            assert qs[0]["explanation"] == "जयपुर 1949 से"
            assert qs[1]["correct_index"] == 0 and qs[1]["explanation"] == "क्षेत्रफल में"
            assert qs[2]["correct_index"] == 3 and qs[2]["explanation"] == ""
            assert len(qs[3]["options"]) == 12 and qs[3]["correct_index"] == 11
            assert db.get_quiz(qid)["status"] == "ready"
            # the created quiz plays with the captured answers
            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(4001)) == 1)
            p = h.polls_for(4001)[0][1]
            assert p["options"] == ["जयपुर", "जोधपुर", "कोटा"] and p["correct_option_id"] == 0
            assert h.api.rejections == []
    run(t())


def test_pre_question_media_and_text_attach_to_next_question_and_undo():
    async def t():
        async with Harness() as h:
            u = h.f.user(4002)
            await h.text(u, "/newquiz")
            await h.text(u, "Pre")
            await h.text(u, "विवरण")
            await h.send(h.f.photo(u, "MAP1", caption="मानचित्र देखें"))
            assert db.count_questions(qs_of(4002)[0]) == 0                # media is never a question
            await h.send(h.f.user_poll(u, "यह कौन-सा ज़िला है?", ["अजमेर", "टोंक"], correct=1))
            await h.text(u, "अगले प्रश्न के लिए सूचना")
            await h.text(u, "बदली हुई सूचना")                                # replaces, not a question
            assert "बदल दिया गया" in h.all_text(4002)
            await h.text(u, "Q2?\n(A) a\n(B) b\nउत्तर: A")
            await h.text(u, "Q3?\n(A) a\n(B) b\nउत्तर: B")
            await h.text(u, "/undo")                                          # removes Q3
            assert "आख़िरी प्रश्न हटा दिया गया। अब कुल 2" in h.all_text(4002)
            await h.text(u, "Q4?\n(A) a\n(B) b")                               # staged, needs answer
            await h.text(u, "/undo")                                          # discards the staged one
            assert "अधूरा प्रश्न हटा दिया गया" in h.all_text(4002)
            assert db.get_state(4002)[0] == "c_q"
            await h.text(u, "/done")
            qid, qs = qs_of(4002)
            assert [q["question"] for q in qs] == ["यह कौन-सा ज़िला है?", "Q2?"]
            assert (qs[0]["pre_media_type"], qs[0]["pre_media_id"], qs[0]["pre_text"]) == ("photo", "MAP1", "मानचित्र देखें")
            assert qs[1]["pre_text"] == "बदली हुई सूचना" and qs[1]["pre_media_type"] == ""
            # at play time the photo comes right before poll 1
            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(4002)) == 1)
            order = [a for a, _ in h.api.calls if a in ("sendPhoto", "sendPoll")]
            assert order[-2:] == ["sendPhoto", "sendPoll"]
            # /undo with nothing to undo
            await h.text(u, "/undo")
            assert "Undo करने के लिए कुछ नहीं" in h.all_text(4002)
    run(t())


def test_creation_17_questions_stays_17_and_done_warns_unsaved():
    async def t():
        async with Harness() as h:
            u = h.f.user(4003)
            await h.text(u, "/newquiz")
            await h.text(u, "सत्रह")
            await h.text(u, "/skip")
            for i in range(17):
                await h.text(u, f"प्रश्न {i + 1}?\n(A) क\n(B) ख\n(C) ग\n(D) घ\nउत्तर: {'ABCD'[i % 4]}")
            await h.text(u, "Q18?\n(A) x\n(B) y")                          # staged, answer not chosen
            await h.text(u, "/done")
            assert "अधूरा प्रश्न" in h.all_text(4003)
            quizzes = db.get_owner_quizzes(4003)
            assert len(quizzes) == 1                                        # never split
            qs = db.get_questions(quizzes[0]["id"])
            assert len(qs) == 17 and [q["correct_index"] for q in qs] == [i % 4 for i in range(17)]
            assert "📝 Questions: 17" in h.api.sent("sendMessage", 4003)[-1]["text"]
    run(t())


def test_creation_stops_at_100_questions():
    async def t():
        async with Harness() as h:
            u = h.f.user(4004)
            await h.text(u, "/newquiz")
            await h.text(u, "सौ")
            await h.text(u, "/skip")
            qid = qs_of(4004)[0]
            for i in range(99):                               # fast path: 99 saved directly
                db.add_question(qid, f"Q{i}", ["a", "b"], 0)
            await h.send(h.f.user_poll(u, "Q100?", ["a", "b"], correct=1))
            assert db.count_questions(qid) == 100
            assert db.get_state(4004)[0] is None and db.get_quiz(qid)["status"] == "ready"
            assert "सीमा पूरी" in h.all_text(4004)
            with pytest.raises(ValueError):
                db.add_question(qid, "Q101", ["a", "b"], 0)
            await h.cb(u, f"e:addq:{qid}")
            assert "सीमा पूरी है" in h.all_text(4004) and db.get_state(4004)[0] is None
    run(t())


def test_creation_state_survives_restart_and_legacy_state_is_migrated():
    async def t():
        async with Harness() as h:
            u = h.f.user(4005)
            await h.text(u, "/newquiz")
            await h.text(u, "Restart")
            await h.text(u, "/skip")
            await h.text(u, "Q1?\n(A) a\n(B) b\nउत्तर: A")
            await h.text(u, "सूचना")
        async with Harness() as h:                            # "restart": new Application, same DB
            u = h.f.user(4005)
            await h.text(u, "Q2?\n(A) a\n(B) b\nउत्तर: B")
            await h.text(u, "/done")
            qid, qs = qs_of(4005)
            assert [q["question"] for q in qs] == ["Q1?", "Q2?"] and qs[1]["pre_text"] == "सूचना"
            # a user who was mid-way in the OLD step-by-step wizard continues in the new flow
            v = h.f.user(4006)
            old_qid = db.new_quiz(4006, "Old", "", status="draft")
            db.set_state(4006, "c_opts", {"mode": "new", "quiz_id": old_qid, "n_saved": 0,
                                          "cur": {"question": "अधूरा", "pre_text": "पुरानी सूचना"}})
            await h.text(v, "नया Q?\n(A) a\n(B) b\nउत्तर: B")
            await h.text(v, "/done")
            q = db.get_questions(old_qid)
            assert len(q) == 1 and q[0]["question"] == "नया Q?" and q[0]["pre_text"] == "पुरानी सूचना"
            state, data = "c_more", {"cur": {"question": "x", "options": ["a"]}}
            assert creator.normalize_creation_state(state, data) == "c_q" and data["cur"] == {}
    run(t())


def test_request_poll_keyboard_only_in_private_and_two_users_do_not_leak():
    async def t():
        async with Harness() as h:
            a, b = h.f.user(4007), h.f.user(4008)
            await h.text(a, "/newquiz", chat_id=-1002)                 # group → redirect only
            assert db.get_state(4007)[0] is None
            assert not any("keyboard" in (m.get("reply_markup") or {}) for m in h.api.sent("sendMessage", -1002))
            await h.text(a, "/newquiz")
            await h.text(b, "/newquiz")
            await h.text(a, "A का quiz")
            await h.text(b, "B का quiz")
            await h.text(a, "/skip")
            await h.text(b, "B desc")
            await h.send(h.f.user_poll(a, "A1?", ["x", "y"], correct=0))
            await h.text(b, "B1?\n(A) p\n(B) q")
            await h.cb(a, "c:correct:1")                               # A has no pending question
            assert db.get_state(4008)[0] == "c_correct"
            await h.cb(b, "c:correct:1")
            await h.text(b, "/skip")
            await h.text(a, "/done")
            await h.text(b, "/done")
            (qa,) = db.get_owner_quizzes(4007)
            (qb,) = db.get_owner_quizzes(4008)
            assert [q["question"] for q in db.get_questions(qa["id"])] == ["A1?"]
            assert [(q["question"], q["correct_index"]) for q in db.get_questions(qb["id"])] == [("B1?", 1)]
            assert h.api.rejections == []                              # no keyboard sent anywhere invalid
            # My Quizzes shows only the owner's own quizzes
            await h.cb(a, "m:my")
            my = h.api.sent("editMessageText", 4007)[-1]
            assert [x["callback_data"] for x in buttons(my["reply_markup"])][0] == f"q:view:{qa['id']}"
            assert qb["id"] not in str(my["reply_markup"])
            # non-owner card: play/share/stats only; settings are owner-only (server-side)
            await h.cb(b, f"q:view:{qa['id']}")
            card = h.api.sent("editMessageText", 4008)[-1]
            datas = [x["callback_data"] for x in buttons(card["reply_markup"]) if x.get("callback_data")]
            assert f"q:edit:{qa['id']}" not in datas and f"q:set:{qa['id']}" not in datas
            await h.cb(b, f"q:set:{qa['id']}")
            await h.cb(b, f"e:so:{qa['id']}")
            assert db.get_quiz(qa["id"])["shuffle_options"] == 0
    run(t())


# ---------------------------------------------------------------- playing
def test_ready_card_result_rank_and_private_stats():
    async def t():
        async with Harness() as h:
            owner, p1, p2 = h.f.user(111), h.f.user(4010), h.f.user(4011)
            qid = make_quiz(owner=111, n=2, timer=0)
            for user, right in ((p1, True), (p2, False)):
                await h.start(user, qid)
                for i in range(2):
                    await h.wait(lambda: len(h.polls_for(user["id"])) == i + 1)
                    await h.answer_current(user, correct=right)
                await h.wait(lambda: "🏁" in h.all_text(user["id"]))
            r2 = h.all_text(4011)
            assert "Correct: 0" in r2 and "Wrong: 2" in r2 and "Percentage: 0.00%" in r2
            assert "rank (best attempt): 2 / 2" in r2
            assert "rank" not in h.all_text(4010)                  # only one participant at that time
            # Try Again from the result card works and double taps are ignored
            await h.cb(p2, f"r:go:{qid}", message_id=4242)
            await h.cb(p2, f"r:go:{qid}", message_id=4242)
            await h.wait(lambda: len(h.polls_for(4011)) == 3)
            await asyncio.sleep(0.1)
            assert len(h.polls_for(4011)) == 3 and "पिछला quiz रोक दिया" not in h.all_text(4011)
            await h.text(p2, "/stop")
            # stats: a player sees only their own results, the owner sees everything
            h.api.reset_calls()
            await h.cb(p2, f"q:stats:{qid}")
            mine = h.all_text(4011)
            assert "आपके results" in mine and "@user4010" not in mine and "Top users" not in mine
            await h.cb(owner, f"q:stats:{qid}")
            full = h.all_text(111)
            assert "Top users" in full and "@user4010" in full
    run(t())


def test_ready_card_for_empty_or_draft_quiz_and_timer_label():
    async def t():
        async with Harness() as h:
            u = h.f.user(4012)
            empty = db.new_quiz(111, "Empty")
            await h.cb(u, f"q:run:{empty}")
            assert "कोई प्रश्न नहीं" in h.all_text(4012)
            draft = make_quiz(owner=111, n=1)
            db.update_quiz(draft, status="draft")
            await h.text(u, f"/start quiz_{draft}")
            assert "तैयार नहीं" in h.all_text(4012)
            await h.cb(u, f"r:go:{draft}", message_id=9)
            assert h.polls_for(4012) == []
            timed = make_quiz(owner=111, n=1, timer=120, sq=1, so=1)
            await h.cb(u, f"q:run:{timed}")
            txt = h.api.sent("sendMessage", 4012)[-1]["text"]
            assert "2 min per question" in txt or "per question" in txt
            assert "shuffled" in txt
    run(t())


# ---------------------------------------------------------------- inline sharing
def test_inline_query_returns_only_ready_quizzes():
    async def t():
        async with Harness() as h:
            owner, other = h.f.user(111), h.f.user(4020)
            qid = make_quiz(owner=111, n=3, title="भूगोल टेस्ट")
            draft = make_quiz(owner=111, n=1, title="भूगोल ड्राफ्ट")
            db.update_quiz(draft, status="draft")
            await h.send(h.f.inline_query(other, f"quiz_{qid}"))
            res = h.api.inline_answers[-1]["results"]
            assert len(res) == 1 and res[0]["title"] == "भूगोल टेस्ट"
            assert res[0]["reply_markup"]["inline_keyboard"][0][0]["url"] == \
                f"https://t.me/TestQuizBot?start=quiz_{qid}"
            await h.send(h.f.inline_query(other, f"quiz_{draft}"))
            assert h.api.inline_answers[-1]["results"] == []
            await h.send(h.f.inline_query(other, ""))                  # other has no quizzes of their own
            assert h.api.inline_answers[-1]["results"] == []
            await h.send(h.f.inline_query(owner, "भूगोल"))
            assert [r["title"] for r in h.api.inline_answers[-1]["results"]] == ["भूगोल टेस्ट"]
    run(t())


def test_share_without_inline_mode_hides_inline_button(monkeypatch):
    import fake_telegram
    monkeypatch.setitem(fake_telegram.BOT_USER, "supports_inline_queries", False)

    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=111, n=1)
            await h.cb(h.f.user(111), f"q:share:{qid}")
            sm = buttons(h.api.sent("sendMessage", 111)[-1]["reply_markup"])
            assert not any("switch_inline_query" in b for b in sm)
    run(t())
