"""Comprehensive tests for the 3 safe additive enhancements:
1. Share Quizzes Through Telegram (deep links, group links, share URL, inline query, /share, owner/timer metadata)
2. First-Time /start Notification to @DAUSA_123 (once per user, no duplicates on repeat/restart/migration, retry on failure)
3. Default Quiz Timer of 45 Seconds (manual, forward, PDF/TXT, HTML imports, preserving existing quizzes & custom timers)
"""
from __future__ import annotations

import sqlite3
from urllib.parse import parse_qs, unquote, urlparse

import fitz

import backup
import config
import database as db
from test_bot_flow import Harness, buttons, isolated, make_quiz, run

__all__ = ["isolated"]


def _support_msgs(h: Harness) -> list[dict]:
    return h.api.sent("sendMessage", config.SUPPORT_NOTIFY_CHAT)


# ==============================================================================
# FEATURE 2 — FIRST-TIME /start NOTIFICATION TO @DAUSA_123
# ==============================================================================
def test_first_time_start_sends_notification_to_dausa_123_once():
    async def t():
        async with Harness() as h:
            u = h.f.user(91001, name="Lokesh", last_name="Meena", username="lokesh_test")
            await h.text(u, "/start")

            # Normal /start menu still sent to user
            user_msgs = h.api.sent("sendMessage", 91001)
            assert user_msgs and "Quiz Bot" in user_msgs[-1]["text"]

            # Notification sent to @DAUSA_123
            notif_list = _support_msgs(h)
            assert len(notif_list) == 1
            text = notif_list[0]["text"]
            assert "🆕" in text and "NEW USER STARTED THE QUIZ BOT" in text
            assert "👤 Name:" in text and "Lokesh Meena" in text
            assert 'href="tg://user?id=91001"' in text
            assert "🔗 Username: @lokesh_test" in text
            assert "🆔 User ID:" in text and "91001" in text
            assert "🕒 First Start:" in text

            # Repeated /start by the same user must NOT send duplicate notifications
            await h.text(u, "/start")
            await h.text(u, "/start")
            assert len(_support_msgs(h)) == 1

            # Persisted in SQLite
            row = db.get_user(91001)
            assert row["first_start_notified"] == 1
            assert row["first_start_at"] != ""
            assert row["last_name"] == "Meena"
    run(t())


def test_first_time_start_handles_user_without_username():
    async def t():
        async with Harness() as h:
            u = h.f.user(91002, name="Ramesh", no_username=True)
            await h.text(u, "/start")

            notif_list = _support_msgs(h)
            assert len(notif_list) == 1
            text = notif_list[0]["text"]
            assert "👤 Name:" in text and "Ramesh" in text
            assert 'href="tg://user?id=91002"' in text
            assert "🔗 Username: No username" in text
            assert "91002" in text

            # Repeat /start does not re-send
            await h.text(u, "/start")
            assert len(_support_msgs(h)) == 1
    run(t())


def test_first_time_start_with_join_gate_and_deep_link_no_duplicate():
    async def t():
        async with Harness(pre_verified=False) as h:
            qid = make_quiz(owner=91003, n=2, timer=45, title="Shared DeepLink Quiz")
            db.mark_first_start_notified(91003, "2026-10-19 10:00:00 IST")

            player = h.f.user(91004, name="Priya", last_name="Sharma", username="priya_s")
            # 1) Player opens deep link /start quiz_<qid> before joining required group
            await h.text(player, f"/start quiz_{qid}")
            # Welcome join gate shown to player AND @DAUSA_123 notified once
            assert len(_support_msgs(h)) == 1
            assert "Priya Sharma" in _support_msgs(h)[0]["text"]

            # 2) Player clicks "✅ I've Joined" -> deep-link quiz ready card opens, NO duplicate notification
            await h.cb(player, f"j:check:quiz_{qid}")
            assert len(_support_msgs(h)) == 1
            assert "Get ready" in h.all_text(91004)

            # 3) Player sends /start again -> still NO duplicate notification
            await h.text(player, "/start")
            assert len(_support_msgs(h)) == 1
    run(t())


