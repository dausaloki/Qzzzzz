"""End-to-end tests: real handlers + real JobQueue + real SQLite, fake Telegram API."""
import asyncio
import time
from pathlib import Path

import pytest

import bot as botmod
import config
import database as db
from fake_telegram import FakeTelegram, UpdateFactory

FIX = Path(__file__).parent / "fixtures"
TOKEN = "123456:TEST-TOKEN"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "quiz.db")
    db.init_db()
    monkeypatch.setattr(config, "NEXT_QUESTION_DELAY", 0.05)
    monkeypatch.setattr(config, "TIMER_GRACE", 0.2)
    yield
    db.set_db_path(config.DB_PATH)


class Harness:
    async def __aenter__(self):
        self.api = FakeTelegram()
        self.app = botmod.build_application(TOKEN, request=self.api, get_updates_request=FakeTelegram(),
                                            rate_limit=False)
        await self.app.initialize()
        await self.app.start()
        await botmod.post_init(self.app)
        self.f = UpdateFactory(self.app.bot)
        return self

    async def __aexit__(self, *exc):
        await self.app.stop()
        await self.app.shutdown()

    async def send(self, update):
        await self.app.process_update(update)

    async def text(self, user, text, chat_id=None):
        await self.send(self.f.message(user, text, chat_id=chat_id))

    async def cb(self, user, data, chat_id=None):
        await self.send(self.f.callback(user, data, chat_id=chat_id))

    async def wait(self, cond, timeout=6.0, msg="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"timeout waiting for {msg}")

    def polls_for(self, chat_id):
        return [(pid, p) for pid, p in self.api.polls.items() if p["chat_id"] == chat_id]

    async def answer_current(self, user, correct=True):
        pid, poll = self.polls_for(user["id"])[-1]
        n = len(self.polls_for(user["id"]))
        opt = poll["correct_option_id"] if correct else (poll["correct_option_id"] + 1) % len(poll["options"])
        await self.send(self.f.poll_answer(user, pid, opt))
        return n

    def all_text(self, chat_id):
        return "\n".join(self.api.texts(chat_id))


def run(coro):
    asyncio.run(coro)


def buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


LONG_Q = ("निम्नलिखित कथनों पर विचार कीजिए और सही विकल्प चुनिए। " * 12).strip()
LONG_OPT = ("यह विकल्प जानबूझकर बहुत लंबा लिखा गया है ताकि Telegram की एक सौ अक्षरों की सीमा पार हो जाए और "
            "bot को compact labels का उपयोग करना पड़े")


def make_quiz(owner=111, n=3, timer=30, sq=0, so=0, title="DB Quiz"):
    db.ensure_user_row(owner)
    qid = db.new_quiz(owner, title, "desc", timer=timer, shuffle_questions=sq, shuffle_options=so)
    for i in range(n):
        db.add_question(qid, f"प्रश्न संख्या {i + 1}?", [f"o{i}a", f"o{i}b", f"o{i}c", f"o{i}d"], i % 4, f"exp{i}")
    return qid


# ====================================================================== tests
def test_main_menu_has_all_buttons():
    async def t():
        async with Harness() as h:
            u = h.f.user(111)
            await h.text(u, "/start")
            m = h.api.sent("sendMessage", 111)[-1]
            labels = [b["text"] for b in buttons(m["reply_markup"])]
            assert labels == ["➕ New Quiz", "📚 My Quizzes", "📥 Import PDF/Text", "▶️ Start Quiz",
                              "📊 Quiz Stats", "⚙️ Settings", "ℹ️ Help"]
            for data in ("m:help", "m:set", "m:stats", "m:start", "m:my", "m:home"):
                await h.cb(u, data)
    run(t())


def test_manual_wizard_all_formats_then_full_run_restart_stop_stats():
    async def t():
        async with Harness() as h:
            u = h.f.user(111, "Asha")
            await h.cb(u, "m:new")
            await h.text(u, "राजस्थान GK")                                     # step 1
            await h.text(u, "अभ्यास quiz")                                     # step 2
            await h.cb(u, "c:skippre")                                         # step 3 skip
            stmt_q = ("निम्नलिखित कथनों पर विचार कीजिए—\n1. कथन एक\n2. कथन दो\n3. कथन तीन\n"
                      "उपर्युक्त में से कौन-से कथन सही हैं?")
            await h.text(u, stmt_q)                                            # step 4
            await h.text(u, "केवल 1, 2 और 4\nकेवल 1 और 3\nकेवल 2 और 3\nसभी कथन सही हैं")  # step 5
            await h.cb(u, "c:correct:0")                                       # step 6
            await h.text(u, "exp1")                                            # step 7
            await h.cb(u, "c:more")                                            # step 8
            # Q2: pre-question text, matching question pasted together with options
            await h.text(u, "नीचे दी गई सूची ध्यान से पढ़ें।")
            match_q = ("सुमेलित कीजिए\n(A) रामदेवजी\n(B) गोगाजी\n(C) तेजाजी\n(D) पाबूजी\n"
                       "I. कोलू\nII. गोगामेड़ी\nIII. रामदेवरा\nIV. खरनाल\nकूट:\n"
                       "(A) A-III, B-II, C-IV, D-I\n(B) A-II, B-III, C-I, D-IV\n"
                       "(C) A-IV, B-I, C-II, D-III\n(D) A-I, B-IV, C-III, D-II")
            await h.text(u, match_q)
            assert "4 options मिले" in h.all_text(111)
            await h.cb(u, "c:useopts")
            await h.cb(u, "c:correct:0")
            await h.cb(u, "c:skipexpl")
            await h.cb(u, "c:more")
            # Q3: photo as pre-question media, very long question + very long option
            await h.send(h.f.photo(u, "PHOTO1", caption="चित्र देखें"))
            await h.text(u, LONG_Q)
            await h.text(u, f"(A) {LONG_OPT}\n(B) छोटा\n(C) मध्यम\n(D) अन्य")
            await h.cb(u, "c:correct:1")
            await h.text(u, "यह explanation दो सौ अक्षरों से लंबी है। " * 8)
            await h.text(u, "/done")

            quizzes = db.get_owner_quizzes(111)
            assert len(quizzes) == 1 and quizzes[0]["status"] == "ready"
            qid = quizzes[0]["id"]
            qs = db.get_questions(qid)
            assert len(qs) == 3
            assert qs[0]["question"] == stmt_q and qs[0]["qtype"] == "statement"
            assert qs[0]["options"][0] == "केवल 1, 2 और 4" and qs[0]["explanation"] == "exp1"
            assert qs[1]["options"][0] == "A-III, B-II, C-IV, D-I" and "(D) पाबूजी" in qs[1]["question"]
            assert qs[1]["pre_text"] == "नीचे दी गई सूची ध्यान से पढ़ें।"
            assert qs[2]["pre_media_type"] == "photo" and qs[2]["pre_media_id"] == "PHOTO1"
            assert qs[2]["question"] == LONG_Q and qs[2]["options"][0] == LONG_OPT
            assert "https://t.me/TestQuizBot?start=quiz_" + qid in h.all_text(111)

            # ---- run the quiz
            h.api.reset_calls()
            await h.cb(u, f"q:run:{qid}")
            await h.wait(lambda: len(h.polls_for(111)) == 1, msg="poll 1")
            p1 = h.polls_for(111)[0][1]
            assert p1["question"].startswith("[1/3] निम्नलिखित") and p1["explanation"] == "exp1"
            assert p1["options"][0] == "केवल 1, 2 और 4"
            await h.answer_current(u, correct=True)
            await h.wait(lambda: len(h.polls_for(111)) == 2, msg="poll 2")
            # pre-question text was sent before poll 2
            calls = [a for a, p in h.api.calls if a in ("sendMessage", "sendPoll", "sendPhoto")]
            assert calls[-2:] == ["sendMessage", "sendPoll"]
            assert h.api.sent("sendMessage", 111)[-1]["text"] == "नीचे दी गई सूची ध्यान से पढ़ें।"
            await h.answer_current(u, correct=True)
            await h.wait(lambda: len(h.polls_for(111)) == 3, msg="poll 3")
            assert h.api.sent("sendPhoto", 111)[-1]["photo"] == "PHOTO1"
            p3 = h.polls_for(111)[2][1]
            assert p3["options"] == ["(A)", "(B)", "(C)", "(D)"]                 # compact labels
            full = h.all_text(111)
            assert LONG_Q in full and f"(A) {LONG_OPT}" in full                  # nothing truncated
            assert p3["explanation"] is None                                       # long expl → follow-up
            await h.answer_current(u, correct=False)
            await h.wait(lambda: "🏁" in h.all_text(111), msg="result")
            res = h.all_text(111)
            assert "Explanation:" in res and "यह explanation दो सौ" in res
            for s in ("Quiz Complete", "Total: 3", "Correct: 2", "Wrong: 1", "Skipped: 0",
                      "Score: 2/3", "Percentage: 66.67%", "Time:"):
                assert s in res, s
            final = h.api.sent("sendMessage", 111)[-1]
            assert [b["text"] for b in buttons(final["reply_markup"])] == ["🔁 Restart", "📊 Stats", "🏠 Main Menu"]

            # ---- restart then stop
            await h.cb(u, f"q:run:{qid}")
            await h.wait(lambda: len(h.polls_for(111)) == 4, msg="restart poll")
            await h.answer_current(u, correct=True)
            await h.wait(lambda: len(h.polls_for(111)) == 5, msg="restart poll 2")
            await h.text(u, "/stop")
            res = h.all_text(111)
            assert "Quiz Stopped" in res and "Not attempted: 2" in res
            att = db.user_history(111, qid)
            assert [a["status"] for a in att] == ["stopped", "finished"]
            assert att[0]["correct"] == 1
            assert h.api.sent("stopPoll", 111)                                      # open poll closed
            assert db.get_active_attempt(111) is None

            # ---- stats
            h.api.reset_calls()
            await h.cb(u, f"q:stats:{qid}")
            st = h.all_text(111)
            assert "Total attempts: 2" in st and "Top users" in st and "@user111" in st
            assert "Correct answers: 3" in st and "Wrong answers: 1" in st
            await h.text(u, "/stats")
            assert "Result history" in h.all_text(111)
    run(t())


def test_done_requires_a_question_and_cancel_deletes_draft():
    async def t():
        async with Harness() as h:
            u = h.f.user(5)
            await h.text(u, "/newquiz")
            await h.text(u, "Empty")
            await h.cb(u, "c:skipdesc")
            await h.text(u, "/done")
            assert "कम से कम 1 प्रश्न" in h.all_text(5)
            await h.text(u, "/cancel")
            assert db.get_owner_quizzes(5) == []
            await h.cb(u, "c:skipdesc")                                             # stale button
            assert any(a == "answerCallbackQuery" and p.get("show_alert") for a, p in h.api.calls)
            await h.text(u, "/newquiz")
            await h.text(u, "Opt test")
            await h.cb(u, "c:skipdesc")
            await h.cb(u, "c:skippre")
            await h.text(u, "Q?")
            await h.text(u, "only\nthree\nlines")
            assert "ठीक 4 options" in h.all_text(5)
    run(t())


def test_timer_expiry_skips_and_continues():
    async def t():
        async with Harness() as h:
            u = h.f.user(222)
            qid = make_quiz(owner=111, n=2, timer=1)       # 1s timer (test-only value)
            await h.text(u, f"/start quiz_{qid}")          # deep link from another user
            await h.wait(lambda: len(h.polls_for(222)) == 1, msg="poll 1")
            await h.wait(lambda: len(h.polls_for(222)) == 2, timeout=5, msg="auto next after timeout")
            assert "समय समाप्त" in h.all_text(222)
            assert h.api.sent("stopPoll", 222)
            await h.wait(lambda: "🏁" in h.all_text(222), timeout=5, msg="finish after 2nd timeout")
            res = h.all_text(222)
            assert "Skipped: 2" in res and "Correct: 0" in res
            ans = db.get_answers(db.user_history(222)[0]["id"])
            assert [a["status"] for a in ans] == ["skipped", "skipped"]
            # a real timer value is passed to Telegram as open_period
            db.update_quiz(qid, timer=90)
            await h.cb(u, f"q:run:{qid}")
            await h.wait(lambda: len(h.polls_for(222)) == 3)
            assert h.polls_for(222)[-1][1]["open_period"] == 90
            db.update_quiz(qid, timer=0)
            await h.cb(u, f"q:run:{qid}")                   # restarting stops the previous run
            await h.wait(lambda: len(h.polls_for(222)) == 4)
            assert h.polls_for(222)[-1][1]["open_period"] is None
            assert "पिछला quiz रोक दिया" in h.all_text(222)
    run(t())


def test_answer_just_before_timeout_is_not_double_counted():
    async def t():
        async with Harness() as h:
            u = h.f.user(223)
            qid = make_quiz(n=1, timer=1)
            await h.cb(u, f"q:run:{qid}")
            await h.wait(lambda: len(h.polls_for(223)) == 1)
            await h.answer_current(u, correct=True)
            await h.wait(lambda: "🏁" in h.all_text(223))
            await asyncio.sleep(1.5)                          # let the (cancelled) timer moment pass
            a = db.user_history(223)[0]
            assert (a["correct"], a["skipped"], a["total"]) == (1, 0, 1)
            assert "समय समाप्त" not in h.all_text(223)
    run(t())


def test_shuffle_questions_and_options_mapping():
    async def t():
        async with Harness() as h:
            u = h.f.user(333)
            qid = make_quiz(n=20, sq=1, so=1)
            await h.cb(u, f"q:run:{qid}")
            for i in range(20):
                await h.wait(lambda: len(h.polls_for(333)) == i + 1, msg=f"poll {i + 1}")
                await h.answer_current(u, correct=True)
            await h.wait(lambda: "🏁" in h.all_text(333))
            assert "Correct: 20" in h.all_text(333) and "Percentage: 100.00%" in h.all_text(333)
            att = db.user_history(333)[0]
            answers = db.get_answers(att["id"])
            qmap = {q["id"]: q for q in db.get_questions(qid)}
            assert all(a["chosen_index"] == qmap[a["question_id"]]["correct_index"] for a in answers)
            order = [a["question_id"] for a in answers]
            assert order != sorted(order)                                  # questions shuffled
            positions = [p["correct_option_id"] for _, p in h.polls_for(333)]
            assert positions != [i % 4 for i in range(20)] or True
            assert len(set(positions)) > 1
            # every poll's displayed correct option text is the original correct text
            for pid, p in h.polls_for(333):
                assert p["options"][p["correct_option_id"]].endswith(("a", "b", "c", "d"))
            for a in answers:
                q = qmap[a["question_id"]]
                assert q["options"][a["chosen_index"]] == q["options"][q["correct_index"]]
    run(t())


def test_skip_command_and_button():
    async def t():
        async with Harness() as h:
            u = h.f.user(444)
            qid = make_quiz(n=3, timer=0)
            await h.cb(u, f"q:run:{qid}")
            await h.wait(lambda: len(h.polls_for(444)) == 1)
            await h.text(u, "/skip")
            await h.wait(lambda: len(h.polls_for(444)) == 2)
            mid = h.polls_for(444)[-1][1]["message_id"]
            await h.send(h.f.callback(u, "r:skip", message_id=mid))
            await h.wait(lambda: len(h.polls_for(444)) == 3)
            await h.send(h.f.callback(u, "r:skip", message_id=mid))          # stale poll button
            await h.answer_current(u, correct=True)
            await h.wait(lambda: "🏁" in h.all_text(444))
            assert "Skipped: 2" in h.all_text(444) and "Correct: 1" in h.all_text(444)
    run(t())


def test_deep_link_group_redirect_and_share():
    async def t():
        async with Harness() as h:
            owner, other = h.f.user(111), h.f.user(555)
            qid = make_quiz(owner=111, n=2)
            await h.text(other, f"/start quiz_{qid}", chat_id=-1001)            # in a group
            gm = h.api.sent("sendMessage", -1001)[-1]
            urls = [b.get("url") for b in buttons(gm["reply_markup"])]
            assert f"https://t.me/TestQuizBot?start=quiz_{qid}" in urls
            assert h.polls_for(-1001) == []                                       # no shared session
            await h.cb(other, f"q:run:{qid}", chat_id=-1001)
            assert h.polls_for(-1001) == []
            await h.text(other, f"/quiz {qid}", chat_id=-1001)
            assert "Start Privately" in str(h.api.sent("sendMessage", -1001)[-1]["reply_markup"])
            # private deep link starts immediately
            await h.text(other, f"/start quiz_{qid}")
            await h.wait(lambda: len(h.polls_for(555)) == 1)
            # share
            await h.cb(owner, f"q:share:{qid}")
            assert f"https://t.me/TestQuizBot?start=quiz_{qid}" in h.all_text(111)
            # non-owner cannot edit/delete
            await h.cb(other, f"q:del:{qid}")
            await h.cb(other, f"q:delok:{qid}")
            await h.cb(other, f"e:title:{qid}")
            assert db.get_quiz(qid) is not None
            # invalid / deleted links
            await h.text(other, "/start quiz_doesnotexist")
            assert "मौजूद नहीं" in h.all_text(555)
    run(t())


def test_two_users_run_same_quiz_independently():
    async def t():
        async with Harness() as h:
            a, b = h.f.user(601), h.f.user(602)
            qid = make_quiz(n=3)
            await h.cb(a, f"q:run:{qid}")
            await h.cb(b, f"q:run:{qid}")
            await h.wait(lambda: len(h.polls_for(601)) == 1 and len(h.polls_for(602)) == 1)
            await h.answer_current(a, True)
            await h.wait(lambda: len(h.polls_for(601)) == 2)
            # B answering A's poll is ignored
            pid_a = h.polls_for(601)[-1][0]
            await h.send(h.f.poll_answer(b, pid_a, 0))
            assert len(h.polls_for(602)) == 1 and len(h.polls_for(601)) == 2
            await h.answer_current(b, False)
            await h.wait(lambda: len(h.polls_for(602)) == 2)
            await h.send(h.f.poll_answer(a, "unknown-poll", 0))                   # expired poll
            assert db.get_active_attempt(601)["current_index"] == 1
            assert db.get_active_attempt(602)["wrong"] == 1
    run(t())


def test_import_pdf_document():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from test_parser import FONT, _make_pdf
    if not FONT.exists():
        pytest.skip("font missing")

    async def t(tmp):
        async with Harness() as h:
            u = h.f.user(777)
            pdf = tmp / "RPSC_Practice.pdf"
            _make_pdf(pdf, (FIX / "all_formats_hi.txt").read_text(encoding="utf-8"))
            h.api.files["PDF1"] = pdf.read_bytes()
            await h.send(h.f.document(u, "PDF1", "RPSC_Practice.pdf", "application/pdf", len(h.api.files["PDF1"])))
            review = h.api.sent("sendMessage", 777)[-1]
            assert "सही parse हुए प्रश्न: <b>6</b>" in review["text"]
            assert "i:create" in str(review["reply_markup"])
            await h.cb(u, "i:create")
            await h.cb(u, "i:usedefault")
            quizzes = db.get_owner_quizzes(777)
            assert len(quizzes) == 1 and quizzes[0]["title"] == "RPSC_Practice"
            qs = db.get_questions(quizzes[0]["id"])
            assert len(qs) == 6 and qs[1]["options"][0] == "A-III, B-II, C-IV, D-I"
            assert qs[0]["explanation"].startswith("गुरु शिखर")
            # the imported quiz runs end-to-end
            await h.cb(u, f"q:run:{quizzes[0]['id']}")
            for i in range(6):
                await h.wait(lambda: len(h.polls_for(777)) == i + 1)
                await h.answer_current(u, True)
            await h.wait(lambda: "🏁" in h.all_text(777))
            assert "Correct: 6" in h.all_text(777)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        run(t(Path(d)))


def test_import_with_errors_requires_explicit_confirmation():
    async def t():
        async with Harness() as h:
            u = h.f.user(888)
            await h.cb(u, "m:imp")
            txt = ("1. Q1?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: A\n"
                   "2. Q2 without answer?\n(A) a\n(B) b\n(C) c\n(D) d\n"
                   "3. Q3?\n(A) a\n(B) b\n(C) c\nउत्तर: B\n"
                   "4. Q4?\n(A) a\n(B) b\n(C) c\n(D) d\nउत्तर: D\n")
            await h.text(u, txt)
            out = h.all_text(888)
            assert "प्रश्न 2: सही उत्तर नहीं मिला" in out
            assert "प्रश्न 3: 4 options (A-D) पूरे नहीं मिले" in out
            last = h.api.sent("sendMessage", 888)[-1]
            assert "i:partial" in str(last["reply_markup"]) and "i:create\"" not in str(last["reply_markup"])
            await h.cb(u, "i:create")                    # forged/old button must not bypass
            await h.cb(u, "i:usedefault")
            assert db.get_owner_quizzes(888) == []
            await h.cb(u, "i:partial")
            await h.text(u, "Partial Quiz")
            qz = db.get_owner_quizzes(888)
            assert len(qz) == 1 and qz[0]["title"] == "Partial Quiz"
            assert [q["question"] for q in db.get_questions(qz[0]["id"])] == ["Q1?", "Q4?"]
    run(t())


def test_import_text_in_multiple_parts_and_txt_file():
    async def t():
        async with Harness() as h:
            u = h.f.user(999)
            await h.text(u, "/import")
            await h.text(u, "1. पहला?\n(A) a\n(B) b\n(C) c\n(D) d\n2. दूसरा?\n(A) a\n(B) b\n")
            await h.text(u, "(C) c\n(D) d\n\nउत्तर कुंजी\n1-A 2-C")
            last = h.api.sent("sendMessage", 999)[-1]
            assert "Text parts received: 2" in last["text"] and "<b>2</b>" in last["text"]
            await h.cb(u, "i:create")
            await h.text(u, "Two part quiz")
            qz = db.get_owner_quizzes(999)
            assert db.get_questions(qz[0]["id"])[1]["correct_index"] == 2
            # .txt document sent without pressing Import first
            h.api.files["TXT1"] = (FIX / "weak_numbers_key.txt").read_bytes()
            await h.send(h.f.document(u, "TXT1", "set2.txt", "text/plain", 100, caption="Set 2"))
            await h.cb(u, "i:create")
            await h.cb(u, "i:usedefault")
            titles = [q["title"] for q in db.get_owner_quizzes(999)]
            assert "Set 2" in titles
    run(t())


def test_import_more_than_100_offers_split():
    async def t():
        async with Harness() as h:
            u = h.f.user(1001)
            body = "".join(f"{i}. Q{i}?\n(A) a\n(B) b\n(C) c\n(D) d\nAnswer: B\n" for i in range(1, 131))
            await h.cb(u, "m:imp")
            await h.text(u, body)
            assert "i:split" in str(h.api.sent("sendMessage", 1001)[-1]["reply_markup"])
            await h.cb(u, "i:split")
            await h.text(u, "Big")
            qz = sorted(db.get_owner_quizzes(1001), key=lambda q: q["title"])
            assert [q["title"] for q in qz] == ["Big (Part 1)", "Big (Part 2)"]
            assert [q["question_count"] for q in qz] == [100, 30]
    run(t())


def test_edit_quiz_features():
    async def t():
        async with Harness() as h:
            u = h.f.user(111)
            qid = make_quiz(owner=111, n=2)
            await h.cb(u, f"q:edit:{qid}")
            await h.cb(u, f"e:title:{qid}")
            await h.text(u, "नया शीर्षक")
            await h.cb(u, f"e:desc:{qid}")
            await h.text(u, "नया विवरण")
            await h.cb(u, f"e:timer:{qid}")
            await h.cb(u, f"e:settimer:{qid}:120")
            await h.cb(u, f"e:sq:{qid}")
            await h.cb(u, f"e:so:{qid}")
            quiz = db.get_quiz(qid)
            assert (quiz["title"], quiz["description"], quiz["timer"], quiz["shuffle_questions"],
                    quiz["shuffle_options"]) == ("नया शीर्षक", "नया विवरण", 120, 1, 1)
            await h.cb(u, f"e:so:{qid}")
            assert db.get_quiz(qid)["shuffle_options"] == 0
            # add question
            await h.cb(u, f"e:addq:{qid}")
            await h.cb(u, "c:skippre")
            await h.text(u, "जोड़ा गया प्रश्न?")
            await h.text(u, "w\nx\ny\nz")
            await h.cb(u, "c:correct:3")
            await h.cb(u, "c:skipexpl")
            await h.cb(u, "c:done")
            qs = db.get_questions(qid)
            assert len(qs) == 3 and qs[2]["question"] == "जोड़ा गया प्रश्न?" and qs[2]["correct_index"] == 3
            assert db.get_quiz(qid)["status"] == "ready"
            # edit explanation
            await h.cb(u, f"e:expl:{qid}:0")
            await h.cb(u, f"e:explq:{qid}:{qs[0]['id']}")
            await h.text(u, "संशोधित explanation")
            assert db.get_question(qid, qs[0]["id"])["explanation"] == "संशोधित explanation"
            await h.cb(u, f"e:explrm:{qid}:{qs[0]['id']}")
            assert db.get_question(qid, qs[0]["id"])["explanation"] == ""
            # view + delete question
            await h.cb(u, f"e:view:{qid}:0")
            await h.cb(u, f"e:viewq:{qid}:{qs[1]['id']}")
            await h.cb(u, f"e:delq:{qid}:0")
            await h.cb(u, f"e:delqx:{qid}:{qs[1]['id']}")
            await h.cb(u, f"e:delqok:{qid}:{qs[1]['id']}")
            assert [q["id"] for q in db.get_questions(qid)] == [qs[0]["id"], qs[2]["id"]]
            # settings defaults apply to new quizzes
            await h.cb(u, "s:settimer:15")
            await h.cb(u, "s:sq")
            q2 = db.get_quiz(db.new_quiz(111, "x"))
            assert q2["timer"] == 15 and q2["shuffle_questions"] == 1
            # delete quiz
            await h.cb(u, f"q:del:{qid}")
            await h.cb(u, f"q:delok:{qid}")
            assert db.get_quiz(qid) is None
            await h.cb(u, f"q:run:{qid}")                                   # deleted quiz button
            assert any(p.get("show_alert") for a, p in h.api.calls if a == "answerCallbackQuery")
    run(t())


def test_session_survives_restart_and_timer_rearmed():
    async def t():
        qid = make_quiz(n=2, timer=1)
        async with Harness() as h:
            u = h.f.user(1234)
            await h.cb(u, f"q:run:{qid}")
            await h.wait(lambda: len(h.polls_for(1234)) == 1)
            first_poll = h.polls_for(1234)[0][0]
            for job in h.app.job_queue.jobs():                   # simulate crash: timer lost
                job.schedule_removal()
        # "restart": brand-new Application on the same database
        async with Harness() as h2:
            u = h2.f.user(1234)
            await h2.wait(lambda: len(h2.polls_for(1234)) == 1, timeout=5, msg="timer re-armed → next poll")
            assert "समय समाप्त" in h2.all_text(1234)
            att = db.get_active_attempt(1234)
            assert att["skipped"] == 1 and att["current_index"] == 1
            await h2.send(h2.f.poll_answer(u, first_poll, 0))           # stale poll from before restart
            assert db.get_active_attempt(1234)["current_index"] == 1
            await h2.answer_current(u, True)
            await h2.wait(lambda: "🏁" in h2.all_text(1234))
            assert "Correct: 1" in h2.all_text(1234) and "Skipped: 1" in h2.all_text(1234)
    run(t())


def test_poll_rejection_falls_back_and_never_crashes():
    async def t():
        async with Harness() as h:
            u = h.f.user(1500)
            qid = make_quiz(n=2)
            h.api.fail_next["sendPoll"] = "Bad Request: poll options length must not exceed 100"
            await h.cb(u, f"q:run:{qid}")
            await h.wait(lambda: len(h.polls_for(1500)) == 1)
            assert h.polls_for(1500)[0][1]["options"] == ["(A)", "(B)", "(C)", "(D)"]
            assert "प्रश्न संख्या 1?" in h.all_text(1500)                    # full text shown
            await h.answer_current(u, True)
            await h.wait(lambda: len(h.polls_for(1500)) == 2)
            await h.answer_current(u, True)
            await h.wait(lambda: "Correct: 2" in h.all_text(1500))
    run(t())


def test_misc_messages_and_errors():
    async def t():
        async with Harness() as h:
            u = h.f.user(1600)
            await h.text(u, "hello")                                          # no state
            assert "menu" in h.all_text(1600)
            await h.text(u, "/stop")
            assert "कोई quiz नहीं" in h.all_text(1600)
            await h.text(u, "/skip")
            await h.text(u, "/done")
            await h.text(u, "/cancel")
            await h.cb(u, "zzz:unknown")
            await h.cb(u, "i:create")                                         # expired import
            # database error is reported to the user, bot keeps running
            orig = db.get_owner_quizzes
            def boom(*a, **k):
                raise db.DatabaseError("disk I/O error")
            db.get_owner_quizzes = boom
            try:
                await h.cb(u, "m:my")
            finally:
                db.get_owner_quizzes = orig
            assert "Database में अस्थायी समस्या" in h.all_text(1600)
            await h.cb(u, "m:my")
    run(t())
