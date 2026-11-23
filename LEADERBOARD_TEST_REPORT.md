# Qzzzzz HTML leaderboard — implementation and test report

## Final outcome
**237 tests passed in 20.440 seconds.** Final verbose results: `tests/results/leaderboard_all_tests.txt`.

- 23 new focused leaderboard tests.
- 214 prior tests, including TXT formats/imports, quiz controls, backup integrity, download handling, existing HTML/browser reports and private group-report access.
- Exactly one prior assertion updated for the newly requested behavior; no tests skipped or marked expected-failure.

No live Telegram, FadeHost access, production DB changes, reset/deletion, deployment, restart or GitHub push. SQLite tests used disposable databases; Telegram sends were mocked. Browser tests used real Chromium locally.

## Requested table and delivery
The final group result now automatically sends an HTML document with these columns:
`# | Name | ✅ | ❌ | Score | Time | %`

Rank medals for the first three; ranks 4 onward are plain numbers. Rows retain the existing rank order. The table has repeated headings per page, alternating row colors, complete wrapping names, Unicode/emoji support, page labels, Previous/Next controls and a page selector. Fifty rows per page is not a total limit: every participant remains in the file. A horizontal scrolling region keeps all columns accessible on mobile; the document itself does not overflow the viewport. No external styles, fonts, scripts, CDNs or network requests are required. Without JavaScript all pages remain visible. Printing shows all page sections.

A normal Telegram HTML-formatted message cannot render an HTML/CSS/JavaScript table. The original text result is therefore preserved and followed automatically by the browser-openable attachment in the same group. No new bot button, command, web app or external service. Existing personal HTML/PDF controls stay unchanged. The automatic table is PUBLIC aggregate data only, not an individual's question/answer review.

The unavailable reference screenshots could not be compared pixel-for-pixel. The final user instruction authorized proceeding with the written specification; that is the tested reference for structure and behavior.

## Source and calculation preservation
Only two existing production files changed:
- `group_runner.py`: added `_group_leaderboard_attachment` and a guarded asynchronous generation/send hook in `_finish_locked`.
- `html_report.py`: appended `build_group_leaderboard_html`; every existing top-level function remains AST-identical.

The existing `group_runner.result_text` function is AST-identical to the baseline. The database query and all existing ranking/scoring/time implementations remain unchanged. The adapter reads `db.group_leaderboard(session_id)` after completion and uses precisely the same stable net-score sort and existing `quiz_modes` functions as `result_text`.

No-negative order: existing SQL `correct DESC, elapsed ASC, user_id ASC`.
Negative order: existing stable sort by descending `compute_net_score(correct, wrong, negative_mark)`; tied net scores keep the SQL order. No alternative tie-breaking/ranking rule introduced.

Score is `row.score / denominator` without negative marking; when negative marking applies it is existing rounded/formatted net score / denominator. The document explicitly labels this convention. Correct/wrong counts are existing stored aggregates. Percentage is the same `100 * correct / denominator`, formatted to two decimals; it is NOT net-score percentage.

Time is the query's existing `SUM(elapsed)` value, displayed in seconds to two decimals. Raw elapsed is retained as a data attribute; display rounding never feeds ranking. No elapsed recomputation, wall-clock completion estimate or pause/timer change.

Finished denominator: total. Stopped denominator: asked, with the same `asked or total` fallback as the existing message. Zero denominator gives 0.00%, matching the existing result.

Names use the existing `_name` function's first-name/username/user-ID fallback, not a new database name model. Full provided names/emoji wrap; they are not shortened. HTML escaping and bdi protect literal markup and mixed directionality. No hidden private answer/user-ID fields from renderer input are emitted.

## Executed new tests
### Existing data / exact values
- No-negative SQL rank order and equal-correct elapsed ordering.
- Stable negative-net order, negative scores and existing one-third rounding.
- Correct percentage rather than net percentage.
- Full and partial/stopped denominator conventions, including zero asked fallback.
- Read-only generation: whole temporary DB dump unchanged by attachment building.
- Renderer does not mutate or reorder input; deliberately reversed input stays reversed.
- Existing display-name fallback, safe literal names/quiz titles, 1000-character-plus names and composite emoji.