def test_first_time_start_failure_does_not_break_start_and_retries_safely():
    async def t():
        async with Harness() as h:
            u = h.f.user(91005, name="Suresh", username="suresh_k")
            # Simulate @DAUSA_123 sendMessage failing (e.g. bot not admin yet)
            h.api.fail_next["sendMessage"] = "Forbidden: bot is not a member of the channel chat"
            h.api.fail_code["sendMessage"] = 403

            await h.text(u, "/start")
            # User still received their normal /start main menu without crashing!
            user_msgs = h.api.sent("sendMessage", 91005)
            assert len(user_msgs) == 1 and "Quiz Bot" in user_msgs[0]["text"]
            # Notification attempt was made (1 API call) and failed with 403, so first_start_notified remains 0
            assert len(_support_msgs(h)) == 1
            assert db.get_user(91005)["first_start_notified"] == 0
            saved_ts = db.get_user(91005)["first_start_at"]
            assert saved_ts != ""

            # Scheduled retry job exists
            jobs = h.app.job_queue.get_jobs_by_name("first_start_notify:91005")
            assert len(jobs) == 1

            # Next /start (after bot permission is fixed) retries, succeeds, and marks notified=1
            await h.text(u, "/start")
            assert len(_support_msgs(h)) == 2
            assert saved_ts in _support_msgs(h)[-1]["text"]
            assert db.get_user(91005)["first_start_notified"] == 1

            # Further /start calls never duplicate
            await h.text(u, "/start")
            assert len(_support_msgs(h)) == 2
    run(t())


def test_old_database_migration_preserves_users_and_timers_without_spam(tmp_path):
    old_db = tmp_path / "legacy_prod.db"
    con = sqlite3.connect(old_db)
    con.executescript("""
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            default_timer INTEGER DEFAULT 30,
            default_shuffle_questions INTEGER DEFAULT 0,
            default_shuffle_options INTEGER DEFAULT 0,
            created_at TEXT DEFAULT '2026-01-15 08:00:00',
            last_seen TEXT DEFAULT '2026-01-15 08:00:00',
            member_verified_ts REAL DEFAULT 1700000000,
            member_checked_ts REAL DEFAULT 1700000000
        );
        CREATE TABLE quizzes (
            id TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            pre_text TEXT DEFAULT '',
            status TEXT DEFAULT 'ready',
            source TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE quiz_settings (
            quiz_id TEXT PRIMARY KEY,
            timer INTEGER DEFAULT 30,
            shuffle_questions INTEGER DEFAULT 0,
            shuffle_options INTEGER DEFAULT 0
        );
        INSERT INTO users(user_id, username, first_name, default_timer) VALUES (777, 'existing_u', 'Existing', 30);
        INSERT INTO quizzes(id, owner_id, title) VALUES ('legacyq01', 777, 'Legacy 30s Quiz');
        INSERT INTO quiz_settings(quiz_id, timer, shuffle_questions, shuffle_options) VALUES ('legacyq01', 30, 0, 0);
    """)
    con.commit()
    con.close()

    db.set_db_path(old_db)
    db.init_db()

    # Pre-existing user is preserved and marked already notified so redeploy doesn't spam @DAUSA_123
    existing = db.get_user(777)
    assert existing["username"] == "existing_u"
    assert existing["first_start_notified"] == 1
    # Pre-existing quiz keeps its saved 30s timer
    assert db.get_quiz("legacyq01")["timer"] == 30


