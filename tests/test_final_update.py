"""MASTER FINAL UPDATE: selectable + custom timer (create / edit / default
settings), Timeout → Skipped → Next with a separate Timeout count in the
result, private and group playback with every timer value."""
import asyncio
import time

import pytest

import config
import database as db
import keyboards as kb
from test_bot_flow import Harness, buttons, make_quiz, run
from test_round5 import G1, answer, gpolls, group_start


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "quiz.db")
    db.init_db()
    monkeypatch.setattr(config, "NEXT_QUESTION_DELAY", 0.05)
    monkeypatch.setattr(config, "TIMER_GRACE", 0.2)
    monkeypatch.setattr(config, "GROUP_NEXT_DELAY", 0.05)
    monkeypatch.setattr(config, "QUESTION_TAG", "जय श्री श्याम")
    yield
    db.set_db_path(config.DB_PATH)


def cbs(markup):
    return [b["callback_data"] for b in buttons(markup)]


def labels(markup):
    return [b["text"] for b in buttons(markup)]


# ------------------------------------------------------------ unit level
@pytest.mark.parametrize("text,val", [
    ("45", 45), ("45 sec", 45), ("45s", 45), ("90 सेकंड", 90), ("2 min", 120), ("2m", 120),
    ("3 मिनट", 180), ("1:30", 90), ("2m 30s", 150), ("2 min 30 sec", 150), ("0", 0),
    ("No Timer", 0), ("3600", 3600), ("60 min", 3600),
])
def test_parse_custom_timer(text, val):
    assert config.parse_timer(text) == val


@pytest.mark.parametrize("bad", ["", "abc", "4", "3601", "61 min", "-5", "1.5 min", "10 घंटे"])
def test_parse_custom_timer_rejects(bad):
    with pytest.raises(ValueError):
        config.parse_timer(bad)


def test_timer_labels():
    assert [config.timer_label(v) for v in (0, 10, 15, 30, 45, 60, 90, 120, 150, 180, 300, 900)] == [
        "No timer", "10 sec", "15 sec", "30 sec", "45 sec", "1 min", "90 sec", "2 min",
        "2 min 30 sec", "3 min", "5 min", "15 min"]


def test_timer_menu_has_all_choices_and_custom():
    m = kb.timer_menu("e:settimer:abc", 30, "q:set:abc").to_dict()
    assert labels(m) == ["10 sec", "15 sec", "✅ 30 sec", "60 sec", "90 sec", "2 min", "3 min", "5 min",
                         "No Timer", "✏️ Custom Time", "⬅️ Back"]
    assert "e:settimer:abc:custom" in cbs(m) and "e:settimer:abc:0" in cbs(m)
    m2 = kb.timer_menu("c:timer", 45, None).to_dict()
    assert "✅ Custom: 45 sec" in labels(m2) and "⬅️ Back" not in labels(m2)


# --------------------------------------------------- settings / creation
def test_edit_settings_preset_and_custom_timer_saved():
    async def t():
        async with Harness() as h:
            u = h.f.user(7001)
            qid = make_quiz(owner=7001, n=2, timer=30)
            await h.cb(u, f"e:timer:{qid}")
            menu = h.api.sent("sendMessage", 7001)[-1] if not [c for c in h.api.calls if c[0] == "editMessageText"] \
                else h.api.calls[-1][1]
            assert "custom" in str(menu.get("reply_markup"))
            for v in (0, 10, 60, 300):
                await h.cb(u, f"e:settimer:{qid}:{v}")
                assert db.get_quiz(qid)["timer"] == v
            await h.cb(u, f"e:settimer:{qid}:custom")
            assert db.get_state(7001)[0] == "e_timer"
            await h.text(u, "abc")                                  # invalid → explained, still waiting
            assert "समय समझ नहीं आया" in h.all_text(7001) and db.get_state(7001)[0] == "e_timer"
            await h.text(u, "45 sec")
            assert db.get_quiz(qid)["timer"] == 45 and db.get_state(7001)[0] is None
            assert "✅ Timer: 45 sec" in h.all_text(7001)
            await h.cb(u, f"e:settimer:{qid}:custom")
            await h.text(u, "15 min")                               # > 600 s is allowed (job-enforced)
            assert db.get_quiz(qid)["timer"] == 900
    run(t())


