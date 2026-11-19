# Simpler tool page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A step-by-step tool page in plain words with one app-wide "Show all settings" switch, full keeper paths in duplicates, a right-click "Show in File Explorer" menu on every results row, and no machine-specific values in the repository.

**Architecture:** Pure wording helpers in a new `gui/wording.py` (no Qt, unit-tested), used by the plan table and the tool page. Reveal targets are computed by a pure function in `gui/reveal.py`; the Explorer launch goes through `core/deps.system_tool("explorer")`. The switch is stored in `gui_state.json` by `AppState` and broadcast to every page and the main window.

**Tech Stack:** Python 3.14, PySide6, pytest + pytest-qt, ruff, pyright.

**Spec:** `docs/superpowers/specs/2026-11-19-simpler-tool-page-design.md`

## Global Constraints

- Nothing is removed: every option and control is reachable with "Show all settings" on.
- `core/` has no Qt. Only `core/deps.py` names executables (`explorer.exe` goes into `SYSTEM_TOOLS`).
- Apply always confirms (twice for permanent delete). No change to scan/apply behaviour, thresholds or 🔒 defaults.
- CLI output and CSV/Excel/HTML exports keep the raw kind names and column names.
- Opening a cloud-only OneDrive file asks first; "Show in File Explorer" never opens file contents.
- Kind words: trash → "Move to Recycle Bin", move → "Move", rename → "Rename", delete → "Delete forever", write → "Create file", convert → "Convert".
- Columns: "What happens", "File", "Goes to", "Why", "Size" (folder tree: "Name", "What happens", "Goes to", "Why", "Size"; rename: "What happens", "Folder", "Old name", "New name", "Why", "Size"; duplicates grouped adds "Kept copy" after "Goes to").
- Run `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright` before saying done.

## Review Focus

- A row whose file no longer exists (after Apply, or deleted outside the app): menu entries for it are disabled, "where it will go" opens the nearest existing parent. Test in Task 3.
- A folder-tree folder row (no action) still gets "Show in File Explorer" for its folder. Test in Task 3.
- Kind filter after the labels change: picking "Move to Recycle Bin" must still filter on `trash`. Test in Task 2.
- Toggling the switch on one page updates pages already open and pages opened later. Test in Task 5.
- Keeper swapped in Compare: the group row and "Kept copy" show the new full path. Test in Task 2.

---

### Task 1: Wording helpers

**Files:**
- Create: `src/various_tools/gui/wording.py`
- Test: `tests/gui/test_wording.py`

**Interfaces:**
- Produces: `KIND_LABELS: dict[ActionKind, str]`; `kind_label(kind: ActionKind | str) -> str` (unknown → the raw string); `apply_label(plan: Plan | None) -> str`; `scan_label(tool: object) -> str` (`getattr(tool, "scan_label", "Look for files")`); `found_nouns(tool: object) -> tuple[str, str]` (`getattr(tool, "found_nouns", ("item", "items"))`); `summary_text(plan: Plan, sizes: Sequence[int | None], nouns: tuple[str, str]) -> str`.

- [ ] **Step 1: Write failing tests**
  - `apply_label`: None or 0 selected → `"Apply…"`; 12 selected trash → `"Move 12 to the Recycle Bin…"`; 1 move → `"Move 1 item…"`, 3 moves → `"Move 3 items…"`; 2 renames → `"Rename 2 items…"`; 1 delete → `"Delete 1 item forever…"`; 2 write → `"Create 2 files…"`; 2 convert → `"Convert 2 items…"`; trash + move selected (5 total) → `"Apply 5 changes…"`. Unselected actions are not counted.
  - `summary_text(plan_with_12_actions, sizes summing 3 MiB, ("empty folder", "empty folders"))` → `"Found 12 empty folders (3.0 MB). Only ticked rows are changed, and you confirm first."` (use `plan_edit.human_size` for the size; omitted with no known sizes); 1 action → singular noun; 0 actions → `"Nothing found. Nothing needs to change."`.
  - `kind_label("trash") == "Move to Recycle Bin"`, `kind_label("odd") == "odd"`.
- [ ] **Step 2:** `uv run pytest tests/gui/test_wording.py -v` → FAIL (module missing).
- [ ] **Step 3:** Implement `gui/wording.py` (no Qt import).
- [ ] **Step 4:** Run again → PASS.
- [ ] **Step 5:** Commit `gui: plain-language wording helpers`.

### Task 2: Plan table words, duplicates keeper path, full-controls switch

**Files:**
- Modify: `src/various_tools/gui/widgets/plan_table.py`
- Test: `tests/gui/test_plan_table.py`, `tests/gui/test_plan_tree.py`, `tests/gui/test_safety_review_gui.py` (update header/kind expectations)

**Interfaces:**
- Consumes: `wording.kind_label`.
- Produces: `PlanTable.set_full_controls(full: bool) -> None` (shows/hides `kind_combo`, the "View:" label + `view_combo`, and any widget added with `add_toolbar_widget(widget, full_only=True)`; the filter box always shows). `kind_combo` items show labels with the raw kind as `itemData`.

