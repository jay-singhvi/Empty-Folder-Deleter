# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Phase 3.5: `media_resize` tool (2026-11-12)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 3, item 3.5. User page: `docs/tools/media_resize.md`.

#### Added
- `tools/media_resize/`: options (`MediaResizeOptions`, `extra="forbid"`) and the `MediaResize` tool.
  `scan()` works out the new size of each image and video, encodes images in memory to report their
  exact output size, and writes nothing. Each resize is a planned `convert` action. `apply()` writes
  the outputs through the generic engine, so each one is journaled.
- `tools/media_resize/geometry.py`: the size rules, as pure functions. `fit_box` is `app.py`'s rule
  (both sides must exceed the box). `shorter_side` and `longer_side` are metadata-8's formula.
  `percent` is `app.py`'s. Video sizes are made even, as `app.py` does.
- `tools/media_resize/images.py`: decode (through `core/media/imaging.open_image`), EXIF orientation,
  resize, and encode with the metadata kept. Writes go through a temporary file, with the size checked
  before the rename, and the source's modified time is kept.
- `tools/media_resize/videos.py`: the ffmpeg arguments from `app.py` (scale, H.264 at the chosen CRF,
  audio copied) and the dry-run size estimate from `app.py`.
- `core/deps.py`: `ExternalTools.ffmpeg()`, a named accessor, as `exiftool()` and `ffprobe()` already
  are, so tool code never spells out a program name.
- `pyproject.toml`: the `media_resize` entry point.
- Tests (`tests/tools/media_resize/`, 41 tests). **Parity with `legacy/app.py`:** the legacy Streamlit
  script runs as it is, with only the Streamlit and pandas modules stubbed. It gets the same answers
  as the tool's options, and the two output folders must match file by file (name and size). Three
  configurations are checked for images (preset 1080p, custom 1280×720, percentage 50), and one for
  videos (720p). Also: the size rules, the defaults, the 🔒 never-upscale rule, scan changing nothing,
  originals untouched, the modified time kept, existing outputs never overwritten, batch name claims,
  cloud-only files never read, suffix output not rescanned, recursion skipping the output folder,
  protected roots, motion photos left alone, metadata kept and orientation applied once, missing
  ffmpeg, RAW conversion needing ExifTool, bad options, a saved plan applied later, a 90° rotated
  video resized to the size the viewer shows, and undo reporting outputs as manual.
- `docs/tools/media_resize.md`: user page.

#### Changed
- Nothing outside the new tool, apart from `core/deps.py` (the accessor), the entry point, and this
  changelog.

#### Decisions (not covered by the plan)
- **🔒 `never_upscale` is locked on.** The field exists so the rule is visible, but it only accepts
  `True`. Every mode also checks again that the target is not larger than the file.
- **Overwriting the original is not offered** (the plan lists `overwrite`). Undo cannot restore a
  replaced file, so the safer option is to keep the originals. Only `new_folder` and `suffix` remain.
- **CR3 originals are kept.** The plan says to remove them after the JPEG is verified (metadata-8
  removed them). Removing them needs the same undo decision as the Duplicate Finder's trash action, so
  I left it for the owner. The RAW path (rawpy decode, ExifTool metadata copy, size check, rename) is
  written, but **not run end-to-end**: there is no sample CR3 in the repository, and I did not add one.
- **Undo cannot reverse a resize.** `core/journal.py` treats a `convert` as not reversible, so undo
  lists each output as "restore manually" and deletes nothing. This is the same engine limit that
  Phase 3.4 notes for trash.
- **Shorter-side and longer-side use metadata-8's formula** (rounded sides). `fit_box` and percentage
  use `app.py`'s (truncated sides). The metadata-8 script cannot be imported without pandas, so
  its formula is tested by value, not by running the script.
- **JPEG quality defaults to 85** (`app.py`, and the plan), not 95 (metadata-8).
- **Percentage 100 is a no-op,** so it is not listed. `app.py` would re-encode the file.
- **Animated images, motion photos and HDR JPEGs are left alone.** `app.py` resized them anyway,
  dropping frames or embedded data. metadata-8 already skipped them, so I followed metadata-8.
