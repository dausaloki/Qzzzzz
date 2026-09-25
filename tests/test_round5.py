"""Round 5: join gate, group quizzes, forwarded content, question tag.

All tests run the real handlers, JobQueue and SQLite against the fake Bot API
(tests/fake_telegram.py) — getChatMember, sendPoll(type=quiz,
correct_option_ids), poll_answer and chat_member updates are emulated with
Bot API 9.6 field names.  The numbers in the test names match the 50 items
of the round-5 test list.
"""
import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import bot as botmod
import config
import database as db
import membership
import pdf_parser as P
import quiz_engine as engine
from test_bot_flow import TOKEN, Harness, buttons, isolated, make_quiz, run

_AUTOUSE_FIXTURES = (isolated,)   # per-test temporary database (autouse fixture from test_bot_flow)

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"
TAG = "जय श्री श्याम"
G1, G2 = -1001, -1002


@pytest.fixture(autouse=True)
def round5_config(monkeypatch):
    monkeypatch.setattr(config, "QUESTION_TAG", TAG)
    monkeypatch.setattr(config, "REQUIRED_CHAT", "@kalam_kranti")
    monkeypatch.setattr(config, "REQUIRED_CHAT_URL", "https://t.me/kalam_kranti")
    monkeypatch.setattr(config, "MEMBERSHIP_RECHECK_SECONDS", 600)
    monkeypatch.setattr(config, "GROUP_DEFAULT_TIMER", 1)
    monkeypatch.setattr(config, "GROUP_NEXT_DELAY", 0.05)
    yield


def last(h, uid):
    return h.api.sent("sendMessage", uid)[-1]


def member_calls(h):
    return [p for a, p in h.api.calls if a == "getChatMember" and str(p["chat_id"]) == "@kalam_kranti"]


def labels(markup):
    return [b["text"] for b in buttons(markup)]


def quizzes_of(uid):
    return db.get_owner_quizzes(uid)


def qs_of(uid):
    qid = quizzes_of(uid)[0]["id"]
    return qid, db.get_questions(qid)


async def verify(h, u):
    """The real flow: /start → welcome → I've Joined (getChatMember says member)."""
    await h.text(u, "/start")
    await h.cb(u, "j:check")
    assert membership.VERIFIED_TEXT in h.all_text(u["id"])


# ============================================================ join gate (1–11)
def test_01_start_shows_welcome_with_join_and_joined_buttons():
    async def t():
        async with Harness(pre_verified=False) as h:
            u = h.f.user(501)
            await h.text(u, "/start")
            m = last(h, 501)
            assert m["text"] == ("👋 Welcome!\n\nQuiz Bot इस्तेमाल करने से पहले हमारे Telegram Group को "
                                 "Join करना जरूरी है।")
            b = buttons(m["reply_markup"])
            assert b[0] == {"text": "📢 Join Group", "url": "https://t.me/kalam_kranti"}
            assert b[1] == {"text": "✅ I've Joined", "callback_data": "j:check"}
            assert "➕ New Quiz" not in str(h.api.calls)          # no menu before verification
    run(t())


def test_02_not_joined_click_shows_fail_text_and_retry_buttons():
    async def t():
        async with Harness(pre_verified=False) as h:
            h.api.default_member_status = "left"
            u = h.f.user(502)
            await h.text(u, "/start")
            await h.cb(u, "j:check")
            assert len(member_calls(h)) >= 1                        # asked Telegram
            m = [p for a, p in h.api.calls if a == "editMessageText"][-1]
            assert m["text"] == "❌ आपने अभी Telegram Group Join नहीं किया है।\n\nPlease join the group first."
            import json
            rm = json.loads(m["reply_markup"]) if isinstance(m["reply_markup"], str) else m["reply_markup"]
            assert labels(rm) == ["📢 Join Group", "🔄 I've Joined"]
            assert db.get_membership(502)[0] in (None, 0)
    run(t())


def test_03_member_click_verifies_and_shows_main_menu():
    async def t():
        async with Harness(pre_verified=False) as h:
            u = h.f.user(503)
            await verify(h, u)
            menu = last(h, 503)
            assert labels(menu["reply_markup"]) == ["➕ New Quiz", "📚 My Quizzes", "📥 Import", "▶️ Start Quiz",
                                                    "📊 Statistics", "⚙️ Settings", "❓ Help"]
            assert db.get_membership(503)[0] > 0
    run(t())


def test_04_administrator_and_creator_statuses_are_members():
    async def t():
        async with Harness(pre_verified=False) as h:
            for uid, st in ((504, "administrator"), (505, "creator"), (506, "member")):
                h.api.members[("@kalam_kranti", uid)] = st
                u = h.f.user(uid)
                await verify(h, u)
    run(t())


def test_05_restricted_left_kicked_statuses():
    async def t():
        async with Harness(pre_verified=False) as h:
            cases = {507: ("restricted", True), 508: ("restricted_left", False), 509: ("left", False),
                     510: ("kicked", False)}
            for uid, (st, ok) in cases.items():
                h.api.members[("@kalam_kranti", uid)] = st
                u = h.f.user(uid)
                await h.text(u, "/start")
                await h.cb(u, "j:check")
                assert (membership.VERIFIED_TEXT in h.all_text(uid)) is ok, st
                assert bool(db.get_membership(uid)[0]) is ok
    run(t())


