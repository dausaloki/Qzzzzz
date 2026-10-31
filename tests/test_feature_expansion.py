"""Comprehensive automated tests for LOKESH DAUSA Quiz Bot Feature Expansion.

Covers:
* Backward-compatible SQLite schema migration & rollback/backup compatibility
* Feature 1: OpenRouter AI question generation, smart poll/text filtering, JSON import
* Feature 2: Quiz editing & management (mode, negative marking, section timers, access, smart filter)
* Feature 3: Multiple quiz modes (Standard, Sectional, Practice, Exam)
* Feature 4: Interactive HTML result reports (Question Navigator, KaTeX/LaTeX math, Hindi, 1/3 negative marking, section breakdown)
* Feature 6: Quiz sharing & inline query privacy guard for private quizzes
* Feature 7: Membership & Access Control (Free, Batch, Private, Authorized Chats, REQUIRED_SUB_CHANNEL) — no paid/premium tier
* Feature 8 (Razorpay payments / premium) was removed — see tests/test_no_payments.py
* Feature 9: Admin & Owner tools (/broadcast, /admin, LOG_GROUP sanitized error logging, secret redaction)
"""
from __future__ import annotations

import json
import sqlite3

import access_control
import admin_tools
import ai_gen
import backup
import config
import database as db
import membership
import quiz_modes
from tests.test_bot_flow import Harness, buttons, isolated, make_quiz, run

_ = isolated


# ---------------------------------------------------------------------------
# 1. Database Migration & Backup Compatibility
# ---------------------------------------------------------------------------
def test_legacy_sqlite_migration_preserves_all_existing_data(tmp_path):
    """Simulate a pre-expansion SQLite database and verify in-place migration."""
    legacy_db = tmp_path / "legacy_pre_expansion.db"
    con = sqlite3.connect(legacy_db)
    con.executescript(
        """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT ''
        );
        CREATE TABLE quizzes (
            id TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT ''
        );
        CREATE TABLE quiz_settings (
            quiz_id TEXT PRIMARY KEY,
            timer INTEGER DEFAULT 45,
            shuffle_questions INTEGER DEFAULT 0,
            shuffle_options INTEGER DEFAULT 0
        );
        CREATE TABLE questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            question TEXT NOT NULL,
            options_json TEXT NOT NULL,
            correct_index INTEGER NOT NULL,
            explanation TEXT DEFAULT ''
        );
        INSERT INTO users(user_id, username, first_name) VALUES (501, 'lokesh', 'Lokesh');
        INSERT INTO quizzes(id, owner_id, title, description) VALUES ('legacyq01', 501, 'पुरानी क्विज़', 'वर्णमाला');
        INSERT INTO quiz_settings(quiz_id, timer, shuffle_questions, shuffle_options) VALUES ('legacyq01', 45, 0, 0);
        INSERT INTO questions(quiz_id, position, question, options_json, correct_index, explanation)
        VALUES ('legacyq01', 1, 'हिंदी वर्णमाला में कुल कितने वर्ण हैं?', '["44","48","52","56"]', 2, 'कुल 52 वर्ण');
        """
    )
    con.commit()
    con.close()

    db.set_db_path(legacy_db)
    db.init_db()

    quiz = db.get_quiz("legacyq01")
    assert quiz is not None
    assert quiz["title"] == "पुरानी क्विज़"
    assert quiz["quiz_mode"] == "standard"
    assert quiz["access_tier"] == "free"
    assert quiz["timer"] == 45
    qs = db.get_questions("legacyq01")
    assert len(qs) == 1 and qs[0]["correct_index"] == 2

    payload = backup.build_payload()
    assert backup.validate_payload(payload) == []
    assert payload["counts"]["quizzes"] == 1
    assert payload["counts"]["questions"] == 1


