# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Version 0.6.0: one working folder, apply in batches, a real photo browser (2026-11-20)

Owner's request: applying 2,113 resized OneDrive files (62 GB) at once could swamp OneDrive; no
quick way to tick only 100 of 2,000, no Ctrl+A or Properties; every tool asks for the same
folder; no Settings or Exit button; too much on screen; Timeline and Map too basic; no way to
fix a wrong date. The owner left every decision to the builder. Design:
`docs/superpowers/specs/2026-11-20-workspace-and-photos-design.md` (with a UX audit).

- **Menu bar and toolbar:** *File* (Choose working folder… Ctrl+O, Recent folders, Settings…
  Ctrl+,, Exit Ctrl+Q), *View* (Show all settings, Show log Ctrl+L, Find a tool Ctrl+K, Home),
  *Help* (Keyboard shortcuts, Open settings folder, About). The toolbar has the working
  folder, History and Settings.
- **One working folder for every tool** (`gui_state.json`, last 8 remembered). A tool page says
  "Looks in: the working folder (…)"; *Use other folders for this tool* shows its own folder
  field again (remembered per tool). Temp Cleaner keeps its own folders (`workspace_field =
  None`). When the working folder changes, a page that already shows a list keeps it and says
  "Look again"; an old list is never applied to a new folder.
- **Home page:** the working folder, tool cards by category, the last 5 runs.
- **Less on screen:** after a scan the settings fold into a one-line summary (*Change
  settings*), and thousands of "skipped: …" notes fold into counts with *Show details*.
- **Results list:** *Tick all*, *Untick all*, *Tick only the first [N]* and *Show: All / Ticked /
  Not ticked* are always visible; "2,113 found · 150 ticked (4.4 GB)". Ctrl+A highlights all
  rows, Space ticks or unticks the highlighted rows, Ctrl+F jumps to the filter. Right-click:
  Tick, Untick, Tick only these, Open, Show in File Explorer, **Properties**, Copy path(s).
- **Apply in batches:** the Apply window offers *All at once* or *In batches of N files, then
  wait X* (presets: 50 then 5 minutes, 100 then 1 minute, 1 then 2 seconds), with the extra
  waiting time shown. Batches are suggested when the job touches OneDrive and is over 1 GB or
  500 files. While it runs: Pause, Resume, Skip the wait, Stop. It is still one run in History,
  undone in one go. CLI: `<tool> apply --batch-size 50 --pause 5m`.
- **Photos & videos:** Timeline and Map share one library and one filter bar (search,
  photos/videos, from/to month, folder); library folders live in a *Library folders…* dialog.
  Timeline is one scrolling grid by month with day headings, a year/month jump list, a
  thumbnail size slider (Ctrl+wheel), a preview pane (I), and a full-window viewer (Enter,
  arrows, Esc). Right-click: Open, Edit, Show in File Explorer, Properties, Copy path(s),
  Show on map, **Change date…**. Map: the clicked group's photos in a side panel, hover
  details, Fit all, double-click zoom, and a banner offering the detailed online map (still off
  by default).
- **New tool Change date** (`set_date`, Organize; also from the Timeline): set an exact date,
  only the month and year (day and time kept), or shift by days/hours/minutes. It shows old →
  new first; with ExifTool it writes the taken date into the file, without it only the
  modified date. The original goes to the Recycle Bin, so History can undo it. Cloud-only
  files are skipped.
- **Review fixes:**
  - An apply that is paused or waiting between batches touches its journal run (a heartbeat,
    at least every 30 seconds), and History and the tool pages refuse to undo a run that a
    live worker in this program is applying.
  - Change date applies the dates the preview planned (kept in the action) and never works
    them out again, so applying one plan twice cannot shift a date twice; a file whose date
    changed since the preview is left alone. The copy now takes the original's place before
    the old content is recycled (the path is never empty); the journal row stays under the
    original path, so History undo still restores it. The dialog clears its plan on every end
    path and survives a failed library refresh. Skipped notes read "skipped: reason: path".
    The tool no longer defaults to the whole working folder.
  - A scan that started under another working folder shows the "working folder changed"
    banner. A one-folder tool (Project Export) says it uses the first working folder. A folder
    set by the shell menu or `--folder` is not remembered as "Use other folders".
  - Ticking thousands of Empty Folders rows is one pass (it froze the window); the results list
    caches its "shown" count and keeps the model-order shortcut with a filter and no sort.
  - Skip the wait is offered only during "Next batch in..." waits, and an old skip no longer
    cuts a later wait short. The pace choice is kept even when remembering options is off; a
    remembered "in batches" choice is preselected only for OneDrive plans or after choosing it
    in this session.
  - Photo grid: right-click does not stat every selected file, paths are looked up through an
    index, and a resize that keeps the column count does not lay the grid out again. Show on
    map before the Map was ever shown waits for the map's size, and says when it cleared a
    filter.
- **Known, left for later:** Change date does not read a video's new date back after writing
  it (photos are checked).
- Version 0.6.0.

### Version 0.5.0: photo timeline, photo map and two new tools (2026-11-19)

Owner's request: see all photos and videos on a timeline and on a world map, kept up to date
without rebuilding, each opening in its default app; add more tools. Design:
`docs/superpowers/specs/2026-11-19-media-library-design.md`. The owner said "continue", so the
calls below are the builder's, the safer way where nothing was decided.

- **Photos & videos** is a new sidebar section with two pages that never change files:
  - **Timeline:** pick your photo folders (`Settings.library_folders`), press *Update the
    timeline*, then browse *Year > Month* with thumbnails. Double-click opens the default app;
    right-click offers *Open*, *Edit* (the Windows "edit" app, photos only), *Show in File
    Explorer* and *Copy path*.
  - **Map:** the photos and videos with a GPS place, grouped in circles that split as you zoom
    in; click a circle to see its photos below.
- **Media library** (`core/media/library.py`, `media_library.sqlite` in the cache folder): one
  row per file with date, place and size. *Update* reads only new or changed files (size,
  modified time and OneDrive status) and drops rows for files that are gone; rows of other
  folders are kept, and a missing folder (an unplugged drive) keeps its rows. Cancel keeps what
  was read. Dates: camera EXIF, video metadata through ffprobe, ExifTool for RAW, else the
  modified date.
- **OneDrive cloud-only files are never opened** (rule 3): they are listed with their modified
  date, a cloud picture instead of a thumbnail and no place, and read on the first update after
  they are on the device.
- **The map works offline.** It draws a world outline built into the app (Natural Earth 1:110m
  land, public domain, `gui/data/world_land.json`, 66 KB). **Detailed online map** (setting
  `map_online_tiles`, off by default) adds OpenStreetMap pictures, cached in
  `<cache>/map_tiles`, with the attribution shown; it is off by default because it tells that
  service which area you look at. The app names itself `VariousTools/<version>` and nothing
  else.
- **New tool Empty Files** (`empty_files`, Clean up): 0-byte files to the Recycle Bin. Files
  that are empty on purpose (`__init__.py`, `.gitkeep`, `.keep`, `py.typed`, `.nojekyll`) are
  kept; never-touch, minimum age and hidden/system options. Nothing is opened.
