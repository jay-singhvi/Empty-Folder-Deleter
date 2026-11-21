# Version 0.8.0: saved recipes, duplicates in the photo library, place names (wave 2)

Owner's request (2026-11-21): "start wave 2". Every decision is the builder's; the safer option
wins where in doubt. Safety rules in AGENTS.md are unchanged, and no 🔒 threshold changes.

## Part A: saved recipes

A **recipe** is a named, ordered list of steps; each step is a tool id plus that tool's saved
options (the same values a tool page would scan with). Recipes run on the working folder.

- **Storage:** `recipes.json` in the config folder (`platformdirs.user_config_dir`), written
  atomically, with a format version. Each step stores `tool`, `options` (the tool's options as
  JSON, validated against the tool's options model on load and on run) and an optional label.
  A step whose tool is missing or whose options no longer validate is shown as "needs
  attention" and blocks the run until fixed. The workspace field of each step is not stored:
  it is filled from the working folder at run time (as on tool pages), unless the step's own
  "Use other folders" was chosen.
- **Core** (`core/recipe.py`, no Qt): `Recipe`, `RecipeStep`, load/save, validate, and
  `run_recipe(recipe, ctx, confirm_step)` for the CLI: for each step in order, scan, call
  `confirm_step(step, plan)`, apply when it says yes (journaled as its own run, so History
  undoes each step), stop on a refusal, a cancel or a failed step. Every step's run records the
  recipe name and a shared recipe-run id in its options JSON (`"_recipe": {...}`), so History
  can show "Recipe: Tidy my camera folder, step 2 of 3".
- **GUI:** a **Recipes** page (sidebar, App section, and View menu). Left: the list (New,
  Duplicate, Rename, Delete with a question). Right: the steps (Add step: pick a tool, then its
  options form, the same SchemaForm as the tool page; move up/down; remove). *Run recipe*:
  step by step, each step scans, shows its list in the normal results table with the normal
  Apply window (batches and the OneDrive wait included), then goes to the next step after the
  apply or on *Skip this step*; *Stop* ends the recipe. A tool page gets **Save as recipe
  step…** (add its current options to a new or existing recipe).
- **History:** runs that belong to one recipe run are labelled with the recipe and step;
  *Undo this recipe run* undoes its steps from last to first (each through the existing undo,
  asking once for all).
- **CLI:** `various-tools recipe list | show NAME | run NAME [--apply] [--select-all]`. Without
  `--apply` it scans the first step only and says that later steps depend on it. With
  `--apply` it applies each step after scanning it (the same "nothing is selected → stop" rule
  as `apply`), printing each step's summary. Pacing flags work as on `apply`.
- **Three starter recipes** offered (not created) on the empty page: "Tidy a folder" (Empty
  Files, then Empty Folder Deleter), "Prepare photos to share" (Resize media, then Strip
  metadata to a new folder), "Fix photo names" (Fix File Extensions, then Rename).

## Part B: photo library (duplicates and place names)

- **Duplicates in the library.** The Timeline gets *Find duplicates…*: it opens the Duplicate
  Finder with its folders set to the library folders (override folders, like `open_tool` with
  a folder), so its thresholds, cache, compare view and Apply are reused unchanged. After that
  tool's scan, the Timeline can show those groups: a *Duplicates* filter in the shared filter
  bar ("Only photos with a duplicate") reads the last duplicates plan for the library folders
  (kept in memory and in `<cache>/library_duplicates.json` with the plan's time; stale entries
  for files that changed are ignored). A grid badge marks items in a group; the preview pane
  lists the other copies with *Show* links. Nothing is moved from the Timeline itself; changes
  go through the Duplicate Finder page.
- **Place names, offline.** A compact city list built from GeoNames `cities15000` (cities over
  15,000 people), `admin1CodesASCII` and `countryInfo`, licensed CC BY 4.0: name, admin-1
  name, country name, latitude, longitude. `packaging/make_places.py` downloads and compacts
  it into `src/various_tools/core/media/data/places.bin.gz` (or similar; target under 1.5 MB),
  committed with its source date; the README and About credit GeoNames. `core/media/places.py`
  finds the nearest city (numpy, a lat/lon grid; under 1 ms per lookup after a lazy load) and
  returns "Paris, Île-de-France, France", or nothing when the nearest is over 50 km away
  (open sea, remote areas). No network at run time.
- Places appear in the preview pane, the map's side panel and hover text, and as a **Place**
  filter (country, then region) in the shared filter bar; library rows store the place
  (migrated, filled lazily for existing rows on the next update without reopening files).
- **Timeline rebuild at 200,000 items** must not block the GUI thread for more than ~100 ms:
  build the grouped layout in the loader worker and hand over a ready structure.

## Not in this wave

Face grouping; trips (automatic events); duplicates without running the Duplicate Finder.
