# Final test report — actual nine TXT formats

## Final result
**214 tests passed in 14.126 seconds, zero failures/errors/skips.** Includes 191 unchanged previous regressions plus 23 new focused tests. The final verbose output is `tests/results/txt_formats_all_tests.txt`.

No live Telegram calls, production data changes, deployment, restart or GitHub writes were performed. Tests use temporary SQLite, fake job queues and mocked Telegram delivery. One older transport test uses real PTB/HTTPX against a local HTTP server; earlier HTML tests use real Chromium. Sandbox log timestamps are not a verified local date.

## Scope verified
Only existing production file `quiz_engine.py` changed. File-hash comparison against the pre-edit manifest confirms all other existing project files unchanged. AST comparison: existing functions changed only `parse_txt_source`, `_parse_topicwise_txt`, `build_poll_payload`; four scoped helpers added. Shared PDF parser, creation/import workflow, database, runners, scoring, negative marking, timers, leaderboard, result/HTML systems, permissions and callbacks unchanged. No production requirements or environment changes.

Baseline main commit: `6ceece417aad24e539085df5e130ead668431909`.
Original quiz_engine.py SHA256: `8b42414ef088486febfd3b2071cc7fae0a5250aeecad996969c9ce0f10bb2ae4`.
Patched quiz_engine.py SHA256: `0fc138c9be1859469ab632262fbae47c1bbb401ded022ed1e3ba8d2b0eb262d9`.

## Actual source files — executed results
| Format | Actual source questions | Before parsed / full import | After parsed / full import | Outcome |
|---|---:|---:|---:|---|
| 1 Assertion & Reason | 42 | 28 / 0 | 42 / 42 | Numeric choices and explicit boundaries repaired |
| 2 Statement-Based | 60 | 60 / 60 | 60 / 60 | Count was misleading: missing introduction/statement label restored |
| 3 Match the Column | 18 | 17 / 0 | 18 / 18 | Final numeric answer codes correctly distinguished from lists |
| 4 Direct Single-Choice | 20 | 20 / 20 | 20 / 20 | Stem/options/key preserved; confirmed structural pollution removed |
| 5 True/False | 14 | 0 / 0 | 0 / 0 | Actual file has no choice pairs; safe rejection unchanged |
| 6 Fill in Blanks | 18 | 0 / 0 | 0 / 0 | Actual file requires written responses; no distractors invented |
| 7 Short Answer / One-Word | 20 | 0 / 0 | 0 / 0 | Free-text response mode not introduced |
| 8 Scenario / Case Study | 8 | 8 / 8 | 8 / 8 | Correct cases now attached to Q1–2, Q3–4, Q5–6, Q7–8 |
| 9 Multi-Select | 5 | 0 / 0 | 0 / 0 | Answer sets not collapsed to a guessed single choice |

The master guide is an index, not another quiz file. Duplicate explicit headers and numbered statements are not counted as additional questions. The fill-in file has 18 questions plus 18 answer-key entries, not 36 questions. Full import counts use existing review/title callbacks; no forced partial-import action is used.

## Individually confirmed causes and fixes
1. **Assertion numeric choices:** bare `1.`–`4.` were excluded by the shared option-marker recognizer and confused question-boundary selection. A narrow TXT adapter now recognizes the final ordered numeric run only with assertion/matching context and an immediate explicit option/code/selection cue. No global numeric-list grammar change.
2. **Statement loss:** boundary tracing proved block 1 began at the first numbered statement instead of the explicit outer question header. Explicit blocks are now authoritative for the audited layout; adjacent original headers remain visible within that block. All original substantive stem lines, blank lines, punctuation and option values in the five supported actual files match an independent source oracle.
3. **Matching Q7:** List-I's A–D entries had been selected as answer options because final numeric code choices were unrecognized. The final ordered code run is now used, retaining both lists in the stem.
4. **Cases:** `[केस N: ...]` boundaries were not understood, so later case passages became previous explanations and case 1 was reused. The TXT adapter now attaches each case to only its following questions until the next case/topic boundary, using the existing pre_text field and existing sender behavior.
5. **Structural pollution:** ruled titles, explicit topic metadata, introductory instructions and post-answer rule-only separators polluted pre_text/explanations. Recognized structural material is separated, topics retained as section labels, and duplicate answer-text/key annotations are not turned into fake explanations. Genuine explicit explanations, substantive content and rule lines inside stems remain preserved. Unrecognized leading passages and valid inline-choice dialects decline the narrow path/fall back safely rather than silently losing content.
6. **Display:** default statement-combination questions have a structural guard requiring numbered statements and combination options. Complete text/source numbering takes precedence over decorative branding when it fits. Native options remain verbatim when safe; multiline/long options keep the full-text companion. Truly oversized sources are never shortened; the existing companion-message path remains. Other types and explicit custom presentation templates retain their display path.
7. **Previously confirmed topicwise regression:** the existing topicwise dialect demanded four choices even for a valid two-option True/False source. It now accepts ordered 2–4 choices in that same dialect; grammar limits/other paths unchanged. This does not auto-create choices for the supplied optionless format 5.