# ==============================================================================
# FEATURE 3 — DEFAULT QUIZ TIMER OF 45 SECONDS
# ==============================================================================
def test_default_timer_is_45_seconds_for_new_manual_and_imported_quizzes():
    assert config.DEFAULT_TIMER == 45
    assert config.GROUP_DEFAULT_TIMER == 45

    async def t():
        async with Harness() as h:
            u = h.f.user(92001, name="Creator")
            # 1) Direct DB quiz creation uses 45s default
            q_direct = db.new_quiz(92001, "Direct Quiz")
            assert db.get_quiz(q_direct)["timer"] == 45

            # 2) Manual /newquiz flow uses 45s default when no custom timer is chosen
            await h.text(u, "/newquiz")
            await h.text(u, "Manual 45s Quiz")
            await h.text(u, "/skip")
            await h.text(u, "राजस्थान की राजधानी कौन-सी है?\n(A) जयपुर\n(B) जोधपुर\nउत्तर: A")
            await h.text(u, "/done")
            manual_q = [q for q in db.get_owner_quizzes(92001) if q["title"] == "Manual 45s Quiz"][0]
            assert db.get_quiz(manual_q["id"])["timer"] == 45

            # 3) Text/PDF import flow uses 45s default
            await h.text(u, "/import")
            await h.text(
                u,
                "1. भारत की राजधानी क्या है?\n(A) नई दिल्ली\n(B) मुंबई\n(C) चेन्नई\n(D) कोलकाता\nउत्तर: A\n\n"
                "2. राजस्थान का राज्य वृक्ष क्या है?\n(A) खेजड़ी\n(B) नीम\n(C) पीपल\n(D) बरगद\nउत्तर: A",
            )
            await h.cb(u, "i:create")
            await h.text(u, "Text Imported 45s Quiz")
            imp_q = [q for q in db.get_owner_quizzes(92001) if q["title"] == "Text Imported 45s Quiz"][0]
            assert db.get_quiz(imp_q["id"])["timer"] == 45

            # 4) HTML import flow uses 45s default
            html_bytes = (
                "<!DOCTYPE html><html><head><title>HTML 45s Test</title></head><body>"
                "<div class='question'><p>1. हल्दीघाटी का युद्ध कब लड़ा गया था?</p>"
                "<ul><li data-correct='true'>1576 ई.</li><li>1526 ई.</li><li>1556 ई.</li><li>1600 ई.</li></ul>"
                "<div class='explanation'>महाराणा प्रताप और अकबर की सेना के मध्य 1576 ई. में।</div></div>"
                "</body></html>"
            ).encode("utf-8")
            h.api.files["html45"] = html_bytes
            await h.send(h.f.document(u, "html45", "history_quiz.html", "text/html", len(html_bytes)))
            last_markup = str(h.api.sent("sendMessage", 92001)[-1].get("reply_markup"))
            cb_btn = "i:reviewed" if "i:reviewed" in last_markup else "i:create"
            await h.cb(u, cb_btn)
            await h.cb(u, "i:usedefault")
            html_q = [q for q in db.get_owner_quizzes(92001) if q["title"] == "history_quiz"][0]
            assert db.get_quiz(html_q["id"])["timer"] == 45
    run(t())