def test_default_timer_custom_in_user_settings_applies_to_new_quiz():
    async def t():
        async with Harness() as h:
            u = h.f.user(7002)
            await h.cb(u, "s:settimer:custom")
            await h.text(u, "1:15")
            assert db.ensure_user_row(7002)["default_timer"] == 75
            assert "✅ Default timer: 1 min 15 sec" in h.all_text(7002)
            await h.cb(u, "s:settimer:0")
            assert db.ensure_user_row(7002)["default_timer"] == 0
            await h.cb(u, "s:settimer:custom")
            await h.text(u, "20")
            await h.text(u, "/newquiz")
            await h.text(u, "Default T")
            await h.text(u, "/skip")
            assert db.get_quiz(db.get_owner_quizzes(7002)[0]["id"])["timer"] == 20
    run(t())


def test_timer_selection_during_creation_preset_and_custom():
    async def t():
        async with Harness() as h:
            u = h.f.user(7003)
            await h.text(u, "/newquiz")
            await h.text(u, "Timer Quiz")
            await h.text(u, "/skip")
            timer_msg = [m for m in h.api.sent("sendMessage", 7003) if "c:timer" in str(m.get("reply_markup"))]
            assert timer_msg, "timer choice not offered during creation"
            assert "c:timer:custom" in str(timer_msg[-1]["reply_markup"])
            qid = db.get_owner_quizzes(7003)[0]["id"]
            await h.cb(u, "c:timer:60")
            assert db.get_quiz(qid)["timer"] == 60
            await h.cb(u, "c:timer:custom")
            await h.text(u, "40")
            assert db.get_quiz(qid)["timer"] == 40 and "✅ Timer: 40 sec" in h.all_text(7003)
            # creation continues normally
            await h.text(u, "राजस्थान की राजधानी?\n(A) जयपुर\n(B) अजमेर\nउत्तर: A")
            await h.text(u, "/done")
            quiz = db.get_quiz(qid)
            assert quiz["status"] == "ready" and quiz["timer"] == 40 and quiz["question_count"] == 1
            # a question sent while a custom timer was pending is never swallowed
            await h.text(u, "/newquiz")
            await h.text(u, "Second")
            await h.text(u, "/skip")
            await h.cb(u, "c:timer:custom")
            await h.text(u, "भारत की राजधानी कौन-सी है?\n(A) दिल्ली\n(B) मुंबई\nउत्तर: A")
            q2 = [q for q in db.get_owner_quizzes(7003) if q["title"] == "Second"][0]
            assert db.get_questions(q2["id"])[0]["question"] == "भारत की राजधानी कौन-सी है?"
    run(t())


# ------------------------------------------------------ private playback
@pytest.mark.parametrize("timer,open_period", [(0, None), (10, 10), (30, 30), (60, 60), (300, 300),
                                               (45, 45), (900, None)])
def test_private_poll_uses_selected_timer(timer, open_period):
    async def t():
        async with Harness() as h:
            u = h.f.user(7100 + timer)
            qid = make_quiz(owner=u["id"], n=2, timer=timer)
            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(u["id"])) == 1)
            sp = h.api.sent("sendPoll", u["id"])[-1]
            assert (int(sp["open_period"]) if sp.get("open_period") else None) == open_period
            assert sp["question"].count("जय श्री श्याम") == 1                 # tag exactly once
            jobs = [j.name for j in h.app.job_queue.jobs() if j.name and "timeout" in j.name]
            assert bool(jobs) == (timer > 0)                                   # No Timer → no timeout job
            await h.text(u, "/stop")
    run(t())


