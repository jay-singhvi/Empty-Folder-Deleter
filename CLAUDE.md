# AGENTS.md: Various Tools

Guidance for AI coding agents (and humans) working in this repository. Read this first, then
[docs/MODERNIZATION_PLAN.md](docs/MODERNIZATION_PLAN.md), which is the source of truth for the
architecture and the phase order.

## What this project is

Various Tools is one installable Windows app with a shared core library, a desktop GUI (PySide6)
and a CLI (Typer + Rich). Each tool (duplicate finder, folder cleaner, media resizer and so on)
is a plugin that declares its own options, so the GUI and the CLI are generated from those
declarations.

This repository started as the **Empty Folder Deleter** Tkinter app, which is now the
`empty_folders` tool. The original scripts are kept unchanged under
`tests/fixtures/legacy_reference/` because the parity tests run them; they are read-only.

## Current state

All plan phases (0 to 8) are implemented on `main`. `CHANGELOG.md` records what each phase did,
the decisions taken and what is left for the owner.

## Stack (decided, do not revisit)

| Concern | Choice |
|---|---|
| Python | Newest 3.x with wheels for every dependency. Pinned in `.python-version` (3.14). |
| Env and packaging | uv, `pyproject.toml` (hatchling), `src/` layout, `uv.lock` |
| GUI | PySide6 (Qt 6) |
| CLI | Typer + Rich |
| Options and config | pydantic v2, pydantic-settings, TOML in `platformdirs.user_config_dir("Various Tools")` |
| Data | SQLite (stdlib) for the journal, caches and media catalog. Excel/CSV are export formats only. |
| Deletes | send2trash (Recycle Bin by default) |
| Ignore rules | pathspec |
| Media | Pillow, pillow-heif, rawpy, imagehash, numpy; ffmpeg, ffprobe, ExifTool as external programs |
| Quality | ruff (lint and format), pyright (basic, strict on `core/`), pytest, pytest-qt, pre-commit |
| Build | PyInstaller one-dir, no UPX, then Inno Setup or the existing NSIS. Per-user install, no admin. |

## Layout

```
pyproject.toml  uv.lock  .python-version
README.md  CHANGELOG.md  LICENSE  AGENTS.md  CLAUDE.md (copy of this file)
docs/MODERNIZATION_PLAN.md   docs/tools/<tool-id>.md
src/various_tools/
  core/                      fs, onedrive, hashing, media/{imaging,exif,ffmpeg}, journal, plan,
                             context, deps, settings, cache
  tools/                     base.py (Tool protocol and registry), one package per tool
  cli/main.py                Typer app
  gui/                       app.py, main_window.py, widgets/, workers.py
tests/                       core/, tools/<tool>/, cli/, gui/, fixtures/legacy_reference/ (the
                             original scripts, read-only, run by the parity tests)
packaging/                   spec, installer script, icon
```

## Commands

```
uv sync                       # install everything, including dev and media groups
uv run pytest                 # tests
uv run ruff check             # lint
uv run ruff format --check    # formatting
uv run pyright                # types
uv run various-tools-gui      # launch the GUI
uv run various-tools --help   # CLI
```

Run all of the above before you say a change is done, and show the output.

## Safety rules 🔒 (never weaken these)

1. **Preview first.** Dry run is the default everywhere. Applying a plan needs an explicit action:
   the Apply button, or `--apply` in the CLI.
2. **Deletes go to the Recycle Bin** by default. Permanent delete is an advanced setting that asks
   for confirmation.
3. **OneDrive cloud-only files are never read or hashed implicitly.** Check
   `onedrive.is_on_device()` before any step that opens file contents. Report such files as
   "skipped: cloud-only". Downloading them needs the user's explicit action.
4. **Never follow junctions or symlinks out of the selected root.** Never delete the root itself.
5. **Every applied action is journaled and can be undone** from History.
6. **Protected paths** (Windows dir, Program Files, user profile root, drive roots) are rejected as
   targets unless the user overrides this in settings.

## Other rules

- **Thresholds are requirements.** The duplicate-finder and video thresholds (for example
  `PHASH_MAX=2`, `DHASH_MAX=4`, `ASPECT_TOL=0.02`, `DUR_TOL_S=0.6`, `FRAME_MEAN_MAX=10`) were tuned by
  hand on real data. Do not change any threshold, safety rule or default marked 🔒 without asking
  the owner first. Port them exactly and cover them with tests.
- **Never run a tool against real user folders** during development or testing. Use generated
  fixture trees under a temp directory. Never commit personal photos, videos or documents as
  fixtures.
- **Only `core/deps.py` may locate external programs.** No other module may call `shutil.which`,
  hardcode an executable name, or hardcode a path to ffmpeg, ffprobe, ExifTool or Ollama. Media
  code receives resolved paths through `RunContext.deps`.
- **GUI words are plain.** The GUI shows the words in `gui/wording.py` ("Move to Recycle Bin",
  not `trash`); a tool sets `scan_label` and `found_nouns`. CLI output and exports keep
  the raw names.
- **`core/` has no UI code.** No Qt imports and no `print` calls. Tools have no `print`, `input`
  or Qt either. Use the logger and the progress reporter on `RunContext`.
- **`scan()` never changes the disk.** Caches are the only exception.
- **`tests/fixtures/legacy_reference/` is read-only** (one owner-approved exception: the
  personal path in `python-find_duplicates.py` was replaced for open-sourcing, and
  `tests/test_no_personal_paths.py` keeps such paths out of every tracked file). Fix behavior
  in `src/`, not in the legacy
  copies.
- **No binaries in git.** Installers and `*.exe` are published through GitHub Releases. `dist/`,
  `build/` and `*.exe` are git-ignored.
- **The version lives in one place:** `pyproject.toml`, read at runtime through package metadata.
- **If a decision isn't covered by the plan, pick the safer option** and note it in the PR
  description and the CHANGELOG.

## Working style

- Make small, logical commits. One PR (or commit series) per phase.
- Each phase ends with green CI and a CHANGELOG entry.
- Don't push or merge without the owner's review.