# ---------------------------------------------------------------------------
# 2. Feature 1: AI Quiz Generation, Smart Poll Filtering & JSON Import
# ---------------------------------------------------------------------------
def test_ai_generation_key_fallback_and_end_to_end_wizard(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bad-key")
    monkeypatch.setenv("OPENROUTER_DEFAULT_KEYS", "sk-or-good-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

    calls = []

    def fake_post(url, headers, payload):
        calls.append(headers.get("Authorization"))
        if "sk-or-bad-key" in headers.get("Authorization", ""):
            return 429, '{"error": "rate limit"}'
        sample = [
            {
                "question": "राजस्थान का क्षेत्रफल की दृष्टि से भारत में कौन-सा स्थान है?",
                "options": ["प्रथम", "द्वितीय", "तृतीय", "चतुर्थ"],
                "correct_index": 0,
                "explanation": "राजस्थान भारत का सबसे बड़ा राज्य है।",
            },
            {
                "question": "हवा महल कहाँ स्थित है?",
                "options": ["उदयपुर", "जयपुर", "जोधपुर", "बीकानेर"],
                "correct_index": 1,
                "explanation": "हवा महल जयपुर में 1799 में सवाई प्रताप सिंह द्वारा बनवाया गया।",
            },
        ]
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(sample, ensure_ascii=False)}}]})

    qs = ai_gen.generate_ai_questions("राजस्थान सामान्य ज्ञान", 2, http_post=fake_post)
    assert len(calls) == 2
    assert len(qs) == 2
    assert qs[0]["correct_index"] == 0 and qs[1]["correct_index"] == 1

    # End-to-end /aiquiz command in bot
    monkeypatch.setattr(ai_gen, "_default_http_post", fake_post)

    async def t():
        async with Harness() as h:
            u = h.f.user(801, "Creator")
            await h.text(u, "/aiquiz राजस्थान सामान्य ज्ञान 2")
            last = [m for m in h.api.sent("sendMessage", 801) if m.get("reply_markup")][-1]
            cb0 = buttons(last["reply_markup"])[0]["callback_data"]
            await h.cb(u, cb0)
            await h.cb(u, "i:usedefault")
            quizzes = db.get_owner_quizzes(801)
            assert len(quizzes) == 1
            assert quizzes[0]["question_count"] == 2

    run(t())


def test_smart_filter_and_json_file_import():
    async def t():
        async with Harness() as h:
            u = h.f.user(802, "JsonUser")
            json_data = {
                "title": "JSON विज्ञान क्विज़",
                "questions": [
                    {
                        "question": "[1/50] जल का रासायनिक सूत्र क्या है? https://t.me/promo_link @promo_bot",
                        "options": ["H2O", "CO2", "NaCl", "O2"],
                        "correct_index": 0,
                        "explanation": "जल H2O होता है। @promo_bot https://t.me/promo_link",
                    },
                    {
                        "question": "[2/50] बल का SI मात्रक क्या है?",
                        "options": ["जूल", "न्यूटन", "वाट", "पास्कल"],
                        "correct_index": 1,
                        "explanation": "बल का SI मात्रक न्यूटन है।",
                    },
                ],
            }
            raw = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            h.api.files["json_fid_1"] = raw
            await h.send(h.f.document(u, "json_fid_1", "science_quiz.json", "application/json", len(raw)))
            last = [m for m in h.api.sent("sendMessage", 802) if m.get("reply_markup")][-1]
            cb0 = buttons(last["reply_markup"])[0]["callback_data"]
            await h.cb(u, cb0)
            await h.cb(u, "i:usedefault")
            quizzes = db.get_owner_quizzes(802)
            assert len(quizzes) == 1
            qid = quizzes[0]["id"]
            assert quizzes[0]["title"] == "JSON विज्ञान क्विज़"

            # Smart filter confirmation & execution via Edit menu
            await h.cb(u, f"e:clean:{qid}")
            assert "Smart Filter" in h.all_text(802)
            await h.cb(u, f"e:cleanok:{qid}")
            cleaned_qs = db.get_questions(qid)
            assert "[1/50]" not in cleaned_qs[0]["question"]
            assert "https://t.me/promo_link" not in cleaned_qs[0]["question"]
            assert "@promo_bot" not in cleaned_qs[0]["question"]
            assert "जल का रासायनिक सूत्र क्या है?" in cleaned_qs[0]["question"]
            assert "@promo_bot" not in cleaned_qs[0]["explanation"]

    run(t())