def test_no_timer_never_times_out():
    async def t():
        async with Harness() as h:
            u = h.f.user(7201)
            qid = make_quiz(owner=7201, n=2, timer=0)
            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(7201)) == 1)
            await asyncio.sleep(2.0)
            assert len(h.polls_for(7201)) == 1 and "समय समाप्त" not in h.all_text(7201)
            await h.answer_current(u, True)
            await h.wait(lambda: len(h.polls_for(7201)) == 2)
            await h.answer_current(u, False)
            await h.wait(lambda: "🏁" in h.all_text(7201))
            res = h.all_text(7201)
            assert "Correct: 1" in res and "Wrong: 1" in res and "Timeout: 0" in res
    run(t())


def test_timeout_skipped_next_and_result_statistics():
    async def t():
        async with Harness() as h:
            u = h.f.user(7202)
            qid = make_quiz(owner=7202, n=4, timer=2)                    # 2 s (test-only value)
            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(7202)) == 1)
            await h.answer_current(u, True)                               # Q1 correct
            await h.wait(lambda: len(h.polls_for(7202)) == 2)
            await h.answer_current(u, False)                              # Q2 wrong
            await h.wait(lambda: len(h.polls_for(7202)) == 3)
            await h.text(u, "/skip")                                      # Q3 skipped by the user
            await h.wait(lambda: len(h.polls_for(7202)) == 4, timeout=6)  # Q4 → timeout
            await h.wait(lambda: "🏁" in h.all_text(7202), timeout=6)
            res = [m["text"] for m in h.api.sent("sendMessage", 7202) if "🏁" in m["text"]][-1]
            for line in ("Total: 4", "Correct: 1 ✅", "Wrong: 1 ❌", "Skipped: 2 ⌛", "Timeout: 1 ⏰",
                         "Score: 1/4", "Percentage: 25.00%"):
                assert line in res, (line, res)
            att = db.user_history(7202)[0]
            assert (att["correct"], att["wrong"], att["skipped"], att["timeouts"]) == (1, 1, 2, 1)
            assert [a["status"] for a in db.get_answers(att["id"])] == ["correct", "wrong", "skipped", "timeout"]
            assert "समय समाप्त" in h.all_text(7202)
    run(t())


def test_custom_timer_over_600_is_enforced_by_bot():
    async def t():
        async with Harness() as h:
            u = h.f.user(7203)
            qid = make_quiz(owner=7203, n=1, timer=900)
            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(7203)) == 1)
            assert h.api.sent("sendPoll", 7203)[-1].get("open_period") is None
            job = [j for j in h.app.job_queue.jobs() if j.name and "timeout" in j.name]
            assert job and 895 < (job[0].next_t.timestamp() - time.time()) <= 901
            await h.text(u, "/stop")
    run(t())


# ------------------------------------------------------------- groups
def test_group_native_poll_with_selected_timer_and_leaderboard():
    async def t():
        async with Harness() as h:
            s, a, b = h.f.user(7301), h.f.user(7302, "Asha"), h.f.user(7303, "Ravi")
            qid = make_quiz(owner=7301, n=2, timer=45, title="Group T")
            await group_start(h, s, qid)
            await h.wait(lambda: len(gpolls(h, G1)) == 1)
            sp = h.api.sent("sendPoll", G1)[-1]
            assert sp["type"] == "quiz" and int(sp["open_period"]) == 45
            assert sp["question"].count("जय श्री श्याम") == 1
            await answer(h, a, G1, correct=True)
            await answer(h, b, G1, correct=False)
            await h.text(s, "/stop", chat_id=G1)
            txt = h.all_text(G1)
            assert "🥇 Asha — 1/1" in txt and "🥈 Ravi — 0/1" in txt
            assert txt.index("Asha") < txt.index("Ravi")
    run(t())