def test_06_all_protected_features_blocked_until_verified():
    async def t():
        async with Harness(pre_verified=False) as h:
            u = h.f.user(511)
            for cmd in ("/newquiz", "/import", "/myquizzes", "/stats", "/settings", "/done", "hello"):
                await h.text(u, cmd)
                assert last(h, 511)["text"] == membership.WELCOME_TEXT, cmd
            for data in ("m:new", "m:my", "m:imp", "m:stats", "m:set", "m:start"):
                await h.cb(u, data)
                assert last(h, 511)["text"] == membership.WELCOME_TEXT, data
            h.api.files["T1"] = b"1. q?\n(A) a\n(B) b\nAnswer: A"
            await h.send(h.f.document(u, "T1", "q.txt", "text/plain", 30))
            await h.send(h.f.user_poll(u, "q?", ["a", "b"], correct=0))
            assert last(h, 511)["text"] == membership.WELCOME_TEXT
            assert db.get_state(511)[0] is None and quizzes_of(511) == []
    run(t())


def test_07_button_click_is_never_trusted():
    async def t():
        async with Harness(pre_verified=False) as h:
            u = h.f.user(512)
            h.api.member_error = "Bad Request: member list is inaccessible"     # bot not in the group
            await h.text(u, "/start")
            for _ in range(3):
                await h.cb(u, "j:check")
            assert membership.CHECK_FAILED_TEXT in h.all_text(512)
            assert membership.VERIFIED_TEXT not in h.all_text(512)
            await h.text(u, "/newquiz")
            assert last(h, 512)["text"] == membership.WELCOME_TEXT
            # forging the callback with a payload does not help either
            h.api.member_error = None
            h.api.default_member_status = "left"
            await h.cb(u, "j:check:quiz_abc")
            assert db.get_membership(512)[0] in (None, 0)
    run(t())


def test_08_verified_user_is_not_asked_again_while_valid():
    async def t():
        async with Harness(pre_verified=False) as h:
            u = h.f.user(513)
            await verify(h, u)
            n = len(member_calls(h))
            await h.text(u, "/newquiz")
            await h.text(u, "My Quiz")
            await h.cb(u, "m:home")
            await h.text(u, "/myquizzes")
            assert len(member_calls(h)) == n                         # cached (re-checked every 10 min)
            assert membership.WELCOME_TEXT not in h.api.texts(513)[-5:]
            assert quizzes_of(513)
    run(t())


def test_09_user_who_left_must_join_again():
    async def t():
        async with Harness(pre_verified=False) as h:
            u = h.f.user(514)
            await verify(h, u)
            h.api.default_member_status = "left"
            await h.text(u, "/start")                                # /start always re-checks
            assert last(h, 514)["text"] == membership.WELCOME_TEXT
            await h.text(u, "/newquiz")
            assert last(h, 514)["text"] == membership.WELCOME_TEXT
            h.api.default_member_status = "member"                   # joined again
            await h.cb(u, "j:check")
            assert h.api.texts(514).count(membership.VERIFIED_TEXT) == 2
            # periodic re-check also catches leaving without /start
            config.MEMBERSHIP_RECHECK_SECONDS = 0
            h.api.default_member_status = "kicked"
            await h.text(u, "/myquizzes")
            assert last(h, 514)["text"] == membership.WELCOME_TEXT
    run(t())


def test_10_chat_member_update_revokes_access():
    async def t():
        async with Harness(pre_verified=False) as h:
            u = h.f.user(515)
            await verify(h, u)
            other = {"id": -100999, "type": "supergroup", "title": "Other", "username": "other_group"}
            await h.send(h.f.chat_member(other, u, "member", "left"))
            assert db.get_membership(515)[0] > 0                     # other chats are ignored
            req = {"id": -100555, "type": "supergroup", "title": "Kalam Kranti", "username": "kalam_kranti"}
            await h.send(h.f.chat_member(req, u, "member", "left"))
            assert db.get_membership(515)[0] in (None, 0)
            h.api.default_member_status = "left"
            await h.text(u, "/newquiz")
            assert last(h, 515)["text"] == membership.WELCOME_TEXT
    run(t())


