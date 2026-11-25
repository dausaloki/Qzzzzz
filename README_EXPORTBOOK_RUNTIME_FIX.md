# Qzzzzz — /exportbook runtime fix and diagnostics

## Use
The configured Bot Owner sends `/exportbook` or `/exportbook@YourBotUsername` in the same quiz group. The latest finished/stopped session in that chat selects the entire persisted quiz bank; the PDF is sent back to that group. No quiz ID required. Private `/exportbook <quiz_id>` remains supported. The original owner-only rule remains; admins who are not config.is_owner() cannot export. Anonymous-admin/channel posting does not establish the Owner's identity: post as the personal configured Owner account.

## What was actually diagnosed
No access to live logs/processes, server files, production database, Telegram token or installed deployment was available. Uploading files does NOT prove a running Python process loaded them. This patch does not claim to identify an unobserved live-server exception.

A concrete blocker WAS reproduced locally: `_load_watermark()` returned None because the configured default image was missing. The old exporter raised ValueError before rendering. Fonts were available, not degraded. Fix: book-only fallback uses THREE low-opacity existing owner-brand text watermarks on every internal page when the image is unavailable. When the image exists, its existing three-image placement is retained. No image is fabricated/downloaded; normal participant Review PDF behavior is unchanged. Missing/degraded required fonts still produce an explicit error.

Other hardening: valid/nonempty PDF/page validation before sending; explicit 180-second upload/read timeouts; stage-specific Telegram errors; progress acknowledgement; exceptions during error notification logged instead of silently disappearing. Delivery is not automatically retried because a timeout might follow a successful Telegram send.

## Whole command path checked
1. Handler registered as CommandHandler("exportbook", ...) in build_application, all chat types.
2. Real Application.process_update tested with group and supergroup command entities.
3. Membership gate correctly bypasses groups, even with membership enabled; no earlier handler intercepts this command in the inspected code. Commands addressed to a different bot correctly do not invoke ours.
4. Selector filters group_sessions by the exact requesting chat ID.
5. Both finished/stopped work. Admin stop/cancellation is stored as stopped; active/pending/expired are intentionally excluded.
6. Complete db.get_questions(quiz_id) bank remains readable after finishing/stopping, including unasked questions. No participant answer reads required. Deleted source data cannot be reconstructed; missing latest content is reported, never replaced with another group's or an older quiz.
7. Real renderer invoked through asyncio.to_thread.
8. Asset availability, paths, font status and PyMuPDF version now logged at rendering. Existing PyMuPDF dependency still required; import/startup failures appear in host process logs before command registration.
9. PDF header, readability, encryption status and page count validated.
10. Mock transport confirms sendDocument targets the originating group ID.
11. Selection/generation/validation/send/notification failures logged and visible in group when Telegram permits text delivery.
12. Running build marker and disk fingerprints now exposed in logs. Live deployment itself was NOT inspected.

## Next-execution diagnosis (no approval needed)
Expected immediate group acknowledgement includes `group-book-runtime-v3:<update_id>`.

Expected server log stages:
- `exportbook handler registered build=group-book-runtime-v3`
- `exportbook runtime`: literal code marker, bot.py/review_pdf.py absolute paths and disk SHA256, DB path and selector/validator availability.
- `exportbook received`: chat/type/actor/sender_chat and reference.
- `exportbook selected`: same-chat session ID, quiz ID, status, finished timestamp and database path (or selection none).
- `exportbook assets` / `exportbook fonts`
- `exportbook rendered`: PDF bytes/pages.
- `exportbook sending`: exact destination group, bytes/pages.
- `exportbook delivered`: Telegram message ID.

Interpretation:
- No new registration marker after your eventual authorized process reload: wrong entry point/files or old running process. Disk SHA is explicitly a DISK fingerprint, not proof that every loaded module matches disk; the literal marker confirms the new handler code ran.
- Registration exists but no received log: Telegram did not route this update here, another deployment consumes updates, wrong bot mention, webhook/polling setup, or the command was not received. Try `/exportbook@ExactBotUsername`; check bot membership/command permissions and host update errors. Code cannot log an update Telegram never delivers.
- Unauthorized: incorrect/unset OWNER_ID or anonymous/channel identity. No is_admin bypass is added.
- Selection none: inspect the logged DB path and same-chat persisted history; do not reset the DB. A migrated group has a new chat ID; old-chat history is deliberately not borrowed.
- Asset warning: text watermark fallback is active; configure the existing image path if image watermarks are desired.
- select/render or validate failure: exact exception/traceback and reference in logs; fonts/content/import errors are not silently suppressed.
- send_document Forbidden/BadRequest: check group Send Documents/Send Messages permissions. Network timeout is reported, not falsely called success.
- notice failed: Telegram blocked even the error/progress message; logs are the only available evidence.

Only operational IDs/paths and PDF sizes are logged, not tokens, question bodies, answers or participant records. Treat server logs as private.

## Actual tests executed
**32 focused tests passed in 3.570s.** Includes prior group/book coverage plus real PTB dispatcher tests, group/supergroup routing and addressed commands, membership enabled, unauthorized users, invalid PDF rejection, send permission errors, notification failures, timeouts and fallback-watermark geometry.

**231 existing regression tests passed in 16.057s.** No failures/errors/skips in final runs; existing Review PDF, quiz controls, numbering, format imports and privacy tests remain passing.

Total: 263 passing tests across the two runs. Local tests use disposable SQLite, real rendering/rasterization, and mocked Telegram transport. No live Telegram or physical Android test. Initial focused checks correctly failed after the intentional missing-image behavior change and because a font-test double lacked path attributes; diagnostics were made tolerant and the old error expectation was replaced with assertions for exactly three branded fallback placements. No failure was skipped/marked expected-failure. Logs/tests remain in workspace, not added to this minimal ZIP.

## Minimal patch / Android installation
ZIP contains only `bot.py`, `review_pdf.py`, and this README. No dependency/schema/migration/runner/scoring/timer/ranking/numbering changes. No data deleted or changed.

1. Download/extract with Android Files/an archive manager.
2. Back up both existing Python files. Stage the supplied files at the project root, preserving names. Merge any unshared custom edits rather than overwriting them.
3. Keep .env, SQLite/WAL, quizzes, backups, assets and fonts unchanged. No migration/reset needed.
4. Uploading on FadeHost may trigger activation/restart. Do not upload until you intentionally authorize maintenance. No GitHub push, deployment, restart or live-host modification was performed here. A currently running Python process will not load changed files until an eventual authorized reload.
5. After your authorized activation, use the command in the group from your personal Owner account. The marker/messages above identify precisely how far the new path ran.

Rollback: restore the two backed-up code files, never reset the database.
