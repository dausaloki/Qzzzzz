# Qzzzzz TXT formats and statement display patch

## Contents
- `quiz_engine.py` — the ONLY changed production file; keep this at the project root.
- `tests/test_txt_formats_display.py` — 23 focused regression/integration tests.
- `TEST_REPORT_TXT_FORMATS.md` — actual results, scope, limitations and compatibility evidence.
- `tests/results/txt_formats_all_tests.txt` — unabridged final 214-test run.

Tested against the inspected `main` baseline at commit `6ceece417aad24e539085df5e130ead668431909`. If your server has other unshared edits to `quiz_engine.py`, merge rather than blindly overwriting them.

## Changes
TXT-only explicit block handling repairs assertion numeric options, statement boundaries, matching answer-code selection, case-to-question context, and the audited separator/preamble pollution. Outer source numbering is stored; embedded original question headings remain visible. Single-answer option order/key is preserved. The previously confirmed two-option topicwise regression is also corrected.

Default statement-combination display prioritizes full source text/source numbering over decorative branding. Truly oversized questions still use the existing full question/options message immediately before the native poll. No statements are summarized or clipped. User-selected custom presentation templates retain their existing behavior/content.

## Important limitations
This patch does NOT introduce free-text answers, auto-generated True/False choices or genuine multi-select scoring. The supplied optionless formats 5–7 and multiple-correct format 9 are safely rejected as before. All nine files were tested; this is NOT a claim that all nine response modes are now playable.

No database migration, scoring/timer/leaderboard/result changes, reset, push, deployment or service restart is included. Existing stored malformed quizzes are NOT rewritten. To obtain corrected parsed content, import the original TXT as a NEW quiz after you intentionally activate the patch; keep historical quizzes/attempts intact. This does not retroactively repair or alter a running quiz.

## Apply using Android
1. Download this ZIP to your phone. Extract it with Android Files or an archive manager.
2. Back up your current `quiz_engine.py` separately. Preserve all other files, especially the SQLite database/WAL, backups and `.env`.
3. Compare/stage the extracted `quiz_engine.py` against your project root. Replace only that one production file; do not replace the whole project directory.
4. Optionally copy the focused test and reports into their original relative paths. No Python dependency or environment-setting changes are required for production.
5. Uploading to the live host may itself trigger deployment/restart. If FadeHost auto-restarts on upload, do NOT upload until you intentionally authorize that deployment. This archive performs no upload, push, restart or deployment. A running Python process will not pick up this file until an eventual authorized reload/restart.
6. Once you choose to activate it, import copies of the original TXT into new quizzes and verify the statement question plus a case-study pair in a test chat before using them in a live group.

Rollback: restore your backed-up `quiz_engine.py` during an authorized maintenance action. No database rollback/reset is needed because the patch has no migration. Keep quizzes/history; do not delete them as part of applying or rolling back.

## Run focused tests on a staging copy
The nine original `Format_*.txt` attachments must be available in one directory; they are deliberately not copied into this minimal patch. Tests hash-check their original bytes and FAIL rather than silently skip if missing or different.

From the project root, with existing project requirements installed:

```sh
QZZZZZ_TXT_TEST_DIR=/path/to/original_samples python -m unittest discover -s tests -p test_txt_formats_display.py -v
```

The focused tests do not need Playwright or live Telegram. The full historical regression log additionally includes earlier HTML/browser tests; their unchanged test sources were restored externally from previous patches and are not redundantly bundled here.

Actual Android Telegram appearance and live Bot API acceptance remain unverified. Local runner tests verify exact outgoing text/options, not Telegram's UI.
