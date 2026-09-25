# 🎯 Telegram Quiz Bot (QuizBot-style) — Railway ready

A production-ready Telegram quiz bot that works like the official **@QuizBot**:
create quizzes step by step, import them from **PDF / text**, and play them
**one question at a time** with Telegram's **native quiz polls**.

* Python 3.11+ · python-telegram-bot **22.8** (Bot API 9.6) · SQLite · PyMuPDF · Tesseract OCR (Hindi + English) · long polling
* Hindi + English. The source text is never guessed, shortened or lost.

---

## ✨ Features

| Area | What it does |
|---|---|
| Join gate | Users must be members of the Telegram group `REQUIRED_CHAT` (default **@kalam_kranti**) before they can use anything. `/start` shows *👋 Welcome!* with **📢 Join Group** and **✅ I've Joined**. The click itself proves nothing — membership is always checked with `getChatMember` (creator / administrator / member, restricted only with `is_member`; left / kicked are refused). Verified users are cached and re-checked on every `/start` and every `MEMBERSHIP_RECHECK_SECONDS` (10 min); a user who left must join again. If the bot is an admin of the group, `chat_member` updates revoke access immediately. Inline mode is gated too. Group quiz participants are never forced into a private chat. |
| Main menu | ➕ New Quiz · 📚 My Quizzes · 📥 Import · ▶️ Start Quiz · 📊 Statistics · ⚙️ Settings · ❓ Help |
| Question tag | Every question the bot sends — private and group polls, manual / PDF / TXT / forwarded questions, and the full-text message used for long questions — starts with `QUESTION_TAG` (default **जय श्री श्याम**), exactly once (a source that already starts with the tag is not doubled). The tag counts toward Telegram's 300-unit poll limit: when question + tag do not fit, the complete question is sent as a message and the poll gets a compact prompt with exact option mapping. Stored questions are never modified. |
| Forwarded content | Forward quiz polls or text MCQs from any channel/group/chat. With no quiz open the bot asks *📚 यह Question किस Quiz में जोड़ना है?* (➕ New Quiz → name, or 📚 Existing Quiz). All forwards go into ONE quiz in forwarding order (17 forwards → 17 questions; items forwarded while the bot waits are queued), each confirmed with *✅ Question imported · 📚 Quiz · 📝 Total Questions*; `/done` → *✅ Quiz तैयार है!* card. Question, options, `correct_option_ids` and explanation are taken from the poll; if Telegram hides the answer the bot says so and asks — it never guesses. Text MCQs use the PDF/Text parser (all formats, several numbered questions in one message) and keep the source wording; the origin (`forward:@channel/123`) is stored per question. Multi-answer polls are refused with a message, never silently dropped. |
| Creating a quiz | Conversational, like @QuizBot (no "Step N" screens): `/newquiz` → send the **title** → send a **description** or `/skip` → send questions one after another → `/done`. Every question is **either** a native Telegram quiz poll made with the **📝 प्रश्न बनाएँ** reply-keyboard button (question, 2–12 options, the correct answer and the explanation are captured from the poll) **or** one text message with `(A) (B) …` options plus optional `उत्तर: B` / `व्याख्या: …` lines (needed for questions/options longer than Telegram's poll limits). If the correct answer is missing (regular poll, forwarded quiz with hidden answer, typed question without `उत्तर:`), the bot asks with buttons — it never guesses. `/undo` removes the last question; max 100 questions. Any other text/photo/video/document sent before a question is **pre-question content of the next question** — it never becomes a question. `/done` needs at least 1 question and opens the management screen. `/cancel` deletes the draft. |
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
| Management screen | 📚 title · 📝 Questions: N · timer · shuffle, with ▶️ Start Quiz · 📤 Share Quiz · ✏️ Edit Quiz · 📊 Statistics · ⚙️ Settings · 🗑️ Delete Quiz. **My Quizzes** lists only your own quizzes. |
| Edit | Title, description, 📋 question list → per question: full text, ✏️ edit text, 🔤 edit options (options + correct answer are saved together atomically), ✅ change correct answer, 💡 edit/remove explanation, ⬆️/⬇️ reorder, 🗑 delete; ➕ add questions (same conversational flow). Ownership is checked server-side on every button. |
| Settings (per quiz) | Timer, shuffle questions, shuffle options |
| Start | ▶️ Start / share link → a **"Get ready"** card (title, questions, timer, shuffle) → **I'm ready** button → polls. A double tap on the same button is ignored. |
| Share | `https://t.me/<BOT_USERNAME>?start=quiz_<QUIZ_ID>` (every user gets an independent private session), a "Share to chat" button, and — only if inline mode is enabled in BotFather — an inline quiz card button (`@YourBot quiz_<id>`). |
| Group quiz | 📤 Share → **👥 Group में Quiz चलाएँ** (Telegram's `?startgroup=quiz_<id>` link) or `/quiz <id>` in a group → **▶️ Start Quiz in this Group**. The quiz runs in the group with native, **non-anonymous** `sendPoll(type="quiz")`, one question at a time, closed after the quiz timer (`GROUP_DEFAULT_TIMER` if the quiz has none). Every member's `poll_answer` is stored separately (session, quiz, question, group, poll, option, correct, score, timestamp — one row per participant, never overwritten; late answers to a closed question are ignored). Then **🏆 Quiz Result**: 🥇🥈🥉 name — x/y with Correct / Wrong / Skipped / Score / Percentage. Sessions are independent (many groups and private chats can play the same quiz simultaneously; one active quiz per group), survive restarts, and `/stop` works for the starter or a group admin. **👤 Start Privately** still gives every member their own private session. |
| Results | 📊 Result — Quiz, Total, Correct, Wrong, Skipped, Score x/y, Percentage (2 decimals), Time, and your rank (best attempt) · 🔁 Try Again · 📤 Share Quiz · 📊 Statistics · 🏠 Main Menu |
| Stats | Owner: attempts, correct/wrong/skipped, average, top users (best attempt per user). Other players see only their own results and rank. `/stats`: personal result history. |
| `/stop` | Stops the active quiz, closes the open poll and saves the partial result |
| Persistence | Everything lives in SQLite: users, quizzes, quiz_settings, questions, attempts (including live session state), answers, and the wizard state. Answers to polls sent before a restart are still counted, and timers are re-armed on startup. |

### Commands
`/start` `/newquiz` `/myquizzes` `/import` `/stats` `/settings` `/done` `/undo` `/skip` `/stop` `/cancel` `/help` `/quiz <id>`

`/skip` skips the optional step while creating (description / explanation) and skips the current question while playing.

### Optional: inline mode
To let users post a quiz card into any chat via `@YourBot quiz_<id>`, enable it in @BotFather → `/setinline`.
The bot reads `supports_inline_queries` from `getMe` at startup and shows the inline button only when it is on
(after enabling it, restart the service).

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
quiz_runner.py    private quiz sessions, PollAnswerHandler, timer, skip/stop, results, restart recovery
group_runner.py   quizzes inside groups: native non-anonymous quiz polls, per-user answers, leaderboard
membership.py     join gate: getChatMember verification, cache, chat_member revocation
keyboards.py      inline keyboards and help text
stats.py          statistics formatting
Dockerfile        python:3.11-slim + pinned Hindi/English OCR models (sha256-checked) — used by Railway
scripts/          fetch_tessdata.py: downloads the exact OCR models (image build + local tests)
requirements.txt  Procfile  railway.json  .python-version  .dockerignore
tests/            234 automated tests (parser, OCR, engine, DB, end-to-end bot flows, real startup)
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
   Logged in as @YourBot (inline mode: off)
   Session recovery: {...}
   Group session recovery: {...}
   Join gate: users must be members of @kalam_kranti (the bot must be a member/admin there)
   OCR ready (eng+hin) — tessdata: /usr/share/tesseract-ocr/5/tessdata
   Starting polling (single instance)…
   ```
6. **Add the bot to @kalam_kranti** (as admin is best) — otherwise `getChatMember` fails and nobody can be verified.
7. Open your bot in Telegram and send `/start`.

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
| `REQUIRED_CHAT` | no | `@kalam_kranti` | Group users must join (`@username` or `-100…` id). **The bot must be a member of this group** (admin for a channel; admin also enables instant revocation via `chat_member`). Empty = gate off |
| `REQUIRED_CHAT_URL` | no | `https://t.me/<username>` | Link of the 📢 Join Group button (needed for a private group: its invite link) |
| `MEMBERSHIP_RECHECK_SECONDS` | no | `600` | Re-check interval for verified members |
| `QUESTION_TAG` | no | `जय श्री श्याम` | Tag at the top of every question sent; empty = no tag |
| `GROUP_DEFAULT_TIMER` | no | `30` | Seconds per question in groups when the quiz has no timer |
| `GROUP_NEXT_DELAY` | no | `2.0` | Pause between group questions |

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
* The join gate depends on `getChatMember`: if the bot is not in the required group, Telegram refuses the call and users see "verify नहीं हो सकी" (never a free pass). Previously verified users keep access during such an outage.
* Group quizzes need the bot in the group; Telegram delivers `poll_answer` only for non-anonymous polls, so group polls are always non-anonymous (voters are visible). A question waits for the full timer, like @QuizBot.
* **Native poll creation button** (`KeyboardButtonPollType`) works only in private chats, so quizzes are created in the bot's private chat.
  A quiz poll *forwarded* from elsewhere may arrive without its correct answer/explanation (Telegram hides them) — the bot then asks for the answer.
  Multiple-answer polls cannot become quiz questions (a Telegram quiz has exactly one correct answer).
* Telegram polls cannot contain media; pre-question photos/videos are sent as separate messages right before the poll.
* Webhook mode is not used: the bot uses long polling (one instance). If a webhook was set for the token earlier,
  PTB's `run_polling` deletes it at startup.