def test_11_env_config_deeplink_payload_inline_and_no_token_in_logs(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    async def t():
        async with Harness(pre_verified=False) as h:
            qid = make_quiz(owner=900, n=2, title="Deep Quiz")
            u = h.f.user(516)
            await h.text(u, f"/start quiz_{qid}")
            b = buttons(last(h, 516)["reply_markup"])
            assert b[1]["callback_data"] == f"j:check:quiz_{qid}"   # payload survives the gate
            await h.cb(u, f"j:check:quiz_{qid}")
            assert "Get ready for the quiz “Deep Quiz”" in h.all_text(516)
            # inline mode is gated too, with a button that opens the join screen
            v = h.f.user(517)
            await h.send(h.f.inline_query(v, ""))
            ans = h.api.inline_answers[-1]
            assert ans["results"] == [] and ans["button"]["start_parameter"] == "join"
            # gate disabled when REQUIRED_CHAT is empty
            monkeypatch.setattr(config, "REQUIRED_CHAT", "")
            w = h.f.user(518)
            await h.text(w, "/start")
            assert "➕ New Quiz" in str(last(h, 518)["reply_markup"])
    run(t())
    assert TOKEN not in caplog.text and TOKEN.split(":")[1] not in caplog.text
    env = {**os.environ, "REQUIRED_CHAT": "@some_group", "QUESTION_TAG": "टैग"}
    out = subprocess.run([sys.executable, "-c", "import config;print(config.REQUIRED_CHAT, config.REQUIRED_CHAT_URL,"
                          " config.QUESTION_TAG)"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["@some_group", "https://t.me/some_group", "टैग"]


# ============================================================ group quiz (12–25)
async def group_start(h, starter, qid, chat=G1):
    await h.text(starter, f"/start@TestQuizBot quiz_{qid}", chat_id=chat)
    card = last(h, chat)
    assert f"g:go:{qid}" in str(card["reply_markup"])
    await h.cb(starter, f"g:go:{qid}", chat_id=chat)


def gpolls(h, chat):
    return h.polls_for(chat)


async def answer(h, user, chat, correct=True, index=-1):
    pid, poll = gpolls(h, chat)[index]
    opt = poll["correct_option_id"] if correct else (poll["correct_option_id"] + 1) % len(poll["options"])
    await h.send(h.f.poll_answer(user, pid, opt))


def session_of(chat):
    with db.connect() as con:
        return dict(con.execute("SELECT * FROM group_sessions WHERE chat_id=? ORDER BY id DESC", (chat,)).fetchone())


def test_12_group_start_shows_group_card_with_privately_option():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=601, n=3, timer=0, title="Group GK")
            u = h.f.user(601)
            await h.text(u, f"/start@TestQuizBot quiz_{qid}", chat_id=G1)
            card = last(h, G1)
            b = buttons(card["reply_markup"])
            assert b[0]["callback_data"] == f"g:go:{qid}"
            assert b[1]["text"] == "👤 Start Privately" and f"start=quiz_{qid}" in b[1]["url"]
            assert "Group Quiz" in card["text"] and gpolls(h, G1) == []
            # share screen offers Telegram's startgroup link
            await h.cb(u, f"q:share:{qid}")
            assert f"startgroup=quiz_{qid}" in str(last(h, 601)["reply_markup"])
    run(t())


def test_13_native_non_anonymous_quiz_poll_one_at_a_time():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=602, n=3, timer=0)
            u = h.f.user(602)
            await group_start(h, u, qid)
            await h.wait(lambda: len(gpolls(h, G1)) == 1)
            sp = h.api.sent("sendPoll", G1)[-1]
            assert sp["type"] == "quiz" and sp["is_anonymous"] in (False, "false")
            assert "correct_option_ids" in sp and "correct_option_id" not in sp       # Bot API 9.6
            pid, poll = gpolls(h, G1)[0]
            assert poll["type"] == "quiz" and poll["is_anonymous"] is False and len(poll["correct_option_ids"]) == 1
            await asyncio.sleep(0.4)
            assert len(gpolls(h, G1)) == 1                          # never two open questions
            await h.wait(lambda: len(gpolls(h, G1)) == 2, timeout=5)
            assert h.api.polls[pid]["is_closed"]                    # previous one closed first
            assert h.api.rejections == []
    run(t())


def test_14_poll_answer_stored_with_all_fields():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=603, n=2, timer=0)
            u, a = h.f.user(603), h.f.user(604, "Asha")
            await group_start(h, u, qid)
            await h.wait(lambda: len(gpolls(h, G1)) == 1)
            await answer(h, a, G1, correct=True)
            s = session_of(G1)
            rows = db.get_group_answers(s["id"])
            assert len(rows) == 1
            r = rows[0]
            pid, poll = gpolls(h, G1)[0]
            assert (r["user_id"], r["quiz_id"], r["chat_id"], r["session_id"], r["poll_id"]) == (604, qid, G1, s["id"], pid)
            assert r["question_id"] == db.get_questions(qid)[0]["id"]
            assert r["chosen_display"] == poll["correct_option_id"] and r["is_correct"] == 1 and r["score"] == 1
            assert r["answered_ts"] and abs(r["answered_ts"] - time.time()) < 30
            await h.text(u, "/stop", chat_id=G1)
    run(t())


def test_15_multiple_participants_never_overwrite_each_other():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=605, n=1, timer=0)
            owner = h.f.user(605)
            users = [h.f.user(610 + i, f"P{i}") for i in range(5)]
            await group_start(h, owner, qid)
            await h.wait(lambda: len(gpolls(h, G1)) == 1)
            for i, usr in enumerate(users):
                await answer(h, usr, G1, correct=i % 2 == 0)
            await answer(h, users[1], G1, correct=True)             # a second answer is ignored
            rows = db.get_group_answers(session_of(G1)["id"])
            assert sorted(r["user_id"] for r in rows) == [610, 611, 612, 613, 614]
            assert {r["user_id"]: r["is_correct"] for r in rows} == {610: 1, 611: 0, 612: 1, 613: 0, 614: 1}
    run(t())


def test_16_timer_closes_poll_and_moves_on_open_period_passed():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=606, n=2, timer=0)
            u = h.f.user(606)
            await group_start(h, u, qid)
            await h.wait(lambda: len(gpolls(h, G1)) == 2, timeout=6)
            assert [a for a, p in h.api.calls if a == "stopPoll"]
            await h.wait(lambda: "🏆" in h.all_text(G1), timeout=6)
            # a real timer (5–600 s) is passed to Telegram as open_period
            qid2 = make_quiz(owner=606, n=2, timer=10)
            await group_start(h, u, qid2, chat=G2)
            await h.wait(lambda: len(gpolls(h, G2)) == 1)
            assert int(h.api.sent("sendPoll", G2)[-1]["open_period"]) == 10
            await h.text(u, "/stop", chat_id=G2)
    run(t())


def test_17_leaderboard_scores_from_real_answers():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=620, n=3, timer=0, title="Board")
            s = h.f.user(620)
            a, b, c = h.f.user(621, "Aman"), h.f.user(622, "Bina"), h.f.user(623, "Chetan")
            await group_start(h, s, qid)
            plan = [(a, [1, 1, 1]), (b, [1, 0, None]), (c, [0, 0, 1])]
            for i in range(3):
                await h.wait(lambda: len(gpolls(h, G1)) == i + 1, timeout=6)
                for usr, ans in plan:
                    if ans[i] is not None:
                        await answer(h, usr, G1, correct=bool(ans[i]))
            await h.wait(lambda: "🏆" in h.all_text(G1), timeout=6)
            txt = h.all_text(G1)
            assert "🏆 <b>Quiz Result</b>" in txt
            assert "🥇 Aman — 3/3" in txt and "🥈 Bina — 1/3" in txt and "🥉 Chetan — 1/3" in txt
            assert "✅ Correct 3 · ❌ Wrong 0 · ⌛ Skipped 0 · Score 3/3 · Percentage 100.00%" in txt
            assert "✅ Correct 1 · ❌ Wrong 1 · ⌛ Skipped 1 · Score 1/3 · Percentage 33.33%" in txt
            assert "✅ Correct 1 · ❌ Wrong 2 · ⌛ Skipped 0 · Score 1/3 · Percentage 33.33%" in txt
            assert txt.index("Bina") < txt.index("Chetan")          # tie → faster total answer time first
            assert session_of(G1)["status"] == "finished"
    run(t())


