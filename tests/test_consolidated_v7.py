"""Regression tests for the consolidated review pass (v7).

* Repository layout: ``fonts/`` (lower case), ``tests/fixtures/``, no 1-byte
  placeholder files, watermark present (the C1-C6 cleanup of the GitHub tree).
* Numeric environment variables: an empty or invalid value must not crash the
  bot at start-up; valid values are parsed exactly as before; the raw value is
  never logged.  Proven on the real ``python bot.py`` process, which must also
  log the PDF font status.
* ``/aiquiz`` cost guard (per-user cooldown, rolling 24 h cap, one request at a
  time, owner/admins exempt).
* Security: strangers cannot use any privileged command (including /backup),
  ``/htmlreport`` only ever returns the caller's own attempts, and hostile quiz
  content is HTML-escaped in the interactive report.
* Guards: every database table is in the backup, every environment variable the
  code reads is documented, every registered command is documented.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import backup
import config
import database as db
import envutil
import html_report
import quiz_modes
from tests.test_bot_flow import Harness, buttons, isolated, make_quiz, run

_ = isolated
ROOT = Path(__file__).resolve().parents[1]
PROD_MODULES = sorted(p for p in ROOT.glob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))


class _U:
    def __init__(self, uid, name):
        self.id, self.username, self.first_name = uid, f"user{uid}", name


def _finished_attempt(qid, uid, name="Player"):
    db.upsert_user(_U(uid, name))
    qs = db.get_questions(qid)
    att = db.create_attempt(qid, uid, uid, [q["id"] for q in qs], 30, False, 1_000_000.0)
    for i, q in enumerate(qs):
        db.record_answer(att, q["id"], i, 0, q["correct_index"] == 0, "correct" if q["correct_index"] == 0 else "wrong", 2.0)
    db.finish_attempt(att, "finished", 30.0)
    return db.get_attempt(att)


def _no_admins(monkeypatch):
    for k in ("OWNER_ID", "ADMIN_IDS", "BACKUP_ADMIN_IDS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "BACKUP_ADMIN_IDS", ())
    if hasattr(config, "ADMIN_IDS"):
        monkeypatch.setattr(config, "ADMIN_IDS", frozenset())
    if hasattr(config, "OWNER_ID"):
        monkeypatch.setattr(config, "OWNER_ID", 0)


# ================================================================ layout (C1-C6)
def test_repository_layout_after_cleanup():
    top = set(os.listdir(ROOT))
    assert "fonts" in top and "Fonts" not in top, "font folder must be lower-case fonts/ (Linux is case-sensitive)"
    for ttf in ("NotoSansDevanagari-Regular.ttf", "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        f = ROOT / "fonts" / ttf
        assert f.is_file() and f.read_bytes()[:4] in (b"\x00\x01\x00\x00", b"true", b"OTTO"), ttf
    assert not (ROOT / "fonts" / "Fonts").exists() and not (ROOT / "assets" / "assets").exists()
    assert not [n for n in top if "→" in n], "stray upload folders like 'x.txt → fixtures' are back"
    fixtures = {p.name for p in (ROOT / "tests" / "fixtures").iterdir()}
    assert {"Topic_02_Varnamala_evam_Lipi_Full.txt", "all_formats_hi.txt", "weak_numbers_key.txt"} <= fixtures
    wm = ROOT / "assets" / "group_dp_watermark.jpg"
    assert wm.is_file() and wm.read_bytes()[:3] == b"\xff\xd8\xff", "official group DP watermark missing"


def test_review_pdf_uses_the_bundled_fonts_in_this_layout(monkeypatch):
    monkeypatch.delenv("PDF_FONT_DIR", raising=False)
    import review_pdf
    ok, info = review_pdf.pdf_font_status()
    assert ok and "source=bundled" in info and "NotoSansDevanagari" in info, info


# ================================================================ numeric env vars
@pytest.mark.parametrize("raw", ["45", " 45 ", "045", "-3", "+7", "0"])
def test_env_int_matches_int_for_every_valid_value(monkeypatch, raw):
    monkeypatch.setenv("V7_PROBE", raw)
    assert envutil.env_int("V7_PROBE", "9") == int(raw)


@pytest.mark.parametrize("raw", ["1.5", " 2 ", "1e2", "-0.5", "3"])
def test_env_float_matches_float_for_every_valid_value(monkeypatch, raw):
    monkeypatch.setenv("V7_PROBE", raw)
    assert envutil.env_float("V7_PROBE", "9") == float(raw)


def test_unset_empty_and_invalid_values_use_the_default(monkeypatch):
    monkeypatch.setattr(envutil, "INVALID", [])
    monkeypatch.delenv("V7_UNSET", raising=False)
    monkeypatch.setenv("V7_EMPTY", "   ")
    monkeypatch.setenv("V7_BAD", "45s")
    assert envutil.env_int("V7_UNSET", "45") == 45
    assert envutil.env_int("V7_EMPTY", "45") == 45
    assert envutil.env_int("V7_BAD", "45") == 45
    assert envutil.env_float("V7_BAD", "1.5") == 1.5
    assert envutil.invalid_settings() == [("V7_BAD", "45"), ("V7_BAD", "1.5")]   # empty = "not set", not an error


def _numeric_env_names() -> list[str]:
    pat = re.compile(r'env_(?:int|float)\(\s*"([A-Z0-9_]+)"')
    return sorted({m.group(1) for p in PROD_MODULES for m in pat.finditer(p.read_text(encoding="utf-8"))})


def test_no_bare_numeric_env_parsing_left_in_production_code():
    bare = re.compile(r'\b(?:int|float)\(\s*os\.(?:getenv|environ)')
    hits = [f"{p.name}:{i}" for p in PROD_MODULES if p.name != "envutil.py"   # its docstring quotes the old pattern
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if bare.search(line) and not line.startswith("OWNER_ID")]      # OWNER_ID is guarded by isdigit()
    assert hits == [], f"crash-prone int()/float() of an env var: {hits}"
    assert len(_numeric_env_names()) >= 25


def test_every_numeric_variable_set_to_garbage_still_imports_with_defaults(tmp_path):
    names = _numeric_env_names()
    env = {k: v for k, v in os.environ.items() if k not in names}
    env.update({n: "not-a-number" for n in names})
    env.update({"DATA_DIR": str(tmp_path), "GROUP_DEFAULT_TIMER": "", "PYTHONPATH": str(ROOT)})
    code = ("import json, config, pdf_extract, envutil;"
            "print(json.dumps({'timer': config.DEFAULT_TIMER, 'group': config.GROUP_DEFAULT_TIMER,"
            "'grace': config.TIMER_GRACE, 'opacity': config.WATERMARK_OPACITY, 'dpi': pdf_extract.OCR_DPI,"
            "'part': config.QUESTIONS_PER_PART, 'ai_cool': config.AI_QUIZ_COOLDOWN_SECONDS,"
            "'invalid': sorted(n for n, _ in envutil.invalid_settings())}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert (got["timer"], got["group"], got["grace"], got["opacity"], got["dpi"], got["part"], got["ai_cool"]) == \
        (45, 45, 1.0, 0.102, 300, 500, 60)
    assert "GROUP_DEFAULT_TIMER" not in got["invalid"]                    # empty -> silent default
    assert set(got["invalid"]) == set(names) - {"GROUP_DEFAULT_TIMER"}


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signals")
def test_real_bot_starts_with_invalid_numeric_env_and_logs_font_status(tmp_path):
    from tests.test_real_startup import FakeBotAPI, _env, _wait
    secretish = "SECRETLIKE-777-VALUE"                   # e.g. a token pasted into the wrong variable
    with FakeBotAPI(conflicts=0) as api:
        env = _env(tmp_path, api.port)
        env.update({"DEFAULT_TIMER": "45s", "GROUP_DEFAULT_TIMER": secretish, "OCR_DPI": ""})
        env.pop("PDF_FONT_DIR", None)
        log_path = tmp_path / "bot.log"
        with open(log_path, "w") as logf:
            proc = subprocess.Popen([sys.executable, "bot.py"], cwd=ROOT, env=env,
                                    stdout=logf, stderr=subprocess.STDOUT)
            try:
                ok = _wait(lambda: any(str(m.get("chat_id")) == "4242" for m in api.sent), 45)
                assert ok, f"/start was never answered\n{log_path.read_text()[-3000:]}"
                assert proc.poll() is None
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                try:
                    rc = proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    raise AssertionError("bot did not exit after SIGTERM")
    log = log_path.read_text()
    assert rc == 0, log[-3000:]
    assert "Environment variable DEFAULT_TIMER is not a valid number — using the default 45" in log
    assert "Environment variable GROUP_DEFAULT_TIMER is not a valid number" in log
    assert "OCR_DPI" not in log.split("PDF fonts")[0].split("Database:")[-1]   # empty = not set, no warning
    assert secretish not in log, "the raw value of an invalid variable must never be logged"
    assert "PDF fonts ready:" in log and "source=bundled" in log, log[-3000:]


# ================================================================ /aiquiz cost guard
def _fake_ai(monkeypatch, bot_mod):
    import ai_gen
    calls = []

    def gen(topic, count):
        calls.append((topic, count))
        return [{"question": f"{topic} प्रश्न?", "options": ["क", "ख", "ग", "घ"], "correct_index": 1}]
    monkeypatch.setattr(ai_gen, "is_ai_configured", lambda: True)
    monkeypatch.setattr(ai_gen, "generate_ai_questions", gen)
    clock = {"t": 1000.0}
    monkeypatch.setattr(bot_mod, "_ai_clock", lambda: clock["t"])
    monkeypatch.setattr(bot_mod, "_AI_USAGE", {})
    monkeypatch.setattr(bot_mod, "_AI_INFLIGHT", set())
    return calls, clock


def test_aiquiz_cooldown_and_daily_cap_for_normal_users(monkeypatch):
    import bot as bot_mod
    _no_admins(monkeypatch)
    calls, clock = _fake_ai(monkeypatch, bot_mod)
    monkeypatch.setattr(config, "AI_QUIZ_COOLDOWN_SECONDS", 60)
    monkeypatch.setattr(config, "AI_QUIZ_DAILY_LIMIT", 2)

    async def t():
        async with Harness() as h:
            u = h.f.user(4601, "AIUser")
            await h.text(u, "/aiquiz इतिहास 3")
            assert len(calls) == 1
            await h.text(u, "/aiquiz भूगोल 3")                    # immediately again -> cooldown
            assert len(calls) == 1 and "सेकंड बाद" in h.all_text(4601)
            clock["t"] += 61
            await h.text(u, "/aiquiz भूगोल 3")
            assert len(calls) == 2
            clock["t"] += 61
            await h.text(u, "/aiquiz विज्ञान 3")                  # 3rd within 24 h -> cap
            assert len(calls) == 2 and "सीमा पूरी" in h.all_text(4601)
            clock["t"] += 86400
            await h.text(u, "/aiquiz विज्ञान 3")                  # window rolled over
            assert len(calls) == 3
            other = h.f.user(4602, "Other")                       # limits are per user
            await h.text(other, "/aiquiz गणित 2")
            assert len(calls) == 4
    run(t())


def test_aiquiz_guard_exempts_admins_and_blocks_parallel_requests(monkeypatch):
    import bot as bot_mod
    _no_admins(monkeypatch)
    calls, clock = _fake_ai(monkeypatch, bot_mod)
    bot_mod._AI_USAGE[4700] = [clock["t"] - 1] * 5     # recent usage far above every limit
    monkeypatch.setenv("OWNER_ID", "4700")
    monkeypatch.setattr(config, "OWNER_ID", 4700)
    monkeypatch.setattr(config, "AI_QUIZ_DAILY_LIMIT", 1)

    async def t():
        async with Harness() as h:
            owner = h.f.user(4700, "Owner")
            for i in range(3):
                await h.text(owner, f"/aiquiz विषय{i} 2")
            assert len(calls) == 3                                  # owner/admin: no limit
    run(t())
    bot_mod._AI_INFLIGHT.add(4701)
    assert "प्रतीक्षा" in (bot_mod._ai_quota_block(4701) or "")    # one request at a time
    bot_mod._AI_INFLIGHT.discard(4701)
    assert bot_mod._ai_quota_block(4701) is None


def test_aiquiz_not_configured_is_not_rate_limited(monkeypatch):
    import ai_gen
    import bot as bot_mod
    _no_admins(monkeypatch)
    monkeypatch.setattr(ai_gen, "is_ai_configured", lambda: False)
    monkeypatch.setattr(bot_mod, "_AI_USAGE", {})

    async def t():
        async with Harness() as h:
            u = h.f.user(4801, "U")
            for _ in range(3):
                await h.text(u, "/aiquiz इतिहास 2")
            assert h.all_text(4801).count("AI Quiz Generation अभी सक्रिय नहीं है") == 3
            assert bot_mod._AI_USAGE == {}
    run(t())


# ================================================================ security
def test_stranger_cannot_use_privileged_commands_including_backup(monkeypatch):
    _no_admins(monkeypatch)
    monkeypatch.setattr(config, "BACKUP_ADMIN_IDS", (4999,))         # backup configured, stranger not in it
    calls = []
    monkeypatch.setattr(backup, "export_now", lambda *a, **k: calls.append("export") or {"ok": True})
    monkeypatch.setattr(backup, "restore_from_file", lambda *a, **k: calls.append("restore") or {"ok": True},
                        raising=False)

    async def t():
        async with Harness() as h:
            h.f.user(4901, "Owner")
            stranger = h.f.user(4902, "Stranger")
            qid = make_quiz(owner=4901, n=3, timer=30, title="Owner quiz")
            q0 = db.get_quiz(qid)
            before = ({k: q0.get(k) for k in ("title", "quiz_mode", "access_tier", "negative_mark",
                                              "section_timers_json", "allowed_chats_json")},
                      [(x["question"], tuple(x["options"]), x["correct_index"]) for x in db.get_questions(qid)])
            for c in ("/broadcast HACK", "/admin", "/batch create X", "/batch add X 4902",
                      "/authchat add -100999", f"/access {qid} private", f"/quizmode {qid} exam",
                      f"/section {qid} A=10s", "/backup", "/backup now", "/backup restore", "/backup sync"):
                await h.text(stranger, c)
            q1 = db.get_quiz(qid)
            after = ({k: q1.get(k) for k in before[0]},
                     [(x["question"], tuple(x["options"]), x["correct_index"]) for x in db.get_questions(qid)])
            assert after == before, "stranger modified someone else's quiz"
            assert db.list_batches() == [] and db.list_authorized_chats() == []
            assert calls == [], "a stranger triggered a backup/restore"
            assert "HACK" not in h.all_text(4901)
            assert "केवल administrator" in h.all_text(4902)            # /backup refused explicitly
            await h.text(stranger, f"/start quiz_{qid}")                # still nobody locked out
            assert "Get ready for the quiz" in h.all_text(4902)
    run(t())


def test_htmlreport_never_returns_another_users_attempt():
    async def t():
        async with Harness() as h:
            qid = make_quiz(owner=5001, n=2, timer=30, title="Private result quiz")
            _finished_attempt(qid, 5002, "Victim")
            attacker, victim = h.f.user(5003, "Attacker"), h.f.user(5002, "Victim")
            for arg in (qid, f"quiz_{qid}", "latest", ""):
                await h.text(attacker, f"/htmlreport {arg}".strip())
            assert h.api.sent("sendDocument", 5003) == []
            assert "कोई पूरा हुआ quiz attempt नहीं मिला" in h.all_text(5003)
            await h.text(victim, f"/htmlreport {qid}")
            assert len(h.api.sent("sendDocument", 5002)) == 1          # the owner of the attempt gets it
    run(t())


def test_html_report_escapes_hostile_quiz_content():
    hostile_title = '<img src=x onerror=alert("title")>'
    db.upsert_user(_U(5101, "Owner"))
    qid = db.new_quiz(5101, hostile_title, "", timer=30, status="ready")
    db.add_question(qid, "<script>alert(1)</script> $<script>alert(5)</script>$ \\frac{<b onclick=alert(6)>}{2}",
                    ['<img src=x onerror=alert(2)>', '</div><script>alert(7)</script>', "C", "D"], 0,
                    '"><svg onload=alert(3)>', pre_text="<iframe src=javascript:alert(8)></iframe>")
    att = _finished_attempt(qid, 5102, "<script>alert(4)</script>")
    page = html_report.build_html_report_for_attempt(att["id"])[0].decode("utf-8")
    for payload in ("<script>alert(", "<img src=x", "<svg onload", "<b onclick", "<iframe", "</div><script>"):
        assert payload not in page, f"unescaped user content in the HTML report: {payload}"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;img src=x onerror=alert(2)&gt;" in page


# ================================================================ guards
def test_backup_exports_every_table_and_never_drops_a_column():
    with db.connect() as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables == set(backup._TABLES), f"not backed up: {tables - set(backup._TABLES)}"
        for t in tables:
            cols = {r[1].lower() for r in con.execute(f"PRAGMA table_info({t})")}
            assert not cols & backup._FORBIDDEN_KEYS, f"{t}: column silently excluded from backups"


def test_negative_marking_stays_off_by_default():
    qid = make_quiz(owner=5201, n=2)
    q = db.get_quiz(qid)
    assert float(q.get("negative_mark") or 0) == 0 and q.get("quiz_mode") == "standard"
    assert quiz_modes.compute_net_score(2, 1, 0) == 2


def _env_names_read_by_code() -> set[str]:
    pat = re.compile(r'''(?:os\.getenv|os\.environ\.get|env_int|env_float|os\.environ\[)\(?\s*["']([A-Z][A-Z0-9_]+)["']''')
    return {m.group(1) for p in PROD_MODULES for m in pat.finditer(p.read_text(encoding="utf-8"))}


def test_every_environment_variable_read_by_the_code_is_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    internal = {"PYTEST_CURRENT_TEST"}                     # set by pytest, not a setting
    names = _env_names_read_by_code() - internal
    assert len(names) >= 60
    missing = sorted(n for n in names if f"`{n}`" not in readme)
    assert missing == [], f"read by the code but not documented in README.md: {missing}"


def test_every_registered_command_is_documented():
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    cmds = set()
    for m in re.finditer(r'CommandHandler\(\s*(\[[^\]]*\]|"[a-z_]+")', src):
        cmds |= set(re.findall(r'"([a-z_]+)"', m.group(1)))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert len(cmds) >= 25
    missing = sorted(c for c in cmds if not re.search(rf"/{c}\b", readme))
    assert missing == [], f"commands missing from README.md: {missing}"


# ================================================================ conflict-warning throttle
def test_conflict_warning_is_logged_once_per_minute_per_process(caplog, monkeypatch):
    """Production keeps the Railway log readable during a 409 storm.  This state is
    per process, which is why test_run_polling_survives_conflict resets it first."""
    import logging

    from telegram.error import Conflict

    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "_last_conflict_log", [0.0])
    clock = {"t": 10_000.0}
    monkeypatch.setattr(bot_mod.time, "time", lambda: clock["t"])

    class Ctx:
        error = Conflict("terminated by other getUpdates request")

    async def storm():
        for _ in range(5):
            await bot_mod.error_handler(None, Ctx())
        clock["t"] += 61
        await bot_mod.error_handler(None, Ctx())
    with caplog.at_level(logging.WARNING):
        run(storm())
    assert caplog.text.count("Telegram Conflict") == 2      # 1 for the first burst, 1 after 60 s


# ================================================================ JSON import: never guess, never drop silently
def _jq(**kw):
    base = {"question": "भारत की राजधानी क्या है?", "options": ["दिल्ली", "मुंबई", "चेन्नई", "कोलकाता"], "correct_index": 0}
    base.update(kw)
    return base


def test_json_blank_option_does_not_shift_the_correct_answer():
    import ai_gen
    raw = json.dumps({"questions": [_jq(options=["दिल्ली", "", "मुंबई", "चेन्नई"], correct_index=2)]}, ensure_ascii=False)
    _title, qs = ai_gen.parse_json_quiz(raw)
    q = qs[0]
    assert q["options"] == ["दिल्ली", "मुंबई", "चेन्नई"]
    assert q["options"][q["correct_index"]] == "मुंबई", "a blank option shifted the answer to a different option"


def test_json_invalid_questions_are_reported_never_silently_dropped():
    import ai_gen
    items = [
        _jq(),                                                    # 1 valid
        {k: v for k, v in _jq().items() if k != "correct_index"},  # 2 no answer
        _jq(correct_index=7),                                     # 3 answer out of range
        _jq(options=["केवल एक"]),                                  # 4 one option
        _jq(question="   "),                                      # 5 empty question
        _jq(correct_index=True),                                  # 6 boolean is not an index
        _jq(options=["दिल्ली", "", "मुंबई"], correct_index=1),      # 7 the correct option is blank
        "not an object",                                          # 8
        _jq(options="दिल्ली, मुंबई"),                              # 9 options not a list
    ]
    title, qs, rejected = ai_gen.parse_json_quiz_report(json.dumps({"title": "T", "questions": items}, ensure_ascii=False))
    assert [q["number"] for q in qs] == [1]
    assert [n for n, _ in rejected] == [2, 3, 4, 5, 6, 7, 8, 9]
    assert all(reason.strip() for _, reason in rejected)
    assert len(qs) + len(rejected) == len(items)                 # every item is accounted for


def test_json_only_invalid_questions_explains_why():
    import ai_gen
    raw = json.dumps([{k: v for k, v in _jq().items() if k != "correct_index"}], ensure_ascii=False)
    with pytest.raises(ValueError) as exc:
        ai_gen.parse_json_quiz_report(raw)
    assert "प्रश्न 1" in str(exc.value)


def test_json_non_numeric_number_field_does_not_abort_the_import():
    import ai_gen
    raw = json.dumps({"questions": [_jq(number="Q1"), _jq(number="2"), _jq(number=None)]}, ensure_ascii=False)
    _t, qs, rejected = ai_gen.parse_json_quiz_report(raw)
    assert rejected == [] and [q["number"] for q in qs] == [1, 2, 3]


def test_json_source_text_is_preserved_exactly():
    import ai_gen
    q_text = "निम्न में से कौन-सा 'संधि' का उदाहरण है? (2019) — x² + y² = r²"
    opts = ["विद्या + आलय = विद्यालय", "राम + ईश = रामेश", "सूर्य + उदय = सूर्योदय", "उपर्युक्त सभी"]
    expl = "स्वर संधि: अ + आ = आ; 'विद्यालय' दीर्घ संधि का उदाहरण है।"
    raw = json.dumps({"questions": [{"question": q_text, "options": opts, "correct": "D", "explanation": expl}]},
                     ensure_ascii=False)
    _t, qs, rejected = ai_gen.parse_json_quiz_report(raw)
    assert rejected == [] and qs[0]["question"] == q_text and qs[0]["options"] == opts
    assert qs[0]["explanation"] == expl and qs[0]["correct_index"] == 3


def test_json_upload_shows_rejected_questions_to_the_user():
    async def t():
        async with Harness() as h:
            u = h.f.user(5301, "JsonUser")
            data = {"title": "JSON मिश्रित", "questions": [
                _jq(), {k: v for k, v in _jq(question="दूसरा प्रश्न?").items() if k != "correct_index"},
                _jq(question="तीसरा प्रश्न?", correct_index=2)]}
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            h.api.files["json_mix"] = raw
            await h.send(h.f.document(u, "json_mix", "mix.json", "application/json", len(raw)))
            text = h.all_text(5301)
            assert "प्रश्न 2" in text, text[-1500:]                    # the rejected question is named
            assert "अनुमान नहीं लगाया गया" in text                     # with the reason; nothing guessed
            assert "Total detected: 3" in text and "Failed: 1" in text
            last = [m for m in h.api.sent("sendMessage", 5301) if m.get("reply_markup")][-1]
            first = buttons(last["reply_markup"])[0]
            assert first["text"].startswith("⚠️ Confirm") and "2" in first["text"]   # partial quiz needs consent
            await h.cb(u, first["callback_data"])
            await h.cb(u, "i:usedefault")
            (quiz,) = db.get_owner_quizzes(5301)
            saved = db.get_questions(quiz["id"])
            assert [(q["question"], q["options"][q["correct_index"]]) for q in saved] == [
                ("भारत की राजधानी क्या है?", "दिल्ली"), ("तीसरा प्रश्न?", "चेन्नई")]
    run(t())


def test_json_extra_quizzes_in_one_file_are_reported_not_ignored():
    import ai_gen
    raw = json.dumps({"quizzes": [
        {"title": "भाग 1", "questions": [_jq(), _jq(question="दूसरा?")]},
        {"title": "भाग 2", "questions": [_jq(question="तीसरा?")]},
        {"title": "भाग 3", "questions": [_jq(question="चौथा?"), _jq(question="पाँचवाँ?")]},
    ]}, ensure_ascii=False)
    title, qs, rejected = ai_gen.parse_json_quiz_report(raw)
    assert title == "भाग 1" and len(qs) == 2                     # behaviour for the first quiz unchanged
    notes = [why for n, why in rejected if n is None]
    assert len(notes) == 1 and "भाग 2" in notes[0] and "भाग 3" in notes[0] and "3" in notes[0]