# ---------------------------------------------------------------------------
# 3. Feature 3: Multiple Quiz Modes (Standard, Practice, Exam, Sectional)
# ---------------------------------------------------------------------------
def test_practice_mode_sends_immediate_feedback_and_explanation():
    async def t():
        async with Harness() as h:
            u = h.f.user(803, "Learner")
            qid = make_quiz(owner=803, n=2, timer=30, title="अभ्यास मोड")
            await h.text(u, f"/quizmode {qid} practice")
            assert db.get_quiz(qid)["quiz_mode"] == "practice"

            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(803)) == 1, msg="practice poll 1")
            await h.answer_current(u, correct=False)
            await h.wait(lambda: len(h.polls_for(803)) == 2, msg="practice poll 2")
            assert "❌ <b>गलत उत्तर!</b>" in h.all_text(803)
            assert "✔️ <b>सही उत्तर:</b>" in h.all_text(803)
            await h.answer_current(u, correct=True)
            await h.wait(lambda: "🏁" in h.all_text(803), msg="practice finish")
            assert "✅ <b>सही उत्तर!</b>" in h.all_text(803)

    run(t())


def test_exam_mode_hides_answers_during_attempt_and_computes_negative_marking():
    async def t():
        async with Harness() as h:
            u = h.f.user(804, "Candidate")
            qid = make_quiz(owner=804, n=3, timer=30, title="मुख्य परीक्षा")
            # Set Exam Mode and 1/3 negative marking
            await h.cb(u, f"e:setmode:{qid}:exam")
            await h.cb(u, f"e:neg:{qid}")  # cycles 0 -> 1/3
            quiz = db.get_quiz(qid)
            assert quiz["quiz_mode"] == "exam"
            assert abs(float(quiz["negative_mark"]) - 1.0 / 3.0) < 0.01

            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(804)) == 1, msg="exam poll 1")
            pid1, p1 = h.polls_for(804)[0]
            # Exam mode uses non-revealing regular polls with no correct_option_ids exposed
            assert p1["type"] == "regular"
            assert p1.get("correct_option_ids") is None

            # make_quiz sets correct_index = i % 4 (0, 1, 2)
            await h.send(h.f.poll_answer(u, pid1, 0))  # correct
            await h.wait(lambda: len(h.polls_for(804)) == 2, msg="exam poll 2")
            pid2, _ = h.polls_for(804)[1]
            await h.send(h.f.poll_answer(u, pid2, 1))  # correct
            await h.wait(lambda: len(h.polls_for(804)) == 3, msg="exam poll 3")
            pid3, _ = h.polls_for(804)[2]
            await h.send(h.f.poll_answer(u, pid3, 0))  # wrong answer
            await h.wait(lambda: "🏁" in h.all_text(804), msg="exam finish")

            # 2 correct (+2), 1 wrong (-0.33) => Net Score 1.67/3
            assert "Net Score" in h.all_text(804)
            assert "1.67/3" in h.all_text(804)

    run(t())


def test_sectional_mode_timers_and_section_score_breakdown():
    async def t():
        async with Harness() as h:
            u = h.f.user(805, "SecUser")
            qid = db.new_quiz(805, "सेक्शनल टेस्ट", "", timer=30, status="ready")
            db.add_question(qid, "Q1 हिंदी?", ["A", "B", "C", "D"], 0, section="भाग-अ (हिंदी)")
            db.add_question(qid, "Q2 गणित?", ["1", "2", "3", "4"], 1, section="भाग-ब (गणित)")
            await h.text(u, f"/section {qid} भाग-अ (हिंदी)=45s")
            await h.text(u, f"/section {qid} भाग-ब (गणित)=90s")

            quiz = db.get_quiz(qid)
            qs = db.get_questions(qid)
            assert quiz["quiz_mode"] == "sectional"
            assert quiz_modes.resolve_question_timer(quiz, qs[0]) == 45
            assert quiz_modes.resolve_question_timer(quiz, qs[1]) == 90

            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(805)) == 1, msg="sec poll 1")
            assert "Section: भाग-अ (हिंदी)" in h.all_text(805)
            assert int(h.polls_for(805)[0][1]["open_period"]) == 45

            await h.answer_current(u, correct=True)
            await h.wait(lambda: len(h.polls_for(805)) == 2, msg="sec poll 2")
            assert "Section: भाग-ब (गणित)" in h.all_text(805)
            assert int(h.polls_for(805)[1][1]["open_period"]) == 90

            await h.answer_current(u, correct=False)
            await h.wait(lambda: "🏁" in h.all_text(805), msg="sec finish")
            res_txt = h.all_text(805)
            assert "Section-wise Score:" in res_txt
            assert "भाग-अ (हिंदी)" in res_txt and "भाग-ब (गणित)" in res_txt

    run(t())