def test_18_no_participants_result():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=624, n=1, timer=0)
            await group_start(h, h.f.user(624), qid)
            await h.wait(lambda: "🏆" in h.all_text(G1), timeout=6)
            assert "किसी participant ने उत्तर नहीं दिया।" in h.all_text(G1)
    run(t())


def test_19_two_groups_same_quiz_simultaneously_isolated():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=625, n=2, timer=0)
            s = h.f.user(625)
            x, y = h.f.user(626, "Xenia"), h.f.user(627, "Yash")
            await group_start(h, s, qid, chat=G1)
            await group_start(h, s, qid, chat=G2)
            for i in range(2):
                await h.wait(lambda: len(gpolls(h, G1)) == i + 1 and len(gpolls(h, G2)) == i + 1, timeout=6)
                await answer(h, x, G1, correct=True)
                await answer(h, y, G2, correct=False)
            await h.wait(lambda: "🏆" in h.all_text(G1) and "🏆" in h.all_text(G2), timeout=6)
            assert "Xenia — 2/2" in h.all_text(G1) and "Yash" not in h.all_text(G1)
            assert "Yash — 0/2" in h.all_text(G2) and "Xenia" not in h.all_text(G2)
            s1, s2 = session_of(G1), session_of(G2)
            assert s1["id"] != s2["id"] and s1["quiz_id"] == s2["quiz_id"] == qid
    run(t())


def test_20_group_and_private_sessions_run_together():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=628, n=2, timer=0)
            owner, p = h.f.user(628), h.f.user(629, "Priya")
            await group_start(h, owner, qid)
            await h.start(p, qid)                                   # private session of the same quiz
            await h.wait(lambda: len(h.polls_for(629)) == 1 and len(gpolls(h, G1)) == 1)
            await h.answer_current(p, True)                         # private answer
            await answer(h, p, G1, correct=False)                   # group answer by the same person
            await h.wait(lambda: len(h.polls_for(629)) == 2)
            await h.answer_current(p, True)
            await h.wait(lambda: "🏁" in h.all_text(629) and "🏆" in h.all_text(G1), timeout=6)
            assert "Correct: 2" in h.all_text(629)
            rows = db.get_group_answers(session_of(G1)["id"])
            assert [(r["user_id"], r["is_correct"]) for r in rows] == [(629, 0)]
    run(t())


def test_21_second_start_in_same_group_is_refused():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=630, n=2, timer=0)
            u = h.f.user(630)
            await group_start(h, u, qid)
            await h.wait(lambda: len(gpolls(h, G1)) == 1)
            await h.cb(h.f.user(631), f"g:go:{qid}", chat_id=G1)
            alerts = [p for a, p in h.api.calls if a == "answerCallbackQuery"]
            assert "पहले से एक quiz चल रहा है" in alerts[-1].get("text", "")
            with db.connect() as con:
                assert con.execute("SELECT COUNT(*) FROM group_sessions WHERE chat_id=?", (G1,)).fetchone()[0] == 1
            await h.text(u, "/stop", chat_id=G1)
    run(t())


def test_22_stop_only_by_starter_or_admin():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=632, n=3, timer=0)
            starter, rando, admin = h.f.user(632), h.f.user(633), h.f.user(634)
            await group_start(h, starter, qid)
            await h.wait(lambda: len(gpolls(h, G1)) == 1)
            await h.text(rando, "/stop", chat_id=G1)
            assert "⛔" in last(h, G1)["text"] and session_of(G1)["status"] == "active"
            h.api.members[(str(G1), 634)] = "administrator"
            await h.text(admin, "/stop", chat_id=G1)
            assert "Quiz stopped — partial result" in h.all_text(G1)
            assert session_of(G1)["status"] == "stopped"
            await group_start(h, starter, qid)
            await h.wait(lambda: len(gpolls(h, G1)) == 2)
            await h.text(starter, "/stop@TestQuizBot", chat_id=G1)
            assert session_of(G1)["status"] == "stopped"
            await h.text(starter, "/stop", chat_id=G1)
            assert "कोई quiz नहीं चल रहा" in last(h, G1)["text"]
    run(t())


def test_23_shuffled_options_map_to_the_right_answer():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=635, n=4, timer=0, so=1)
            s, a = h.f.user(635), h.f.user(636)
            await group_start(h, s, qid)
            for i in range(4):
                await h.wait(lambda: len(gpolls(h, G1)) == i + 1, timeout=6)
                await answer(h, a, G1, correct=True)
            await h.wait(lambda: "🏆" in h.all_text(G1), timeout=6)
            qs = {q["id"]: q for q in db.get_questions(qid)}
            for r in db.get_group_answers(session_of(G1)["id"]):
                assert r["is_correct"] == 1 and r["chosen_index"] == qs[r["question_id"]]["correct_index"]
                poll = h.api.polls[r["poll_id"]]
                # the option text the user tapped is the stored original option
                assert poll["options"][r["chosen_display"]] == qs[r["question_id"]]["options"][r["chosen_index"]]
    run(t())


