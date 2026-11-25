# Qzzzzz — Owner-only Quiz PDF Book Export

## Use
In the configured Bot Owner's PRIVATE chat with the bot:

`/exportbook <quiz_id>`

Example: `/exportbook Ab12Cd34` (replace with a real quiz ID).

The command is registered but adds no menu button or public command-menu entry. Only `config.is_owner(user_id)` authorizes it; admin/quiz-creator permissions are not substitutes. Group requests are redirected to private chat without looking up quiz content. Invalid/missing IDs, missing quizzes and empty quizzes get clear errors. Authorization is checked again before sending. The document is sent only to that verified owner's Telegram ID, with Telegram content protection requested (not a DRM guarantee).

## Book design
- Dedicated blue/white branded cover, full flowing quiz title, study-series subtitle, owner name/username and official group link.
- Questions begin on a new page, never on the cover.
- Existing portrait dimensions, readable font sizes, section headings, source passages, original question/statement text, options, answer letters and relevant explanations.
- Automatic wrapping and page breaks; long content is flowed, never intentionally shortened.
- Three low-opacity instances of the existing image watermark on EVERY internal page, positioned near the top/middle/bottom; no cover watermark.
- Existing Hindi/Devanagari shaping, English font handling, searchable Unicode text layers, branded headers/footers and page numbers reused.
- Book questions use sequential book positions. No changes to active-session numbering or stored source references.

## Strict scope
Exactly TWO existing production files change, preserved at project-root paths:
1. `review_pdf.py`: adds separate `_BookCanvas` and `build_quiz_book()`; every pre-existing function/class remains AST-identical.
2. `bot.py`: adds `exportbook_cmd()` and one command registration. All other original code remains byte-identical.

No new runtime module/dependency, button, database schema/migration, quiz-data update or production-data deletion. No changes to scoring, negative marking, timers, rankings, question/import formats, runners, active numbering or quiz behavior. Existing participant Review PDFs, consent, auto-delivery and personal records are unchanged. Export reads only quiz metadata/questions; it does not use attempt/participant responses or review-PDF tracking tables.

## Assets and fonts
Uses the EXISTING `WATERMARK_IMAGE_PATH`, falling back through the existing loader to `assets/group_dp_watermark.jpg`. No image is newly bundled or invented. If the existing image is missing/unreadable, export reports that problem instead of silently omitting watermarks. Uses existing PDF font configuration/fallbacks; a degraded font status also stops book export with a clear message. The normal Review PDF's fallback behavior is not changed.

The workspace snapshot did not contain the default assets directory. Watermark tests used a temporary local test image through the real loader and renderer; they are not proof that the live server's actual watermark file exists. If your running Review PDF already has its configured watermark/font assets, this command reuses them. No asset upload or server modification has been performed here.

## Android-friendly installation
1. Download this ZIP and extract it with Android Files/an archive manager.
2. Back up the current `bot.py` and `review_pdf.py` from your project.
3. Stage the two supplied Python files at the project root. If you have unshared edits, merge carefully instead of overwriting them.
4. Leave `.env`, SQLite/WAL, backups, assets, fonts and all quiz data untouched. No migration/reset is needed.
5. Uploading files to FadeHost may trigger deployment/restart. Do not upload until you intentionally authorize activation. This archive executes nothing; no GitHub push, live deployment or restart was performed.
6. Once you choose to activate it, privately send `/exportbook <real_quiz_id>` from the configured OWNER_ID account. There is no need to complete a quiz or enable participant Review PDFs to export a study book.

The existing `OWNER_ID` configuration must identify the actual owner's numeric Telegram user ID. Branding names/usernames alone do not grant permission. Rollback is restoring these two backed-up Python files; do not reset the database.

## Actual executed test results
- **15 focused book-export tests passed in 2.113 seconds.**
- **231 existing regression tests passed in 18.591 seconds.**
- Total: **246 passing tests**, across the two runs. No skips/failures/errors in the final runs.

Focused tests covered dedicated cover and question-page separation; multipage generation; Hindi/English searchable text plus actual raster rendering; long statements/options/explanations and page wrapping; three image placements on every internal page with soft-opacity masks; valid, malformed, missing and empty quiz IDs; read-only database dump equality; unauthorized builder/handler rejection before lookup, including non-owner admins; owner group-chat rejection; private delivery of actual generated PDF bytes using mocked Telegram; reauthorization before sending; oversize output rejection; missing assets/degraded font errors; and delivery failure handling.

Initial focused assertions needed correction for PDF text split across lines/page footers and image draw-order reversal; production content was not shortened to satisfy them. The final assertions check retained long-text occurrences, sorted placement geometry, page bounds, text extraction, and rasterization.

Regressions include previous import/format, numbering, controls, privacy, Review PDF, HTML/browser and backup tests. Existing PDF generation/delivery/button tests still pass. New focused tests were kept outside the project/ZIP to honor the two-production-file scope. Tests/logs are retained in the workspace, not bundled as extra patch files.

Scope audit: manifest hashes show only the two production files changed. Existing `review_pdf.py` definitions unchanged; existing bot code differs only by the new handler and its registration.

No live Telegram API send or physical Android test was performed. Telegram transport was mocked; SQLite tests used temporary databases. Existing dependency/test-only Chromium setup was installed only in the sandbox. Actual production assets, production credentials and live-host configuration were not altered. Very large exports remain subject to server resources and the existing 49 MiB send guard; no automatic retry or persistent export job is introduced.
