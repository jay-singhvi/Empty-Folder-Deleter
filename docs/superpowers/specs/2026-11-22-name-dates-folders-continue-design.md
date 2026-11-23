# Version 0.9.0: dates from file names, one folder list, continue a stopped apply

Owner's report (2026-11-22): "Organize photos and videos by date" put
`Camera Roll\2022\2022 - 06\20220624_005115.mp4` into `2025 - 03`. The date is in the name.
Also: folder pickers take one folder at a time; "working folder" and "Use other folders for
this tool" should be one thing; a stopped apply should continue without a new scan; the pace
should be changeable after the apply has started. Decisions not given by the owner are the
builder's; the safer option wins. Safety rules in AGENTS.md are unchanged.

## Part A: dates from file names (`core/media/name_dates.py`, `tools/organize_by_date/`)

Cause: a video's embedded creation time (and EXIF) changes when a file is edited, re-encoded,
re-saved or exported, and the modified time changes on a copy. The camera's or WhatsApp's file
name keeps the original date.

1. `core/media/name_dates.py`: `date_from_name(name, *, min_year, now) -> NameDate | None`, pure.
   Recognised (year 4 digits, a real calendar date, not before `min_year`, not after `now` +
   1 day; digits that belong to a longer number are never used):
   - WhatsApp: `IMG-20220624-WA0001.jpg`, `VID-20220624-WA0001.mp4`, `PTT-…`, `AUD-…`, also
     `WhatsApp Image 2022-06-24 at 00.51.15.jpeg` / `WhatsApp Video …`.
   - Compact camera names: `20220624_005115.mp4` (Samsung), `IMG_20220624_005115.jpg`,
     `VID_20220624_005115.mp4`, `PXL_20220624_005115123.jpg` (Pixel), `DJI_20220624005115_0001_D.JPG`
     (DJI), `Screenshot_20220624-005115.png`, `MVIMG_…`, `signal-2022-06-24-005115.jpg`.
   - Dashed: `2022-06-24 00.51.15.jpg`, `photo_2022-06-24_00-51-15.jpg` (Telegram),
     `Screenshot 2022-06-24 at 00.51.15.png`.
   A time is used only when it is a real time of day; otherwise the date alone (midnight).
   Names with no such pattern (for example iPhone `IMG_1234.HEIC`, older DJI `DJI_0001.JPG`)
   give None and the metadata decides, as before.
2. New option `name_date_first: bool = True` ("Use the date in the file name first", group
   Dates). When on, the name is tried before every source in `date_priority`. Plan label
   "Date in the file name". A new option, not a new `date_priority` entry, so remembered
   `date_priority` values from 0.8 keep working and still get the fix.
3. With *Skip already processed files*, a file whose name gives a date is scanned again when
   the catalog's `date_source` for it is not the file-name label (so files moved by an earlier
   version by the wrong date are put right). No file is opened to decide this.
4. Tests: every name above, near misses (`IMG_1234`, `20221399_…`, a 10-digit timestamp,
   year 1970 with min year 1990, a future date), the owner's example end to end (an MP4 whose
   creation time says 2025-03 goes to `2022/2022 - 06`), the option off keeps the old order.

## Part B: one folder list, choose several folders (`gui/`)

5. `widgets/path_picker.py`: `choose_folders(parent, title, start="") -> list[str]` opens a
   Qt folder dialog that allows selecting several folders (the native Windows folder dialog
   cannot). Used by `PathListEdit` (Add…), the Home page / menu "Choose working folder…" and
   "Add a folder to the working folder…". Single-folder pickers keep the native dialog.
6. The tool page drops the "Use other folders for this tool" check box and the "Looks in" line.
   The tool's folder option is always shown and **is** the working folder: it starts with the
   working folder, and Add, Remove or a drop in it changes the working folder (every page and
   the toolbar follow). A single-folder option (`root`) shows the first working folder; a new
   value replaces that first folder when the scan starts. The remembered
   `_use_other_folders` flag from 0.8 is ignored. `set_folder` / `set_folders` (shell menu,
   `--folder`, Timeline "Find duplicates…") set the working folder.
7. Recipe steps keep their own "Use other folders for this step" choice (a recipe can still
   pin folders); a recipe step page with its own folders shows them and does not change the
   working folder.

## Part C: continue a stopped apply, change the pace while it runs (`core/plan.py`, `gui/`)

8. After Stop (a `cancelled` RunResult), the page keeps the list: rows that were done are
   removed, and Apply reads "Continue (N left)". Continue opens the Apply window again (so the
   pace can be changed) and applies only the rest, as a new journaled run. No new scan. The
   same after a run that ended with failures: the failed rows stay and can be retried.
   Done rows are matched by the action's kind and source (destination when there is no
   source). A new scan or changed options drop the continue state.
9. While an apply runs, "Change pace…" opens the pace part of the Apply window; the new pace
   takes effect from the next batch. `apply_plan` reads `ctx.pace` before each action (the
   batch count is worked out from the actions since the last wait), so a change from the GUI
   thread is safe (a frozen `Pace` object is swapped, never edited).
10. CLI: unchanged (a stopped CLI apply can be run again with `--apply`; files already moved
    are skipped by the guards).

## Left for later

- Continuing after the app was closed (the plan is not stored on disk).
- Changing tool options (not the pace) mid-run: the list depends on them, so they need a scan.
