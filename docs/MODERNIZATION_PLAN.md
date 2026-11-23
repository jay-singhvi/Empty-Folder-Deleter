# Modernization Plan: Empty Folder Deleter → a unified file & media toolkit

> **Audience:** the AI coding agents (and the human reviewing them) who will carry out this plan.
> Work through the phases in order. Each task has acceptance criteria. Don't skip them.
> **Don't change the tuned thresholds or safety rules marked 🔒** unless the owner asks you to.

---

## 1. Goal

Turn this 2-year-old, single-purpose Tkinter app plus seven separate scripts into **one
installable Windows app** with:

- a **shared core library**, so hashing, EXIF reading, ffmpeg calls, OneDrive checks, file
  walking and move/undo logging each exist in exactly one place;
- **tools as plugins**: each tool declares its own options, and both the GUI and the CLI build
  themselves from those declarations, so nothing is hardcoded;
- **one safe workflow for every tool**: *Scan → Preview plan → Pick items → Apply → Undo*;
- a desktop GUI for everyday use and a CLI for scripting.

Name: **Various Tools** (package `various_tools`, CLI `various-tools`). Confirmed by the owner in
Phase 0 and applied everywhere.

---

## 2. What exists today

### 2.1 This repo (`Empty-Folder-Deleter`)

| File | State |
|---|---|
| `empty_folder_deleter.py` (162 lines) | Tkinter GUI. Walks the tree bottom-up, lists empty leaf folders, asks yes/no, deletes, repeats. |
| `empty_folder_deleter.spec` | PyInstaller one-file build with UPX. |
| `installer.nsi` | NSIS installer. Installs per machine (needs admin) and has a "start on Windows startup" checkbox. |
| `releases/EmptyFolderDeleterSetup.exe` | Built binary committed to git. |
| `README.md` | Has `your-username` placeholders and mentions a `requirements.txt` that doesn't exist. |

