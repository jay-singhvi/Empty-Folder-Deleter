# Handoff for the next session

Last updated 2026-11-21, after release 0.8.0. Read `AGENTS.md` (rules) first, then this file,
then `docs/MODERNIZATION_PLAN.md` §7 (what exists and the Phase 9 candidates).

## Where things stand

- `main` is at the merge of PR #4 (`3fffe74`). Latest release: **v0.8.0**, published with an
  installer and a portable zip. The tree is clean; no open branches or worktrees.
- Checks on `main`: `uv run pytest -q` 3694 passed, 1 skipped (the opt-in real-OneDrive test,
  `VT_TEST_ONEDRIVE=1`); ruff, ruff format and pyright clean; `packaging/smoke_test.py` 9 of 9.
- The owner left product decisions to the builder in 0.6 to 0.8 ("all decision making has been
  left up to you"), always the safer option where unsure, and asked each time to *push, open
  the PR, merge after CI and release*. That was said per release: ask again before pushing.
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
5. One **Opus** read-only review of `git diff main..release-X.Y.Z`; give its findings to one
   Sonnet fixer working on the release branch; re-run all checks yourself.
6. `uv run python packaging/build.py --installer`, then `uv run python
   packaging/smoke_test.py`. Open the PR (body written for a code reviewer: what changed,
   safety points, checks, left for later). Wait for CI (`gh run watch`), merge with `gh pr
   merge N --merge`, check `main`'s tree equals the built commit's tree.
7. Release: see "Release assets" below.

## Things that went wrong, and how to avoid them

- **Release assets.** `.github/workflows/release.yml` runs on every `v*` tag: it re-runs
  lint, types, tests, builds, smoke-tests and attaches the installer and zip with
  `softprops/action-gh-release`, **replacing** files uploaded by hand (0.6.0 to 0.8.0 were
  replaced about 9 minutes after publishing; the release notes were kept). So the published
  files are CI builds of the tagged commit, not the local builds. Next time: `gh release create
  vX.Y.Z --target main --title ... --notes-file ...` **without** files, then watch the
  release workflow (`gh run list --workflow release.yml`) and check the assets appear.
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
4. Release assets attached by `release.yml` only (see above).

## Useful commands

```
uv sync
OPENBLAS_NUM_THREADS=1 uv run pytest -q -p no:cacheprovider
uv run ruff check && uv run ruff format --check && uv run pyright
uv run python packaging/build.py --installer && uv run python packaging/smoke_test.py
gh pr checks N ; gh run watch RUN --exit-status ; gh pr merge N --merge
gh run list --workflow release.yml --limit 3
```
