# Handoff for the next session

Last updated 2026-11-22, after release 0.9.0. Read `AGENTS.md` (rules) first, then this file,
then `docs/MODERNIZATION_PLAN.md` §7 (what exists and the Phase 9 candidates).

## Where things stand

- `main` is at the merge of PR #5 (`2c13ddc`). Latest release: **v0.9.0**, with the installer
  and portable zip attached by `release.yml` (CI builds of the tag). No open worktrees; old
  `release-0.x.0` branches are kept locally.
- Checks on `main`: `uv run pytest -q` 3781 passed, 1 skipped (the opt-in real-OneDrive test,
  `VT_TEST_ONEDRIVE=1`); ruff, ruff format and pyright clean; `packaging/smoke_test.py` 9 of 9.
- 0.9.0 came from an owner bug report, not a plan item: *Organize by date* put
  `Camera Roll22 - 0620624_005115.mp4` into `2025 - 03` (its video creation time
  had been rewritten by an edit). Also asked: several folders at once, one folder list instead
  of "working folder" plus "Use other folders", Continue after Stop without a new scan, change
  the pace while applying. See the CHANGELOG entry and
  `docs/superpowers/specs/2026-11-22-name-dates-folders-continue-design.md`.
- The owner said "consider the name first" and accepted the change to the tool's date rule 1
  (the name beats EXIF and video metadata; the option `name_date_first` turns it off). The
  owner uses Samsung, Apple, DJI and WhatsApp names; Pixel `PXL_` names are UTC (handled).
- The owner left product decisions to the builder in 0.6 to 0.9, always the safer option where
  unsure. For 0.9 they said "build the installer and release it once checks pass" and gave
  permission to use agents for decisions and help. Release permission is given per release:
  ask again before pushing the next one.
- The owner's subagent rule (see the session memory): build, fix and review subagents on
  **Sonnet**, lookups on **Haiku**, **Opus only for one final whole-branch review** per release.
  Keep handoffs few.

## How a release was made (worked well, keep it)

1. Write a short design in `docs/superpowers/specs/<date>-<topic>-design.md`; commit it on a
   `release-X.Y.Z` branch.
2. Split the work by **file ownership** into 2 or 3 parts and give each builder its own
   worktree made by hand next to the repo: `git worktree add -b part-x ../vt-part-x
   release-X.Y.Z`. The Agent tool's `isolation: "worktree"` is refused in this repo (Windows
   `c:` vs `C:` path casing).
3. Brief each builder with: its worktree path, the spec section, the files it owns and the
   files it must not touch, "tests first", the four checks, small commits ending with the
   `Co-Authored-By` line, no push, one final report.
4. Merge each part with `git merge --no-ff`, resolve (so far always "keep both sides"), add any
   cross-part wiring yourself with a test, bump the version in `pyproject.toml` only, add the
   CHANGELOG entry, run the full suite.
5. One **Opus** read-only review of `git diff main..release-X.Y.Z` (the
   `pr-review-toolkit:code-reviewer` agent with `model: opus` worked well in 0.9: it found the
   Change date and Continue bugs). Give its findings to one Sonnet fixer on the release branch,
   or fix small ones yourself; re-run all checks yourself.
6. `uv run python packaging/build.py --installer`, then `uv run python
   packaging/smoke_test.py`. Open the PR (body written for a code reviewer: what changed,
   safety points, checks, left for later). Wait for CI (`gh run watch`), merge with `gh pr
   merge N --merge`, check `main`'s tree equals the built commit's tree.
7. Release: `gh release create vX.Y.Z --target main --title ... --notes-file ...` **without**
   files; `release.yml` builds and attaches the installer and zip (about 10 minutes); check
   with `gh release view vX.Y.Z --json assets`. Done this way for 0.9.0 and it worked.

## Things that went wrong, and how to avoid them

- **Release assets.** `.github/workflows/release.yml` runs on every `v*` tag and attaches the
  installer and zip, replacing anything uploaded by hand (0.6.0 to 0.8.0 were). Do not upload
  local builds; build locally only for the smoke test.
- **A full run under load took 2 h 22 min** (0.9, while builders and a reviewer were running);
  on an idle machine it is about 7 minutes. Start the final full run only after every agent
  has finished.
- **`git worktree remove` can refuse** a merged builder worktree that shows a file as modified
  only by line endings (`git diff` empty). Check the diff, then `--force`.
- **The working folder is now shared by every tool page** (0.9). A test or feature that sets
  folders on one page changes them everywhere; `set_folder(s)` set the working folder, except
  on a recipe step page with its own folders.
- **Continue after Stop is per tool**: `gui/continue_run.py` holds the tools that must scan
  again (`NO_CONTINUE_TOOLS`, `MOVES_ONLY_TOOLS`). A new tool whose rows depend on each other
  while applying (a row that follows another's outcome, or two rows for one file) must be added
  there, with a test.
- **Builders leave background test runs** and then re-send the same report many times while
  they wait. Tell them to run pytest in the foreground (timeout 600000). When a report is in
  and its commits are merged, stop the agent (`TaskStop`) instead of answering repeats.
- **Machine load makes tests flaky.** With 2 or 3 builders running suites at once, ffmpeg and
  timing tests fail or stall (for example
  `test_filtering_two_hundred_thousand_items_does_not_freeze_the_window`,
  `test_main_without_tool_shows_the_welcome_page`). Judge only a full run on an idle machine.
  `OPENBLAS_NUM_THREADS=1` avoids OpenBLAS memory errors under load.
- **The full suite takes about 6 to 7 minutes**; run it in the background and wait for the
  notification. `| tail` hides the exit code: read the summary line.
- **CRLF files.** Most files are CRLF in the working copy. Scripted edits must keep the line
  endings, and must not turn a `\n` inside a string literal into a real line break (that broke
  `main_window.show_about` once). The Edit tool is safer for small changes.
- **Usage limits.** A 5-hour limit stopped all builders once; their work was safe in the
  worktrees, and `SendMessage` to each agent resumed it after the reset.
- **Screenshots** (`packaging/make_screenshots.py`) use a marked demo folder at
  `C:\VariousTools-Demo` and delete it; look at the PNGs before committing (they showed two
  real Home-page bugs in 0.7.0).

## Next (Phase 9 candidates, in order)

See `docs/MODERNIZATION_PLAN.md` §7. In short:

1. Recipe steps that use the previous step's output folder.
2. Timeline handover at 200,000 items (about 0.25 s now).
3. Faces and trips on the Timeline (needs owner decisions on the face model and privacy).
4. Continue after the app was closed (store the kept plan).
5. The date-from-name rules in Rename and Change date too.

Watch for owner feedback on 0.9 first: whether the name rule misfiles anything in their real
library (a name pattern not yet known, or a name that holds a date that is not the capture
date). New patterns go in `core/media/name_dates.py` with a test in
`tests/core/test_name_dates.py`; never test against the owner's real folders.

## Useful commands

```
uv sync
OPENBLAS_NUM_THREADS=1 uv run pytest -q -p no:cacheprovider
uv run ruff check && uv run ruff format --check && uv run pyright
uv run python packaging/build.py --installer && uv run python packaging/smoke_test.py
gh pr checks N ; gh run watch RUN --exit-status ; gh pr merge N --merge
gh run list --workflow release.yml --limit 3
```