- **New tool Fix File Extensions** (`wrong_extension`, Organize): reads the first 64 bytes to
  find files whose content does not match the extension (a PNG named `.jpg`) and renames them.
  Equivalent names are left alone (`.jpeg`/`.jpg`, `.mp4`/`.m4v`/`.mov`, `.mkv`/`.webm`, Office
  files as zip); unknown content and files without an extension are never touched; cloud-only
  files are skipped and reported.
- Not done yet: panning across the 180° line on the map, a speed test with 50,000 points, and
  screenshots of the two new pages.
- Version 0.5.0.
- **Review fixes:**
  - Fix File Extensions: an AVIF with major brand `mif1` is told from a HEIC by its compatible brands, so a correct `.avif` is no longer renamed to `.heic`.
  - Fix File Extensions: an `ID3` header no longer means `.mp3` (FLAC and WAV can start with an ID3 tag), and a UTF-16 byte order mark is not taken for an MP3 frame.
  - Media library: a photo that goes cloud-only after being read keeps its camera date, GPS and size, so it stays on the Timeline and the Map.
  - Map: a failed tile is retried after 5 minutes at the earliest, and after 5 failures in a row no more tiles are requested until the tile source is set again.
  - Empty Files: a file written to between Scan and Apply is skipped ("changed since the scan: no longer empty") instead of being trashed.
- **Known, left for later (review minors):** a photo folder inside another chosen folder is read
  and counted twice; a subfolder that cannot be listed (access denied) loses its rows until the
  next update; RAW photos and videos get no GPS place or pixel size; the whole library is
  loaded on the GUI thread (slow past about 100,000 items); the map tile cache has no size limit
  and is not tied to the tile address; Fix File Extensions leaves sidecar files (`.xmp`,
  `.aae`) with the old name, and MP1/MP2 audio is no longer recognised (left alone, never
  renamed).

### Version 0.4.0: a step-by-step tool page (2026-11-19)

Owner's request: the app should be easy for a non-technical 15-year-old, without losing any
feature. Design: `docs/superpowers/specs/2026-11-19-simpler-tool-page-design.md`.

