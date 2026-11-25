# Qzzzzz — /exportbook directly in the quiz group

## Usage
The configured Bot Owner sends `/exportbook` (or `/exportbook@YourBotUsername`) inside the quiz group. No quiz ID is needed. The PDF is delivered directly to THAT group, not the owner's private chat.

The existing owner-only security rule is retained: config.is_owner(), not is_admin(). A group admin who is not the configured Bot Owner cannot export. This patch changes group selection/delivery, not who is authorized. Authorization occurs before quiz lookup and is rechecked before sending.

## Selection and contents
- Reads persisted group_sessions filtered by the exact requesting chat ID.
- Eligible states: finished and stopped. The actual runners store admin stop/cancellation as stopped, not cancelled/canceled.
- Latest means greatest finished_ts (started_ts fallback for legacy NULL finish times), then highest session ID for ties.
- Active, pending and expired sessions are excluded. Participant answers/attempts are not consulted.
- Exports ALL current persisted questions belonging to the selected quiz_id, not only questions asked or answered during the session. Includes options, correct answer letters and available explanations.
- Existing book cover, Hindi/English renderer, wrapping, pagination, branding and three image watermarks per internal page are reused unchanged.
- Arguments in a group cannot override the same-chat selection: even if supplied, they are ignored.
- Clear message when that group has no eligible quiz. Missing/deleted/empty content for the latest selected quiz is reported; the bot never silently substitutes an older quiz or one from another group.
- Private `/exportbook <quiz_id>` remains available to the owner as before.

This is a complete study-book export, not a participant Review PDF, personal report, HTML report or leaderboard. It reads the current quiz bank; deleted historical questions cannot be reconstructed without an existing backup. Source references in question content are preserved. Telegram receives protect_content=True as before (not DRM).

## Minimal patch
Only two production files, at their original project-root paths:
- bot.py: existing export command now branches for group selection and same-group delivery.
- review_pdf.py: one added read-only same-group selector wrapping the existing book renderer.

No renderer redesign, scoring, negative marking, timer, ranking, numbering, runner, import, database schema/migration or participant Review PDF changes. No quiz data updates/reset/deletion. Existing asset/font configuration and dependencies are reused. The prior book feature's code is included in these complete replacement files.

## Actual tests
- 25 focused tests passed in 2.891s (10 group-specific plus 15 prior book tests).
- 231 existing regressions passed in 16.332s.
- Total 256 passing tests; no failures/errors/skips in these runs.

Focused tests used real temporary SQLite sessions with finished/stopped/active/pending/expired states and multiple group IDs. Verified complete stopped/unasked question bank, persisted finish-time ordering and tie breaks, strict same-group isolation, no-history handling, deleted content, unchanged DB dump, owner checks before database access, actual PDF bytes delivered to the same group with mocked Telegram, and preserved private export. Existing cover/multipage, Hindi/English extraction/rasterization, long-content wrapping, three watermark placements/opacity masks, missing assets/fonts, oversize and delivery-failure tests also passed.

Test image was temporary, through the existing real watermark loader. Live Telegram, physical Android and production-host assets were not tested. The configured watermark/font resources must be available on the host; book export reports a clear error if not. No live API send, GitHub push, deployment or restart occurred. Test files/logs remain outside this minimal ZIP.

## Android installation
1. Download/extract this ZIP using Android Files/an archive manager.
2. Back up your current bot.py and review_pdf.py. Stage the two supplied files at the project root. Merge any unshared custom edits instead of overwriting them.
3. Leave .env, SQLite/WAL, quiz data, assets, fonts and backups unchanged. No migration/reset is needed.
4. Uploading to FadeHost can trigger activation/restart. Do not upload until you intentionally authorize that maintenance action. This patch does not deploy or restart anything itself.
5. After your authorized activation, finish or stop a group quiz, then send `/exportbook` there from the configured Bot Owner account. Ensure the bot can receive commands and send documents in that group.

Rollback: restore the two backed-up Python files; do not roll back/reset the database.
