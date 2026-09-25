# 🎯 Telegram Quiz Bot (QuizBot-style) — Railway ready

A production-ready Telegram quiz bot that works like the official **@QuizBot**:
create quizzes step by step, import them from **PDF / text**, and play them
**one question at a time** with Telegram's **native quiz polls**.

* Python 3.11+ · python-telegram-bot **22.5** · SQLite · PyMuPDF · long polling
* Hindi + English. The source text is never guessed, shortened or lost.

---

## ✨ Features

| Area | What it does |
|---|---|
| Main menu | ➕ New Quiz · 📚 My Quizzes · 📥 Import PDF/Text · ▶️ Start Quiz · 📊 Quiz Stats · ⚙️ Settings · ℹ️ Help |
| Creator wizard | Name → Description → optional pre-question text/photo/video/document → Question → exactly 4 options → correct answer → optional explanation → add another / `/done` (max 100 questions) |
| Question formats | Normal MCQ, statement based, matching, List-I/List-II, assertion-reason, ordering. Multi-line text is saved exactly as typed. You can paste the question and its (A)-(D) options together and the bot detects the options. |
| PDF / Text import | Selectable-text PDF, `.txt` file or pasted text (long text can arrive in several messages) |
| Parser | Question markers `प्रश्न 1.` `1.` `1)` `Q1` `Q.1` `Question 1:`. It finds the **real** A-D answer choices even when the question body contains A-D data (matching lists, ordering items, assertion (A)/(R)). |
| Answer key | `उत्तर: (A)`, `उत्तर: A`, `Answer: B`, `Ans. (C)`, `सही उत्तर: D`, answer-key sections (`Answer Key` / `उत्तर कुंजी` / `उत्तरमाला`) in the forms `1-A 2-C`, `1. (B)`, `1 A`, vertical tables, or a key with explanations. **Answers are never guessed.** |
| Error reporting | Every problem is shown with its question number. A partial quiz is created **only after you confirm it explicitly**. |
| Native quiz | `sendPoll(type="quiz")`, one question at a time. Telegram shows correct/wrong natively, then the explanation, then the next question. |
| Long text | If a question is over 300 characters or an option over 100, the **complete** question and **complete** (A)-(D) options are sent as a normal message first. The poll then uses a short prompt, plus compact `(A) (B) (C) (D)` labels if an option is too long. Nothing is truncated. |
| Timer | 10s, 15s, 30s, 60s, 90s, 2m, 3m, 5m or no timer. When it expires the poll closes, the question counts as skipped, and the next one starts. |
| Shuffle | Shuffle questions and/or options. The correct answer is re-mapped every time. |
| My Quizzes | ▶ Start · 🔗 Share · ✏️ Edit · 📊 Stats · 🗑 Delete |
| Edit | Title, description, add question, delete question, view questions, edit/remove explanation, timer, shuffle questions, shuffle options |
| Share | `https://t.me/<BOT_USERNAME>?start=quiz_<QUIZ_ID>` starts the quiz in private chat, plus a "Share to chat" button |
| Groups | The bot never runs a shared sequential session in a group. It posts a **Start Privately** deep-link button so every member gets an independent session. |
| Results | 🏁 Quiz Complete: Total, Correct, Wrong, Skipped, Score, Percentage, Time · 🔁 Restart · 📊 Stats · 🏠 Main Menu |
| Stats | Attempts, correct/wrong/skipped, average, top users (best attempt per user), personal result history |
| `/stop` | Stops the active quiz, closes the open poll and saves the partial result |
| Persistence | Everything lives in SQLite: users, quizzes, quiz_settings, questions, attempts (including live session state), answers, and the wizard state. Answers to polls sent before a restart are still counted, and timers are re-armed on startup. |

### Commands
`/start` `/newquiz` `/myquizzes` `/import` `/stats` `/settings` `/done` `/skip` `/stop` `/cancel` `/help` `/quiz <id>`

---

## 🗂 Project structure

```
bot.py            entry point: one Application, one polling loop, handlers, menus, error handling
config.py         env vars, Telegram limits, timer choices, data directory
database.py       SQLite schema, in-place migration from the old schema, all queries
pdf_parser.py     PDF/text extraction and the MCQ parser (formats, answer keys, errors)
quiz_engine.py    pure logic: shuffle mapping, Telegram-safe poll payloads, lossless splitting
quiz_creator.py   creation wizard, quiz editor, PDF/Text import flow (state kept in the DB)
quiz_runner.py    quiz sessions, PollAnswerHandler, timer, skip/stop, results, restart recovery
keyboards.py      inline keyboards and help text
stats.py          statistics formatting
requirements.txt  Procfile  railway.json  .python-version
tests/            84 automated tests (parser, engine, DB, end-to-end bot flows, startup)
```