## Source-level and delivery verification
- Independent oracle, not production parsing, extracts stems, final options and answer tokens from the actual files. All **148 supported source questions** match in source order with correct outer numbering, complete stems, exact option values and correct answer indices. All have no genuine explanation paragraphs in the source; structural rules and repeated answer text are no longer fake explanations.
- No duplicate (stem, options) content in these accepted files.
- Source screenshot-match: stored outer number **26**, visible embedded heading **प्रश्न 16.**, four intact statements, exact file option order and answer index **2 / (c)**. The screenshot puts that combination at (A); it must not override this uploaded file's key.
- All 148 supported questions were imported through document -> review -> title -> existing create callbacks into temporary SQLite, then exercised through the real private and group current-question runners: **296 outgoing native-poll calls** with mocked Telegram. Simulated answers/advances preserve existing anti-unanswered safeguards. Every outgoing question/options/correct-index payload and companion/context message was checked. Stored question rows remained unchanged by sending.
- Every supported actual answer key was checked under reversed and rotated option permutations (296 additional payload cases). Numbered statement variations cover Hindi/English, 2/3/4/7 statements, short/near-long/very long text, both option orders, Unicode and lossless message splitting.
- Tests cover genuine explanations, answer conflicts, missing answers, duplicate outer numbers, multiline options, rule lines inside stems, unruled Topic text as literal content, unknown passages, native-send rejection and the existing lossless fallback.

## Backward-compatibility evidence
- Frozen pre-edit legacy parse + default/fallback payload digest across unrelated formats/permutations remains exactly equal: `f13dfb454a3f57338aaefd1d105c75ed58178e39d9345232a1d56ace79350abb`.
- Frozen pre-edit actual unsupported-format parse/error digest remains exactly equal: `d8cd03ba9d52c551484694e337d8e508a5bde47464c75976790ce329877155ef`.
- Existing 191 tests passed unchanged, including controls, TXT support/section imports, timeout handling, reference HTML/browser behavior and private group-report delivery. Historical test files were restored externally, not weakened or bundled as modified production files.
- Exact-output equality is required for unaffected tested cases. Confirmed corrupted stem/context/preamble cases intentionally differ as described above. This is finite test evidence, not a universal guarantee for every possible TXT dialect.

## Honest limitations
- **All nine supplied files audited does not mean all nine response models are now playable.** Formats 5–7 lack native choices and format 9 requires answer-set execution. Enabling them would require new import/response behavior and, for genuine multi-select, answer handling/storage/scoring changes beyond this patch. No fabricated options or answers are introduced. Existing generic error wording for these rejected files is unchanged, including Answer Not Found on the actual multi-select syntax.
- No live Telegram API acceptance or Android visual test. Local tests verify runner behavior and full outgoing content, not client rendering.
- Both source numbering layers are preserved. The actual screenshot-matching stem with its embedded original header is longer than the earlier standalone 293-unit example; it can legitimately still require a companion message under native poll constraints. Removing the original header to make it fit would violate the approved numbering rule.
- Explicit custom presentation templates retain their original heading/branding/companion behavior; their user-selected content is not discarded by the default statement-display improvement.
- Existing stored quizzes are not automatically rewritten. Corrected parsing applies to new imports; preserve history and import the original file as a new quiz if existing stored content was incomplete.
- New adapter deliberately handles only the audited explicit structure; it does not reinterpret arbitrary numeric lists, unsupported global-key teaching sheets or unknown passage dialects.

## Reproduction
Original nine attachments are required in QZZZZZ_TXT_TEST_DIR; the focused tests verify their SHA256 identities and fail on missing/mismatching fixtures. They are input files, not production modifications, and are not redundantly included in the minimal patch.

```sh
QZZZZZ_TXT_TEST_DIR=/path/to/original_samples python -m unittest discover -s tests -p test_txt_formats_display.py -v
```

The final 214-test run command used the external restored historical test directory plus the new test file:
`PYTHONPATH=/home/user/inline_pdf_update/project python -m unittest discover -s /home/user/nine_formats_audit/full_regressions -v`.