**Bugs found in the current app (fix these, or make sure the rewrite doesn't repeat them):**

1. **Crash on any delete error.** `os.errno` was removed in Python 3.7, so any `OSError`
   (permission denied, folder in use) raises `AttributeError` in the worker thread. The Start
   button then stays disabled until the app restarts. Use the `errno` module.
2. **The selected root folder can be deleted.** If the root is empty, or becomes empty after a
   pass, `find_empty_folders` includes it and it gets removed.
3. **Tk is called from a worker thread.** `log_message`, `messagebox.askyesno` and
   `button.config` all run off the main thread. That isn't thread-safe and can freeze or crash
   the app.
4. **Deletes are permanent.** Nothing goes to the Recycle Bin and there's no undo.
5. **Junctions and symlinks aren't handled.** On Windows, a walk can follow directory
   junctions out of the selected tree.
6. **"Empty" is too strict.** A folder that holds only `desktop.ini`, `Thumbs.db` or
   `.DS_Store` is effectively empty, but the app never treats it that way.
7. **The rescan loop is slow and confusing.** Every pass rescans the whole tree and asks again.
   One bottom-up pass can work out which folders will end up empty.
8. **Unused imports:** `logging`, `concurrent.futures`, `datetime`, `ttk`, `sys`.
9. **Installer:** the startup-shortcut state is kept in `$0`, which may be overwritten before
   the install section reads it. Registry values for Publisher and DisplayVersion are wrapped
   in literal quote characters. Admin rights are required for no good reason. Starting a
   folder cleaner at Windows startup makes no sense, so drop the option.
10. **The version is hardcoded in three places** (`.py`, `.nsi`, README).
11. **UPX plus one-file PyInstaller** often triggers antivirus false positives.

### 2.2 External scripts (in `%USERPROFILE%\OneDrive\Scripts\`, outside this repo)

| Script | What it does | Hardcoded values to replace with options |
|---|---|---|
| `app.py` (Streamlit, 770 lines) | Image & video **resolution reducer**: presets, percentage, custom box, JPEG quality, video CRF, dry run, output to a new folder or overwrite. Uses ffmpeg/ffprobe. Opens a Tk folder picker from inside Streamlit, which is a hack. | Presets dict, format lists, output folder name, CRF presets |
| `python-find_duplicates.py` (608 lines) | **Image duplicate finder.** SHA-256 exact matches, plus pHash+dHash visual matches with aspect, EXIF capture-time and 8×8 pixel-block checks, burst grouping, band indexing, a SQLite cache, multiprocessing, optional **Ollama vision check** of borderline pairs, `--explain A B`, a review list, and a CSV log that allows undo. | `BASE = <user>\OneDrive\Pictures`, input-list file next to the script, thresholds, Ollama URL and model |
| `find_duplicate_videos.py` (197 lines) | **Video duplicate finder.** SHA-256 exact matches, plus duration, aspect and pHash of 5 sampled frames. Moves files to `duplicate/` or `review/` and keeps a CSV log. | `FOLDER` = the script's own folder, thresholds, sample points |
| `python-extract_file_metadata-8.py` (1703 lines) | **Organize photos and videos by date** into `YYYY\YYYY - MM` folders using EXIF, ffprobe, ExifTool or mtime. Aware of OneDrive placeholders (never hydrates without asking). Resizes by shorter side, converts CR3→JPEG, keeps an Excel master log with upsert, checkpoint/resume, `--errors-only`, `--skip-processed`, workers. | `SOURCE_FOLDER` (a specific month), `DEST_ROOT`, `FOLDER_NAME_FORMAT`, master-log path, JPEG quality |
| `rename_snapchat.py` (39 lines) | Renames `Snapchat-<digits>.ext` to `Snapchat-YYYYMMDD-HH_MM-NNN.ext` using mtime. | Folder `"."`, regex, name template. **Doesn't check for name collisions and has no dry run.** |
| `python-v3-Extract_Project_Code.py` (408 lines) | Concatenates source files and notebooks into one text file (to give an LLM context). Can strip comments, markdown and outputs. | A hardcoded list of ~20 notebook paths, exclusion list, output name. Its comment-stripping regex also deletes ordinary triple-quoted strings. |
| `python-v1-Extract_Project_Structure.py` (152 lines) | Lists a project's files with include/exclude folder and extension filters. | The call at the bottom has hardcoded folders. `".venv" in root` is a substring check. |
| `requirements-find_duplicates.txt` | `ImageHash numpy pillow pillow-heif` | Move into `pyproject.toml` |

### 2.3 Redundancy map (what to merge)

| Duplicated logic | Where it lives today | Single home in the new design |
|---|---|---|
| Streaming SHA-256 + size pre-bucketing | find_duplicates, find_duplicate_videos | `core/hashing.py` |
| `keep_score` + `COPY_MARK` "(1)/copy" detection | find_duplicates, find_duplicate_videos | `tools/duplicates/keeper.py` |
| ffmpeg/ffprobe discovery, probe, frame grab, rotation | app.py, find_duplicate_videos, metadata-8 | `core/media/ffmpeg.py` |
| EXIF capture time, orientation, GPS | app.py, find_duplicates, metadata-8 | `core/media/exif.py` |
| HEIF/RAW opener registration | find_duplicates, metadata-8 | `core/media/imaging.py` |
| Image resize (two different rules: "fit box" in app.py, "shorter side" in metadata-8) | app.py, metadata-8 | `tools/media_resize/` with selectable modes |
| Collision-safe destination name (`_dup1`, unique suffix) | find_duplicates, find_duplicate_videos, metadata-8 | `core/fs.py: unique_destination()` |
| Move + CSV/Excel log + undo | find_duplicates, find_duplicate_videos, metadata-8 | `core/journal.py` (SQLite) |
| Folder walk with include/exclude filters | every script | `core/fs.py: walk()` |
| OneDrive cloud-only detection | metadata-8 only, but **every tool needs it** (hashing a cloud-only file downloads it) | `core/onedrive.py` |
| Dry run / `--move` flag | every script, implemented differently | The Plan → Apply workflow in `tools/base.py` |
| Folder picker | Tk in two places, including the Streamlit hack | GUI widget generated from the option type |
| Project tree + project code export | v1 + v3 | `tools/project_export/` |

---

## 3. Target architecture

### 3.1 Stack decisions (already made, don't revisit)

| Concern | Choice | Why |
|---|---|---|
| Python | Use the **newest 3.x that has wheels for every dependency**: PySide6, rawpy, pillow-heif, numpy, imagehash. Verify this in Phase 1 and pin it in `.python-version`. The machine has 3.15, but RAW/HEIF wheels often lag behind; 3.13 is the likely safe pin. | Reproducible builds |
| Env & packaging | **uv** + `pyproject.toml` (hatchling backend), `src/` layout | One lockfile and fast installs. Replaces the scattered requirements files. |
| GUI | **PySide6 (Qt 6)** | Native Windows look, real worker threads with signals, sortable and checkable tables for plan previews, thumbnail grids for duplicate review, native folder dialogs, and it packages cleanly. Streamlit and Tkinter can't do these well. |
| CLI | **Typer** + **Rich** | Commands and flags are generated from each tool's options model. Rich provides progress bars and tables. |
| Options / config | **pydantic v2** models + **pydantic-settings**. Settings are stored as TOML in `platformdirs.user_config_dir("Various Tools")`. | One schema drives validation, the GUI form, CLI flags and saved presets. |
| Data / logs | **SQLite** (stdlib) for the journal, caches and the media catalog. Excel/CSV are **export formats only**. | Upserting into an Excel file gets slower with every run. SQLite is safe to write from parallel workers and resumable. |
| Recycle Bin | **send2trash** | Every delete becomes reversible. |
| Gitignore-aware filters | **pathspec** | Project export and other walks can respect `.gitignore`. |
| Media | Pillow, pillow-heif, rawpy, imagehash, numpy; ffmpeg, ffprobe and ExifTool as external binaries detected at runtime | Same dependencies the scripts already use |
| Quality | ruff (lint + format), pyright (basic → strict on `core/`), pytest, pytest-qt, pre-commit | |
| Build | PyInstaller **one-dir** (no UPX) → **Inno Setup** or the existing NSIS, **per-user install (no admin)**. GitHub Actions builds the installer on each `v*` tag and attaches it to a GitHub Release. | Fewer AV false positives, and no binaries in git |

### 3.2 Repository layout

```
pyproject.toml            # single source of version, deps, entry points
uv.lock
.python-version
README.md  CHANGELOG.md  LICENSE  AGENTS.md
docs/
  MODERNIZATION_PLAN.md   # this file
  tools/<tool-id>.md      # one user doc per tool
legacy/                   # read-only copies of the original scripts (Phase 0), deleted in Phase 8
src/various_tools/
  __init__.py             # __version__ read from package metadata
  core/
    fs.py                 # walk(), filters, unique_destination(), reparse-point checks, long-path support
    onedrive.py           # sync status, is_on_device(), hydrate(), unpin()   🔒 never hydrate implicitly
    hashing.py            # sha256 streaming, size buckets, perceptual hashes, band index
    media/
      imaging.py          # open_image() with HEIF/RAW registration, exif_transpose, save with metadata
      exif.py             # capture_time(), gps(), camera info; ExifTool fallback
      ffmpeg.py           # locate binaries, probe(), grab_frame(), display_size(), transcode()
    journal.py            # SQLite operation log: runs, actions, status, undo()
    plan.py               # Plan, PlannedAction (Move/Rename/Trash/Delete/Write/Convert), apply engine
    context.py            # RunContext: settings, progress reporter, cancel token, logger, journal
    deps.py               # external tool detection + install hints (winget ids)
    settings.py           # AppSettings (pydantic-settings), presets store
    cache.py              # versioned SQLite cache helper (generalized from find_duplicates)
  tools/
    base.py               # Tool protocol + registry (entry points group "various_tools.tools")
    empty_folders/
    duplicates/           # images.py, videos.py, keeper.py, verify_ollama.py, explain.py
    media_resize/
    organize_by_date/
    rename/
    project_export/
    (new tools from Phase 7)
  cli/main.py             # Typer app; one sub-command per registered tool
  gui/
    app.py  main_window.py
    widgets/              # schema_form.py, plan_table.py, image_compare.py, log_panel.py, path_picker.py
    workers.py            # QThread/QRunnable bridge for RunContext progress + cancel
tests/
  core/  tools/<tool>/  gui/  fixtures/   # generated fixture trees and media, no personal files
packaging/
  various_tools.spec  installer.iss (or installer.nsi)  icon.ico
.github/workflows/ci.yml  release.yml
```

### 3.3 The Tool contract (this is what keeps things dynamic)

```python
class ToolOptions(BaseModel):            # each tool subclasses this
    model_config = ConfigDict(extra="forbid")

class Tool(Protocol):
    id: str                               # "duplicates"
    name: str                             # "Duplicate Finder"
    description: str
    category: Literal["Clean up", "Media", "Organize", "Developer", ...]
    Options: type[ToolOptions]
    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan: ...      # must not change the disk
    def apply(self, plan: Plan, ctx: RunContext) -> RunResult: ...       # default: generic engine
```

Rules:
- `scan()` **never changes anything on disk** (caches are the one exception). Dry run is just
  "scan, then don't apply", so tools never need their own `--move` or `--dry-run` flags.
- Every `PlannedAction` has `kind`, `src`, `dst`, a human-readable `reason`, a `group_id` (for
  duplicates) and an `selected: bool` default. The GUI shows these in a table with checkboxes.
- `apply()` writes every action to the journal **before** running it and marks it done or
  failed afterwards. Undo replays the journal in reverse: move back, rename back, restore from
  the Recycle Bin when possible.
- Option fields carry UI hints through `Field(json_schema_extra=...)`:
  `widget: "folder" | "folders" | "file" | "glob_list" | "ext_list" | "slider" | "template"`,
  `group`, `advanced: bool`, `depends_on`. `gui/widgets/schema_form.py` renders **any**
  options model from those hints, and `cli/main.py` turns the same model into flags.
  **Adding a tool should never need GUI or CLI code.**
- Tools are discovered through the `various_tools.tools` entry-point group, so a new tool is just a
  new package that registers itself.
- Presets: any options instance can be saved by name (e.g. "Phone camera roll dedupe") and
  reloaded from the GUI dropdown or with `--preset NAME` in the CLI. *(Removed in 0.3.0 by the
  owner's decision; see CHANGELOG.md.)*
- `RunContext` provides `progress(done, total, msg)`, `check_cancelled()`, `log`,
  `settings` and `journal`. Tools never print or touch Qt directly.

### 3.4 Global safety rules 🔒

1. Dry-run preview is the default everywhere. Applying a plan always requires an explicit
   action: the Apply button, or `--apply` in the CLI.
2. Deletes go to the Recycle Bin by default. Permanent delete is an advanced setting that asks
   for confirmation.
3. **OneDrive cloud-only files are never read or hashed implicitly.** Any step that would open
   file contents checks `onedrive.is_on_device()` first. Files that aren't on the device are
   reported as "skipped: cloud-only" and come with an optional "download these N files"
   action. This rule comes from metadata-8 and now applies to all tools.
4. Never follow junctions or symlinks out of the selected root. Never delete the root itself.
5. Every applied action is journaled and can be undone from History.
6. Protected paths (Windows dir, Program Files, user profile root, drive roots) are rejected as
   targets unless the user explicitly overrides it in settings.

### 3.5 External tools: found automatically, or set by the user

The app uses four external programs: **ffmpeg**, **ffprobe**, **ExifTool** and **Ollama**. Code
never assumes any of them is on PATH or in a fixed folder. Every lookup goes through
`core/deps.py`, and **every tool path can be overridden by the user**.

**Lookup order** (first match wins). `resolve(tool_id) -> ToolLocation | None`:

1. **User override in settings**, e.g. `[external_tools.exiftool] path = "C:\Program Files\Exiftool\exiftool.exe"`,
   set from the GUI or `various-tools config set`.
2. **Environment variable**: `VARIOUS_TOOLS_FFMPEG`, `VARIOUS_TOOLS_FFPROBE`, `VARIOUS_TOOLS_EXIFTOOL` or
   `VARIOUS_TOOLS_OLLAMA_URL`. These are handy for CI and portable installs.
3. **PATH** (`shutil.which`).
4. **Known install locations.** This list is data in `deps.py`, not code branches, so adding a
   location is a one-line change:
   - ExifTool: `%ProgramFiles%\ExifTool\exiftool.exe`, `%ProgramFiles(x86)%\ExifTool\exiftool.exe`,
     `%LOCALAPPDATA%\Programs\ExifTool\ExifTool.exe`, and the WinGet links/packages folders.
     The first two come from metadata-8's `find_exiftool()`, and the owner's actual install is
     `C:\Program Files\Exiftool\exiftool.exe`. Compare paths case-insensitively.
   - ffmpeg/ffprobe: `%LOCALAPPDATA%\Microsoft\WinGet\Links\`,
     `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin\` (the owner's ffmpeg is
     installed this way), `C:\ffmpeg\bin\`, `%ProgramFiles%\ffmpeg\bin\`. If ffprobe isn't set,
     look next to the resolved ffmpeg.
   - Ollama: the URL defaults to `http://localhost:11434`, and the executable to
     `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`.
5. Not found: the dependent feature is **disabled with a clear message**, not a crash. For
   example, "CR3 conversion needs ExifTool: Locate… / Install with winget".

**Validation.** Before accepting a path, from any source, run a version command
(`exiftool -ver`, `ffmpeg -version`, `ffprobe -version`, `GET /api/version` for Ollama) with a
short timeout. Store the version alongside the path. If a saved override stops working (the
file was moved or uninstalled), show a warning and fall back to the next step of the lookup
order instead of failing.

**Caching.** Resolve each tool once per app session and cache the result. A "Re-detect" action
clears the cache.

**Where the user sets it:**
- **GUI → Settings → External tools:** one row per tool showing its status (✅ found / ⚠️
  missing / ❌ invalid), path, version, and where it was found (override / env / PATH /
  auto-detected). Each row has **Browse…** (a file dialog filtered to `.exe`), **Test**,
  **Auto-detect**, **Reset to automatic**, and a copyable install command. Ollama has a URL
  field instead of Browse.
- **Inline prompt:** when a tool needs a missing program, offer "Locate…" right there. The
  chosen path is validated and then saved to settings.
- **CLI:** `various-tools doctor` prints the same table, and
  `various-tools config set external_tools.exiftool.path "C:\Program Files\Exiftool\exiftool.exe"` /
  `various-tools config unset …` change it. Any command also accepts a one-off `--exiftool PATH`
  (likewise `--ffmpeg`, `--ffprobe`, `--ollama-url`).

**Rule for executing agents:** no module other than `core/deps.py` may call `shutil.which` or
hardcode an executable name or path. Media code receives the resolved paths through
`RunContext.deps`.

---

## 4. Phased execution plan

Each phase ends with a green CI run and a short entry in `CHANGELOG.md`.
Make one PR (or one commit series) per phase.

### Phase 0: Prep (small, do first)
1. Copy the 8 external scripts into `legacy/` unchanged. They're the reference for behavior.
   Add `legacy/README.md` saying they're read-only and listing where each one is ported to.
2. Write `AGENTS.md` (also symlinked or copied to `CLAUDE.md`) covering the stack, layout,
   commands (`uv run pytest`, `uv run ruff check`, `uv run various-tools-gui`), the 🔒 rules, and
   "don't change thresholds without asking".
3. Remove `releases/*.exe` from git. Releases will go to GitHub Releases. Add `dist/`,
   `build/` and `*.exe` to `.gitignore`.
4. Fix the README placeholders and confirm the product name with the owner.
5. *(Optional hotfix 1.0.1 on the old code, for people still using the current exe)*:
   bugs 1–3 from §2.1, i.e. `errno`, root protection, and marshalling Tk calls with `after()`.

**Done when:** the legacy copies are in, AGENTS.md exists, and there are no binaries in git.

### Phase 1: Skeleton & tooling
1. Write `pyproject.toml` with uv, the `src/` layout, dependency groups (`gui`, `media`, `dev`),
   and scripts `various-tools = various_tools.cli.main:app` and `various-tools-gui = various_tools.gui.app:main`.
2. Set up ruff, pyright and pytest configs, plus pre-commit.
3. Set up GitHub Actions CI on `windows-latest`: lint, type-check and test. Install ffmpeg in CI
   for the media tests (or mark those tests to skip when it's missing).
4. Pin the Python version after checking that wheels exist for every dependency (see §3.1).

**Done when:** `uv sync && uv run pytest` passes on a clean clone, and CI is green.

### Phase 2: Core library (`src/various_tools/core`)
Port these from the legacy scripts and **write unit tests for each one**. Generate fixtures with
Pillow and ffmpeg in `conftest.py`; never use personal photos.
- `fs.walk(root, include_globs, exclude_globs, exts, recursive, respect_gitignore, skip_reparse_points=True)`
- `fs.unique_destination(folder, name)`
- `onedrive.*`, ported from metadata-8 (`get_sync_status`, `is_on_device`, `is_regular_file_no_hydrate`, `force_download`, `unpin_file`)
- `hashing.sha256`, `hashing.group_exact(paths)` (size bucket → hash), and `hashing.phash/dhash`, `band_keys`, `hamming` from find_duplicates
- `media.exif.capture_time` (merging find_duplicates `capture_time` with metadata-8 `get_exif_data`, `parse_exif_datetime`, `is_plausible_date` and the ExifTool fallback)
- `media.ffmpeg.probe/grab_frame/display_size/transcode`, including the "step back if the frame is empty" logic from find_duplicate_videos and the rotation handling from app.py
- `media.imaging.open_image/save_image`, which registers HEIF and handles RAW
- `plan.py`, `journal.py` (schema: `runs(id, tool, options_json, started, finished, status)` and `actions(id, run_id, seq, kind, src, dst, reason, status, error, ts)`), `undo(run_id)`
- `cache.py`, generalized from the find_duplicates SQLite cache: keyed by path + size + mtime, versioned, resumable
- `deps.py`: implement §3.5 in full (user override → env var → PATH → known locations → disabled), with validation, caching and a `winget install` hint. Tests must cover each lookup step, an override that has become invalid falling back, and case-insensitive paths, using fake executables in a temp dir.
- `settings.py`: defaults include the OneDrive root (resolved the same way metadata-8 does), default output folder names, worker count = CPU count, Ollama URL

**Done when:** core coverage is ≥ 85% and there are no Qt or print calls in `core/`.

### Phase 3: Port the tools (one PR each, in this order)
For every tool: write the options model, `scan()`, tests that compare against the legacy script
on the same fixture tree, and a doc page.

**3.1 `empty_folders`** (rewrite of this repo's app)
- Options: `roots: list[folder]`, `treat_as_empty: list[str] = ["desktop.ini","Thumbs.db",".DS_Store"]`, `exclude_globs` (default `.git`, `node_modules`), `min_age_days: int = 0`, `skip_hidden_system: bool`, `delete_mode: trash|permanent`.
- One bottom-up pass works out which folders will end up empty, including nested ones. The plan
  shows them as a tree, and unchecking a folder unchecks its parents. Never include the root.
- Fixes every bug in §2.1.

**3.2 `project_export`** (merges v1 structure + v3 code export)
- Options: `root`, `include_globs`, `exclude_globs` (defaults: `.git`, `.venv`, `__pycache__`, `node_modules`, `dist`, `build`), `respect_gitignore=True`, `exts`, `mode: tree|contents|both`, `strip_comments`, `include_markdown_cells`, `include_outputs`, `max_file_kb`, `output_format: txt|md`, `output_path`, `copy_to_clipboard`.
- Strip comments **using `tokenize`** for Python, not regex. Keep real string literals (this fixes the v3 bug). Show a token or size estimate in the preview.
- This tool's "plan" is a single `Write` action.

**3.3 `rename`** (generalizes rename_snapchat)
- Options: `roots`, `match` (glob or regex, with presets such as "Snapchat export", "WhatsApp IMG-…", "Screenshot_…"), `template` (e.g. `{prefix}-{date:%Y%m%d}-{time:%H_%M}-{counter:03}{ext}`), `date_source: capture|modified|created|filename`, `counter_scope: per-minute|per-day|global`, `case`.
- Live preview of old → new names. Detect collisions both within the batch and against existing files. Undo through the journal.
- The Snapchat behavior becomes a built-in preset that gives identical output.

**3.4 `duplicates`** (merges the image and video finders)
- Options: `roots` (replaces `find_duplicates_input_list.txt`), `media: images|videos|both`, `exact_only`, `duplicate_folder` and `review_folder` (default to inside each root, replacing the hardcoded `BASE`), `action: move|trash`, `use_cache`, `workers`, an advanced threshold group (prefilled with today's values 🔒: `PHASH_MAX=2`, `DHASH_MAX=4`, `ASPECT_TOL=0.02`, `BURST_PHASH_MAX=40`, `PIXEL_BLOCK_MAX=3.0`, video `DUR_TOL_S=0.6`, `DUR_TOL_PCT=0.01`, `FRAME_MEAN_MAX=10`, `FRAME_WORST_MAX=24`, `SAMPLE_AT=(.1,.3,.5,.7,.9)`), and `verify_with_ollama` + `ollama_model` (the dropdown is filled live from `GET /api/tags`; default `lfm3-vl:latest`).
- Port the matching logic **exactly**: band index, capture-time guard, burst grouping by filename second, sharpest-frame keeper, block-diff check, borderline → model check. Merge the two `keep_score` functions into `keeper.py`, keeping each media type's ordering.
- Plan groups: "exact" → duplicate folder, "visual" → duplicate folder, "burst" and "re-encode" → review folder, each with its `reason` string.
- GUI: a side-by-side compare view per group (thumbnails, resolution, size, date, reason) where the user can change which copy is kept. The `--explain A B` command becomes "Explain match" in the GUI and `various-tools duplicates explain A B` in the CLI.
- Respect the OneDrive rule: cloud-only files are skipped and reported, not hashed.
- Regression test: on a fixture set, the new plan must equal the legacy script's plan.

**3.5 `media_resize`** (merges app.py + metadata-8 resize + CR3 conversion)
- Options: `roots`, `recursive`, `media: images|videos|both`, `mode: fit_box|shorter_side|longer_side|percent`, size presets (360p … 4K, from app.py) + custom, `never_upscale=True` 🔒, `jpeg_quality=85`, `video_crf` presets (18/23/28), `output: new_folder|overwrite|suffix`, `output_folder_name="resized"`, `convert_raw_to_jpeg`, `keep_metadata=True`.
- Keep app.py's rule that a file is processed only when **both** dimensions exceed the target box (`fit_box` mode). Keep metadata-8's shorter-side rule (`shorter_side` mode). Remove CR3 originals only after the JPEG is verified and its metadata copied 🔒.
- The preview shows size before and after (an estimate for videos) and the total space saved.

**3.6 `organize_by_date`** (metadata-8)
- Options: `sources`, `dest_root`, `folder_template="{year}/{year} - {month:02d}"`, `recursive`, `date_priority: [exif, video_tag, exiftool, modified]`, `unknown_folder="Unknown Date"`, `min_plausible_year=1990`, `cloud_only: skip|ask|download`, `download_wait_seconds=30`, `keep_pinned`, `workers`, `also_resize` (reuses the media_resize options), `skip_processed`, `errors_only`.
- Replace the Excel master log with a SQLite **media catalog** (`catalog.sqlite`: path, capture date, source of date, camera, GPS, size, dims, sync status, last action). Add "Export catalog to Excel/CSV" so the owner can keep the `Photo_Metadata_Master.xlsx` workflow if they want. Offer a one-time import of the existing xlsx.
- Checkpoint/resume becomes the journal plus the catalog, so no separate checkpoint file is needed.
- Keep rules 1–4 from the script's docstring exactly 🔒.

### Phase 4: CLI
- `various-tools list`, `various-tools <tool> scan [opts] [--preset P] [--json]`, `various-tools <tool> apply [--plan plan.json | same opts] [--yes]`, `various-tools history`, `various-tools undo <run-id>`, `various-tools doctor` (dependency check), `various-tools presets list|save|delete`.
- Flags are generated from each options model. Hand-written flags are allowed only for subcommands like `duplicates explain`.
- A plan can be saved as JSON and applied later. This makes it easy to review a plan in a text editor or hand it to an AI.

**Done when:** every legacy script's documented usage has an equivalent CLI command, and that
mapping table is in the README.

### Phase 5: GUI (PySide6)
- **Main window:** a sidebar of tools grouped by category (from the registry), with a search box.
- **Tool page:** a form generated from the schema (Basic and Advanced sections, a preset
  dropdown, Save preset), then **Scan** → a plan table (sortable, filterable, checkable,
  grouped, with a reason column and totals), then **Apply**. There's a progress bar and a
  **Cancel** button, and a log panel you can collapse.
- **Duplicates compare view** and **rename live preview**, as described in Phase 3.
- **History page:** past runs from the journal, with details and an **Undo** button.
- **Settings page:** default folders, Recycle Bin vs permanent delete, workers, Ollama URL,
  theme (light, dark or follow Windows), protected paths.
- **External tools panel** (in Settings, see §3.5): status, path, version and source for
  ffmpeg, ffprobe, ExifTool and Ollama, with Browse…, Test, Auto-detect, Reset and a copyable
  `winget install` command. The app also offers "Locate…" inline whenever a feature needs a
  missing tool.
- Drag and drop folders onto any folder field. The app remembers the last used folders for
  each tool.
- Add a shell integration option (per-user registry, no admin): "Open with Various Tools → Find
  empty folders / Find duplicates" in the folder right-click menu. Off by default, toggled in
  Settings.
- All work runs in worker threads, and the UI only updates through signals.

**Done when:** each tool can do a full scan → apply → undo in the GUI, and a pytest-qt smoke
test exists for each tool page.

### Phase 6: Reporting
- Export any plan or run to CSV, XLSX or a self-contained HTML report (with thumbnails for
  duplicates). This replaces `find_duplicates_review_list_<date>.txt` and the per-tool CSV
  logs, and those formats remain available as export options.

### Phase 7: New tools (each one is just a new `tools/` package)
In priority order. Each one gets a plan preview, journal entries and undo like the others.
1. **Disk usage / large files**: a treemap or sunburst plus a top-N largest files and folders
   list, with filters by type and age. Read-only, but items can be sent to Trash from the view.
2. **Sort by type**: rules like "*.pdf → Documents/PDF", edited in a table and saved as a preset.
3. **Media converter**: HEIC→JPG, WebP→JPG/PNG, MOV→MP4, reusing `media/` (shares code with
   media_resize).
4. **Privacy / metadata stripper**: remove GPS or all EXIF before sharing, with a preview of
   what will be removed.
5. **Old & temp file cleaner**: `%TEMP%`, browser caches (read-only listing), files older than
   N days matching patterns.
6. **Checksum & verify**: create or verify SHA-256 manifests for a folder, e.g. to confirm a
   backup copy matches.
7. **Corrupt media finder**: lists images and videos that fail to open or decode (the
   duplicates and organize scanners already collect these errors, so surface them here).
8. *(Later)* **Scheduled / watch-folder runs**: run a saved preset on a schedule through
   Windows Task Scheduler (`various-tools <tool> apply --preset P --yes`). Every run is still journaled.
   *(Built in 0.2.0, removed in 0.3.0 by the owner's decision together with presets.)*

### Phase 8: Packaging, release, cleanup
1. PyInstaller one-dir spec in `packaging/`, without UPX. Bundle the icon and version info.
   Don't bundle ffmpeg; detect it at runtime and show how to install it.
2. Installer: per-user (`%LOCALAPPDATA%\Programs\Various Tools`), Start menu shortcut, optional
   desktop shortcut, optional context menu, an uninstaller that offers to keep settings and
   history. Remove the startup option.
3. `release.yml`: on a `v*` tag → build → smoke-launch the exe → create a GitHub Release with
   the installer and a portable zip. Read the version from `pyproject.toml` only.
4. Rename the GitHub repo once the name is confirmed (GitHub redirects the old URL) and update
   the README.
5. Delete `legacy/` once every tool has passed its parity test. Tell the owner that the
   OneDrive `Scripts` copies can then be archived.

---

## 5. Definition of done (whole project)

- [ ] One install gives both the GUI and the CLI. No hardcoded personal paths anywhere in `src/`.
- [ ] Every legacy script's behavior can be reproduced through a tool and a preset, with parity tests.
- [ ] Every destructive action is previewed first, journaled and can be undone. Deletes go to the Recycle Bin by default.
- [ ] Cloud-only OneDrive files are never downloaded without the user's consent.
- [ ] Every external program (ffmpeg, ffprobe, ExifTool, Ollama) is found automatically, or the user can point to it from the GUI or CLI. A missing one disables only the features that need it.
- [ ] Adding a tool needs no GUI or CLI code changes, proven by building one Phase 7 tool that way.
- [ ] CI runs lint, types and tests on Windows. Tagged releases publish an installer automatically.
- [ ] README has screenshots, a tool list, legacy→new mapping, and dependency setup (`winget install Gyan.FFmpeg`, `winget install OliverBetz.ExifTool`, optional Ollama).

## 6. Notes for executing agents

- The legacy scripts contain thresholds that were **tuned by hand on real data** (the comments
  say so, e.g. "was 4", "~17 s per pair on this machine"). Treat them as requirements, not as
  code to tidy up.
- Never run a tool against real user folders during development. Use generated fixture trees
  under a temp directory.
- Keep `core/` free of UI code. Keep tools free of `print`, `input` and Qt.
- If a decision isn't covered here, choose the safer option and note it in the PR description.

---

## 7. After the plan: releases 0.4 to 0.8, and what comes next

Phases 0 to 8 are done. Everything since then came from the owner's requests and is recorded in
`CHANGELOG.md` and the design notes in `docs/superpowers/specs/`. The handoff for the next
session is `docs/HANDOFF.md`.

| Version | What it added | Design |
|---|---|---|
| 0.4.0 | Step-by-step tool page, plain words, one "Show all settings" switch | `2026-11-19-simpler-tool-page-design.md` |
| 0.5.0 | Media library, Timeline and Map pages; Empty Files and Fix File Extensions tools | `2026-11-19-media-library-design.md` |
| 0.6.0 | One working folder, menu bar, Home page, results-list picking, apply in batches, photo browser, Change date tool | `2026-11-20-workspace-and-photos-design.md` |
| 0.7.0 | Wait for OneDrive to upload each batch; every 0.5/0.6 leftover (nested library folders, RAW/video places, map wrap, sidecars, …) | `2026-11-21-left-for-later-design.md` |
| 0.8.0 | Saved recipes, duplicates in the photo library, offline place names (GeoNames) | `2026-11-21-wave-2-design.md` |

Additions to the architecture since the plan was written:

- **Working folder** (GUI state, `gui/state.py`): a tool's workspace field is its first folder
  field, or its `workspace_field` attribute (`None` opts out: Temp Cleaner, Change date).
- **Pacing** (`core/context.py` `Pace`, `core/plan.py` `apply_plan`): batches, pauses, Pause /
  Skip the wait, a journal heartbeat while waiting, and the OneDrive upload wait
  (`core/onedrive.upload_pending`, Cloud Files API, never recalls a file). Still one journal
  run per apply. `apply_plan` reads `ctx.pace` before each action, so the GUI's "Change pace..."
  can swap the (frozen) `Pace` of a running apply; it counts from the next batch.
- **Continue after Stop** (`gui/continue_run.py`, `ToolPage._prepare_continue`): after a
  cancelled run, or one with failures, the page keeps its list without the done rows (matched by
  kind and source) and Apply reads "Continue (N left)"; Continue applies the rest as a new
  journaled run. A new scan or changed options drop it. Fix File Extensions and Organize by Date
  fall back to "scan again" because their rows depend on each other while applying.
- **Hooks in `apply_plan`**: `before_action` (a tool can retarget or skip an action from what
  was journaled before it, used by Fix File Extensions sidecars) and `PlannedAction.extra`
  (facts to re-check at apply time, used by Change date).
- **Recipes** (`core/recipe.py`): steps never store 🔒 locked, `replaces_originals` or
  permanent-delete values; a hand-edited one is blocked as "needs attention". The recipe marker
  lives only in the journal's copy of the options.
- **Photo library** (`core/media/library.py`, schema v2, `gui/library_model.py`): read in a
  worker, shared by Timeline and Map; `core/media/places.py` (offline, `data/places.npz`);
  `core/media/library_duplicates.py` (fed by Duplicate Finder scans, changes no file).

### Phase 9 candidates (not started; ask the owner before starting a large one)

1. **Recipe steps that use the previous step's output folder** (then "Prepare photos to share"
   can be Resize, then Strip metadata, again).
2. **Timeline at 200,000 items**: the GUI-thread handover is about 0.25 s; a grid row layout
   without one object per row would bring it near 0.1 s.
3. **Face grouping** and **trips** (automatic events from date and place gaps) on the
   Timeline. Faces need a model choice (local only, privacy) and an owner decision.
4. **Release assets**: let `release.yml` attach the installer (it already rebuilds, tests and
   uploads on a `v*` tag) and stop uploading local builds by hand; see `docs/HANDOFF.md`.