### Full result flow
- Real `_finish_locked`: saves result, sends original unchanged message and exactly one automatic table document.
- Existing personal HTML link/PDF markup preserved; no new Telegram button.
- Empty group and stopped group delivery.
- Idempotent finish call does not create duplicate attachments.
- Renderer/attachment failures do not corrupt saved results or suppress existing PDF hook.
- Text-message failure still allows the aggregate attachment attempt.
- Chunked text delivery unchanged, with existing markup only on final chunk.

### Pagination, mobile and security
- Page boundaries: 0, 1, 50, 51, 100 and 101 participants.
- All 1001 synthetic participants present with no mutation, clipping or row cap.
- Real Chromium: 123 participants across three pages at 360px, 390px and 412px widths.
- Visited all pages; ranks 1–123 present once, with correct first/last disabled controls and direct page selection.
- Seven headings on every page; complete horizontal access to the last column and no document-level overflow.
- Alternating row backgrounds verified through computed CSS.
- Long name and emoji text preserved; source markup remains literal and cannot execute.
- Saved .html file opens offline with zero HTTP/HTTPS requests.
- JavaScript disabled: all 123 rows across all three visible sections remain accessible.

## Prior-test expectation update, disclosed
The first complete run executed all 237 tests and had one failure: the older group-report test asserted that the group bot received NO document at all after finish. That assertion conflicts with this request's automatic public leaderboard attachment.

`tests/test_group_html_delivery.py` replaces only that expectation with stronger checks: exactly one automatic aggregate document to the existing group, correct leaderboard filename, and absence of private question text and explanations. Existing private participant HTML generation/access tests remain intact. Final rerun: all 237 passed. This is a required behavior update, not a silently ignored failure or a removed privacy test.

## Scope audit
File-hash comparison against the fresh task baseline: only `group_runner.py` and `html_report.py` changed among existing production files. In particular, the prior TXT parser/display patch is NOT bundled or modified by this patch. No database, quiz creation, parsing, answer handling, permission, command, control, private-runner or ranking/scoring module edits.

Baseline/patched SHA256:
- group_runner.py baseline `0919e4ba5b56be81f37e71bec04d2fe6ba81d00d875ee6d6555fb8772914d725`
- group_runner.py patched `7a764d2cc2263e3a975bdea719e89ed01761ff2961d05101b80eddb9db80d00d`
- html_report.py baseline `6cfdded13b30276856b7704f8bce599507c273a787c95711f86978fd40022029`
- html_report.py patched `635868a3aa37703cc0956b251d5f9da11654ab2e7f87e39f798b0d075c96d3eb`

## Limitations
- No physical Android Telegram/browser or live Bot API test. Mobile viewport and saved-file browser tests are local evidence, not live-device verification.
- Telegram may require Download → Open with browser; its internal preview may not execute HTML pagination. Without script execution the document still contains every row/page.
- Platform file/upload limits and network failures still apply. No arbitrary participant cap or data discard was added. On attachment failure the existing text result remains available; no uncertain send is automatically retried.
- Already-finished historical sessions are not automatically re-posted. New attachment delivery runs when the normal group finish/stop flow executes. Private quiz attempts and individual saved-result HTML reports are unchanged.
- Times are accumulated answer times, not wall-clock completion time, intentionally preserving existing data/calculations.

## Reproduction
Production adds no dependency. For the focused browser tests, install the project's existing requirements and test-only Playwright/Chromium.
`python -m unittest discover -s tests -p test_html_leaderboard.py -v`

The final full regression run used the restored earlier test files plus the new tests at `/home/user/leaderboard_regressions`, with PYTHONPATH set to the current project. These earlier tests are not redundantly bundled, except the one necessary changed regression file. Original TXT fixtures are still required by earlier format tests. Verbose logs include intentional simulated-failure warnings and existing dependency/resource warnings; they are not live service incidents. Sandbox timestamps are not a verified local date.
