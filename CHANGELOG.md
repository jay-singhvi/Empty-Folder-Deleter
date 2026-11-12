# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Phase 3.1: `empty_folders` tool (2026-11-12)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 3, item 3.1. User page: `docs/tools/empty_folders.md`.

#### Added
- `tools/base.py`: the tool contract (`ToolOptions` with `extra="forbid"`, the `Tool` protocol) and
  `discover()`, which reads the `various_tools.tools` entry-point group.
- `tools/empty_folders/`: one bottom-up scan that plans every folder that will be empty, including
  nested ones. Removals are ordered deepest first. Junk files (`desktop.ini`, `Thumbs.db`,
  `.DS_Store`) go first, each as its own action. Plan groups point at the top of each empty tree.
  `set_folder_selected()` unchecks a folder and every folder above it.
- `pyproject.toml`: the `empty_folders` entry point.
- Tests (`tests/tools/empty_folders/`, 22 tests): parity with the original app on a generated tree,
  each §2.1 bug fix, the safety rules, and discovery. The parity test runs the original app's
  `find_empty_folders` loop, read from `empty_folder_deleter.py` with `ast`, so tkinter is never
  imported.
- `docs/tools/empty_folders.md`: user page.

#### Changed
- `core/plan.py`: trash and delete now refuse a folder that still has contents. Before, a trash
  of a parent would have sent its whole subtree to the Recycle Bin.
- `tests/core/test_plan.py`: a refused non-empty delete is now reported as "skipped" with a clear
  reason, not "failed". The run status is "done", as it is for other refusals.

#### Fixed (from the original app, plan §2.1)
- No `os.errno`. A failed removal is reported and the rest of the run goes on.
- The selected root is never a target, even when it is empty.
- Junctions and symlinks are never followed, and they count as content.
- `desktop.ini`, `Thumbs.db` and `.DS_Store` count as empty.
- One bottom-up pass instead of a rescan loop.
- Removal goes to the Recycle Bin by default, and the apply engine journals it.
- Bugs 3 (Tk threads), 8 (unused imports) and 9–11 (installer and version) are either GUI work
  for Phase 5 or belong to the packaging phase. This module has none of them.

#### Decisions (not covered by the plan)
- **Parity reference is the root `empty_folder_deleter.py`**, not a file in `legacy/`. The legacy
  README says the root app is ported here. Its source is read with `ast`, so the test runs the
  original code without importing tkinter.
- **Non-empty folders are refused by the core engine, not only by this tool.** Trash on a folder
  takes everything inside it, so the check belongs with the generic guard. A hand-edited plan
  cannot bypass it.
- **`skip_hidden_system` defaults to on.** Hidden and system folders such as `$RECYCLE.BIN` or
  `System Volume Information` are kept. The plan does not give a default.
- **Excluded entries, junctions, symlinks, unreadable entries and hidden or too-recent folders all
  count as content.** Their parents stay. This matches the original app, where `os.walk` lists a
  symlinked folder under its parent's subfolders.
- **Junk files are separate actions**, shown in the preview and journaled, so nothing is removed
  without appearing in the plan.
- **Options fields are typed with `json_schema_extra` widget hints** (`folders`, `glob_list`), as
  §3.3 describes. The `treat_as_empty` names use the `glob_list` widget. Names match exactly,
  without regard to case.
- **Protected roots** (drive roots, the profile folder, Windows and Program Files) are refused
  before anything is listed.
- **Overlapping roots are scanned once**: a root inside another selected root is dropped.
- **Delete mode is set on the tool and checked again by Settings.** `permanent` in the options
  is not enough on its own: Settings must also allow permanent delete.

### Phase 2: Core library (2026-11-12)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 2.