# ==============================================================================
# FEATURE 1 — SHARE QUIZZES THROUGH TELEGRAM & FULL WORKFLOW VERIFICATION
# ==============================================================================
def test_share_quiz_links_metadata_and_multi_user_private_and_group_workflows(monkeypatch):
    monkeypatch.setattr(config, "GROUP_NEXT_DELAY", 0.05)

    async def t():
        async with Harness() as h:
            owner = h.f.user(93001, name="Lokesh", username="lokesh_owner")
            user_a = h.f.user(93002, name="Anita", username="anita_u")
            user_b = h.f.user(93003, name="Vikram", no_username=True)

            qid = db.new_quiz(owner["id"], "राजस्थान GK शेयर टेस्ट", "महत्वपूर्ण प्रश्न")
            db.add_question(qid, "कालीबंगा किस जिले में स्थित है?", ["हनुमानगढ़", "गंगानगर", "बीकानेर", "चूरू"], 0,
                            "कालीबंगा हनुमानगढ़ जिले में घग्गर नदी के किनारे स्थित है।")
            db.add_question(qid, "विजय स्तंभ कहाँ स्थित है?", ["चित्तौड़गढ़", "उदयपुर", "जोधपुर", "अजमेर"], 0,
                            "महाराणा कुंभा द्वारा चित्तौड़गढ़ दुर्ग में निर्मित।")

            quiz = db.get_quiz(qid)
            assert quiz["timer"] == 45

            # 1) Share via button (q:share:<qid>) and via /share command
            await h.cb(owner, f"q:share:{qid}")
            share_msg = h.api.sent("sendMessage", owner["id"])[-1]
            stext = share_msg["text"]
            assert "राजस्थान GK शेयर टेस्ट" in stext
            assert "महत्वपूर्ण प्रश्न" in stext
            assert "Questions: <b>2</b>" in stext
            assert "45 sec" in stext
            assert config.OWNER_NAME in stext and config.OWNER_TELEGRAM in stext
            assert f"https://t.me/TestQuizBot?start=quiz_{qid}" in stext

            sbtns = buttons(share_msg["reply_markup"])
            start_btn = next(b for b in sbtns if b.get("url") == f"https://t.me/TestQuizBot?start=quiz_{qid}")
            group_btn = next(b for b in sbtns if b.get("url") == f"https://t.me/TestQuizBot?startgroup=quiz_{qid}")
            tg_share_btn = next(b for b in sbtns if b.get("url", "").startswith("https://t.me/share/url?"))
            inline_btn = next(b for b in sbtns if b.get("switch_inline_query") == f"quiz_{qid}")
            assert start_btn and group_btn and tg_share_btn and inline_btn

            # Check encoded text in https://t.me/share/url
            parsed_share = parse_qs(urlparse(tg_share_btn["url"]).query)
            shared_caption = unquote(parsed_share["text"][0])
            assert "राजस्थान GK शेयर टेस्ट" in shared_caption
            assert "2 questions" in shared_caption
            assert "45 sec" in shared_caption
            assert config.OWNER_NAME in shared_caption and config.OWNER_TELEGRAM in shared_caption

            # /share command also works
            await h.text(owner, f"/share {qid}")
            assert f"https://t.me/TestQuizBot?start=quiz_{qid}" in h.api.sent("sendMessage", owner["id"])[-1]["text"]

            # Inline query sharing includes title, question count, 45s timer, and owner attribution
            await h.send(h.f.inline_query(owner, f"quiz_{qid}"))
            iq_res = h.api.inline_answers[-1]["results"][0]
            assert iq_res["title"] == "राजस्थान GK शेयर टेस्ट"
            assert "45 sec" in iq_res["description"] and config.OWNER_NAME in iq_res["description"]
            msg_content = iq_res["input_message_content"]["message_text"]
            assert "45 sec" in msg_content and config.OWNER_NAME in msg_content

            # 2) User A & User B open the shared deep link in private chat and play independently
            await h.text(user_a, f"/start quiz_{qid}")
            await h.text(user_b, f"/start quiz_{qid}")
            assert "Get ready" in h.api.sent("sendMessage", user_a["id"])[-1]["text"]
            assert "45 sec per question" in h.api.sent("sendMessage", user_a["id"])[-1]["text"]
            assert "Get ready" in h.api.sent("sendMessage", user_b["id"])[-1]["text"]

            await h.go(user_a, qid)
            await h.go(user_b, qid)
            await h.wait(lambda: len(h.polls_for(user_a["id"])) == 1 and len(h.polls_for(user_b["id"])) == 1)

            # Verify native poll uses open_period == 45
            poll_a1 = h.api.sent("sendPoll", user_a["id"])[-1]
            assert int(poll_a1["open_period"]) == 45

            # User A answers both right; User B answers 1 right, 1 wrong
            await h.answer_current(user_a, correct=True)
            await h.answer_current(user_b, correct=True)
            await h.wait(lambda: len(h.polls_for(user_a["id"])) == 2 and len(h.polls_for(user_b["id"])) == 2)
            await h.answer_current(user_a, correct=True)
            await h.answer_current(user_b, correct=False)
            await h.wait(lambda: "🏁" in h.all_text(user_a["id"]) and "🏁" in h.all_text(user_b["id"]))

            # Both receive their separate auto-generated Review PDFs in private chat
            await h.wait(lambda: h.api.sent("sendDocument", user_a["id"]) and h.api.sent("sendDocument", user_b["id"]))
            pdf_a_bytes = next(
                files["document"][1]
                for api, params, files in h.api.uploads
                if api == "sendDocument" and int(params["chat_id"]) == user_a["id"]
            )
            doc_a = fitz.open(stream=pdf_a_bytes, filetype="pdf")
            page0_text = doc_a[0].get_text("text")
            assert config.QUIZ_BOT_OWNER in page0_text
            assert config.QUIZ_BOT_OWNER_USERNAME in page0_text
            assert any(link.get("uri") == config.OFFICIAL_TELEGRAM_GROUP_URL for link in doc_a[0].get_links())
            assert len(doc_a[0].get_images(full=True)) >= 1
            doc_a.close()

            # 3) Shared quiz in a group chat via ?startgroup=quiz_<qid>
            group_chat = -1008881
            await h.text(user_a, f"/start@TestQuizBot quiz_{qid}", chat_id=group_chat)
            gcard = h.api.sent("sendMessage", group_chat)[-1]
            assert "45 sec per question" in gcard["text"]
            await h.cb(user_a, f"g:go:{qid}", chat_id=group_chat)
            await h.wait(lambda: len(h.polls_for(group_chat)) == 1)
            gpoll = h.api.sent("sendPoll", group_chat)[-1]
            assert gpoll["type"] == "quiz" and int(gpoll["open_period"]) == 45
            await h.text(user_a, "/stop", chat_id=group_chat)

            # 4) Backup and restore round-trip preserves all new fields
            payload = backup.build_payload()
            assert backup.validate_payload(payload) == []
            u_backup = next(x for x in payload["users"] if x["user_id"] == user_a["id"])
            assert u_backup["first_start_notified"] == 1
            assert u_backup["default_timer"] == 45
    run(t())