- **HEIC and HEIF are not resized** here; they are not in `app.py`'s format list. Phase 7 can add them.
- **Rotated videos:** ffmpeg applies the stored rotation, and the target is given in the orientation
  the viewer sees. `app.py` swaps the target for rotated videos. I did not run `app.py` on a rotated
  video, so I cannot say what its output is. The tool's output is checked with ffprobe.
- **Output folder is excluded from the scan** by its position (`<root>/<output folder>/`), not by name
  anywhere in the tree. In suffix mode, files named `*_resized` are not scanned.
- **Image sizes are exact** (encoded in memory during scan). **Video sizes are estimates** (`app.py`'s
  dry-run formula), and the plan says so.
- **Each apply re-reads the image** to render it again, so the output reflects the file as it is at
  apply time. A file that changed since the scan is handled the same way as a new file.
- **Metadata:** the EXIF block is kept, with the orientation reset to 1 because the pixels are already
  turned. The source's modified time is copied to the output.
- **`_root_refusal` is a fourth local copy** (rename, project_export and duplicates each have one). It
  should move into `core/fs.py` in a later cleanup, together with the others.
- **Local environment note:** `uv` is not on this machine, so the entry point was added to the venv's
  installed metadata by hand. `uv sync` regenerates the same entry.

### Phase 3.4: `duplicates` tool (2026-11-12)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 3, item 3.4. User page: `docs/tools/duplicates.md`.

#### Added
- `tools/duplicates/`: options (`DuplicatesOptions`, `extra="forbid"`) and the `Duplicates` tool. `scan()`
  finds exact, visual, burst and re-encode duplicates, writes nothing (apart from the scan cache), and
  lists each copy that would move, with the copy that stays. `apply()` runs the plan through the generic
  engine, so every move is journaled and can be undone from History.
- `tools/duplicates/images.py`: a port of `legacy/python-find_duplicates.py`: the three passes, the band
  index, the capture-time guard, the burst grouping, the pixel check, and the raw EXIF text as written.
- `tools/duplicates/videos.py`: a port of `legacy/find_duplicate_videos.py`: exact copies, and the
  duration, aspect and sampled-frame check.
- `tools/duplicates/keeper.py`: the two keeper orderings, kept separate as in the legacy scripts, and the
  `Match` record. `tools/duplicates/verify_ollama.py`: the optional model check, with errors counted as
  "not confirmed". `tools/duplicates/explain.py`: every check for two images, as a pure function.
- Thresholds are advanced options, prefilled with the tuned values (🔒). A test pins the defaults.
- `pyproject.toml`: the `duplicates` entry point.
- Tests (`tests/tools/duplicates/`, 30 tests). Parity with both legacy scripts: the same generated tree is
  run through the legacy finder and the tool, and the plans must match file by file. The image fixture
  covers exact, visual and burst matches, and the video fixture covers exact and re-encode matches. Also:
  the tuned defaults, scan changes nothing, apply then undo, a saved plan applied later, managed folders
  not rescanned, the trash action, exact-only, explicit folders, cloud-only files never read, unreadable
  files never moved, the capture-time guard, a refused protected root, a missing ffprobe, the cache
  (reused until a file changes, with a round trip that keeps every feature), keeper ordering, explain,
  and the Ollama check (YES, NO, errors, a missing model, and an unreachable server).
- `docs/tools/duplicates.md`: user page.

#### Changed
- Nothing outside the new tool, apart from the `pyproject.toml` entry point and the changelog.

#### Decisions (not covered by the plan)
- **Each root has its own `duplicate` and `review` folders** (the names come from the settings). These
  replace the hardcoded `BASE`. Explicit folder options apply to every root.
- **Burst and re-encode matches always go to the review folder.** The trash action applies only to exact
  and visual copies. Review is for a person to confirm, so those matches are never trashed.
