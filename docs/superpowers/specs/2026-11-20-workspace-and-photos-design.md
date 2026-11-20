# Version 0.6.0: one working folder, paced apply, a real photo browser

Owner's request (2026-11-20): applying 2,113 resized OneDrive files at once (62 GB) could swamp
OneDrive; the results list has no quick way to tick only 100 of 2,000; no Ctrl+A, no Properties;
every tool asks for the same folder again; no Settings or Exit button; too much on screen at
once; Timeline and Map are "basic and naive"; a wrong date can't be fixed from the Timeline.
The owner left every decision to the builder. Safety rules in AGENTS.md are unchanged.

## 1. App shell (part B)

- **Menu bar.** *File*: Choose working folder… (Ctrl+O), Recent folders ▸, Settings… (Ctrl+,),
  Exit (Ctrl+Q). *View*: Show all settings, Show log (Ctrl+L), Find a tool (Ctrl+K: focuses the
  sidebar search), Home. *Help*: Keyboard shortcuts, Open the log/config folder, About.
- **Working folder bar** at the top of the page area, shared by every tool: "Working folder:
  `C:\…` [Change…] [▾ recent]". One or more folders. Stored in `gui_state.json`
  (`workspace`, `recent_workspaces`, last 8), so it survives restarts even with "Remember last
  folders" off. It is GUI state, not a tool option; the CLI keeps explicit paths.
- **Tools use it by default.** A tool's *workspace field* is its first folder field (`roots`,
  `sources`, `root`), or the tool's optional `workspace_field` attribute (`None` opts out; Temp
  Cleaner opts out because its folders are extra temp folders). On a page that uses it, the field
  is folded into one line: "Looks in: the working folder (`C:\…`)  ·  Use other folders for this
  tool". Ticking "Use other folders" shows the original field (remembered per tool). When the
  working folder changes, idle pages follow it, and a page that already has a list shows "The
  working folder changed. Look again to update the list." (the old list is kept, never applied
  silently to the new folder: Apply stays tied to the plan that was shown).
- **Home page** (replaces the welcome label): the working folder, tool cards by category with
  their one-line descriptions, and the last 5 runs from History with a link to History.
- **Progressive disclosure on tool pages.** After a scan, step 1 (the options) folds into a one
  line summary of the non-default choices with an *Change settings* link; the list gets the
  room. Nothing is removed.
- **Toolbar** under the menu: the working folder chip, History, Settings (gear) buttons.
- **Notes stay short.** The scan's notes box no longer lists thousands of "skipped: …" lines:
  it shows the counts by reason ("86,691 skipped: already smaller than the target") with a
  *Show details* disclosure that reveals the full list. The skipped files are also in exports.

## 2. Results list (part A)

- Always visible above the list (not only with "Show all settings"): **Tick all**, **Untick
  all**, and **Tick only the first [100] ▸** (unticks every shown row, then ticks the first N
  shown rows in the current sort and filter). A **Show: All / Ticked / Not ticked** filter.
- Status line: "2,113 found · 150 ticked (4.4 GB)".
- **Keyboard:** Ctrl+A selects (highlights) every shown row; Space ticks or unticks the
  highlighted rows (all to the opposite of the first one); Shift/Ctrl+click extend the
  highlight. Ctrl+F focuses the filter box.
- **Right-click** works on the highlighted rows: Tick, Untick, Tick only these; Open, Show in
  File Explorer, **Properties** (the Windows Properties dialog, `ShellExecuteExW` verb
  `properties`, GUI-only helper in `gui/reveal.py`), Copy path(s).
- Pagination is not added: the list is virtual and handles 100,000 rows; "Tick only the first N"
  plus the Ticked filter does what pages would.

## 3. Paced apply (part A)

- `core/context.py`: `Pace(batch_size: int, pause_s: float)` and `RunContext.pace: Pace | None`,
  plus `RunContext.pause_event` (set = paused). `apply_plan` counts the actions it really ran
  (skipped ones do not count); after each full batch, if more remain, it waits `pause_s` in
  short steps, checking cancel and pause, and reports "Batch 3 of 43 done. Next batch in 4:32."
  A pause is honoured between actions. Cancel during a wait ends the run as *cancelled*. It
  stays one journal run, so History undoes it in one go.
- **Apply dialog** (replaces the plain yes/no box; the permanent-delete second question stays):
  the summary, then *How fast*: **All at once** (default) or **In batches of [50] files, then
  wait [5] [minutes]**, with presets "50, then wait 5 minutes (gentle on OneDrive)", "100, then
  wait 1 minute", "1, then wait 2 seconds", and the estimated extra waiting time. The last
  choice is remembered in `gui_state.json`. When the plan writes inside a OneDrive folder and
  is over 1 GB or 500 files, *In batches* is preselected with a one-line reason.
- **While it runs:** Pause / Resume, *Skip the wait* (during a pause between batches) and Stop. Stop finishes the current file, then stops; what
  was done is journaled.
- **CLI:** `<tool> apply --batch-size N --pause 5m` (`s`, `m`, `h` or plain seconds).
- Later (not in 0.6.0): wait until OneDrive reports the uploaded files as in sync.

## 4. Photos & videos (part C)

- **One library, two views.** Timeline and Map share one filter bar: search (name or folder),
  Photos / Videos / Both, a date range (From/To month), and the folder. The library folders move
  into a *Library folders…* dialog (with "Add the working folder"). *Update* stays one button.
- **Timeline view:** one scrolling grid grouped by month, with day sub-headers ("Saturday 22
  November 2022 · 14"); a year/month jump list (slim, collapsible); thumbnail size slider
  (Ctrl+wheel); a **preview pane** (toggle with the *i* button or the I key): large picture,
  date, size, pixel size, place, folder. Enter or double-click on a photo opens a full-window
  viewer (left/right arrows, Esc closes); videos open in the default app. Multi-select with Ctrl/Shift, Ctrl+A.
- **Right-click on the selection:** Open, Edit, Show in File Explorer, Properties, Copy path,
  Show on map (when it has a place), **Change date…**.
- **Change date…** (new tool `set_date`, category Photos & videos, so the CLI gets it too):
  modes *Exact date and time*, *Month and year only* (keeps day and time; the day is clamped to
  the month's length), *Shift by ± days/hours/minutes* (camera clock was wrong). The dialog shows
  old → new for each file before anything is changed (preview first). Apply writes the taken
  date with ExifTool (photos: `DateTimeOriginal`, `CreateDate`; videos: QuickTime
  `CreateDate`, `MediaCreateDate`) into a copy, sends the original to the Recycle Bin and puts
  the copy in its place (the Strip metadata in-place pattern), then sets the modified time. So
  it is journaled and undone from History. Without ExifTool only the modified time is changed,
  and the dialog says so. Cloud-only files are skipped and reported. The library re-reads the
  changed files afterwards.
- **Map view:** the clicked circle's photos open in a side panel (not below), hover shows count
  and date span, *Fit all* button, double-click zooms, and the shared filters apply (a date range
  on the map). A banner offers the detailed online map (still off by default, with the privacy
  reason). *Show on map* from the Timeline centres and highlights the item.

## 5. Later

OneDrive-sync-aware pacing; saved "recipes" (a chain of tools on the working folder); find
duplicates inside the library; places by name (offline reverse geocoding).