def test_24_shuffled_questions_each_asked_exactly_once():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=637, n=5, timer=0, sq=1)
            await group_start(h, h.f.user(637), qid)
            await h.wait(lambda: "🏆" in h.all_text(G1), timeout=12)
            texts = [p["question"].split("] ", 1)[1] for _, p in gpolls(h, G1)]
            assert sorted(texts) == sorted(q["question"] for q in db.get_questions(qid))
    run(t())


def test_25_late_answers_ignored_and_session_recovers_after_restart():
    async def t():
        qid = make_quiz(owner=638, n=2, timer=0)
        async with Harness() as h1:
            s = h1.f.user(638)
            await group_start(h1, s, qid)
            await h1.wait(lambda: len(gpolls(h1, G1)) == 1)
            first_pid = gpolls(h1, G1)[0][0]
        # restart: a brand-new Application on the same database
        async with Harness() as h2:
            late = h2.f.user(639)
            await h2.wait(lambda: len(gpolls(h2, G1)) == 1, timeout=6)   # timer re-armed → next question
            await h2.send(h2.f.poll_answer(late, first_pid, 0))            # answer to the closed poll
            await answer(h2, late, G1, correct=True)
            await h2.wait(lambda: "🏆" in h2.all_text(G1), timeout=6)
            rows = db.get_group_answers(session_of(G1)["id"])
            assert [(r["poll_id"], r["is_correct"]) for r in rows] == [(gpolls(h2, G1)[0][0], 1)]
            await h2.send(h2.f.poll_answer(late, gpolls(h2, G1)[0][0], 1))   # after finish → ignored
            assert len(db.get_group_answers(session_of(G1)["id"])) == 1
    run(t())


# ============================================================ forwarded content (26–35)
async def fw_to_new_quiz(h, u, first_update, title):
    await h.send(first_update)
    assert last(h, u["id"])["text"].startswith("📚 यह Question किस Quiz में जोड़ना है?")
    await h.cb(u, "f:new")
    await h.text(u, title)


def test_26_forwarded_quiz_poll_with_answer_and_explanation():
    async def t():
        async with Harness() as h:
            u = h.f.user(701)
            up = h.f.user_poll(u, "भारत की राजधानी?", ["मुंबई", "नई दिल्ली", "कोलकाता"], correct_ids=[1],
                               explanation="1911 से", forward_from_chat="gk_channel")
            await fw_to_new_quiz(h, u, up, "Forward GK")
            qid, qs = qs_of(701)
            assert len(qs) == 1 and qs[0]["question"] == "भारत की राजधानी?"
            assert qs[0]["options"] == ["मुंबई", "नई दिल्ली", "कोलकाता"] and qs[0]["correct_index"] == 1
            assert qs[0]["explanation"] == "1911 से"
            with db.connect() as con:
                ref = con.execute("SELECT source_ref FROM questions WHERE id=?", (qs[0]["id"],)).fetchone()[0]
            assert ref.startswith("forward:@gk_channel/")
    run(t())


def test_27_forwarded_poll_without_answer_asks_and_never_guesses():
    async def t():
        async with Harness() as h:
            u = h.f.user(702)
            up = h.f.user_poll(u, "छिपा उत्तर?", ["a", "b", "c", "d"], correct=None, forward_from_chat="ch")
            await fw_to_new_quiz(h, u, up, "Hidden")
            assert ("⚠️ इस Forwarded Quiz Poll का सही उत्तर Telegram से उपलब्ध नहीं है।\n\n"
                    "कृपया सही Option चुनें।") in h.all_text(702)
            assert db.get_state(702)[0] == "c_correct"
            qid = quizzes_of(702)[0]["id"]
            assert db.count_questions(qid) == 0                     # nothing saved yet
            await h.cb(u, "c:correct:2")
            qs = db.get_questions(qid)
            assert len(qs) == 1 and qs[0]["correct_index"] == 2
            assert "✅ Question imported" in h.all_text(702)
    run(t())


def test_28_legacy_scalar_multi_correct_and_multi_answer_polls():
    async def t():
        async with Harness() as h:
            u = h.f.user(703)
            await h.text(u, "/newquiz")
            await h.text(u, "Mixed")
            await h.text(u, "/skip")
            await h.send(h.f.user_poll(u, "old api?", ["x", "y"], correct=1, legacy_scalar=True,
                                       forward_from_chat="c"))
            await h.send(h.f.user_poll(u, "two right?", ["x", "y", "z"], correct_ids=[0, 2],
                                       forward_from_chat="c"))
            assert "एक से अधिक सही उत्तर" in h.all_text(703)
            await h.send(h.f.user_poll(u, "multi?", ["x", "y"], quiz=False, multiple=True))
            assert "एक से अधिक उत्तर चुने जा सकते हैं" in h.all_text(703)
            qid, qs = qs_of(703)
            assert [(q["question"], q["correct_index"]) for q in qs] == [("old api?", 1)]
    run(t())


HI_MCQ = """प्रश्न 12. राजस्थान का राज्य पक्षी कौन-सा है?
(A) मोर
(B) गोडावण
(C) तोता
(D) कबूतर
उत्तर: (B)
व्याख्या: गोडावण (Great Indian Bustard) 1981 में राज्य पक्षी घोषित हुआ।"""


def test_29_forwarded_hindi_text_mcq_source_preserved():
    async def t():
        async with Harness() as h:
            u = h.f.user(704)
            await fw_to_new_quiz(h, u, h.f.forwarded_text(u, HI_MCQ), "Hindi")
            qid, qs = qs_of(704)
            assert qs[0]["question"] == "राजस्थान का राज्य पक्षी कौन-सा है?"
            assert qs[0]["options"] == ["मोर", "गोडावण", "तोता", "कबूतर"] and qs[0]["correct_index"] == 1
            assert qs[0]["explanation"] == "गोडावण (Great Indian Bustard) 1981 में राज्य पक्षी घोषित हुआ।"
            with db.connect() as con:
                assert con.execute("SELECT source_ref FROM questions").fetchone()[0].startswith("forward:@mcq_channel")
    run(t())