---

## 🚀 Deploy on Railway

1. Push this repository to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo →** pick this repo.
3. **Variables:** add `BOT_TOKEN` = the token from @BotFather.
4. **Add a Volume** (strongly recommended): service → *Settings → Volumes → New Volume*, mount path `/data`.
   Railway then sets `RAILWAY_VOLUME_MOUNT_PATH`, and the bot automatically stores `quiz.db` there.
   *Without a volume, the database is wiped on every redeploy.*
5. Deploy. The logs should show:
   ```
   Logged in as @YourBot (id …)
   Session recovery: {...}
   Starting polling (single instance)…
   ```
6. Open your bot in Telegram and send `/start`.

`railway.json` already sets `startCommand: python bot.py`, `numReplicas: 1`,
`overlapSeconds: 0` and restart-on-failure. The `Procfile` (`worker: python bot.py`) is there too.

### Environment variables

| Name | Required | Default | Purpose |
|---|---|---|---|
| `BOT_TOKEN` | **yes** | – | Telegram bot token from @BotFather |
| `DATA_DIR` | no | Railway volume path, or `./data` | Folder for `quiz.db` and the lock file |
| `DB_PATH` | no | `$DATA_DIR/quiz.db` | Explicit SQLite file path |
| `DROP_PENDING_UPDATES` | no | `false` | `true` ignores updates that queued up while the bot was offline |
| `NEXT_QUESTION_DELAY` | no | `1.5` | Seconds between an answer and the next question |
| `SESSION_EXPIRY_HOURS` | no | `24` | Unfinished sessions older than this are expired on startup |

### "Conflict: terminated by other getUpdates request"
The code creates exactly **one** `Application` and calls `run_polling()` exactly
**once**. A file lock also stops a second process from polling in the same
container. If Telegram still reports a Conflict, **another copy of the bot is
running with the same token** (an old Railway deployment, a second replica, or a
copy on your PC). The bot logs a clear warning, keeps retrying without crashing,
and takes over as soon as the other copy stops. Keep replicas at 1 and don't run
the bot locally while it is deployed.

---

## 📄 Import format guide

```
प्रश्न 1. सुमेलित कीजिए—
(A) रामदेवजी
(B) गोगाजी
(C) तेजाजी
(D) पाबूजी
I. कोलू  II. गोगामेड़ी  III. रामदेवरा  IV. खरनाल
कूट:
(A) A-III, B-II, C-IV, D-I
(B) A-II, B-III, C-I, D-IV
(C) A-IV, B-I, C-II, D-III
(D) A-I, B-IV, C-III, D-II
उत्तर: (A)
व्याख्या: …optional…
```

* The answer choices are the **last complete (A)→(D) sequence** of each question.
  Any A-D data before it (List-I, ordering items, assertion (A)/(R)) stays in the question text.
* Statement numbers `1. 2. 3.` and List-II numbers inside a question are **not** treated as new questions.
* Options may be one per line, several on one line (`(A) x (B) y (C) z (D) w`) or wrap across lines.
  `a)`, `A.`, `(क)` and `(1)–(4)` styles also work.
* The answer can be inline or in an answer-key section at the end. If an inline answer and the key
  disagree, the question is reported as an error instead of being guessed.
* A 5th option `(E) अनुत्तरित प्रश्न` (RPSC style) is ignored. Any other 5th option is reported.

---

## 🧪 Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -q
```
The end-to-end tests plug a fake Telegram Bot API into python-telegram-bot's HTTP
layer, so the real handlers, JobQueue and SQLite run unchanged. The fake enforces
Telegram's poll limits (question ≤300, option ≤100, explanation ≤200), so an
oversized poll fails the test just as it would in production.

---

## ⚠️ Limitations
* **Scanned/image PDFs** need OCR first. The bot reports "no selectable text".
* PDFs made with **legacy Hindi fonts (Kruti Dev etc.)**, or PDFs whose Devanagari conjuncts were
  saved as private glyphs, extract as garbled text. The bot reports "no questions found" instead of
  importing garbage. Use Unicode (Mangal/Noto) PDFs or a `.txt` export.
* Multi-column PDF layouts can interleave columns during extraction. Any resulting problems are
  reported per question.
* Only 4-option (A-D) questions are supported, as specified.
* Telegram's own limits apply: `open_period` must be 5–600s, so all offered timers fit.