- [ ] **Step 1: Update/write failing tests**
  - Header labels per Global Constraints for grouped, rename, tree and duplicates (`["What happens", "File", "Goes to", "Kept copy", "Why", "Size"]`).
  - Duplicates group row text is `f"Keeps {keeper_full_path}"`, tooltip the full path; each duplicate row's "Kept copy" cell is the keeper's full path. After setting `action.group_id` to a new keeper and `table.refresh()`, both show the new path.
  - Kind column shows `"Move to Recycle Bin"` for trash; `kind_combo` items are `["All kinds", "Move to Recycle Bin", "Move"]`; choosing `"Move to Recycle Bin"` hides move rows.
  - `set_full_controls(False)` hides `kind_combo` and `view_combo`, keeps `filter_edit` visible (`isVisibleTo(table)`); `True` shows them.
- [ ] **Step 2:** Run those test files → FAIL.
- [ ] **Step 3:** Implement: kind labels in column 0 display (sort role stays the raw kind), `_PlanProxy` compares with the raw kind from `itemData`, duplicates detection already in `group_label` reused for the extra column, `add_toolbar_widget(widget, *, full_only=False)`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `gui: plain column words, full keeper path in duplicates`.

### Task 3: Right-click "Show in File Explorer" / "Open" / "Copy path"

**Files:**
- Modify: `src/various_tools/core/deps.py` (`SYSTEM_TOOLS["explorer"] = "explorer.exe"`, but `system_tool` looks in `%SystemRoot%` itself for it: add an optional per-name folder so explorer resolves to `%SystemRoot%\explorer.exe`)
- Create: `src/various_tools/gui/reveal.py`
- Modify: `src/various_tools/gui/widgets/plan_table.py` (context menu)
- Test: `tests/gui/test_reveal.py`, `tests/core/test_deps.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class RevealTarget: label: str; path: Path; exists: bool; is_destination: bool`
  - `reveal_targets(action: PlannedAction | None, node_path: Path | None, is_duplicates: bool) -> list[RevealTarget]`. Labels: one place → `"Show in File Explorer"`; duplicates → `"Show this copy"`, `"Show the kept copy"` (from `group_id`); move/rename with `dst` → `"Show where it is now"`, `"Show where it will go"` (path = `dst` if it exists else the nearest existing parent). `exists` from `os.path.exists` (attributes only, never reads contents).
  - `show_in_explorer(path: Path) -> bool` (`subprocess.Popen([system_tool("explorer"), f"/select,{path}"])`; a folder without `/select` when it is a drive root); `open_path(path: Path) -> bool` (`QDesktopServices.openUrl(QUrl.fromLocalFile(...))`); `needs_download(path: Path) -> bool` (`not onedrive.is_on_device(onedrive.get_sync_status(path))` for files).
  - `PlanTable` hooks replaceable in tests: `self.reveal: Callable[[Path], bool]`, `self.open_file: Callable[[Path], bool]`, `self.confirm: Callable[[str, str], bool]`; `PlanTable.context_menu_at(pos: QPoint) -> QMenu | None` (built, not shown, so tests can read `menu.actions()`).
- [ ] **Step 1: Failing tests** (fixture tree under `tmp_path`):
  - trash action on an existing file → one target `"Show in File Explorer"`, exists True.
  - duplicates move action → labels `["Show this copy", "Show the kept copy"]` with the keeper path second.
  - move whose `dst` folder doesn't exist → `"Show where it will go"` path is the nearest existing parent.
  - source deleted → its target `exists` False and the menu action is disabled.
  - folder-tree folder row (no action) → `"Show in File Explorer"` for `node.path`.
  - menu also has `"Open"` and `"Copy path"`; triggering "Show this copy" calls `table.reveal` with the source; "Open" on a path where `needs_download` is patched True calls `table.confirm` first and does not open on False.
  - `system_tool("explorer")` ends with `explorer.exe`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; `view.setContextMenuPolicy(CustomContextMenu)`, `customContextMenuRequested` → `context_menu_at(pos).exec(globalPos)`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `gui: right-click to show a row's files in File Explorer`.

### Task 4: Remembered "Show all settings" preference

**Files:**
- Modify: `src/various_tools/gui/state.py`
- Test: `tests/gui/test_app.py` (or new `tests/gui/test_state_show_all.py`)

**Interfaces:**
- Produces: `AppState.show_all_settings() -> bool` (default False; stored under `"show_all_settings"` in `gui_state.json`, not gated by `remember_last_folders`); `AppState.set_show_all_settings(on: bool) -> None` (writes, then calls listeners); `AppState.on_show_all_changed(listener: Callable[[bool], None]) -> None`. Refactor the atomic write in `remember_options` into `_write_gui_state(data)`.
- [ ] **Step 1: Failing tests:** default False; set True → new `AppState` on the same paths reads True; listener receives True; `last_options` still works alongside; unreadable file → False.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `gui: remember the Show all settings switch`.

### Task 5: Step-by-step tool page and app-wide switch

