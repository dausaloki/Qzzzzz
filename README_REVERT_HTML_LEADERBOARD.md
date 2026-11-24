# Qzzzzz — remove only the newly added aggregate HTML leaderboard

## Rollback
The TWO production files in this archive are restored byte-for-byte to their saved pre-leaderboard versions:
- `group_runner.py`
- `html_report.py`

Removed: `_group_leaderboard_attachment`, `build_group_leaderboard_html`, the newly added html_report import in group_runner, and the automatic aggregate HTML generation/send hook in group completion. No aggregate leaderboard HTML file is generated or sent by that flow.

The new leaderboard feature did NOT add a Telegram button. The earlier `मेरा HTML Report (private)` link, `/htmlreport`, report deep links and individual saved-result renderer existed before it and are intentionally preserved. Existing private-quiz HTML behavior is likewise unchanged. This is NOT a removal of all historical HTML reporting.

The previous text result, its markup/chunking, Review PDF button, PDF auto-delivery hook and PDF consent restrictions are restored/preserved exactly. No scoring, negative marking, ranking, timer, answer processing, database, question formats, TXT/PDF imports or quiz data changed. Earlier TXT fixes are not rolled back.

## ZIP contents
- The two production Python files above.
- `tests/test_group_html_delivery.py`: restores the original pre-leaderboard regression expectations.
- `tests/test_html_leaderboard.py`: replaces the removed feature's tests with seven rollback/PDF-preservation tests, avoiding stale tests that require the removed feature.
- This README/test report.
- `tests/results/leaderboard_rollback_all_tests.txt`: actual final verbose test output.

## Executed verification
**221 tests passed in 18.152 seconds; no failures/errors/skips.** This includes 214 previous regressions and seven rollback tests.

Rollback tests verify:
- New aggregate renderer/adapter no longer exist.
- Finished/stopped group quizzes use the existing text result with no automatic HTML attachment.
- Review PDF button and earlier private HTML link remain unchanged.
- Actual personalized Review PDF generation produces valid `%PDF-` bytes and auto-delivers privately with valid pre-start consent (Telegram transport mocked).
- The existing `p:g:<session>` callback also generates/delivers a PDF privately.
- PDF consent restrictions remain enforced.
- Repeated finish calls do not duplicate results or invoke the PDF hook twice.

File-hash comparison confirms only the two production files changed during rollback and every other existing project file remains unchanged. Both restored files exactly match the pre-feature backups.

Restored SHA256:
- group_runner.py: `0919e4ba5b56be81f37e71bec04d2fe6ba81d00d875ee6d6555fb8772914d725`
- html_report.py: `6cfdded13b30276856b7704f8bce599507c273a787c95711f86978fd40022029`

Tests used temporary SQLite, actual local PDF/HTML rendering and mocked Telegram. No live Telegram/device verification, FadeHost access, deployment, restart, GitHub push or production-data changes. Sandbox log timestamps are not a verified local date.

## Apply from Android
1. Download and extract this ZIP using Android Files/an archive manager.
2. Back up your current two Python files. Do not touch `.env`, SQLite/WAL, quizzes, attempts or backups.
3. Stage `group_runner.py` and `html_report.py` at the project root. If you have made additional unshared edits since the leaderboard patch, merge carefully rather than overwriting those edits.
4. Copy the two test files only if maintaining the project's test suite; they replace the feature-specific expectations. Reports/logs are not runtime requirements.
5. Uploading to FadeHost can trigger a deployment/restart. Do not upload until you intentionally authorize that maintenance action. This ZIP does not execute anything or restart the bot. A running Python process requires an eventual authorized reload to use changed files.

No database migration/reset is required. Existing downloaded HTML documents/messages are not remotely deleted; the feature is removed from future quiz-finish execution.

Focused staging test: `python -m unittest discover -s tests -p test_html_leaderboard.py -v`.
