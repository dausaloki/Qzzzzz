# Review, publication and future production operations

**Review-only release. No production deployment is authorized.** No live DB, volume,
variable, start command, replica count, service or deployment was changed here.
This document is a plan, not evidence that Railway was inspected or deployed.

## Source and feature inventory

Base: GitHub `main` `0e62d92c16cbc18cdd61dc70eea71cc78fc93dbe` (verify again before publishing).
Prepared branch: `review/consolidated-v7`. Its sanitized v7 ancestor is
`2e6000f916d532b5bb9b2d5523a86ea76b723389`, directly parented by the base.
Do not publish old v7 bundles/ZIPs: they contain the superseded book fixture.
The original GitHub main history is preserved; old local copyrighted review
commits are not ancestors of this review branch.

| Area | Existing implementation retained |
|---|---|
| Commands and buttons | All original commands/menu entries and callback routes; aliases retained; see generated feature inventory in the review package |
| Creation | Typed text, Telegram Quiz Polls, forwarded polls, PDF/TXT/HTML/JSON, OCR with pinned models, multi-file topic merge, automatic 500-question parts, original-order preview and explicit partial-import confirmation |
| Editing | Title/description, question/options/key/explanation, add/reorder/delete, timer/shuffle, confirmation for question/quiz deletion; atomic option+key edits |
| Quiz modes | Private standard/practice/exam/sectional; immediate feedback in practice, regular polls hide answers in exam; per-section per-question timer overrides |
| Group quiz | Existing shared native Quiz Polls, 45s default, leaderboard, membership/access checks, private personal review PDF |
| Reports | Stored attempts, best-attempt rankings, private/group results, section scores, interactive offline HTML review/navigation, automatic PDF and `/review` |
| Sharing | Stable IDs, deep links, play buttons, access-aware inline query handler |
| Access | Free quizzes, mandatory independent joins, private/batch/authorized-chat controls; no financial gates |
| Owner/admin | Configured admins, broadcast, sanitized error-group logging, community references and optional OpenRouter |
| Persistence | SQLite paths, volume selection, backups, idempotent restore, timeout counts, multi-part links and all expansion tables |
| Branding | Bundled fonts, exact group DP, LOKESH DAUSA / @LOKESH_DAUSA, clickable https://t.me/kalam_kranti on every PDF page, first-start @DAUSA_123 notification |

### This final pass

* All delivered quiz PDF routes use `review_pdf.render_report`: completion,
  answers/review, private and group. No separate alternate user-PDF renderer exists.
* Mobile portrait pages are 420 × 842 pt, with 28pt side margins; the original
  footer height is retained. Main title 22pt, headings 16pt, questions 14pt,
  options/explanations 12pt, footer/page numbers 9pt. Source content never shrinks.
* Flow adds pages, keeps ordinary question blocks/options together when feasible,
  flows oversized explanations, wraps full-width summary fields, and protects
  Devanagari marks/conjuncts when breaking overlong tokens. Shape-aware safety
  space avoids edge clipping. Original source wording is not rewritten.
* Per-section PDF scores are added, using the recorded report statuses and stored
  negative-mark setting. Section names from both import/edit schemas are retained.
* Private attempt mode and negative-mark rules are captured at creation in the
  same DB transaction. Owner settings edits cannot switch an active exam into
  feedback mode or retrospectively change its penalty. No new schema is needed.
* The conflict startup test now closes its owned event loop on the main thread.
  Leaving signal handlers on an unclosed loop could make garbage collection in a
  PDF worker emit a signal-handler cleanup warning. The closed loop is also
  cleared from the event-loop policy so repeat runs create a fresh loop. No
  assertion was changed.
* Original self-written 50-question Hindi fixture retained; roundtrip assertions
  check every question, option, answer and explanation, not only sample rows.

### Honest interface boundaries

* Native Telegram polls advance sequentially. There is no arbitrary back-and-edit
  answer navigator during a live poll attempt. Navigation/review is in the HTML
  result report after completion; `/skip` and `/stop` remain available live.