**Files:**
- Modify: `src/various_tools/gui/tool_page.py`, `src/various_tools/gui/main_window.py`, `src/various_tools/gui/widgets/schema_form.py` (button text "More settings for this tool")
- Modify: each tool class (13 files under `src/various_tools/tools/*/tool.py`): add `scan_label` and `found_nouns`
- Test: `tests/gui/test_tool_pages.py`, `tests/gui/test_main_window.py`, `tests/gui/test_schema_form.py`

**Interfaces:**
- Consumes: Task 1 helpers, `PlanTable.set_full_controls`, `add_toolbar_widget(..., full_only=True)`, Task 4 state API.
- Produces: `ToolPage.show_all_check: QCheckBox` ("Show all settings"); `ToolPage.set_show_all(on: bool) -> None`; `ToolPage.results: QWidget` (hidden until the first scan finishes); step labels `ToolPage.step_labels: list[QLabel]` with texts `"1  Choose where to look"`, `f"2  {scan_label(tool)}"`, `"3  Check the list, then confirm"`.

Per-tool words (`scan_label`, `found_nouns`):

| tool | scan_label | found_nouns |
|---|---|---|
| empty_folders | Look for empty folders | empty folder / empty folders |
| duplicates | Find duplicates | duplicate / duplicates |
| disk_usage | Find what takes space | large item / large items |
| temp_cleaner | Look for temporary files | temporary file / temporary files |
| media_convert | Find files to convert | file to convert / files to convert |
| corrupt_media | Check photos and videos | damaged file / damaged files |
| media_resize | Find files to resize | file to resize / files to resize |
| metadata_strip | Find files with hidden details | file to clean / files to clean |
| organize_by_date | Plan the folders by date | file to move / files to move |
| rename | Preview new names | file to rename / files to rename |
| sort_by_type | Plan the folders by type | file to move / files to move |
| checksum | Check the files | file / files |
| project_export | Preview the export | file / files |

- [ ] **Step 1: Failing tests**
  - New page: `page.results.isVisibleTo(page)` False; after a scan True. `scan_button.text() == "Look for empty folders"` for empty_folders.
  - After scan with 2 trash rows checked: `apply_button.text() == "Move 2 to the Recycle Bin…"`; status text starts with `"Found 2 "`.
  - `show_all_check` off: form's advanced section collapsed, `table.kind_combo` hidden, Select all hidden; on: advanced section open, those visible.
  - Main window: ticking the switch on page A → page B (already open) and page C (opened after) are on; `window.log_panel.isVisibleTo(window)` follows the switch; value survives a new `AppState`.
  - Update existing tests that read `"Advanced options"` or `APPLY_TEXT`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement: step labels around the form, the action row and results; the switch sits in the action row; `Select all/none` added with `full_only=True`; `_update_buttons` uses `apply_label`; `_scan_done` uses `summary_text` and shows `results`. MainWindow: `state.on_show_all_changed` → every page's `set_show_all` and `log_panel.setVisible`; new pages start from `state.show_all_settings()`. A page's checkbox calls `state.set_show_all_settings`.
- [ ] **Step 4:** `uv run pytest tests/gui -q` → PASS.
- [ ] **Step 5:** Commit `gui: step-by-step tool page with one Show all settings switch`.

### Task 6: Open-source scrub

**Files:**
- Modify: `tests/fixtures/legacy_reference/python-find_duplicates.py:76` (owner-approved one-line exception to the read-only rule): `BASE = os.path.join(os.path.expanduser("~"), "OneDrive", "Pictures")`, keeping the comment; make sure `os` is imported there.
- Create: `tests/test_no_personal_paths.py`

**Interfaces:** none.
- [ ] **Step 1: Failing test:** for every file from `git ls-files` (skip binaries by extension: png, ico, exe, lock), no match of `(?i)[a-z]:[\\/]+users[\\/]+(?!<you>|me\b|me too\b|name~1|public\b|default\b|you\b)[a-z0-9._-]+` with a real account name. Skip with `pytest.skip` when git is unavailable.
- [ ] **Step 2:** Run → FAIL on the legacy line.
- [ ] **Step 3:** Change that line. Run the duplicates parity tests: `uv run pytest tests/tools/duplicates -q`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Remove the last machine-specific path; guard against new ones`.

### Task 7: Docs, screenshots, version, full check

**Files:** `CHANGELOG.md`, `README.md` (UI description), `pyproject.toml` (version 0.4.0), `docs/images/*` via `packaging/make_screenshots.py`, `AGENTS.md`/`CLAUDE.md` (note the scrub exception and the wording module).
- [ ] **Step 1:** CHANGELOG entry "Version 0.4.0: a step-by-step tool page" listing each spec goal, the builder calls (step 1 holds folders and basic options together), and that the old path remains in git history.
- [ ] **Step 2:** Regenerate screenshots; check them visually.
- [ ] **Step 3:** Run all four checks, plus `uv run various-tools --help`; all green.
- [ ] **Step 4:** Commit `Version 0.4.0`.