# ---------------------------------------------------------------------------
# 4. Feature 4: Interactive Self-Contained HTML Report (Math + Hindi + Navigator)
# ---------------------------------------------------------------------------
def test_interactive_html_report_renders_math_hindi_and_navigator():
    async def t():
        async with Harness() as h:
            u = h.f.user(806, "MathHindiUser")
            qid = db.new_quiz(806, "गणित एवं हिंदी रिपोर्ट", "", timer=30, status="ready")
            db.update_quiz(qid, negative_mark=1.0 / 3.0)
            db.add_question(
                qid,
                r"यदि $x^2 - 5x + 6 = 0$ हो, तो $\frac{\alpha + \beta}{\sqrt{4}}$ का मान क्या है?",
                [r"$\frac{5}{2}$", r"$3$", r"$\pi$", r"$0$"],
                0,
                explanation=r"मूलों का योग $\alpha + \beta = 5$ तथा $\sqrt{4} = 2$ होता है।",
                section="बीजगणित",
            )
            db.add_question(
                qid,
                "‘कमल’ का पर्यायवाची शब्द कौन-सा है?",
                ["जलज", "अंबुद", "वारिद", "नीरद"],
                0,
                explanation="जलज का अर्थ जल में जन्म लेने वाला (कमल) है।",
                section="हिंदी व्याकरण",
            )

            await h.start(u, qid)
            await h.wait(lambda: len(h.polls_for(806)) == 1, msg="html poll 1")
            await h.answer_current(u, correct=True)
            await h.wait(lambda: len(h.polls_for(806)) == 2, msg="html poll 2")
            await h.answer_current(u, correct=False)
            await h.wait(lambda: "🏁" in h.all_text(806), msg="html finish")

            await h.text(u, "/htmlreport")
            html_uploads = [
                files["document"]
                for api, params, files in h.api.uploads
                if api == "sendDocument" and "document" in files and str(files["document"][0]).endswith(".html")
            ]
            assert len(html_uploads) == 1
            fname, raw_html = html_uploads[0]
            assert fname.endswith(".html")
            html_str = raw_html.decode("utf-8") if isinstance(raw_html, bytes) else str(raw_html)

            assert "<!DOCTYPE html>" in html_str
            assert "question-navigator" in html_str
            assert "jumpToQuestion(1)" in html_str and "jumpToQuestion(2)" in html_str
            assert "math-frac" in html_str and "math-sqrt" in html_str
            assert "α + β" in html_str
            assert "कमल" in html_str and "जलज" in html_str
            assert "YOUR ANSWER" in html_str and "CORRECT ANSWER" in html_str
            assert "Section-wise Breakdown" in html_str
            assert "बीजगणित" in html_str and "हिंदी व्याकरण" in html_str
            assert config.QUIZ_BOT_OWNER in html_str
            assert config.OFFICIAL_TELEGRAM_GROUP_URL in html_str

    run(t())