def test_30_forwarded_english_formats_and_missing_answer_is_asked():
    async def t():
        async with Harness() as h:
            u = h.f.user(705)
            en = "Q1. Which planet is called the Red Planet?\na) Venus\nb) Mars\nc) Jupiter\nd) Saturn\nAnswer: b"
            await fw_to_new_quiz(h, u, h.f.forwarded_text(u, en), "English")
            no_ans = "Which gas do plants absorb?\n(A) Oxygen\n(B) Carbon dioxide\n(C) Nitrogen\n(D) Helium"
            await h.send(h.f.forwarded_text(u, no_ans))
            assert db.get_state(705)[0] == "c_correct" and "सही उत्तर कौन-सा है" in h.all_text(705)
            await h.cb(u, "c:correct:1")
            qid, qs = qs_of(705)
            assert [(q["question"], q["correct_index"]) for q in qs] == [
                ("Which planet is called the Red Planet?", 1), ("Which gas do plants absorb?", 1)]
            assert qs[0]["options"] == ["Venus", "Mars", "Jupiter", "Saturn"]
    run(t())


def test_31_seventeen_forwards_make_one_quiz_in_order():
    async def t():
        async with Harness() as h:
            u = h.f.user(706)
            ups = []
            for i in range(17):
                if i % 2 == 0:
                    ups.append(h.f.user_poll(u, f"प्रश्न-{i + 1}?", ["क", "ख", "ग", "घ"], correct_ids=[i % 4],
                                             forward_from_chat="src"))
                else:
                    ups.append(h.f.forwarded_text(u, f"{i + 1}. सवाल-{i + 1}?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: C"))
            for up in ups:                                           # burst before choosing the quiz
                await h.send(up)
            assert "📥 Queue में जोड़ा गया (कुल 16)" in h.all_text(706)
            await h.cb(u, "f:new")
            await h.text(u, "सत्रह प्रश्न")
            await h.text(u, "/done")
            qid, qs = qs_of(706)
            assert len(quizzes_of(706)) == 1 and len(qs) == 17
            expected = [f"प्रश्न-{i + 1}?" if i % 2 == 0 else f"सवाल-{i + 1}?" for i in range(17)]
            assert [q["question"] for q in qs] == expected
            assert "📝 Questions: 17" in h.all_text(706)
    run(t())


def test_32_choose_new_or_existing_quiz_with_ownership_check():
    async def t():
        async with Harness() as h:
            u = h.f.user(707)
            mine = make_quiz(owner=707, n=2, title="Mine")
            theirs = make_quiz(owner=708, n=1, title="Theirs")
            await h.send(h.f.forwarded_text(u, HI_MCQ))
            b = buttons(last(h, 707)["reply_markup"])
            assert [x["text"] for x in b][:2] == ["➕ New Quiz", "📚 Existing Quiz"]
            await h.cb(u, "f:exist")
            assert f"f:pick:{mine}" in str(h.api.calls[-1]) and theirs not in str(h.api.calls[-1])
            await h.cb(u, f"f:pick:{theirs}")                       # forged → refused
            assert db.count_questions(theirs) == 1
            await h.cb(u, f"f:pick:{mine}")
            assert db.count_questions(mine) == 3
            await h.text(u, "/done")
            assert "✅ Quiz तैयार है!" in h.all_text(707)
    run(t())


def test_33_import_confirmation_text_after_each_question():
    async def t():
        async with Harness() as h:
            u = h.f.user(709)
            up = h.f.user_poll(u, "q1?", ["a", "b"], correct_ids=[0], forward_from_chat="c")
            await fw_to_new_quiz(h, u, up, "Confirm")
            await h.send(h.f.user_poll(u, "q2?", ["a", "b"], correct_ids=[1], forward_from_chat="c"))
            texts = h.api.texts(709)
            assert any(t.startswith("✅ Question imported\n\n📚 Quiz: Confirm\n📝 Total Questions: 1") for t in texts)
            assert any(t.startswith("✅ Question imported\n\n📚 Quiz: Confirm\n📝 Total Questions: 2") for t in texts)
    run(t())


def test_34_done_shows_ready_card_with_actions():
    async def t():
        async with Harness() as h:
            u = h.f.user(710)
            up = h.f.user_poll(u, "q?", ["a", "b"], correct_ids=[0], forward_from_chat="c")
            await fw_to_new_quiz(h, u, up, "Ready Card")
            await h.text(u, "/done")
            assert "✅ Quiz तैयार है!" in h.all_text(710)
            card = last(h, 710)
            assert card["text"].startswith("📚 <b>Ready Card</b>") and "📝 Questions: 1" in card["text"]
            lb = labels(card["reply_markup"])
            for want in ("▶️ Start Quiz", "📤 Share Quiz", "✏️ Edit Quiz", "📊 Statistics", "🗑️ Delete Quiz"):
                assert want in lb
            assert db.get_quiz(quizzes_of(710)[0]["id"])["status"] == "ready"
    run(t())