* Group workflows remain shared native Quiz Polls; private practice/exam/sectional
  behavior is not claimed for shared group polls. Private exam polls are regular
  (not Quiz) polls because native Quiz Polls reveal the answer.
* Section timers mean a **per-question** override for a named section, not a shared
  section countdown with free navigation.
* PDF samples verify Unicode math (powers, roots, fractions, Greek symbols).
  PDFs preserve raw TeX verbatim rather than running a general TeX typesetter;
  HTML reports have a limited offline TeX-to-HTML converter. Arbitrary TeX,
  imported diagrams and arbitrary unsupported scripts are not guaranteed.
* Old PDF reviews use the current quiz question content; no full historical copy
  of edited/deleted questions is added. Stored attempt counters and scoring-rule
  snapshots persist. Export important reviews before changing question content.
* AI outputs need human review. Guards are in-memory and reset on restart.

## Secure branch-only publication (no PR)

Authentication must use a supported integration, GitHub's device/browser login,
or an already-authenticated Git credential manager. Never send credentials in
chat, embed them in a remote URL, store them in project files, or print them.
If no secure authentication is available, the release ZIP/patch/bundle is the
ready-to-publish alternative. This environment reports no supported GitHub connector.

On a trusted desktop with GitHub CLI and Git installed:

```bash
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
git clone https://github.com/dausaloki/Qzzzzz
cd Qzzzzz
git fetch /path/to/review_final.bundle refs/heads/review/consolidated-v7:refs/heads/review/consolidated-v7
git fetch origin main
test "$(git rev-parse origin/main)" = 0e62d92c16cbc18cdd61dc70eea71cc78fc93dbe
# STOP if main moved or the remote review branch already exists.
git ls-remote origin refs/heads/review/consolidated-v7
git switch review/consolidated-v7
git status --short
git merge-base --is-ancestor origin/main HEAD
# Confirm read-only in Railway: the production service tracks main and no other
# service/custom branch automation targets review/consolidated-v7. Do not change settings.
git push origin refs/heads/review/consolidated-v7:refs/heads/review/consolidated-v7
git ls-remote origin refs/heads/main refs/heads/review/consolidated-v7
```

Review URL after publication:
https://github.com/dausaloki/Qzzzzz/tree/review/consolidated-v7

**Do not open a PR.** If Railway PR environments are enabled, a PR can clone
production variables and start a second bot with the same token. There are no
GitHub Actions workflows in the prepared project, but external branch triggers
cannot be verified without Railway access.

### Mobile-friendly fallback

Download the ZIP with the direct link. On Android, inspect the source, PDFs,
`FINAL_AUDIT_REPORT.md`, `CHANGED_FILES.txt` and checksums without deploying.
For publication, a trusted authenticated desktop is preferable. Alternatively,
in a trusted Android terminal install Git, Python and GitHub CLI; use the browser
login above, import the provided bundle and push only the named review ref.
Do not use GitHub's web "Upload files" on main, deploy a second Railway service,
or upload a database or `.env`. Avoid arbitrary mobile Git apps requesting tokens.

If using patches instead of the bundle, in a fresh clone based on the verified
main use `git am /path/to/format-patch/*.patch`, then name that new branch
`review/consolidated-v7`. Cherry-pick/am may create different commit hashes;
verify the resulting tree against the supplied source manifest before pushing.

## Backup BEFORE any future migration/deployment

These steps need separate production authorization. They were tested only on
simulated data. Use the actual configured DB path; never guess `/data` if the
volume mount is different. Do not print variable values/tokens into logs.

1. Ask the authorized owner to use `/backup`, `/backup now`, `/backup verify`,
   and `/backup download`. Save the **full** JSON off the volume securely.
2. Determine the effective `config.DB_PATH` and `config.DATA_DIR` in the existing
   service without changing variables. Capture the current image/deployment ID,
   main commit, volume ID and existing start command privately.
3. Make a transactionally consistent SQLite copy using the SQLite backup API,
   not `cp` on an active WAL database. For example, after substituting the real
   paths (the destination must not already exist):