# ---------------------------------------------------------------------------
# 5. Feature 6 & 7: Access Control, Batches, Auth Chats & Inline Privacy Guard
# ---------------------------------------------------------------------------
def test_access_control_tiers_batches_auth_chats_and_inline_privacy(monkeypatch):
    monkeypatch.setenv("OWNER_ID", "999")

    async def t():
        async with Harness() as h:
            owner = h.f.user(901, "QuizOwner")
            stranger = h.f.user(902, "Stranger")
            batch_student = h.f.user(903, "BatchStudent")
            any_user = h.f.user(904, "AnyUser")
            admin_user = h.f.user(999, "BotOwner")

            qid = make_quiz(owner=901, n=2, timer=30, title="प्रतिबंधित क्विज़")

            # 1) Private quiz: blocked for stranger, hidden in inline query for stranger, allowed for owner
            await h.text(owner, f"/access {qid} private")
            assert db.get_quiz(qid)["access_tier"] == "private"

            await h.text(stranger, f"/start quiz_{qid}")
            assert "Private Quiz" in h.all_text(902)
            assert not h.polls_for(902)

            await h.send(h.f.inline_query(stranger, f"quiz_{qid}"))
            assert h.api.inline_answers[-1]["results"] == []

            await h.send(h.f.inline_query(owner, f"quiz_{qid}"))
            assert len(h.api.inline_answers[-1]["results"]) == 1

            # 2) No paid access: "premium" is not a tier any more. /access ... premium and a
            #    legacy stored 'premium' value (old development build) both mean FREE.
            await h.text(owner, f"/access {qid} premium")
            assert db.get_quiz(qid)["access_tier"] == "free"
            db.update_quiz(qid, access_tier="premium")             # simulate a legacy row
            assert access_control.check_quiz_access(904, 904, db.get_quiz(qid)) == (True, "")
            await h.text(any_user, f"/start quiz_{qid}")
            assert "Get ready for the quiz" in h.all_text(904)
            assert "Premium" not in h.all_text(904)
            assert db.get_quiz(qid)["access_tier"] == "premium"   # row itself not modified

            # 3) Batch quiz: blocked for stranger, allowed for batch member
            await h.text(owner, f"/access {qid} batch REET_2026")
            await h.text(stranger, f"/start quiz_{qid}")
            assert "Batch Restricted Quiz" in h.all_text(902) and "REET_2026" in h.all_text(902)

            await h.text(admin_user, "/batch add REET_2026 903")
            assert db.user_in_batch(903, "REET_2026") is True
            await h.text(batch_student, f"/start quiz_{qid}")
            assert "Get ready for the quiz" in h.all_text(903)

            # 4) Authorized group chat check
            access_control.set_allowed_chats(qid, [-10055555])
            await h.text(owner, f"/access {qid} free")
            ok_unauth, msg_unauth = access_control.check_quiz_access(902, -10099999, db.get_quiz(qid))
            assert ok_unauth is False and "Unauthorized Group Chat" in msg_unauth
            ok_auth, _ = access_control.check_quiz_access(902, -10055555, db.get_quiz(qid))
            assert ok_auth is True

            # 5) REQUIRED_SUB_CHANNEL compatibility alias
            monkeypatch.setattr(config, "REQUIRED_CHAT", "")
            monkeypatch.setattr(config, "REQUIRED_SUB_CHANNEL", "@sub_channel_test")
            assert membership.enabled() is True
            assert membership.effective_required_chat() == "@sub_channel_test"

    run(t())


# ---------------------------------------------------------------------------
# 7. Feature 9: Owner/Admin Tools (/broadcast, /admin, LOG_GROUP redaction)
# ---------------------------------------------------------------------------
def test_admin_broadcast_dashboard_and_log_group_redaction(monkeypatch):
    monkeypatch.setenv("OWNER_ID", "990")
    monkeypatch.setenv("LOG_GROUP", "-100777888")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-super-secret-token-value")

    async def t():
        async with Harness() as h:
            owner = h.f.user(990, "OwnerLokesh")
            u1 = h.f.user(991, "UserOne")
            u2 = h.f.user(992, "UserTwo")

            await h.text(u1, "/start")
            await h.text(u2, "/start")

            # Non-admin cannot run /broadcast or /admin
            await h.text(u1, "/broadcast हैलो")
            assert "केवल Owner / Admin" in h.all_text(991)
            await h.text(u1, "/admin")
            assert "केवल Owner / Admin" in h.all_text(991)

            # Owner runs /admin — dashboard shown without exposing any secret key
            await h.text(owner, "/admin")
            admin_txt = h.all_text(990)
            assert "Admin & Owner Control Panel" in admin_txt
            assert "sk-or-super-secret-token-value" not in admin_txt

            # Owner runs /broadcast — delivered to all registered users
            await h.text(owner, "/broadcast कल शाम 8 बजे महा-मेगा टेस्ट होगा!")
            assert "Broadcast पूरा हुआ" in h.all_text(990)
            assert "कल शाम 8 बजे महा-मेगा टेस्ट होगा!" in h.all_text(991)
            assert "कल शाम 8 बजे महा-मेगा टेस्ट होगा!" in h.all_text(992)

            # Error logging to LOG_GROUP redacts secrets
            err = RuntimeError("Failed with key sk-or-super-secret-token-value on endpoint")
            sent_ok = await admin_tools.notify_log_group_error(h.app.bot, err)
            assert sent_ok is True
            log_msgs = h.api.sent("sendMessage", -100777888)
            assert len(log_msgs) == 1
            assert "sk-or-super-secret-token-value" not in log_msgs[0]["text"]
            assert "[REDACTED]" in log_msgs[0]["text"]

    run(t())
