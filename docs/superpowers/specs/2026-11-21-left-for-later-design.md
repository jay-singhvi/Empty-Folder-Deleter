# Version 0.7.0: the items left for later (wave 1)

Owner's request (2026-11-21): "start on the items left for later". Every decision is the
builder's; the safer option wins where in doubt. Safety rules in AGENTS.md are unchanged.

Wave 1 closes every known gap from 0.5.0 and 0.6.0 and adds OneDrive-aware pacing. Wave 2
(separate design, later): saved recipes, duplicates inside the photo library, place names.

## Part 1: library and map (`core/media/library.py`, `gui/library_model.py`, map files)

1. A chosen photo folder inside another chosen folder is read and counted once (nested roots
   are folded into their outermost root for sync and counting; rows are not duplicated).
2. A subfolder that cannot be listed (access denied, transient error) keeps its existing rows
   until it can be listed again; it is reported in the update's notes.
3. RAW photos and videos get GPS place and pixel size: RAW through ExifTool (via `ctx.deps`),
   videos through ffprobe tags (`location`, `com.apple.quicktime.location.ISO6709`) and
   stream width/height. Cloud-only files are still never read.
4. The library is loaded off the GUI thread (a worker), with the page showing "Loading…"; the
   views stay responsive at 200,000 items.
5. Map tile cache: keyed by the tile source address (a hash folder per source) and capped
   (setting `map_tile_cache_mb`, default 200 MB, oldest files removed first).
6. The map pans across the 180° line (wraps horizontally) and stays smooth with 50,000 points
   (a timed test).

## Part 2: file-level fixes (`tools/set_date`, `tools/wrong_extension`, `plan_table.py`)

7. Change date reads a video's new date back after writing (ExifTool `-api QuickTimeUTC=1`,
   or ffprobe `creation_time`) and fails the action if it differs.
8. An original replaced by Change date goes to the Recycle Bin under its real file name (moved
   aside into a hidden temporary folder next to it, not renamed), so restoring it by hand gives
   the right name; History undo keeps working; the empty temporary folder is removed.
9. Fix File Extensions renames sidecar files (`.xmp`, `.aae`, `.thm`, and `name.ext.xmp`
   style) together with their file, journaled, undone together.
10. MP1/MP2 audio is recognised again, only from a strict frame-header check of two
    consecutive frames (so FLAC/WAV with ID3 tags are not mistaken).
11. Results list: ticking one row with "Show: Ticked/Not ticked" on does not re-filter the
    whole list (the row stays until the filter is changed or *Refresh* is pressed, with a
    small "list out of date" hint), so a tick at 100,000 rows is instant.

## Part 3: OneDrive-aware pacing (`core/onedrive.py`, `core/plan.py`, apply dialog, CLI)

12. `onedrive.upload_pending(path) -> bool | None`: true while a file in a OneDrive folder is
    not yet in sync, read with the Cloud Files API (`CfGetPlaceholderInformation`, basic info,
    `InSyncState`) on a handle opened with `FILE_READ_ATTRIBUTES` only (never hydrates). None
    when it cannot tell (not OneDrive, API missing).
13. Pace gets `wait_for_upload: bool` and `upload_timeout_s`. After each batch apply_plan waits
    until every file the batch wrote (outputs and moved/renamed destinations) reports in sync,
    or the timeout passes, then also waits the normal pause. Messages: "Waiting for OneDrive to
    upload 37 of 50 files… (2:10)". Cancel, Pause and Skip the wait work as today, and the
    heartbeat keeps running.
14. Apply window: a checkbox "Wait until OneDrive has uploaded each batch (at most [30]
    minutes)", shown when the plan touches OneDrive, on by default there with batches. CLI:
    `--wait-for-upload [--upload-timeout 30m]`.
15. Screenshots of the Home, tool page, Timeline and Map pages for README/docs, made with
    `packaging/make_screenshots.py` on generated fixture data only.