```python
from pathlib import Path
import sqlite3, os
src = Path('/actual-volume/quiz.db').resolve()
dst = Path('/secure-backup/quiz-before-review.db')
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(fd)
with sqlite3.connect(src.as_uri() + '?mode=ro', uri=True) as source:
    with sqlite3.connect(dst) as target:
        source.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
```

4. Verify the copy's hash, integrity and record counts, and test restoration into
   an isolated directory. Do not run a second polling bot against the production
   token. Preserve JSON, SQLite and existing deployment for rollback.

## Export/restore on an ISOLATED copy only

The CLI supports global flags before the subcommand:

```bash
python scripts/backup_tool.py --db /isolated/quiz.db --out /isolated/full.json export
python scripts/backup_tool.py --db /isolated/quiz.db verify --file /isolated/full.json
# Dry run first; --apply is the explicit writing operation.
python scripts/backup_tool.py --db /isolated/restored.db restore --file /isolated/full.json
python scripts/backup_tool.py --db /isolated/restored.db restore --file /isolated/full.json --apply --migrate
python scripts/backup_tool.py --db /isolated/restored.db verify --file /isolated/full.json
```

The restore tool makes a safety copy before an applied restore; migrations are
additive. Repeat restore is idempotent. Full JSON includes users, chats, quizzes,
questions, attempts/answers, group sessions, timeouts, multipart links, delivery
records, batches and authorized chats. A redacted GitHub content copy omits
identifiers and is **not restorable**. Payment-era extra data is ignored on import,
not used or deleted. Do not delete legacy production tables without a separate
usage check and approval. Backups do not include secrets or already-sent PDF files.

## Future Railway deployment — separate explicit approval required

1. Recheck the exact main and reviewed branch hashes, verified backups and
   simulated restore; read-only confirm PR environments/branch triggers and the
   actual active production revision. No PR is needed.
2. Keep the existing service, volume/path, variables and start command
   `python bot.py`. Keep one replica. Repo configuration already has one replica,
   zero overlap and graceful drain; do not change production settings here.
3. Required variable: `BOT_TOKEN` (secure Railway variable only). Existing volume
   selection, owner/admin and membership variables stay as configured. There are
   no newly required variables. Optional OpenRouter/HTML reporting remain opt-in.
   All supported env variables and defaults are listed in README, with blank
   secret fields in `.env.example`.
4. Only after approval, fast-forward the verified main to the exact reviewed head.
   That push may autodeploy Railway. Never force-push, create another polling
   service, or run the new bot concurrently against the production token.
5. Watch the existing service's build/start logs: OCR check, bundled PDF font
   check, migrations, exactly one poller, no Conflict/401 or repeated shutdowns.
6. Owner smoke tests: /start and first-start notification; membership refusal and
   success; original menus; typed/import/forwarded quiz; 45-second private and
   group quiz; practice/exam; optional negative mark; saved attempts/leaderboard;
   full Hindi PDF at mobile width, watermark/link every page; HTML review;
   unauthorized admin rejection; /backup verify. AI smoke only if separately
   configured with a budget. Check counts and retained historic records.
7. Monitor errors, polling conflicts, missed jobs, delivery retries, CPU/memory,
   disk usage and backup freshness. Do not print secrets or personal data.

## Rollback

Use the existing Railway service's previous successful deployment via Rollback,
keeping the volume. Confirm Railway's variable-restore behavior before clicking;
no variable changes are proposed by this release. Do not restore data as a routine
code rollback. Old main starts on the additive schema in the simulated probe,
but it ignores optional negative marking and batch/private access; batch quizzes
may become accessible, and the PDF returns to the fallback serif font. If these
restrictions matter, prefer pausing access under an approved incident plan rather
than exposing restricted quizzes.

Never restore a new JSON backup using old code: newer tables are lost. A deliberate
DB rollback would discard post-backup writes and needs a separate incident/data
reconciliation decision. Preserve the current DB first.

To align Git after a code rollback, create reviewed revert commits (no force),
for example `git revert --no-edit <base>..<final-reviewed-head>` on a new incident
branch. This changes main only after separate approval; it can itself autodeploy.