def test_35_queue_cancel_errors_and_non_questions_never_dropped_silently():
    async def t():
        async with Harness() as h:
            u = h.f.user(711)
            up = h.f.user_poll(u, "hidden?", ["a", "b"], correct=None, forward_from_chat="c")
            await fw_to_new_quiz(h, u, up, "Queue")
            await h.send(h.f.user_poll(u, "next1?", ["a", "b"], correct_ids=[1], forward_from_chat="c"))
            await h.send(h.f.forwarded_text(u, "next2?\n(A) x\n(B) y\nउत्तर: A"))
            assert "📥 Queue में जोड़ा गया (कुल 2)" in h.all_text(711)
            await h.cb(u, "c:correct:0")
            qid, qs = qs_of(711)
            assert [q["question"] for q in qs] == ["hidden?", "next1?", "next2?"]
            # a broken multi-question message: nothing saved, problem reported with numbers
            bad = "1. first?\n(A) a\n(B) b\nउत्तर: A\n\n2. second?\n(A) a\n(B) b"
            await h.send(h.f.forwarded_text(u, bad))
            assert db.count_questions(qid) == 3 and "parse नहीं हुए" in h.all_text(711)
            await h.text(u, "/done")
            # a forwarded chat message that is not a question does not start an import
            v = h.f.user(712)
            await h.send(h.f.forwarded_text(v, "सुप्रभात दोस्तों"))
            assert db.get_state(712)[0] is None and "menu" in last(h, 712)["text"]
            # /cancel while choosing the quiz saves nothing
            await h.send(h.f.forwarded_text(v, HI_MCQ))
            await h.text(v, "/cancel")
            assert quizzes_of(712) == [] and db.get_state(712)[0] is None
    run(t())


# ============================================================ tag (36–42)
def test_36_tag_added_from_central_config():
    q = {"question": "राजधानी?", "options": ["a", "b"], "correct_index": 0}
    p = engine.build_poll_payload(q, [0, 1], 0, 5)
    assert p.question == f"{TAG}\n\n[1/5] राजधानी?"
    config.QUESTION_TAG = "Custom Tag"
    assert engine.build_poll_payload(q, [0, 1], 0, 5).question == "Custom Tag\n\n[1/5] राजधानी?"
    config.QUESTION_TAG = ""
    assert engine.build_poll_payload(q, [0, 1], 0, 5).question == "[1/5] राजधानी?"
    # the literal tag lives only in config.py
    hits = [f.name for f in ROOT.glob("*.py") if TAG in f.read_text(encoding="utf-8")]
    assert hits == ["config.py"]


def test_37_tag_never_duplicated():
    q = {"question": f"{TAG}\n\nपहले से टैग वाला प्रश्न?", "options": ["a", "b"], "correct_index": 1}
    p = engine.build_poll_payload(q, [0, 1], 0, 1)
    assert p.question.count(TAG) == 1
    assert engine.tag_text(engine.tag_text("x")) == engine.tag_text("x") == f"{TAG}\n\nx"
    long_q = dict(q, question=f"{TAG} " + "लंबा प्रश्न " * 60)
    lp = engine.build_poll_payload(long_q, [0, 1], 0, 1)
    assert "".join(lp.full_text_chunks).count(TAG) == 1 and lp.question.count(TAG) == 1


def test_38_pdf_imported_questions_are_tagged_when_sent(tmp_path):
    from test_parser import FONT, _make_pdf
    if not FONT.exists():
        pytest.skip("font missing")

    async def t():
        async with Harness() as h:
            u = h.f.user(801)
            pdf = tmp_path / "Tag.pdf"
            _make_pdf(pdf, (FIX / "all_formats_hi.txt").read_text(encoding="utf-8"))
            h.api.files["P1"] = pdf.read_bytes()
            await h.send(h.f.document(u, "P1", "Tag.pdf", "application/pdf", len(h.api.files["P1"])))
            await h.cb(u, "i:create")
            await h.cb(u, "i:usedefault")
            qid = quizzes_of(801)[0]["id"]
            assert all(TAG not in q["question"] for q in db.get_questions(qid))   # source stored untouched
            await h.start(u, qid)
            for i in range(6):
                await h.wait(lambda: len(h.polls_for(801)) == i + 1)
                await h.answer_current(u, True)
            for _, p in h.polls_for(801):
                assert p["question"].startswith(TAG + "\n\n") and p["question"].count(TAG) == 1
            assert h.api.rejections == []
    run(t())


def test_39_forwarded_questions_tagged_once_when_played():
    async def t():
        async with Harness() as h:
            u = h.f.user(802)
            up = h.f.user_poll(u, f"{TAG}\n\nटैग सहित forwarded?", ["a", "b"], correct_ids=[0],
                               forward_from_chat="c")
            await fw_to_new_quiz(h, u, up, "Fwd Tag")
            await h.send(h.f.forwarded_text(u, "दूसरा?\n(A) a\n(B) b\nउत्तर: B"))
            await h.text(u, "/done")
            qid = quizzes_of(802)[0]["id"]
            await h.start(u, qid)
            for i in range(2):
                await h.wait(lambda: len(h.polls_for(802)) == i + 1)
                await h.answer_current(u, True)
            qs = [p["question"] for _, p in h.polls_for(802)]
            assert all(q.startswith(TAG + "\n\n") and q.count(TAG) == 1 for q in qs)
    run(t())


def test_40_group_polls_are_tagged():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=803, n=2, timer=0)
            await group_start(h, h.f.user(803), qid)
            await h.wait(lambda: "🏆" in h.all_text(G1), timeout=6)
            assert all(p["question"].startswith(f"{TAG}\n\n[") for _, p in gpolls(h, G1))
    run(t())


def test_41_private_manual_questions_are_tagged():
    async def t():
        async with Harness() as h:
            u = h.f.user(804)
            await h.text(u, "/newquiz")
            await h.text(u, "Manual")
            await h.text(u, "/skip")
            await h.text(u, "हाथ से लिखा प्रश्न?\n(A) एक\n(B) दो\nउत्तर: A")
            await h.text(u, "/done")
            qid = quizzes_of(804)[0]["id"]
            assert db.get_questions(qid)[0]["question"] == "हाथ से लिखा प्रश्न?"
            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(804)) == 1)
            assert h.polls_for(804)[0][1]["question"] == f"{TAG}\n\n[1/1] हाथ से लिखा प्रश्न?"
    run(t())


