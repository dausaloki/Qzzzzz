# 🎯 Telegram Quiz Bot (QuizBot-style) — Railway ready

A production-ready Telegram quiz bot that works like the official **@QuizBot**:
create quizzes step by step, import them from **PDF / text**, and play them
**one question at a time** with Telegram's **native quiz polls**.

* Python 3.11+ · python-telegram-bot **22.5** · SQLite · PyMuPDF · Tesseract OCR (Hindi + English) · long polling
* Hindi + English. The source text is never guessed, shortened or lost.

---

## ✨ Features

| Area | What it does |
|---|---|
| Main menu | ➕ New Quiz · 📚 My Quizzes · 📥 Import PDF/Text · ▶️ Start Quiz · 📊 Quiz Stats · ⚙️ Settings · ℹ️ Help |
| Creator wizard | Name → Description → optional pre-question text/photo/video/document → Question → **2 to 12 options** → correct answer → optional explanation → add another / `/done` (max 100 questions) |
| Question formats | Normal MCQ, statement based, matching, List-I/List-II, assertion-reason, ordering. Multi-line text is saved exactly as typed. You can paste the question and its options together and the bot detects the options. |
| PDF / Text import | Text PDFs, **scanned/image PDFs (OCR, Hindi + English)**, PDFs in legacy Hindi fonts (Kruti Dev etc. are OCR'd), **two-column** layouts (read column by column), `.txt` files or pasted text (long text can arrive in several messages) |
| Options | 2–12 options per question (Telegram's limit, Bot API 9.1+). **(E) and later options are never dropped.** |
| Numbering | Numbering that restarts per section/page (`भाग-अ 1…100, भाग-ब 1…100`) is detected, with or without headings. Section-wise or combined answer keys are matched to the right section. |
| Parser | Question markers `प्रश्न 1.` `1.` `1)` `Q1` `Q.1` `Question 1:`. It finds the **real** answer choices even when the question body contains A-D data (matching lists, List-I/II, ordering items, assertion (A)/(R), statements `1. 2. 3.`). |
| Answer key | `उत्तर: (A)`, `उत्तर: A`, `Answer: B`, `Ans. (C)`, `सही उत्तर: D`, answer-key sections (`Answer Key` / `उत्तर कुंजी` / `उत्तरमाला`) in the forms `1-A 2-C`, `1. (B)`, `1 A`, vertical tables, or a key with explanations. **Answers are never guessed.** |
| Error reporting | Every problem is shown with its section, question number and the exact reason. A partial quiz is created **only after you confirm it explicitly**. OCR'd or ambiguous items go through a **review** step before the quiz can be created. Every removed line (page numbers, headers, watermarks) is listed, never silently dropped. |
| Native quiz | `sendPoll(type="quiz")`, one question at a time. Telegram shows correct/wrong natively, then the explanation, then the next question. |
| Long text | If a question is over 300 characters or an option over 100, the **complete** question and **all complete** options are sent as a normal message first. The poll then uses a short prompt, plus compact `(A)`…`(L)` labels if any option is too long. Nothing is truncated and a poll option never exceeds 100 characters. |
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
pdf_extract.py    PDF → text: text layer, column order, garbled/legacy-font detection, OCR (dual-pass verify)
pdf_parser.py     MCQ parser: formats, sections/restarts, answer keys, per-question errors
quiz_engine.py    pure logic: shuffle mapping, Telegram-safe poll payloads, lossless splitting
quiz_creator.py   creation wizard, quiz editor, PDF/Text import flow (state kept in the DB)
quiz_runner.py    quiz sessions, PollAnswerHandler, timer, skip/stop, results, restart recovery
keyboards.py      inline keyboards and help text
stats.py          statistics formatting
Dockerfile        python:3.11-slim + pinned Hindi/English OCR models (sha256-checked) — used by Railway
scripts/          fetch_tessdata.py: downloads the exact OCR models (image build + local tests)
requirements.txt  Procfile  railway.json  .python-version  .dockerignore
tests/            169 automated tests (parser, OCR, engine, DB, end-to-end bot flows, real startup)
```

---

## 🚀 Deploy on Railway

1. Push this repository to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo →** pick this repo.
3. **Variables:** add `BOT_TOKEN` = the token from @BotFather.
4. **Add a Volume** (strongly recommended): service → *Settings → Volumes → New Volume*, mount path `/data`.
   Railway then sets `RAILWAY_VOLUME_MOUNT_PATH`, and the bot automatically stores `quiz.db` there.
   *Without a volume, the database is wiped on every redeploy.*
5. Deploy. Railway builds the `Dockerfile`. It downloads the pinned tessdata 4.1.0 Hindi + English models and fails the build if OCR would not work. The logs should show:
   ```
   Logged in as @YourBot (id …)
   Session recovery: {...}
   OCR ready (eng+hin) — tessdata: /usr/share/tesseract-ocr/5/tessdata
   Starting polling (single instance)…
   ```
6. Open your bot in Telegram and send `/start`.

`railway.json` sets `builder: DOCKERFILE`, `startCommand: python bot.py`, `numReplicas: 1`,
`overlapSeconds: 0` (the old deployment stops before the new one polls) and restart-on-failure.
The `Procfile` (`worker: python bot.py`) is kept for other hosts. If you deploy without Docker
(Nixpacks/Heroku-style), everything works except OCR: scanned PDFs are then reported, not imported.

### Environment variables

| Name | Required | Default | Purpose |
|---|---|---|---|
| `BOT_TOKEN` | **yes** | – | Telegram bot token from @BotFather |
| `DATA_DIR` | no | Railway volume path, or `./data` | Folder for `quiz.db` and the lock file |
| `DB_PATH` | no | `$DATA_DIR/quiz.db` | Explicit SQLite file path |
| `DROP_PENDING_UPDATES` | no | `false` | `true` ignores updates that queued up while the bot was offline |
| `NEXT_QUESTION_DELAY` | no | `1.5` | Seconds between an answer and the next question |
| `SESSION_EXPIRY_HOURS` | no | `24` | Unfinished sessions older than this are expired on startup |
| `OCR_LANGUAGES` | no | `eng+hin` | Tesseract languages for scanned pages |
| `OCR_VERIFY` | no | `1` | Second English-only OCR pass that checks option labels and answer letters; disagreements become review items |
| `OCR_DPI` | no | `300` | Render resolution for OCR |
| `MAX_OCR_PAGES` | no | `60` | Upper limit of pages OCR'd per file (the rest is reported) |
| `TESSDATA_PREFIX` | no | set in Dockerfile | Folder with `*.traineddata` |
| `TELEGRAM_API_BASE` | no | – | Custom Bot API server URL (for tests / self-hosted Bot API) |

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
* The answer can be inline or in an answer-key section (per section or at the end). If an inline answer
  and the key disagree, or a question has no answer, it is reported as an error instead of being guessed.
* 2–12 options: `(A)…(L)`. A 5th option such as `(E) अनुत्तरित प्रश्न` is **kept** as a real option.
* Supported: MCQ, statements, matching, List-I/List-II, assertion-reason, ordering, "which are correct"
  (single answer), numerical options `(1)–(4)`, Hindi `(क)–(घ)` options.

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
`tests/test_real_startup.py` launches `python bot.py` as a real process against a local fake
Bot API, injects two 409 Conflicts, and checks that exactly one `getUpdates` is ever in flight and
that a second process refuses to start. OCR tests need the model files:
`TESSDATA_PREFIX=$HOME/tessdata python scripts/fetch_tessdata.py`, then run the tests with the same
`TESSDATA_PREFIX`. A test checks that these files are byte-identical to what the Docker image installs.
Debian's apt "fast" models are deliberately **not** used: with them, Hindi words were misread and an
answer-key letter was lost in the scanned-PDF test.

---

## ⚠️ Limitations
* **Poll length:** all limits are measured in UTF-16 units, as Telegram counts them (emoji and `𝑥` count as 2).
* **OCR is not perfect.** Poor scans, photos, handwriting or tiny fonts can misread words or matras.
  Answers are cross-checked by a second OCR pass. Unreadable or conflicting letters are never guessed
  (they become errors), and every OCR'd quiz goes through a review step. Still, proof-read OCR'd
  question text before sharing.
* Telegram bots can only download files up to **20 MB**. Large PDFs must be split.
* OCR is slow on a small Railway instance (~2–5 s per page), and at most `MAX_OCR_PAGES` pages are OCR'd.
* Complex layouts (tables drawn as images, 3+ columns, text inside figures) may need review.
* The DB supports at most 100 questions per quiz; longer PDFs can be split into several quizzes from the import screen.
* Telegram's own limits apply: `open_period` must be 5–600s, so all offered timers fit.