- **Three steps on every tool page:** *1 Choose where to look* (the options), *2* a button that
  says what it looks for (each tool's `scan_label`, for example "Look for empty folders"), and
  *3 Check the list, then confirm*, shown after the first scan, when the list also gets most of
  the page. A plain sentence says what was found ("Found 11 items to remove (2.0 KB). Only
  ticked rows are changed, and you confirm first."). The confirm button says what it will do:
  "Move 12 to the Recycle Bin…", "Rename 9 items…", "Apply 5 changes…" for mixed kinds.
- **One "Show all settings" switch,** app-wide, remembered in `gui_state.json` (even with
  *Remember last folders and options* off), off by default. On: every page's extra options are
  open, and the kind filter, the view switch, Select all / none and the log panel are shown.
  Nothing was removed. The form's toggle is now called *More settings for this tool*.
- **Plain words in the list:** *What happens / File / Goes to / Why / Size*, and "Move to
  Recycle Bin", "Move", "Rename", "Delete forever", "Create file", "Convert" instead of the raw
  kind names (`gui/wording.py`). CLI output and CSV/Excel/HTML exports keep the raw names.
- **Duplicates show which copy is kept:** the group row reads "Keeps <full path>" and each
  duplicate has a *Kept copy* column, so two kept files with the same name can be told apart.
- **Right-click any row:** *Show in File Explorer* (selects the item), *Open* (default app; a
  cloud-only OneDrive file asks before it is downloaded) and *Copy path*. A duplicate offers
  *Show this copy* and *Show the kept copy*; a move or rename offers *Show where it is now* and
  *Show where it will go* (the nearest folder that exists). Entries for a path that is gone are
  disabled. `explorer.exe` is named in `core/deps.SYSTEM_TOOLS`; `SYSTEM_TOOLS` values are now
  relative to `%SystemRoot%`.
- **Open source:** the legacy copy of `python-find_duplicates.py` named the owner's Pictures
  folder; it now uses the home folder (owner-approved change to the read-only legacy folder).
  `tests/test_no_personal_paths.py` fails when any tracked file holds a real
  `C:\Users\<account>` path. The old value is still in the git history; rewriting history
  was not done.
- Builder's calls: step 1 holds the folders and the basic options together (the form stays one
  piece); extras and *More* sit in the step-2 row so they work without a scan; Empty Folder
  Deleter counts "items to remove", because its plan also removes junk files.
- Screenshots in `docs/images/` regenerated. Version 0.4.0.

### Version 0.3.0: a simpler tool page (2026-11-17)

Owner's request: simplify the GUI without losing options or control.
- **One action row:** Scan (it turns into Cancel while a job runs), Apply N… (says how many
  checked actions it will carry out), Compare group (duplicates), Undo this run (when it can),
  the tool's extra buttons, and a *More* menu with Export… and Reset options to defaults.
- Select all / Select none moved into the plan table's toolbar, next to the filter.
- The progress bar shows only while a job runs. The scan-notes box shows only when a scan left
  notes, and is only as tall as they need (up to 5 lines). A folder list is as tall as the folders
  in it (2 to 6 rows).
- **Remembered options replace presets.** A page now remembers the options of its last scan, not
  only the folders (setting *Remember last folders and options*, on by default). For safety,
  🔒 locked options, options marked `replaces_originals` and a permanent-delete choice are never
  remembered: they always start from their defaults. Folders saved by 0.2 are still read.
- Every option stays available: basic options are shown, advanced ones stay under *Advanced
  options*. Screenshots in `docs/images/` are regenerated.

### Version 0.3.0: presets and scheduled runs removed (2026-11-17)

The owner decided to remove two features entirely.

- **Saved option presets are gone.** Removed: `core/presets.py` (`PresetStore`), the CLI
  `presets list|save|delete` commands and the `--preset NAME` option on `TOOL scan` and
  `TOOL apply`, `Settings.presets_path()` and `PRESETS_FILE`, and the GUI preset row (preset
  dropdown, *Save preset...*, *Delete preset*) on every tool page. `--preset` stays a reserved
  flag name, so an old `--preset NAME` is a usage error (exit 2) instead of silently meaning a
  tool option called `preset`; rename's built-in presets are still `--opt-preset`.
- **Scheduled runs are gone.** Removed: `core/schedule.py`, the CLI `schedule add|list|remove|run`
  commands, the hidden `--scheduled` flag on `TOOL apply` and its run-time refusals,
  `docs/scheduled-runs.md`, and `schtasks` from `core/deps.SYSTEM_TOOLS`. The
  `replaces_originals` option marker stays: the GUI now uses it to decide which options it never
  remembers between sessions.
- Not affected: the rename tool's built-in presets (snapchat, whatsapp, screenshot),
  media_resize's size presets and the x264/x265 speed preset (`video_preset`) of media_resize and
  media_convert. These are tool options, not saved presets.
- **Existing user files.** A `presets.json` left in the data folder by an earlier version is
  simply ignored; nothing deletes it. A task that `schedule add` created in Task Scheduler (folder
  *Various Tools*) is not removed either: when it runs, `apply --preset ... --scheduled` now stops
  with a usage error before scanning, so it changes nothing. Delete such tasks in Task Scheduler.
- The uninstaller's "keep your data" question no longer mentions presets. The GUI's unused
  `ask_text` dialog hook (only *Save preset...* used it) is removed.
- Version 0.3.0.

### Media converter: iPhone spatial audio and pre-cropped HEIC (2026-11-17, version 0.2.1)

- **MOV with Apple spatial audio.** iPhone 16 and newer videos carry a second sound track in
  Apple Positional Audio (`apple_apac`). ffmpeg has no decoder for it and the MP4 muxer cannot copy
  it, so every such video failed at apply with "no decoder found for: apple_apac". They are now
  skipped at scan with that reason, instead of being planned and failing. They are never converted
  with a sound track missing (owner's decision).
- **HEIC refused by libheif for its size.** Some HEIC files crop the picture in the HEVC stream
  and again in the container; libheif 1.23 then refuses them ("Decoded image does not have the
  size signaled in the file"). `media_convert` now decodes such a file with ffmpeg
  (`core/media/ffmpeg.decode_still`, `-apply_cropping codec`) and uses the result only when it has
  exactly the header's size. Without ffmpeg the error says that installing it would help.

### Whole-plan review and fixes (2026-11-15)

Plan reference: `docs/MODERNIZATION_PLAN.md` §4 Phases 7.8 and 8.5 and §5. User-facing text is in
`README.md`, `docs/tools/` and `docs/scheduled-runs.md`.

#### Final safety review (same day)
An independent review of the whole change set found these; all are fixed, with tests.
- **Recycle only where a Recycle Bin exists.** On a network or mapped drive, a removable drive
  without a bin, a bin set to delete immediately, or an item larger than the bin's limit, Windows
  deletes for good without asking. `core/recycle.py: can_recycle()` now runs before every recycle
  (plan rows, `trash_extra`, undo). When it refuses, nothing is removed and the row says why.
- A recycle that reported an error after the item was already gone is journaled as done, so undo
  can still restore it. A Recycle Bin restore also matches the item's modified time.
- `root_refusal` refuses a root below a junction and compares the real path with the protected
  list, including administrative shares.
- Scheduled runs use a hidden `--scheduled` marker instead of `--yes`. `schedule add` and each
  run refuse presets that delete permanently, change a 🔒 value, or set an option marked
  `replaces_originals` (overwrite, in-place strip, recycle originals, remove RAW originals,
  also-resize, cloud-only download). Tasks created before this change must be removed and added
  again.
- With a filter on, ticking a folder or group in the plan changes only the rows shown.
- `temp_cleaner` re-checks its root refusals at apply. `media_resize` checks a video's duration
  as well as its size before the output replaces anything.
- A run that is still applying cannot be undone until it finishes or goes stale.
- The Apply confirmation says when ticked rows will download cloud-only files;
  `various-tools download` refuses folders.
- `packaging/build.py --installer` compiles the installer. Version 0.2.0.

#### Added
- Undo restores Recycle Bin items while they are still there (`core/recycle.py`), sends an
  unchanged output file (write or convert) to the Recycle Bin, and undoes rows a crash left
  pending. The journal runs in WAL mode.
- `Plan.cloud_only` and a download action: "Download these N files..." in the desktop app and
  `various-tools download [PATHS] [--plan FILE] [--wait SECONDS] [--yes]`.
- `organize_by_date`: `cloud_only` (skip, ask, download) with `download_wait_seconds` and
  `keep_pinned`, a download at Apply; `also_resize` with `resize_mode`, `resize_px` and
  `resize_jpeg_quality`; catalog export (`.xlsx` or `.csv`) and import of the old master workbook
  (`catalog-export PATH`, `catalog-import PATH`, and two buttons in the app), through the generic
  `ToolExtra` mechanism in `tools/base.py`.
- `media_resize`: `output = overwrite` (with `allow_overwrite`) and `remove_raw_originals`, both
  sending originals to the Recycle Bin and journaled.
- Case-only renames.
- Live Ollama model list (`core/choices.py`).
- `disk_usage`: `tree_depth` and `tree_min_share_pct`, size map notes and a `tree-size` note, a
  "Size map" tab and "Tick files in this folder" in the app.
- Folder-tree plan view (the default for `empty_folders`) and a table editor for `sort_by_type`
  rules.
- `project_export`: `copy_to_clipboard`.
- Scheduled runs (Phase 7.8): `various-tools schedule add|list|remove|run`
  (`docs/scheduled-runs.md`).
- CLI: `--allow-locked` and `--force` (for `--export`); the one-off `--exiftool`, `--ffmpeg`,
  `--ffprobe` and `--ollama-url` are accepted on tool commands and `doctor`.
- README screenshots in `docs/images/`, generated on demo data by
  `packaging/make_screenshots.py`.

#### Changed
- Phase 8.5 is done: `legacy/` is deleted, and the scripts the parity tests run moved to
  `tests/fixtures/legacy_reference/`.
- `AGENTS.md` and `CLAUDE.md` are brought up to date.
- CI runs on `main` and on pull requests.
- `README.md`: screenshots, the new commands and options, the old-script table rows that said a
  feature had no equivalent (cloud-only download, `--resize`, the Excel master log, CR3 removal),
  the Undo note, and the stale legacy and acknowledgments text.

#### Fixed (safety)
- Apply engine: moves never overwrite. A cross-volume move checks the file is on the device first,
  copies to a `.partial` file, and refuses folders. A plan with no roots is refused. A source below
  a junction inside the root is refused. Plan roots are checked again at apply. WRITE and CONVERT
  are guarded by the engine too.
- Undo runs the destination and protected-path guards and never overwrites.
- Link detection uses the reparse tag, so OneDrive placeholders are not treated as junctions, and a
  non-empty folder is never trashed whole.
- Protected paths cannot be bypassed with a `\\?\` prefix or an 8.3 short name.
- Program lookup never uses the current directory.
- Originals recycled by `media_convert` (`trash_originals`) and `metadata_strip` (in place) are
  their own journal rows.
- `temp_cleaner` judges age on the newer of the modified and created times, checks again at apply,
  merges short and long forms of the same root, and refuses app-data roots.
- `corrupt_media` no longer calls a file corrupt after a memory or system I/O error.
- `empty_folders` no longer recycles a junk file whose folder is unticked.
- The CLI asks before a 🔒 value changes, and `--allow-locked` is the explicit way around the
  question. `config set` validates a program path.
- Closing the window during a run asks and waits. The plan cannot be edited while it is applying.
- The release workflow now fails on lint and type errors.
- No test can reach the real Recycle Bin.

#### Fixed (correctness)
- `organize_by_date` crashed on apply, undo, apply again (catalog primary key).
- `project_export`: comment stripping with form feeds, and re-including an earlier export.
- `duplicates`: zero-size media. `duplicates explain` on a cloud-only file.
- `checksum`: long paths.
- `media_convert` verified a video after giving it its final name.
- `sort_by_type`: clash settings at apply, and commas in rules.
- History with very large runs.
- The Explorer menu on a drive root.
- `--export` overwrote an existing file silently.

#### Decisions
- The previously deferred options (overwrite, cloud-only download, `also_resize`, Excel export and
  import) are implemented on the owner's instruction. Each is off by default and advanced.
- `cloud_only` is 🔒 with `skip` as the default, and nothing is downloaded during a scan.
- `also_resize` resizes before the move, because the recycle helper refuses files outside the plan
  roots.
- Downloads are not journaled, because no path changes.
- Scheduled runs refuse presets that delete permanently or change a 🔒 value.
- The 🔒 thresholds and operators were compared with the legacy scripts value by value and are
  unchanged.
- The settings file still uses plain pydantic, not pydantic-settings (the earlier decision is
  unchanged).

#### Tests
- Tests for each fix above. The test suite no longer reaches the real Recycle Bin.

#### Follow-ups for the owner (not done here)
- Rename the GitHub repository, then update the URLs (plan Phase 8.4).
- Code signing.
- There is no real CR3 sample, so the RAW path is tested with fakes.
- `media_convert/images.py` and `media_resize/images.py` still share near-duplicate helpers.
- `LIVE_PREVIEW_TOOLS` and `COMPARE_TOOLS` in the GUI are still keyed on two tool ids (the plan
  permits these two).

### Phase 8: Packaging and release (2026-11-13)

Plan reference: `docs/MODERNIZATION_PLAN.md` §4 Phase 8, items 1 to 3. Build steps are in
`packaging/README.md`; install and run instructions in `README.md` ("Install").

#### Added
- `packaging/various_tools.spec`: PyInstaller one-dir build, no UPX, into `dist/Various Tools/`
  with two programs sharing one `_internal` folder: `Various Tools.exe` (the desktop app, no
  console) and `various-tools.exe` (the command line). Both carry the icon and Windows version
  info written by `packaging/make_version_info.py` from the `pyproject.toml` version.
  ffmpeg, ffprobe and ExifTool are not bundled; they are found at run time as before.
- `packaging/build.py` (build plus `dist/Various-Tools-<version>-portable.zip`) and
  `packaging/smoke_test.py`, which checks the frozen build on temp folders with a temp settings
  file: the desktop app opens its window and stays up (plain and with `--tool --folder`), `--help`,
  `version`, `list` shows all 13 tools, `doctor`, `empty_folders` scan, apply (Recycle Bin) and
  undo, `rename` scan, apply and undo (names restored), and a `duplicates` scan with HEIC input and
  an Excel export (Pillow, pillow-heif, imagehash, openpyxl in the frozen app).
- `packaging/installer.iss` (Inno Setup 6): per-user install to
  `%LOCALAPPDATA%\Programs\Various Tools` with no admin prompt, Start menu shortcut, optional
  desktop shortcut, optional Explorer folder menu (the same keys and commands as
  `gui/shell.py`), no startup option. The uninstaller always removes the Explorer menu and asks
  whether to keep settings, presets, History and caches (`%LOCALAPPDATA%\Various Tools`); a silent
  uninstall keeps them. The version is passed in (`/DAppVersion=`); the script stops without it.
- `.github/workflows/release.yml`: on a `v*` tag, checks the tag against the `pyproject.toml`
  version, runs lint, types and tests, builds, smoke-tests the frozen build, installs Inno Setup
  with Chocolatey, compiles the installer and publishes a GitHub Release with the installer and
  the portable zip.
- `pyinstaller` in the dev dependency group.

#### Changed
- `README.md`: install (installer, portable zip, from source), external programs with their
  `winget install` commands, and the desktop app run instructions. The old Tkinter app section is
  now a short note. Fixed the Undo note: items in the Recycle Bin are restored from there by hand,
  Undo reports them as not reversible.
- The old app's `empty_folder_deleter.spec` and `installer.nsi` moved from the repository root to
  `legacy/` (nothing referenced them). `empty_folder_deleter.py` stays at the root for the
  empty_folders parity tests.

#### Decisions
- Two executables in one folder rather than one program with a mode switch, so the command line
  gets a real console and the desktop app never flashes one.
- Inno Setup rather than the old NSIS script: the plan allows either, and Inno's per-user mode,
  tasks and uninstall hooks cover Phase 8.2 directly. It cannot be compiled on the development
  machine (not installed, and nothing is installed system-wide); CI compiles it.
- The installer's context-menu task writes the same registry values as the app's setting, but
  does not change the setting, so Settings still shows it off. Uninstalling always removes the
  menu; inside the app, switching the setting on and then off removes it too.
- The build is about 290 MB unpacked (134 MB zipped): PySide6, SciPy (imagehash's pHash), x265
  (pillow-heif) and NumPy are most of it. Unused Qt modules (WebEngine, Pdf, Quick, Qml, 3D) and
  Tkinter are excluded.
- send2trash runs its ctypes backend in the frozen app (pywin32 is not a dependency), the same
  as from source.

#### Follow-ups for the owner (not done here)
- Rename the GitHub repository once the name is confirmed, then update the URLs in `README.md`
  and `packaging/installer.iss` (plan Phase 8.4).
- Delete `legacy/` once every tool has passed its parity test, then archive the OneDrive
  `Scripts` copies (plan Phase 8.5). The parity tests still read `legacy/` and the root
  `empty_folder_deleter.py`. **Done:** `legacy/` is deleted and the parity references moved to
  `tests/fixtures/legacy_reference/` (see "Whole-plan review and fixes"). Archiving the OneDrive
  `Scripts` copies is still the owner's.
- Code signing: the exes and installer are unsigned, so SmartScreen warns on first run.

### Phase 5: Desktop GUI and Phase 6: Reporting (2026-11-13)

Plan reference: `docs/MODERNIZATION_PLAN.md` §4 Phases 5 and 6. Usage is in `README.md`
("Desktop app" and "Reports").

#### Added
- `various-tools-gui` (also `python -m various_tools.gui`), PySide6. Main window with a sidebar of
  tools by category and a search box, History, Settings and a collapsible log panel fed from any
  thread. Window title "Various Tools <version>"; the icon is drawn in code (`gui/icon.py`) and
  `packaging/make_icon.py` writes `packaging/icon.ico`.
- Tool page for every registered tool, with no per-tool form code: `gui/widgets/schema_form.py`
  renders any options model (and the Settings model) from the `core/schema.py` hints: Basic and
  Advanced sections by group, dropdowns, bounded spinners, "use app setting" for `X | None`
  fields, `depends_on` visibility, reorderable choice lists, key/value tables, inline validation
  errors, and a confirmation before a 🔒 value changes. Presets (load, save, delete), folders
  dropped from Explorer, last folders remembered per tool.
- Scan in a worker thread, then a plan table (`gui/widgets/plan_table.py`): sortable, filterable
  by text and kind, checkable, grouped by `group_id`, reason and size columns, totals. Unchecking
  an empty folder goes through `set_folder_selected`. Apply, Cancel, progress bar, Export, and
  Undo of the run just applied.
- Duplicates compare view (`gui/widgets/compare_view.py`): a group side by side (thumbnail,
  resolution, size, date, reason), "Keep this one" (`gui/plan_edit.py: change_keeper`) and
  "Explain match". Rename shows Folder / Old name / New name and re-scans as you type (live
  preview, read-only).
- History page: runs from the journal, each run's actions, Undo and Export.
- Settings page: the Settings model through the same generic form, plus the External programs
  panel (status, path, version, source, Browse, Test, Auto-detect, Reset to automatic, copyable
  `winget install` command; Ollama is tested at its address). A tool page offers "Locate…" when
  its scan notes say a program is missing.
- New settings (group "Desktop app"): `theme` (system, light, dark), `remember_last_folders`
  (on), `shell_integration` (off). Theme uses Fusion with a light or dark palette; system follows
  Windows.
- Per-user Explorer menu (`gui/shell.py`, HKCU `Software\Classes\Directory\shell` and
  `Directory\Background\shell`, no admin): "Open with Various Tools > Find empty folders / Find
  duplicates", running `--tool ID --folder PATH`. Off by default; written only when the setting
  changes. The registry sits behind a backend so tests never touch the real one.
- `gui/state.py` (shared AppState, no Qt) and `gui/workers.py` (QThread worker with throttled
  progress, cancel, and the same context recipe as `cli/runner.py`).
- Phase 6, `core/report.py` (no Qt): export a plan, an applied result or a journal run as CSV
  (UTF-8 with BOM), an Excel workbook (Actions and Summary sheets), a self-contained HTML report
  (duplicate groups with base64 thumbnails), the old duplicates review list (`.txt`, replaces
  `find_duplicates_review_list_<date>.txt`) and the old duplicates CSV log. CLI: `--export FILE`
  (and `--export-format`) on `scan` and `apply`, and `various-tools export OUTPUT (--plan F |
  --run ID)`.
- `core/media/thumbs.py`: shared previews and file facts. A cloud-only file is never opened.
- Dependencies: `openpyxl` (and `types-openpyxl` for development).

#### Decisions
- Apply always asks; a selected permanent delete asks a second time. Cancel stops at the next
  safe point. Undo from History and from the tool page both run `undo_run` on their own journal
  connection in a worker.
- External program checks, thumbnails, Explain match, undo, export and scans all run in workers;
  pages update only from signals. One program lookup (`AppState.deps`) is shared per session and
  rebuilt when settings are saved; Auto-detect clears it.
- Changing the keeper swaps the old keeper into the moved action (same destination folder) and
  rewrites "keeps NAME" in the reasons. Matches were measured against the previous keeper; for
  exact copies that is the same file content.
- The rename live preview is the only per-tool page behaviour besides the duplicates compare
  view (`LIVE_PREVIEW_TOOLS`, `COMPARE_TOOLS` in `gui/tool_page.py`).
- Missing programs are detected from the scan notes (a program name with "not found" or
  "needs"); Ollama has no Locate button because it is an address set in Settings.
- `tools/base.py: discover()` falls back to the built-in tool packages (`pkgutil` over
  `various_tools.tools`) when no entry point is registered, so a frozen build without package
  metadata still finds every tool.
- The Excel export imports Pillow (openpyxl does); CSV does not.

#### Fixed
- The plan table used `QSortFilterProxyModel.invalidateFilter()`, deprecated in PySide6 6.10, so
  every refresh raised under `filterwarnings = error`. It now uses `beginFilterChange()` and
  `endFilterChange()`; PySide6 6.10 or newer is required.

#### Tests
- `tests/gui/` (pytest-qt, run headless with `QT_QPA_PLATFORM=offscreen`): a smoke test and a form
  round trip for every tool page (parametrized over `discover()`, so a new tool is covered
  automatically, and a test fails if a tool has no round-trip values), and scan -> apply -> undo
  through the GUI for all 13 tools (`test_tool_pages.py`, `test_tool_pages_phase7.py`). Also the
  schema form, plan table, compare view, History, Settings, External programs, the Explorer menu
  (fake registry backend), theme, log panel and the app entry point (in a subprocess). Every test
  uses generated temp trees, a temp settings file and a faked Recycle Bin; `temp_cleaner` runs
  with `TEMP`, `APPDATA` and `LOCALAPPDATA` pointed at the temp folder. The full ExifTool test of
  `metadata_strip` skips itself when ExifTool is not installed.

### Phase 7: new tools (2026-11-13)

Plan reference: `docs/MODERNIZATION_PLAN.md` §4 Phase 7, items 1–7. Item 8 (scheduled runs) is
not part of this change. Each tool is a package under `src/various_tools/tools/`, with one
entry-point line in `pyproject.toml`, tests under `tests/tools/<tool>/` and a page in
`docs/tools/<tool>.md`. None needed GUI, CLI or core code.

#### Added
- `disk_usage` (Clean up): the largest files and folders, with size, type, category and age
  filters. Files are `trash` actions, none selected by default. Folder totals are plan notes in
  the documented formats `root-size: <bytes> <path>` and `folder-size: <bytes> <path>`
  (`parse_size_note()`), for a later treemap.
- `sort_by_type` (Organize): ordered rules `PATTERN[|PATTERN...]=FOLDER` with defaults (for
  example `*.pdf=Documents/PDF`). Moves are journaled and undoable. Clashes with existing files
  and within the batch are renamed (`_dup` suffix) or skipped. Sorted files are never re-sorted.
- `temp_cleaner` (Clean up): files older than N days (default 7) matching patterns under the
  chosen roots, which default to `%TEMP%`, `%TMP%` and `%LOCALAPPDATA%\Temp`. Optionally removes
  folders left empty. Browser caches (Chrome, Edge, Firefox) are notes only, in the format
  `browser-cache (read-only): <name> | <path> | <bytes|unknown>`, and never get actions.
- `media_convert` (Media): HEIC to JPG or PNG, WebP to JPG or PNG, and MOV to MP4. A video is
  remuxed when MP4 can hold its streams and re-encoded otherwise. Every output is verified.
  Originals are kept unless `trash_originals` (off, advanced) is on.
- `metadata_strip` (Media): removes GPS (default) or all metadata with ExifTool, without
  re-encoding. The preview lists what each file loses. Output goes to a new `stripped` folder by
  default, or uses a suffix. In-place mode is advanced and off by default.
- `checksum` (Developer): `create` writes one `sha256sum`-compatible manifest per root through a
  `write` action; `verify` is read-only and reports `mismatch:`, `missing:`, `extra:`,
  `unreadable:` and `invalid:` notes. sha1, md5 and sha512 are offered for old manifests.
- `corrupt_media` (Media): pictures and videos that fail to open or decode (Pillow; ffprobe and,
  at `deep`, an ffmpeg decode through `ctx.deps`). Quarantine moves (undoable) or trash, none
  selected by default.

#### Decisions (not covered by the plan, safer option picked)
- **Categories:** no new category. `checksum` is under Developer.
- **Folders are never trash actions in `disk_usage`.** The engine refuses to trash a non-empty
  folder, so folder sizes are information only.
- **Cloud-only files:** `disk_usage` neither counts nor offers them (they take no local space,
  and deleting one deletes it from the cloud). `sort_by_type` skips them, because a cross-volume
  move would download them. The other tools skip them as "cloud-only".
- **Trashing originals (`media_convert`) and in-place stripping (`metadata_strip`)** happen
  inside the `convert` handler, only after the output is verified, and go to the Recycle Bin.
  The action's reason says so, so the journal records it. In-place writes and verifies a temp
  copy, recycles the original, then renames the copy. A failed check keeps the original.
  Undo cannot reverse a `convert`; restore the original from the Recycle Bin.
- **`metadata_strip` needs ExifTool.** Without it, nothing is planned and a note gives the install
  command. A lossy Pillow re-encode is not offered.
- **ExifTool arguments go through a UTF-8 argument file** (`tools/metadata_strip/exiftool.py:
  run_exiftool`), because names with accents or CJK characters fail as plain arguments on
  Windows. The helper now lives in `core/media/exif.py`, and every ExifTool call uses it.
- **`checksum` hashes at apply time,** not at scan time: the plan stores actions only, and the
  manifest should match the folder when it is written. A manifest is never overwritten (temp
  file, then rename without replace). Undo cannot remove a written manifest; delete it by hand.
- **`corrupt_media`:** Pillow's `LOAD_TRUNCATED_IMAGES` is forced off during the run and restored
  afterwards. A timeout, permission error or missing ffprobe is "could not check", never corrupt.
- **Dedicated output folder defaults** (`converted`, `stripped`) instead of the
  `default_output_folder` setting, whose title is "Resized output folder name".
- **Plans with nothing selected** (`disk_usage`, `corrupt_media`) are applied from the GUI by
  ticking actions, or from the CLI by setting `"selected": true` in a saved plan file.

#### Fixed
- `media_resize`'s RAW-to-JPEG metadata copy and the ExifTool capture-time fallback
  (`exiftool_capture_time`, used by `organize_by_date`) failed on file names with characters
  outside the ANSI code page. Both now use `run_exiftool` from `core/media/exif.py`, tested with
  names like `café 写真 🙂.jpg`.

#### Follow-ups (not done here)
- A shared `usable_roots()` helper and a pruning `walk()` in `core/fs.py` (the root checks are
  copied in several tools), and a shared hidden-path check.
- A `--select-all` (or similar) flag for `apply` in the CLI, left to the CLI/GUI stages.
- `make_video` in `tests/conftest.py` could create parent folders.

### Phase 4: CLI (2026-11-13)

Plan reference: `docs/MODERNIZATION_PLAN.md` §4 Phase 4. Usage and the legacy-script mapping table
are in `README.md` ("Command line").

#### Added
- `various-tools list`, `<tool> scan [PATHS] [opts] [--preset P] [--json] [--save-plan FILE]`,
  `<tool> apply [--plan FILE | opts] [--yes]`, `history [RUN_ID]`, `undo RUN_ID`, `doctor`,
  `presets list|save|delete`, `config show|get|set|unset`, `duplicates explain A B`, and the
  global one-off options `--config`, `--exiftool`, `--ffmpeg`, `--ffprobe`, `--ollama-url`.
- Tool flags are generated from each options model (`core/schema.py: describe()`), with the field
  title, description, default, bounds and 🔒 marker as help text. Advanced fields are in an
  "Advanced" help panel. No per-tool CLI code except `duplicates explain`.
- `core/presets.py: PresetStore` (JSON in the app data folder), shared with the Stage 2 GUI.
- Rich progress on stderr; Ctrl+C cancels the run cleanly (exit 130).
- Tests under `tests/cli/` for every command, plus `tests/core/test_schema.py` and
  `test_presets.py`.

#### Decisions
- The script entry point is now `various_tools.cli.main:main`, which makes stdout/stderr tolerant
  of the console encoding (Rich tables and 🔒 crashed when piped on cp1252). `app` is still
  exported.
- Typer only (this Typer vendors click). Tool commands get a signature built from `describe()`.
  Every generated flag defaults to "not given", so only typed flags override preset values.
- Positional PATHS fill the tool's first folder field. A field whose name clashes with a CLI flag
  (`preset`, `json`, `plan`, `yes`, ...) becomes `--opt-<name>`; rename's `preset` is
  `--opt-preset`.
- `apply` always asks for confirmation unless `--yes`, and asks a second "PERMANENT DELETE"
  question when a selected action is a permanent delete. With no input (EOF) and no `--yes` it
  refuses. `--plan` cannot be mixed with option flags.
- `config set` asks before moving a 🔒 setting away from its default. A settings file that cannot
  be parsed is never overwritten. A corrupt presets file is copied to `presets.json.corrupt`
  before a later save replaces it.
- Exit codes: 0 ok, 1 failed/refused/partial, 2 usage or validation, 130 cancelled.

### Nothing is constant: every value configurable, with hints (2026-11-13)

Owner requirement: whatever was constant or hardcoded becomes a default value with a hint saying
what it is for and what it does.

#### Added
- **Hint convention** (documented in `core/settings.py` and `tools/base.py`): every settings and
  options field has a `title` and a `description` (units, what the default means); numbers have
  `ge`/`le` bounds; fixed choices are `Literal` (dropdowns); `json_schema_extra` keeps `group`,
  `advanced`, `widget`, `depends_on`, plus `locked: True` for 🔒 values. A field whose app-wide
  default is a setting is `X | None = None` ("empty uses the setting").
- `tests/tools/test_option_hints.py` fails when any registered tool's option or any setting lacks a
  title, description, group or bounds, or a 🔒 field is not advanced or does not say so.
- **New settings:** `data_folder`, `cache_folder`, `protected_folders`, `protect_drive_roots`,
  `protect_user_profile`, `cloud_only_files`, `hash_chunk_kb`, and timeouts
  `version_check_timeout_s`, `ollama_probe_timeout_s`, `ffprobe_timeout_s`,
  `frame_grab_timeout_s`, `transcode_timeout_s`, `exiftool_read_timeout_s`,
  `exiftool_write_timeout_s`, `ollama_tags_timeout_s`, `ollama_chat_timeout_s`. Helpers:
  `Settings.protected_paths()`, `resolved_data_folder()`, `resolved_cache_folder()`,
  `journal_path()`, `presets_path()`.
- `core/context.py`: `make_context()` and `make_deps()` build a run context from settings (saved
  program paths, one-off paths, timeouts, journal).
- `core/fs.py`: `ProtectedPaths` and `DEFAULT_PROTECTED_FOLDERS`; `protected_reason(..., rules=)`
  and `root_refusal(root, ctx.settings.protected_paths())`.
- **New tool options** (defaults are the old values; see each `docs/tools/<tool>.md`):
  - `duplicates`: `image_extensions`, `video_extensions`, `visual_skip_extensions`, `burst_name`,
    `copy_mark`, `verify_borderline_block`, `thumb_size`, `phash_size`, `video_phash_size`,
    `sharpness_px`, `frame_width`, `frame_retries`, `frame_step_back`, `ollama_prompt`,
    `ollama_image_px`, `ollama_jpeg_quality`.
  - `media_resize`: `suffix_tag`, `video_crf`, `video_preset`, `image_extensions`,
    `video_extensions`, `optimize_output`, `resample`, `estimate_compression`, `extras_scan_kb`;
    `output_folder_name` empty uses `settings.default_output_folder`.
  - `organize_by_date`: `dest_subfolder`, `photo_exts`, `raw_exts`, `video_exts`,
    `skip_dot_files`, `clash_suffix`; `unknown_folder`, `min_plausible_year`, `workers` and
    `catalog_path` empty use the settings / data folder.
  - `rename`: `min_plausible_year` (empty uses the setting), `filename_date_pattern`.
  - `project_export`: `output_name`, `separator`, `chars_per_token`, `language_by_suffix`.
  - `empty_folders`: hints and bounds only.

#### Changed
- 🔒 values are now configurable, with the locked value as the default, under Advanced, marked
  `locked`, and with a description saying what changing them risks: the `duplicates` thresholds
  (unchanged values), `media_resize` `never_upscale` (now a `bool`, default True), delete mode
  (trash), protected paths (default list unchanged), and the cloud-only rule.
- `tools/base.py`: `category` is `Literal["Clean up", "Media", "Organize", "Developer"]`, and
  `discover()` logs and skips a tool whose entry point fails to load or breaks the contract, or
  whose id is taken, instead of failing for every tool.
- `duplicates`: the cache folder comes from settings (the `CACHE_DIR` global is gone); the image
  cache file and name include the hash parameters, so changing them never reuses stale hashes;
  missing-program messages use `core/deps.install_hint()`.
- Core media, hashing and `deps` take timeouts and the read size as parameters; their defaults
  are the old constants, and tools pass the settings values.
- `save_settings()` writes only values that differ from the defaults.
- `organize_by_date` scans with `settings.workers` threads when `workers` is empty (it was 1). The
  plan is the same for any worker count (tested).

#### Decisions (not covered by the plan, safer option picked)
- **Cloud-only rule:** exposed as `cloud_only_files: Literal["skip"]`, a one-choice dropdown. Any
  download choice stays deferred (owner-accepted deferral) until a journaled download action
  exists.
- **Rules that are not values stay code:** never deleting the selected root, never following
  junctions or symlinks out of the root, journaling every action, and legacy date rules 1–4.
- `media_resize` with `never_upscale=False`: `fit_box` scales up or down to fit, and the "both
  dimensions exceed" rule applies only when it is True. Side modes set the side exactly.
  `percent` now allows up to 400, but values over 100 need `never_upscale=False`.
- `duplicates` threshold upper bounds were widened so larger hash grids stay usable (for example
  `phash_max` up to 1024). Defaults are unchanged.
- Validation keeps each tool's style: `organize_by_date` and `rename` report a bad value as a plan
  note; `project_export`, `duplicates` and `media_resize` reject it in the options model.
- Timeout defaults live in `core/settings.py` (not `core/media`), so reading settings never
  imports Pillow.

#### Kept internal (implementation details, one-line reasons)
- SQL schemas and cache/plan format versions (`journal.SCHEMA`, catalog `COLUMNS`/`SCHEMA`,
  `PLAN_FORMAT`, cache versions): storage format, not behaviour.
- Windows attribute bitmasks (`core/fs.py`, `core/onedrive.py`, `empty_folders`), `MAX_PATH`,
  invalid file-name characters, reserved names and the 255-character name limit: Windows rules.
- EXIF tag numbers, `EXTRAS_MARKERS` (motion-photo byte signatures), `MP4_FAMILY`: file-format
  facts.
- `core/deps.py` `TOOL_SPECS` (program names, version flags, env-variable names, winget ids,
  known install folders): identify the programs; the user overrides with a saved path or env var.
- `duplicates` `BLOCK_PX = 8` (part of what the tuned `pixel_block_max` means), Ollama
  `temperature 0` / `num_predict 16` and the YES parsing (keep the check deterministic).
- `media_resize` codec `libx264` and `-c:a copy` (other codecs need container checks and other
  CRF scales; possible follow-up), `PRESET_BOXES` and `MODE_LABELS` (dropdown choices).
- Template syntax (`rename` `TEMPLATE_FIELDS`, `organize_by_date` `ALLOWED_TEMPLATE_FIELDS`),
  built-in rename presets (a dropdown), reason and message strings, and file names inside the app
  data folder (`journal.sqlite`, `presets.json`, `catalog.sqlite`; the folder is configurable).

#### Tests
- Hint coverage over every field (above); settings round-trip and 🔒 defaults; protected-path
  rules from settings in `fs` and the apply engine; discovery isolation; timeouts and read sizes
  reaching core calls; per-tool tests that each new option changes behaviour. 1372 tests pass.

### Review fixes 2 (2026-11-13)

#### Fixed (safety)
- `media_resize`: apply checks `ensure_on_device` before `has_embedded_extras()` reads a file, so a
  file that turned cloud-only after the scan is never read (rule 3).
- `empty_folders`: unchecking a folder now also unchecks the junk files in every parent folder it
  unchecks. At apply, a junk file is trashed only if its folder still holds nothing but junk and
  folders that are also going; otherwise the row is journaled as skipped.
- `core/fs.py`: a cloud-only `.gitignore` is never read; its rules are not applied and a warning
  is logged.
- Output-side junctions (rule 4): new `core/fs.py` `reparse_ancestor()` / `destination_refusal()`.
  The apply engine refuses a MOVE or RENAME whose destination passes through a junction or symlink
  (up to the selected root that holds it, else up to the drive root), a RENAME that leaves its
  folder, and a MOVE or RENAME with no destination (was an `assert`). `duplicates` skips a managed
  folder behind a junction at scan; `media_resize` refuses its output at apply; `organize_by_date`
  refuses a `dest_root` that is a junction; `project_export` refuses such an output path.
- No silent overwrites: new `core/fs.py` `rename_no_replace()` replaces `os.replace` for the final
  step of `media_resize` images, `ffmpeg.transcode` and `project_export`, whose `.partial` file is
  now created exclusively. `rename` journals rows dropped by its apply-time recheck as skipped.

#### Fixed (correctness)
- `organize_by_date`: cloud-only rows (MOVE with no destination) stay unselected and the engine
  refuses them as a backstop. A failing EXIF, ffprobe or ExifTool reader falls through to the next
  date source, as in the legacy script.
- `project_export` no longer exports its own earlier outputs (`<name>_dupN.txt/.md`, `.partial`).
- ffprobe and ExifTool timeouts and malformed output degrade: the file is skipped with a note
  (`media_resize`) or the next date source is used (`organize_by_date`).
- `media_resize`: `keep_metadata=True` keeps the ICC profile and dpi. Preview totals count only
  selected rows and show the bytes saved.
- `duplicates`: `settings.ollama_url` is used; a failed Ollama check on a pair is a plan note and
  the pair is not moved; image passes 2 and 3 check for cancel and report progress.
- Docs: `organize_by_date` CSV export is a library function (`Catalog.export_csv`) only for now.

#### Tests
- Threshold boundary tests for `duplicates` (PHASH 2/3, DHASH 4/5, pixel block, aspect, video
  duration, frame mean and worst frame, burst 40/41); no value changed. The two Ollama tests that
  asserted nothing now check that the model's verdict decides a borderline match.

#### Decisions (not covered by the plan, safer option picked)
- For a destination outside every selected root (for example `organize_by_date` `dest_root`), the
  engine checks every existing ancestor up to the drive root for a junction.
- Refusals found by a tool's apply-time recheck (`empty_folders`, `rename`) are journaled as
  skipped after the engine's rows, so they get later sequence numbers.
- `media_resize` reports cloud-only and junction refusals at apply as failed, not skipped.
- Aspect tolerance is tested just inside and outside 2%, because exactly 2% is lost to float
  rounding; the comparison is unchanged.

#### Not done (left for later)
- Settings still ignored by tools (`media_resize` `default_output_folder`, `organize_by_date`
  `unknown_date_folder` / `min_plausible_year` / `workers`), and `tools/base.py` category Literal
  and `discover()` error isolation.

### Review fixes (2026-11-12)

#### Changed
- `core/fs.py: root_refusal()` replaces the five local `_root_refusal` copies in rename, project_export,
  duplicates, media_resize and organize_by_date. Behavior is unchanged. Test added.
- Fixed a stale docstring in `tools/__init__.py`.

#### Accepted by the owner (2026-11-13)
- Undo of trash, delete and convert actions stays as it is: the journal records them and undo lists
  them for manual restore. No code change.
- The real-CR3 end-to-end test is skipped for now (no sample file).
- The dropped options (`overwrite`, `cloud_only` ask/download, `also_resize`, Excel export/import)
  are deferred. They stay out of the tools until the owner asks for them.

### Phase 3.6: `organize_by_date` tool (2026-11-12)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 3, item 3.6. User page: `docs/tools/organize_by_date.md`.

#### Added
- `tools/organize_by_date/`: options (`OrganizeByDateOptions`, `extra="forbid"`) and the
  `OrganizeByDate` tool. `scan()` works out each file's date and destination and writes nothing to
  the media. `apply()` runs the moves through the generic engine, so each one is journaled and can
  be undone.
- `tools/organize_by_date/dates.py`: the date rules, as legacy rules 1 and 2. The sources are tried
  in the order the options give (default: EXIF, video creation time, ExifTool for RAW files,
  modified time). A source is used only when its date is plausible. A file with no usable date
  goes to the unknown-date folder.
- `tools/organize_by_date/catalog.py`: the SQLite media catalog that replaces the Excel master log.
  One row per file (path, capture time, date source, camera, size, dimensions, GPS, sync status,
  destination, status, error). Includes CSV export.
- Rules 1–4 of the legacy docstring, kept as they are (🔒): the capture date first, the modified
  time only when it is plausible, cloud-only files never read or moved, and only on-device files
  moved.
- `pyproject.toml`: the `organize_by_date` entry point.
- Tests (`tests/tools/organize_by_date/`, 26 tests). **Parity with
  `legacy/python-extract_file_metadata-8.py`:** the legacy `process_file()` is run read-only on a
  generated tree (EXIF, digitized date, modified date, implausible modified date, implausible EXIF,
  and a video's creation time). Its destinations and date sources must match the tool's plan, and
  the applied layout must match the legacy paths. Also: the date rules and their order, cloud-only
  files never read (the read is patched to fail if it happens), scan changing nothing, apply and
  undo, name clashes, files already in place, dot-files and subfolders, junctions not followed,
  protected destinations refused, resume and retry, the same plan for any worker count, a saved
  plan applied later, a file that turns cloud-only before apply, and bad options.
- `docs/tools/organize_by_date.md`: user page.

#### Changed
- Nothing outside the new tool, the entry point and this changelog.

#### Decisions (not covered by the plan)
- **Cloud-only files are always skipped, and nothing is downloaded.** The plan lists `ask` and
  `download` options and `download_wait_seconds` / `keep_pinned`. A download changes the disk
  before any move is journaled, and the engine has no journaled action kind for it. Adding one
  needs owner review, so those options are left out for now. Cloud-only files are reported as
  skipped, with the reason, and their date is not guessed.
- **`also_resize` is not implemented.** The legacy `--resize` overwrites each image in place, and
  CR3 originals are deleted. Both conflict with the rule that originals are kept and that undo can
  restore what it moved. The owner should decide this, as the Phase 3.5 entry says for the same
  question. Use `media_resize` after organizing.
- **Excel export and the one-time import of the old `Photo_Metadata_Master.xlsx` are not
  implemented.** That needs pandas or openpyxl, which are not project dependencies. The catalog's
  CSV export covers the export side.
- **Name clashes use `name_dupN`** (from `core/fs.py: unique_destination`), not the legacy
  `name (N)`. Names are also claimed within one batch, so two files with the same name in one run
  get different names at scan time, and the plan shows them. The legacy script could give both the
  same name, and the second move would then overwrite or fail.
- **ExifTool is used only for RAW files,** as in the legacy script. Other files are not sent to it,
  so the default order gives the same answers as the legacy script.
- **The default destination is `<OneDrive>\Pictures\Camera Roll`,** derived from the settings, as
  the legacy `DEST_ROOT` was. No personal path is hardcoded. The source folders have no default.
- **The catalog is written during the scan,** as a cache of what was read. That is the one
  exception to "scan never changes the disk" (AGENTS.md). It lives in the user data folder by
  default, not in a media folder. Each move updates its row.
- **Apply checks sync status again** before each move. A file that became cloud-only after the
  scan is not moved, because a move across volumes would read it.
- **The legacy `run()` is never called in tests,** because it reads and writes the owner's real
  master log. Parity uses the legacy `process_file()` only. Its pandas import is stubbed, and the
  located ffprobe folder is put on `PATH` for that run only, because the legacy code calls
  `ffprobe` by name.
- **Dot-files are ignored** (the legacy script's `run()` did the same).
- **Folder template** may use only `{year}` and `{month}`, and it must stay inside the destination.
- **Unknown-date folder** is used when no usable date exists, including when the modified time is
  implausible (as in the legacy script).
- **`_root_refusal` is a fifth local copy** (rename, project_export, duplicates, media_resize and
  this tool each have one). It should move into `core/fs.py` in the cleanup mentioned in 3.5.
- **Local environment note:** `uv` is not on this machine, so the entry point was added to the
  venv's installed metadata by hand. `uv sync` regenerates the same entry.

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