def test_42_long_question_plus_tag_full_text_and_exact_mapping():
    async def t():
        async with Harness() as h:
            u = h.f.user(805)
            # fits in 300 on its own (290), but not together with the tag + number
            body = ("क" * 280) + " अंत शब्द?"
            assert engine.tg_len(body) <= 300 < engine.tg_len(f"{TAG}\n\n" + body)
            qid = db.new_quiz(805, "Long", "", timer=0)
            db.add_question(qid, body, ["पहला", "दूसरा", "तीसरा", "चौथा"], 2, "")
            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(805)) == 1)
            full = "\n".join(h.api.texts(805))
            assert body in full and full.count(TAG) == 1              # complete source text, never truncated
            pid, poll = h.polls_for(805)[0]
            assert poll["question"].startswith(TAG) and engine.tg_len(poll["question"]) <= 300
            assert poll["options"][poll["correct_option_id"]] == "तीसरा"
            await h.answer_current(u, True)
            await h.wait(lambda: "🏁" in h.all_text(805))
            assert "Correct: 1" in h.all_text(805)
            assert h.api.rejections == []
    run(t())


# ============================================================ regression (43–50)
def test_43_regression_text_pdf_import(tmp_path):
    from test_parser import FONT, _make_pdf
    if not FONT.exists():
        pytest.skip("font missing")
    pdf = tmp_path / "r.pdf"
    _make_pdf(pdf, (FIX / "all_formats_hi.txt").read_text(encoding="utf-8"))
    res = P.parse_pdf_result(pdf)
    assert res.errors == [] and [q.number for q in res.questions] == [1, 2, 3, 4, 5, 6]


def test_44_regression_ocr_scanned_pdf(tmp_path):
    import pdf_extract as X
    ok, info = X.ocr_status()
    if not ok:
        pytest.skip(f"OCR data not available: {info}")
    import pdfgen as G
    from test_round2 import SCAN_TEXT
    pdf = tmp_path / "scan.pdf"
    G.make_scanned_pdf(pdf, SCAN_TEXT, dpi=300)
    res = P.parse_pdf_result(pdf)
    assert res.ocr_pages == [1] and res.errors == [] and len(res.questions) >= 3


def test_45_regression_hindi_statement_question():
    res = P.parse_source((FIX / "all_formats_hi.txt").read_text(encoding="utf-8"))
    q = {x.number: x for x in res.questions}[1]
    assert q.qtype == "statement" and "4. इसका गठन 1 नवंबर 1956 को पूर्ण हुआ।" in q.question
    assert q.options[0] == "केवल 1, 2 और 4" and q.explanation.startswith("गुरु शिखर")
    # the same statement question typed/forwarded as one message stays ONE question
    one = engine.parse_forwarded_text("निम्नलिखित कथनों पर विचार कीजिए—\n1. कथन एक\n2. कथन दो\n"
                                      "सही कथन चुनें?\n(A) केवल 1\n(B) केवल 2\n(C) दोनों\n(D) कोई नहीं\nउत्तर: C")
    assert len(one) == 1 and "2. कथन दो" in one[0]["question"] and one[0]["correct"] == 2


def test_46_regression_matching_with_koot():
    q = {x.number: x for x in P.parse_source((FIX / "all_formats_hi.txt").read_text(encoding="utf-8")).questions}[2]
    assert "(A) रामदेवजी" in q.question and q.question.rstrip().endswith("कूट:")
    assert q.options[0] == "A-III, B-II, C-IV, D-I" and q.correct_index == 0


def test_47_regression_assertion_reason():
    q = {x.number: x for x in P.parse_source((FIX / "all_formats_hi.txt").read_text(encoding="utf-8")).questions}[3]
    assert q.qtype == "assertion_reason"
    assert q.options[0] == "(A) और (R) दोनों सही हैं तथा (R), (A) की सही व्याख्या है।"


def test_48_regression_ordering():
    q = {x.number: x for x in P.parse_source((FIX / "all_formats_hi.txt").read_text(encoding="utf-8")).questions}[4]
    assert "(C) उत्तर प्रदेश" in q.question
    assert q.options == ["A, C, D, B", "A, D, C, B", "C, A, D, B", "A, C, B, D"]


def test_49_regression_list_i_list_ii():
    q = {x.number: x for x in P.parse_source((FIX / "all_formats_hi.txt").read_text(encoding="utf-8")).questions}[6]
    assert q.qtype == "list_matching" and "4. जयपुर" in q.question and q.options[1] == "A-2, B-1, C-4, D-3"


def test_50_regression_railway_startup_single_poller_and_handlers(tmp_path):
    from telegram import Update
    from telegram.ext import ChatMemberHandler, TypeHandler
    from fake_telegram import FakeTelegram
    app = botmod.build_application(TOKEN, request=FakeTelegram(), get_updates_request=FakeTelegram(),
                                   rate_limit=False)
    assert any(isinstance(x, TypeHandler) for x in app.handlers[-1])            # join gate runs first
    assert any(isinstance(x, ChatMemberHandler) for x in app.handlers[0])
    assert "chat_member" in Update.ALL_TYPES and "poll_answer" in Update.ALL_TYPES
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert src.count("run_polling(") == 1 and "allowed_updates=Update.ALL_TYPES" in src
    assert (ROOT / "requirements.txt").read_text().startswith("python-telegram-bot[job-queue,rate-limiter]==22.8")
    # missing token → clean exit, token never printed
    env = {k: v for k, v in os.environ.items() if k not in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")}
    env["DB_PATH"] = str(tmp_path / "x.db")
    env["LOCK_PATH"] = str(tmp_path / "x.lock")
    out = subprocess.run([sys.executable, "bot.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 1 and "BOT_TOKEN" in (out.stdout + out.stderr)