#### Added
- `core/fs.py`: `walk()` (include/exclude globs, extensions, `.gitignore` via pathspec, never follows
  junctions or symlinks), `FileInfo`, `unique_destination()`, `protected_reason()` and `long_path()`
  (the `\\?\` prefix for paths past MAX_PATH).
- `core/onedrive.py`: sync status from file attributes (`get_sync_status`, `is_on_device`,
  `ensure_on_device`, `is_regular_file_no_hydrate`), `unpin_file()` and `force_download()`.
  🔒 Cloud-only files are refused before any read. `force_download()` is for explicit user requests only.
- `core/hashing.py`: streaming `sha256()`, `group_exact()` (size bucket, then hash; cloud-only files are
  reported as skipped), `phash()`/`dhash()` (256-bit), `hamming()` and `band_keys()`. No thresholds here.
- `core/media/imaging.py`: `open_image()` (HEIF registered, CR3 via rawpy under a lock) and an atomic
  `save_jpeg()`.
- `core/media/exif.py`: `read_exif()`, `capture_time()` (EXIF, then ExifTool for RAW), plausibility
  check, GPS conversion.
- `core/media/ffmpeg.py`: `ffprobe_json()`, `probe_duration()`, `display_size()` (rotation-aware),
  `creation_time()`, `grab_frame()` (with the step-back rule) and `transcode()` (atomic output).
- `core/deps.py`: finds ffmpeg, ffprobe, ExifTool and Ollama in the §3.5 order (override → env var →
  PATH → known locations → disabled). Each candidate is checked by running its version command.
  Results are cached, and `redetect()` clears them. Includes `winget` install hints and `run_tool()`.
- `core/settings.py`: pydantic `Settings` with safe defaults (Recycle Bin, protected paths refused),
  TOML load and save in `platformdirs.user_config_dir("Various Tools")`. A broken file is logged and
  left in place.
- `core/cache.py`: `FileCache`, a versioned SQLite cache keyed by path, size and mtime. A version or
  name change drops it. Each write commits, so scans can resume.
- `core/journal.py`: SQLite journal (`runs`, `actions`) and `undo_run()`. Moves and renames are moved
  back. Trashed and deleted items cannot be restored by code, so they are listed for manual restore.
- `core/plan.py`: `Plan`, `PlannedAction` (move, rename, trash, delete, write, convert), JSON
  round-trip, and `apply_plan()`. Every action is journaled before it runs. Guard rules run again at
  apply time, and a failed action does not stop the rest.
- `core/context.py`: `RunContext` (settings, deps, journal, progress reporter, cancel event, logger)
  and `Cancelled`.
- `core/jsonutil.py`: `as_dict()` and `as_list()`, which narrow JSON data for strict type checking.
- Tests (`tests/core/`, 164 in total) run on generated fixtures in `tmp_path` only. Image fixtures are
  generated with Pillow. Video and ffprobe tests use ffmpeg when it is installed and skip otherwise.
  External programs are fakes (`.bat` files), and the Recycle Bin is replaced by a recording fake.
- `tests/conftest.py` shared fixtures: `make_image`, `fake_exe`, `make_video`, and session fixtures for
  ffmpeg and ffprobe.
- `pytest-cov` (dev group). Core coverage: 94%.
- CI installs ffmpeg with Chocolatey before the tests run.

#### Decisions (not covered by the plan)
- **Duplicate name suffix is `_dupN`** (`photo_dup1.jpg`), as the duplicate finders use. The
  metadata-8 style ` (n)` is not used, because `keeper.py` treats `(n)` as a copy marker.
- **`attrib.exe` is called by absolute path** from `%SystemRoot%\System32`. Its name lives in
  `core/deps.py` (`system_tool()`), so no other module names an executable.
- **Plain pydantic instead of pydantic-settings.** Environment overrides are read in `deps.py`, and a
  second `VARIOUS_TOOLS_` prefix would collide with them. TOML is written by a small serializer, so no
  TOML writer dependency is needed.
- **Outside Windows, an existing path reports `LOCAL`.** Placeholders only exist on Windows. This keeps
  the code testable everywhere while the rule is enforced on Windows.
- **Protected paths include everything inside Windows and Program Files**, not only the folders
  themselves. The user profile folder and drive roots are protected as well.
- **Permanent delete removes only an empty folder or a single file.** It is never recursive.
- **`display_size()` reads rotation from ffprobe** and does not decode a frame, as `grab_size` did. The
  result is the same and it is much cheaper.
- **Trash and permanent delete are not undoable by code.** `send2trash` cannot restore an item, so the
  undo report lists them for manual restore.
- **Tests that need ffmpeg use the installed program** found through `ExternalTools`. They skip when it
  is missing. Tests for lookup logic use fake programs and never run a real one.
- **No real CR3 sample is in the repository.** The RAW path is covered only by a broken-file test.
- **Long paths:** `long_path()` is used for the attribute call and directory listings. Move and copy
  calls still depend on Windows' `LongPathsEnabled` setting.

### Phase 1: Skeleton & tooling (2026-11-12)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 1.

#### Added
- `pyproject.toml`: uv project with a hatchling build and `src/` layout. Dependency groups `gui`,
  `media` and `dev`; `uv sync` installs all three. Version `0.1.0` is the only version field.
- `uv.lock`, and `.python-version` pinned to **3.14**.
- `src/various_tools/` skeleton: `core/`, `tools/`, `cli/`, `gui/`. `__version__` is read from
  package metadata.
- `various-tools` CLI (Typer) with a `version` command, and `various-tools-gui`, which opens a
  placeholder window. Qt is imported only inside the GUI package.
- Tests: version, CLI and GUI smoke tests. Qt runs offscreen, so no display is needed.
- pytest uses `--import-mode=importlib`, so test files with the same name in different folders
  (for example `tests/core/test_x.py` and `tests/tools/y/test_x.py`) can coexist. Guarded by
  `tests/core/test_layout.py` and `tests/gui/test_layout.py`.
- Quality tooling: ruff (lint and format), pyright (basic, strict on `core/`), pytest, and
  pre-commit (ruff and pyright hooks).
- `.github/workflows/ci.yml`: lint, format check, type check and tests on `windows-latest`.

#### Changed
- `.gitignore`: ignores `.claude/`, `.ruff_cache/` and `.pyright/`.

#### Decisions (not covered by the plan)
- **Python 3.14, not 3.13.** Python 3.15 fails: `pyyaml` (a pre-commit dependency) has no 3.15
  wheel. Python 3.14 installs every dependency from wheels.
- ruff skips `legacy/` and the root `empty_folder_deleter.py` until Phase 3 ports them, and it
  formats only Python files. The pre-commit ruff hooks are limited to Python for the same reason.
- CI does not install ffmpeg yet, because Phase 1 has no media tests. Phase 2 adds it.

### Phase 0: Prep (2026-11-11)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 0.

#### Added
- `legacy/`: read-only, byte-identical copies of the 8 standalone scripts from the OneDrive
  `Scripts` folder, with `legacy/README.md` mapping each one to its planned new home.
- `AGENTS.md` (stack, layout, commands, 🔒 safety rules) and `CLAUDE.md` (a copy of it).
- `CHANGELOG.md`.
- `*.exe` to `.gitignore`, so binaries are no longer committed.
- `docs/MODERNIZATION_PLAN.md` committed to the repo.

#### Changed
- Product name confirmed as **Various Tools** (package `various_tools`, CLI `various-tools`).
  The name is applied in the plan, the legacy README and the README. The `TidyKit` placeholder
  is gone.
- README: the title is now Various Tools, with the current app described as the first tool.
  Placeholder owner names are replaced with `jay-singhvi`, and the nonexistent
  `requirements.txt` step is removed.

#### Removed
- `releases/EmptyFolderDeleterSetup.exe` is no longer tracked by git. The file is still on disk.
  Installers will be published through GitHub Releases. The binary remains in git history.