- **The trash action cannot be undone by the engine.** The journal records it, and undo lists it as
  "restore manually". Moves are undoable. Rule 5 asks for undo of every action, so this is a limit to
  decide on. The default is move, not trash.
- **Hashing uses threads, not processes.** The legacy script used a process pool. Threads avoid spawn
  costs and PyInstaller issues, and Pillow releases the GIL while decoding.
- **Videos are scanned recursively,** as photos are. The legacy video script looked only at the top level.
- **The scan cache is for photos only,** as in the legacy script, and lives in the user cache folder
  (`duplicates-images.sqlite`, version 1). Videos are probed on every scan.
- **Photos with a capture time are compared as written.** The legacy script compared the EXIF strings.
  The parsed, plausibility-checked time from `core/media/exif.py` is not used here, so the matches stay
  the same.
- **Missing ffprobe skips the video part with a note.** Missing ffmpeg skips only the re-encode check, and
  exact video copies are still found. Nothing crashes.
- **Ollama unreachable or model missing:** borderline matches are not moved, and the plan says why.
- **A video that is also an exact copy but cannot be read is not moved,** as in the legacy script.
- **Recursion and the managed folders are always on.** The duplicate and review folders are never scanned.
- **`explain()` is a pure function.** The CLI command and the GUI "Explain match" button are wired in
  Phases 4 and 5.
- **The root check is a third copy** of the helper in `rename` and `project_export`. It should move into
  `core/fs.py` in a later cleanup.
- **Local environment note:** `uv` is not on this machine, so the entry point was added to the venv's
  installed metadata by hand. `uv sync` regenerates the same entry.

### Phase 3.3: `rename` tool (2026-11-12)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 3, item 3.3. User page: `docs/tools/rename.md`.

#### Added
- `tools/rename/`: options (`RenameOptions`, `extra="forbid"`) and the `Rename` tool. `scan()` dates
  each matching file, numbers it and works out the new names, and writes nothing. The plan lists
  every old → new name. `apply()` renames the selected files through the generic engine, so each
  rename is journaled and can be undone from History.
- `tools/rename/presets.py`: three presets. `snapchat` reproduces `legacy/rename_snapchat.py`. `whatsapp`
  and `screenshot` are built-in examples named in the plan.
- `tools/rename/naming.py`: pure helpers for dates in file names, name templates, letter case and
  Windows file-name rules.
- `pyproject.toml`: the `rename` entry point.
- Tests (`tests/tools/rename/`, 34 tests). Parity with `legacy/rename_snapchat.py`: the same tree is
  renamed by the legacy script and by the tool, and every file ends with the same name. Also: scan
  writes nothing, apply then undo restores every name, a saved plan applied later, existing files
  never overwritten, names claimed twice in one batch, a re-check at apply time, a plan edited to
  select a blocked row, case-only changes, each date source, cloud-only files never read, bad
  options, invalid Windows names, counters per minute, per day and global, recursion, overlapping
  roots, protected roots, and junctions that are not followed.
- `docs/tools/rename.md`: user page.

#### Changed
- `core/deps.py`: `ExternalTools.exiftool()` and `ffprobe()`, named accessors. Tool code uses them
  instead of spelling out a program name, which AGENTS.md keeps inside `core/deps.py`.

#### Decisions (not covered by the plan)
- **The `whatsapp` and `screenshot` presets are my reading** of the plan's "WhatsApp IMG-…" and
  "Screenshot_…" examples. Their templates and matches are data in `presets.py` and easy to change.
- **A taken name is never renamed to `_dupN`.** The engine adds a `_dupN` suffix when a move's target
  exists. For a rename that would change a name the user previewed, so the tool lists the row
  unselected instead. Apply checks the names again, so a plan that is old, or edited, cannot rename
  onto a file that has appeared since.
- **Renames that change only letter case are listed, not performed.** The engine treats the file as
  taken, which would give a `_dupN` name. Fixing this needs an engine change, so it is deferred.
- **The extension is the only part of the name whose case the `case` option changes.** The name
  itself is kept.
