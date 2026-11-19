# Simpler tool page, keeper paths, "Show in File Explorer", open-source scrub

Date: 2026-11-19. Status: draft for the owner's approval.

## Intent

Version 0.3.0 still feels packed: every tool page shows the whole options form, a toolbar of
buttons, a filter row, a technical table and a log at the same time. The owner wants a
**completely non-technical 15-year-old** to be able to use every tool, **without removing any
feature or control**. Separately: duplicates must show *which* copy is kept (full path), every
results table needs a right-click "show me this file", and nothing in the repository may be
specific to the owner's machine before it is open-sourced.

Later, separate designs (not in this one): a media timeline, a photo/video map, and new tools
from research.

## Goals

1. **Step-by-step page (owner picked approach A).** Every tool page reads top to bottom as three
   numbered steps, in plain words:

   ```
   Empty Folder Deleter
   Finds folders with nothing in them and moves them to the Recycle Bin.

   1  Choose a folder      [ D:\Downloads                         ] [Add…]
   2  Look for empty folders                 [ Look ]   ☐ Show all settings
   3  Check the list, then confirm
      Found 12 empty folders (3 MB). Untick any you want to keep.
      ┌───────────────────────────────────────────────────────────┐
      │ ☑ Old project\build        Move to Recycle Bin   empty    │
      └───────────────────────────────────────────────────────────┘
      [ Move 12 to the Recycle Bin… ]   [ Undo ]          [ More ▾ ]
   ```

   * Step 1 is the folder field(s). Step 2 is the scan button plus the tool's basic options.
     Step 3 (the results) stays hidden until a scan has run, so a new page is short.
   * The scan button says what it looks for ("Look for empty folders", "Find duplicates").
     The apply button says what it will do, from the checked actions: "Move 12 to the Recycle
     Bin…", "Move 40 files…", "Rename 9 files…" (mixed kinds: "Apply 12 changes…").
   * A summary line in plain words above the table ("Found 12 empty folders (3 MB). Untick any
     you want to keep.").

2. **One "Show all settings" switch, app-wide and remembered.** Off by default. Off: the page
   shows folders, basic options and the simple table. On: the "Advanced options" section is
   open on every page, the table toolbar shows the kind filter, the view switch (Grouped /
   Folder tree) and Select all / none, and the log button is shown. Every option and control
   that exists today is reachable with the switch on. Nothing is removed. The filter box stays
   visible in both modes.

3. **Plain words in the table.** Columns: *What happens*, *File* (or *Folder*), *Goes to*,
   *Why*, *Size*. Kinds as words: trash → "Move to Recycle Bin", move → "Move", rename →
   "Rename", delete → "Delete forever", write → "Create file", convert → "Convert". Exports and
   the CLI keep the existing kind names (they are a file format).

4. **Duplicates show the kept copy's full path.** The group row reads "Keeps
   `D:\Photos\2024\IMG_001.jpg`" (full path, elided in the middle when narrow, full path in the
   tooltip), and the duplicate rows get a *Kept copy* column, so two kept files with the same
   name in different folders can be told apart. Swapping the keeper in Compare updates it.

5. **Right-click menu on every results row** (all tools, both views):
   * **Show in File Explorer**: opens Explorer with the item selected (`explorer /select,`).
     Works for files and folders (empty folders: the folder is selected in its parent).
   * **Open**: the file in its default app (image viewer, video player), a folder in
     Explorer (`QDesktopServices.openUrl`).
   * **Copy path**.
   * A row with two places gets one entry per place: duplicates "Show this copy" / "Show the
     kept copy"; move and rename "Show where it is now" / "Show where it will go" (the
     destination folder, since the file is not there yet). Entries whose path no longer
     exists are disabled (or, for "where it will go", open the nearest existing parent).
   * Opening never reads file contents, so cloud-only OneDrive files are not downloaded by
     *Show in File Explorer*. *Open* on a cloud-only file asks first, because opening it
     downloads it (safety rule 3).

6. **Open-source scrub.** Remove the one machine-specific value (`BASE =
   r"C:\Users\<owner>\OneDrive\Pictures"` in the legacy reference copy of
   `python-find_duplicates.py`) and add a test that fails when a tracked file contains a
   `C:\Users\<name>` path other than placeholders (`<you>`, `me`, `NAME~1`, ...). The owner's
   name in LICENSE, README, installer and `pyproject.toml` stays (authorship); the GitHub URL
   stays. Note in the CHANGELOG that the old value is still in git history.

## Non-goals

* No home screen of task cards (approach B) and no second app-wide mode (approach C).
* No change to any tool's scan or apply behaviour, thresholds or safety rules (🔒).
* No change to CLI output or export formats.

## Anti-goals (a failure even if it works)

* Any option or control that is no longer reachable.
* Plain-language wording that hides what will happen ("Clean up" instead of "Move to Recycle
  Bin").
* Apply becoming easier to trigger by accident: it still always asks to confirm, twice for a
  permanent delete.

## Constraints

* AGENTS.md: `core/` has no Qt; only `core/deps.py` names external programs, so `explorer` is
  added to `deps.SYSTEM_TOOLS` and the GUI asks deps for it.
* The legacy reference folder is read-only by rule; changing that one line is the owner's
  explicit request (open-sourcing) and the parity tests must still pass.
* Tests, ruff, pyright and screenshots (`packaging/make_screenshots.py`) are updated.

## Approaches considered

* **A. Step-by-step page + one "Show all settings" switch** (picked by the owner): the least
  new UI, everything stays on one page, and a beginner sees only three steps.
* B. Home screen with task cards opening the same pages: friendlier first screen, more work, an
  extra screen to maintain. Could be added later on top of A.
* C. Separate Simple and Expert modes: clearest split, but two layouts to keep in sync.

## Left to the builder

Exact wording of each tool's scan button and summary line (a short per-tool text, falling back
to "Look for files" / "Found N items"), spacing and step-number styling, where the setting is
stored (next to the other GUI preferences in `AppState`).