- **`{ext}` is required in every template,** so a file keeps its type.
- **Counters are kept per folder** for `per-minute` and `per-day`, and across the whole batch for
  `global`. Files are numbered in date order. Files with the same date are numbered by name, not by
  the directory order the legacy script happened to get.
- **Only files are renamed.** The legacy script also renamed folders whose names matched.
- **Recursion is off by default,** as in the legacy script.
- **Matching ignores letter case,** as Windows does. Regular expressions use `IGNORECASE` too.
- **`capture` date source:** EXIF, then ExifTool, then ffprobe for videos. A cloud-only file is never
  read for its capture date. Renaming it by modified or created time changes only the name, so that
  is allowed.
- **`created` uses the file's creation time** (`st_birthtime` where Python provides it, otherwise
  `st_ctime`, which on Windows is the creation time).
- **Filename dates must fall in or after `min_plausible_year`** from the settings, and a file name
  that holds no valid date is skipped with a note.
- **Each root is checked by a local copy of the root refusal from `project_export`.** It is not
  shared yet, and moving it into `core/fs.py` is a candidate for a later cleanup.

### Phase 3.2: `project_export` tool (2026-11-12)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 3, item 3.2. User page: `docs/tools/project_export.md`.

#### Added
- `tools/project_export/`: options (`ProjectExportOptions`, `extra="forbid"`) and the `ProjectExport`
  tool. `scan()` lists and reads the selected files, reports what is skipped and an estimated token
  count, and writes nothing. The plan is one `write` action. `apply()` renders the export again from
  the saved options, so a plan saved as JSON can be applied later.
- `tools/project_export/source.py`: per-file text. Python comments are removed with `tokenize`, and
  notebooks are read cell by cell.
- `pyproject.toml`: the `project_export` entry point.
- Tests (`tests/tools/project_export/`, 25 tests). Parity with the legacy scripts in `legacy/`: the
  v1 file list (same files for the include, exclude and extension cases) and the v3 contents (exact
  match without stripping, same lines with comments stripped, including a notebook and a binary file).
  Plus the comment-stripping fix, the default folder exclusions, the output file never being part of
  its own export, scan writing nothing, cloud-only files never read, the size limit, protected roots
  and output locations, no overwriting, and a saved plan applied later.
- `docs/tools/project_export.md`: user page.

#### Decisions (not covered by the plan)
- **Parity reference is `legacy/`.** The v1 walk runs when its module is imported, so the test
  extracts only `generate_file_structure` from the file with `ast`. The v3 module is imported, since
  its entry code sits behind `__main__`.
- **Include notebook outputs is off by default.** The legacy script included them. Printed output
  can hold secrets, so the safer default is off. The parity test turns it on.
- **Python docstrings are kept.** The plan says to keep string literals, and the original regex
  deleted ordinary triple-quoted strings. Only real comment tokens are removed.
- **Trailing comments in notebook code cells are removed too.** The legacy script removed only whole
  comment lines in notebooks. Removing trailing ones as well matches the Python behavior.
- **Output order ignores case.** Folders and files are sorted with names compared without case. The
  legacy script sorted case-sensitively, so `README.md` came before lowercase names.
- **Tree output is indented, not a flat list.** The parity test compares the set of files.
- **Empty folders are left out of the contents.** The legacy script printed a header for every
  folder, including ones with no matching files.
- **`root` is required.** There is no default folder, so a scan can never start on a folder that was
  not chosen.
- **The output folder must already exist.** It is also checked against protected paths.
- **A write never overwrites.** When the name is taken, the export gets a free name (`_dupN`), and
  the journal records the name it used.
- **Undo does not remove the export file.** The journal lists it for manual removal, as it does for
  other writes.
- **Clipboard copy is deferred to Phase 5 (GUI).** The plan lists `copy_to_clipboard`. Core code
  has no UI, so the clipboard belongs in the Qt layer. The option is not in this release.
- **Size limit is off by default** (`max_file_kb` is not set). Larger files are listed as skipped
  when it is set.

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
