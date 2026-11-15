"""One page per tool: options form, presets, Scan, the plan table, Apply, Export and Undo.

Everything here is generic. The page is built from the tool's options model through
:class:`~various_tools.gui.widgets.schema_form.SchemaForm`. The only per-tool touches are the
duplicates compare view (``COMPARE_TOOLS``) and the rename live preview (``LIVE_PREVIEW_TOOLS``).

Optional tool attributes (capabilities, read with ``getattr``; no tool id is ever checked):

* ``plan_view = "tree"``: the plan opens in the folder-tree view (the person can switch);
* ``size_tree(plan)``: a "Size map" tab next to the plan, a treemap of that tree;
* ``extras``: :class:`~various_tools.tools.base.ToolExtra` items, one button each, run on a file
  the person picks, in a worker with a normal run context.

A plan whose scan skipped cloud-only files (``Plan.cloud_only``) gets a banner with "Download
these N files…", which asks first and downloads in a cancellable worker.

Safety (AGENTS.md): Scan never changes the disk. Apply is a separate button that always asks for
confirmation, and asks a second time when a selected action is a permanent delete. Every apply
runs through the tool's ``apply()``, which journals each action, so History can undo it.

Apply works on a copy of the plan taken before the confirmation, so nothing the person does while
it runs (check boxes, Select all, "Keep this one") can change what the worker is doing. While an
apply is being confirmed or runs, plan editing is switched off, the compare dialog is closed, and
no scan (not even the rename live preview) can start.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import shiboken6
from pydantic import ValidationError
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from various_tools.cli.clipboard import (
    ClipboardText,
    read_for_clipboard,
    wants_clipboard,
    written_file,
)
from various_tools.cli.toolcmds import download_actions, is_download
from various_tools.core import onedrive
from various_tools.core.deps import TOOL_SPECS, install_hint
from various_tools.core.journal import Journal, UndoReport, undo_run
from various_tools.core.plan import ActionKind, Plan, RunResult
from various_tools.core.report import action_sizes
from various_tools.gui.state import AppState
from various_tools.gui.undo_rules import (
    UndoInfo,
    in_progress_message,
    read_undo_info,
    stale_after,
)
from various_tools.gui.widgets.plan_table import FOLDER_TREE, PlanTable
from various_tools.gui.widgets.schema_form import SchemaForm
from various_tools.gui.widgets.size_map import SizeMapView, actions_under
from various_tools.gui.workers import Worker, tool_worker
from various_tools.tools.base import Tool, ToolOptions

log = logging.getLogger(__name__)

NO_PRESET = "(defaults)"
LIVE_PREVIEW_TOOLS = frozenset({"rename"})
COMPARE_TOOLS = frozenset({"duplicates"})
LIVE_PREVIEW_DELAY_MS = 700

Confirm = Callable[[str, str], bool]
Inform = Callable[[str, str], None]
AskText = Callable[[str, str, str], str | None]
ChooseExport = Callable[[], tuple[str, str] | None]
ChooseExtraPath = Callable[[Any], str | None]  # a tool extra -> the chosen file, or None


def missing_programs(notes: list[str]) -> list[str]:
    """External programs (keys of ``TOOL_SPECS``) that the plan notes say are missing."""
    found: list[str] = []
    for name in TOOL_SPECS:
        word = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        for note in notes:
            lowered = note.casefold()
            if word.search(note) and ("not found" in lowered or "needs" in lowered):
                found.append(name)
                break
    return found


def apply_summary(plan: Plan, permanent_delete: bool) -> str:
    """The confirmation text shown before Apply."""
    selected = plan.selected_actions()
    downloads = download_actions(plan)
    counts = Counter(str(a.kind) for a in selected if not is_download(a))
    lines = [f"Apply {len(selected)} selected action(s)?", ""]
    lines += [f"  {kind}: {count}" for kind, count in sorted(counts.items())]
    if downloads:
        lines.append(f"  download from OneDrive: {len(downloads)}")
        lines += [
            "",
            f"This will download {len(downloads)} cloud-only file(s) from OneDrive to this "
            "device as part of this apply. That uses disk space and network.",
        ]
    lines.append("")
    if counts.get(str(ActionKind.TRASH)):
        lines.append("Trashed items go to the Recycle Bin, where you can restore them.")
    if permanent_delete:
        lines.append("Some items will be PERMANENTLY deleted. You will be asked again.")
    lines.append("Moves and renames can be undone from History.")
    return "\n".join(lines)


def undo_info_from_result(result: RunResult) -> UndoInfo:
    """What the undo rule needs, straight from an apply's outcomes (no journal read)."""
    done = Counter(o.kind for o in result.outcomes if o.status == "done")
    return UndoInfo(result.run_id, result.status, dict(done))


def result_summary(result: RunResult) -> str:
    return (
        f"Run {result.run_id} {result.status}: {result.count('done')} done, "
        f"{result.count('skipped')} skipped, {result.count('failed')} failed."
    )


def _default_confirm(parent: QWidget) -> Confirm:
    def confirm(title: str, text: str) -> bool:
        answer = QMessageBox.question(
            parent,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    return confirm


def _default_inform(parent: QWidget) -> Inform:
    def inform(title: str, text: str) -> None:
        QMessageBox.information(parent, title, text)

    return inform


def _default_ask_text(parent: QWidget) -> AskText:
    def ask(title: str, label: str, text: str) -> str | None:
        value, ok = QInputDialog.getText(parent, title, label, text=text)
        return value if ok else None

    return ask


def _default_choose_extra_path(parent: QWidget) -> ChooseExtraPath:
    def choose(extra: Any) -> str | None:
        title = str(extra.label)
        if extra.path_kind == "save_file":
            path, _ = QFileDialog.getSaveFileName(parent, title, "", extra.file_filter)
        else:
            path, _ = QFileDialog.getOpenFileName(parent, title, "", extra.file_filter)
        return path or None

    return choose


def _default_choose_export(parent: QWidget) -> ChooseExport:
    def choose() -> tuple[str, str] | None:
        from various_tools.core.report import EXPORT_FILTER

        path, selected_filter = QFileDialog.getSaveFileName(parent, "Export", "", EXPORT_FILTER)
        return (path, selected_filter) if path else None

    return choose


class ToolPage(QWidget):
    """The page for one tool. Emits ``runApplied(run_id)`` after an apply was journaled and
    ``runUndone(run_id)`` after this page undid a run."""

    runApplied = Signal(int)
    runUndone = Signal(int)
    busyChanged = Signal(bool)
    clipboardCopied = Signal(str)

    def __init__(self, state: AppState, tool: Tool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.tool = tool
        self.plan: Plan | None = None
        self.applied_plan: Plan | None = None  # the copy the last apply worked on
        self.result: RunResult | None = None
        self.last_run_id: int | None = None
        self.undo_info: UndoInfo | None = None  # whether last_run_id can still be undone
        self.worker: Worker | None = None
        self.compare_dialog: QWidget | None = None
        self._applied = False
        self._live = False  # the running job is a live-preview scan; the form stays editable
        self._apply_pending = False  # the Apply confirmation is open
        self._closing = False  # the window is closing: no new jobs, no message boxes

        # Hooks that tests replace so no modal dialog blocks.
        self.confirm: Confirm = _default_confirm(self)
        self.inform: Inform = _default_inform(self)
        self.ask_text: AskText = _default_ask_text(self)
        self.choose_export: ChooseExport = _default_choose_export(self)
        self.choose_extra_path: ChooseExtraPath = _default_choose_extra_path(self)

        # ----- header and presets ------------------------------------------------------
        title = QLabel(f"<h2>{tool.name}</h2>")
        description = QLabel(tool.description)
        description.setWordWrap(True)
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(200)
        self.preset_combo.setToolTip("Load a saved set of options for this tool.")
        self.save_preset_button = QPushButton("Save preset…")
        self.delete_preset_button = QPushButton("Delete preset")
        presets_row = QHBoxLayout()
        presets_row.addWidget(QLabel("Preset:"))
        presets_row.addWidget(self.preset_combo, 1)
        presets_row.addWidget(self.save_preset_button)
        presets_row.addWidget(self.delete_preset_button)

        # ----- options form ------------------------------------------------------------
        self.form = SchemaForm(
            tool.Options, self._initial_values(), load_choices=self._choices_worker
        )
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_scroll.setWidget(self.form)

        # ----- actions -----------------------------------------------------------------
        self.scan_button = QPushButton("Scan (preview)")
        self.scan_button.setToolTip("Find what would change. Nothing on disk changes.")
        self.scan_button.setDefault(True)
        self.cancel_button = QPushButton("Cancel")
        self.apply_button = QPushButton("Apply…")
        self.apply_button.setToolTip("Carry out the checked actions. Asks for confirmation first.")
        self.export_button = QPushButton("Export…")
        self.export_button.setToolTip(
            "Save the plan (or the applied result) as CSV, Excel or HTML."
        )
        self.compare_button = QPushButton("Compare group…")
        self.compare_button.setToolTip(
            "Show the selected duplicate group side by side, change which copy is kept, "
            "or explain a match."
        )
        self.compare_button.setVisible(tool.id in COMPARE_TOOLS)
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.setToolTip(
            "Check every action, including ones the tool left unchecked. Apply still asks first."
        )
        self.select_none_button = QPushButton("Select none")
        self.select_none_button.setToolTip("Uncheck every action.")
        self.undo_button = QPushButton("Undo this run")
        self.undo_button.setToolTip("Move and rename everything this run changed back.")
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.status_label = QLabel("Choose options, then Scan to preview.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        buttons = QHBoxLayout()
        for button in (
            self.scan_button,
            self.cancel_button,
            self.apply_button,
            self.export_button,
            self.compare_button,
            self.select_all_button,
            self.select_none_button,
            self.undo_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)

        # ----- missing program banner --------------------------------------------------
        self.missing_banner = QFrame()
        self.missing_banner.setFrameShape(QFrame.Shape.StyledPanel)
        self._missing_layout = QHBoxLayout(self.missing_banner)
        self.missing_label = QLabel()
        self.missing_label.setWordWrap(True)
        self.missing_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._missing_layout.addWidget(self.missing_label, 1)
        self.locate_buttons: dict[str, QPushButton] = {}
        self.missing_banner.setVisible(False)

        # ----- cloud-only banner (AGENTS.md rule 3: download only when asked) -----------
        self.cloud_banner = QFrame()
        self.cloud_banner.setFrameShape(QFrame.Shape.StyledPanel)
        cloud_layout = QHBoxLayout(self.cloud_banner)
        self.cloud_label = QLabel()
        self.cloud_label.setWordWrap(True)
        self.download_button = QPushButton("Download these files…")
        self.download_button.setToolTip(
            "Ask OneDrive to download the skipped files to this device, so the next scan can "
            "read them. Asks first."
        )
        cloud_layout.addWidget(self.cloud_label, 1)
        cloud_layout.addWidget(self.download_button)
        self.cloud_banner.setVisible(False)

        # ----- tool extras (for example catalog export and import) ----------------------
        self.extras: tuple[Any, ...] = tuple(getattr(tool, "extras", ()) or ())
        self.extra_buttons: dict[str, QPushButton] = {}
        extras_row = QHBoxLayout()
        for extra in self.extras:
            button = QPushButton(f"{extra.label}…")
            button.setToolTip(extra.description)
            button.clicked.connect(lambda _c=False, e=extra: self.run_extra(e))
            extras_row.addWidget(button)
            self.extra_buttons[extra.id] = button
        extras_row.addStretch(1)

        # ----- results -----------------------------------------------------------------
        self.notes_view = QPlainTextEdit()
        self.notes_view.setReadOnly(True)
        self.notes_view.setMaximumHeight(90)
        self.notes_view.setPlaceholderText("Scan notes appear here.")
        self.table = PlanTable()
        if getattr(tool, "plan_view", None) == FOLDER_TREE:
            self.table.set_view_mode(FOLDER_TREE)
        # A tool that can build a size tree from its plan gets a "Size map" tab (plan 7.1).
        self.size_map: SizeMapView | None = None
        results: QWidget = self.table
        if callable(getattr(tool, "size_tree", None)):
            self.size_map = SizeMapView()
            self.results_tabs = QTabWidget()
            self.results_tabs.addTab(self.table, "Plan")
            self.results_tabs.addTab(self.size_map, "Size map")
            self.size_map.folderChosen.connect(self._size_map_chose)
            self.size_map.tickRequested.connect(self.tick_folder)
            results = self.results_tabs

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(title)
        top_layout.addWidget(description)
        top_layout.addLayout(presets_row)
        top_layout.addWidget(form_scroll, 1)
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addLayout(buttons)
        if self.extras:
            bottom_layout.addLayout(extras_row)
        bottom_layout.addWidget(self.progress_bar)
        bottom_layout.addWidget(self.status_label)
        bottom_layout.addWidget(self.missing_banner)
        bottom_layout.addWidget(self.cloud_banner)
        bottom_layout.addWidget(self.notes_view)
        bottom_layout.addWidget(results, 1)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        # ----- live preview (rename) ---------------------------------------------------
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(LIVE_PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._live_preview)

        # ----- wiring ------------------------------------------------------------------
        self.scan_button.clicked.connect(lambda _c=False: self.scan())
        self.cancel_button.clicked.connect(lambda _c=False: self.cancel())
        self.apply_button.clicked.connect(lambda _c=False: self.apply())
        self.export_button.clicked.connect(lambda _c=False: self.export())
        self.compare_button.clicked.connect(lambda _c=False: self.open_compare())
        self.undo_button.clicked.connect(lambda _c=False: self.undo_last())
        self.select_all_button.clicked.connect(lambda _c=False: self.select_all(True))
        self.select_none_button.clicked.connect(lambda _c=False: self.select_all(False))
        self.download_button.clicked.connect(lambda _c=False: self.download_cloud_only())
        self.save_preset_button.clicked.connect(lambda _c=False: self.save_preset())
        self.delete_preset_button.clicked.connect(lambda _c=False: self.delete_preset())
        self.preset_combo.activated.connect(self._preset_chosen)
        self.form.changed.connect(self._form_changed)
        self.table.selectionChanged.connect(self._update_buttons)
        self.table.currentActionChanged.connect(lambda _a: self._update_buttons())
        self.table.filterChanged.connect(self._label_select_buttons)

        self.reload_presets()
        self._update_buttons()

    # ----- options and presets ----------------------------------------------------------------

    def _initial_values(self) -> dict[str, Any]:
        remembered = self.state.last_folders(self.tool.id)
        known = set(self.tool.Options.model_fields)
        return {name: value for name, value in remembered.items() if name in known}

    def reload_presets(self, select: str | None = None) -> None:
        current = select if select is not None else self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem(NO_PRESET)
        self.preset_combo.addItems(self.state.presets().names(self.tool.id))
        index = self.preset_combo.findText(current)
        self.preset_combo.setCurrentIndex(max(index, 0))
        self.preset_combo.blockSignals(False)
        self._update_buttons()

    def _preset_chosen(self, index: int) -> None:
        name = self.preset_combo.itemText(index)
        self.load_preset(None if name == NO_PRESET else name)

    def load_preset(self, name: str | None) -> bool:
        """Fill the form from a preset (None: the defaults). Folder fields keep their values."""
        folders = {
            k: v for k, v in self.form.raw_values().items() if k in self.form.folder_fields()
        }
        try:
            values = self.state.presets().get(self.tool.id, name) if name is not None else {}
        except KeyError as exc:
            self._set_status(str(exc), error=True)
            return False
        merged = {**folders, **values}
        self.form.set_values(merged)
        self.reload_presets(select=name or NO_PRESET)
        self._set_status(f"Loaded preset {name!r}." if name else "Options reset to the defaults.")
        return True

    def save_preset(self, name: str | None = None) -> bool:
        """Save the current options under a name (asks for one when not given)."""
        options = self._build_options()
        if options is None:
            return False
        if name is None:
            current = self.preset_combo.currentText()
            name = self.ask_text(
                "Save preset", "Preset name:", "" if current == NO_PRESET else current
            )
        if not name or not name.strip():
            return False
        try:
            self.state.presets().save(self.tool.id, name, options)
        except (OSError, ValueError) as exc:
            self.inform("Save preset", f"Could not save the preset: {exc}")
            return False
        self.reload_presets(select=name.strip())
        self._set_status(f"Saved preset {name.strip()!r}.")
        return True

    def delete_preset(self) -> bool:
        name = self.preset_combo.currentText()
        if name == NO_PRESET:
            return False
        if not self.confirm("Delete preset", f"Delete the preset {name!r}?"):
            return False
        try:
            self.state.presets().delete(self.tool.id, name)
        except (OSError, ValueError) as exc:
            self.inform("Delete preset", f"Could not delete the preset: {exc}")
            return False
        self.reload_presets(select=NO_PRESET)
        self._set_status(f"Deleted preset {name!r}.")
        return True

    def set_folder(self, path: str) -> bool:
        """Put a folder into the first folder field (used by ``--folder`` and the shell menu)."""
        return self.form.set_first_folder(path)

    def _build_options(self) -> ToolOptions | None:
        try:
            return cast(ToolOptions, self.form.build())
        except ValidationError:
            self._set_status("Some options are not valid; see the messages in the form.", True)
            return None

    def _remember_folders(self) -> None:
        values = self.form.raw_values()
        self.state.remember_folders(
            self.tool.id, {name: values.get(name) for name in self.form.folder_fields()}
        )

    # ----- scan -------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.worker is not None

    def scan(self, *, quiet: bool = False) -> Worker | None:
        """Scan in a worker and show the plan. Never changes the disk.

        Refused while busy, while an apply is being confirmed, and while the window closes.
        """
        if self.busy or self._apply_pending or self._closing:
            return None
        options = self._build_options()
        if options is None:
            return None
        self._remember_folders()
        tool = self.tool

        def job(ctx: Any) -> tuple[Plan, list[int | None]]:
            plan = tool.scan(options, ctx)
            ctx.check_cancelled()
            ctx.report(0, 0, "measuring file sizes")
            return plan, action_sizes(plan)

        worker = tool_worker(self.state, job, with_journal=False)
        worker.succeeded.connect(self._scan_done)
        self._live = quiet
        self._start(worker, "Scanning…" if not quiet else "Updating the preview…")
        return worker

    def _scan_done(self, value: object) -> None:
        plan, sizes = value  # type: ignore[misc]
        assert isinstance(plan, Plan)
        self.plan = plan
        self.result = None
        self._applied = False
        self.table.set_plan(plan, self.tool.id, sizes)  # type: ignore[arg-type]
        self.notes_view.setPlainText("\n".join(plan.notes))
        self._show_missing(missing_programs(plan.notes))
        self._show_cloud_only(plan)
        self._show_size_map(plan)
        count = len(plan.actions)
        if count:
            self._set_status(
                f"Preview ready: {count} action(s). Uncheck anything you want to keep, then Apply."
            )
        else:
            self._set_status("Nothing to do: the scan found no actions.")

    def _applying(self) -> bool:
        """An apply is being confirmed or is running."""
        return self._apply_pending or (self.worker is not None and self.worker.writes)

    def _form_changed(self) -> None:
        if self.tool.id in LIVE_PREVIEW_TOOLS and not self._applying() and not self._closing:
            self._preview_timer.start()

    def _live_preview(self) -> None:
        """Rename live preview: re-scan (read-only) after the options stop changing.

        Never during an apply (pending or running): the preview is dropped, not postponed.
        """
        if self._applying() or self._closing:
            return
        if self.busy:
            self._preview_timer.start()
            return
        values = self.form.raw_values()
        if not any(values.get(name) for name in self.form.folder_fields()):
            return
        self.scan(quiet=True)

    def select_all(self, selected: bool = True) -> int:
        """Check (or uncheck) every action of the plan. Returns how many changed.

        While a filter is active only the rows it shows change ("Select all shown"), so a
        filtered view never ticks rows the person cannot see.
        """
        if self.plan is None or self._applied or self.busy or self._apply_pending:
            return 0
        if self.table.filter_active():
            changed = self.table.set_shown_checked(selected)
        else:
            changed = self.plan.select_all(selected)
        self.table.refresh()
        self._update_buttons()
        return changed

    def _label_select_buttons(self, filtered: bool) -> None:
        """Say on the buttons whether they act on every row or only on the shown ones."""
        if filtered:
            self.select_all_button.setText("Select all shown")
            self.select_all_button.setToolTip(
                "Check every action the filter shows, including ones the tool left unchecked. "
                "Hidden rows are not changed. Apply still asks first."
            )
            self.select_none_button.setText("Select none shown")
            self.select_none_button.setToolTip(
                "Uncheck every action the filter shows. Hidden rows are not changed."
            )
        else:
            self.select_all_button.setText("Select all")
            self.select_all_button.setToolTip(
                "Check every action, including ones the tool left unchecked. Apply still asks "
                "first."
            )
            self.select_none_button.setText("Select none")
            self.select_none_button.setToolTip("Uncheck every action.")

    # ----- apply ------------------------------------------------------------------------------

    def apply(self) -> Worker | None:
        """Ask for confirmation, then apply the checked actions in a worker (journaled).

        The worker gets a copy of the plan taken before the question, so later edits to the
        shown plan cannot reach it. Editing and scans are off until the apply has finished.
        """
        if self.busy or self.plan is None or self._applied or self._apply_pending:
            return None
        if self._closing:
            return None
        self._preview_timer.stop()
        self._apply_pending = True
        self._update_buttons()
        try:
            return self._confirm_and_apply(copy.deepcopy(self.plan))
        finally:
            self._apply_pending = False
            self._update_buttons()

    def _confirm_and_apply(self, plan: Plan) -> Worker | None:
        selected = plan.selected_actions()
        if not selected:
            self.inform("Apply", "No action is checked, so there is nothing to apply.")
            return None
        permanent = any(a.kind is ActionKind.DELETE for a in selected)
        if not self.confirm(f"Apply: {self.tool.name}", apply_summary(plan, permanent)):
            self._set_status("Apply cancelled; nothing changed.")
            return None
        if permanent:
            deletes = sum(1 for a in selected if a.kind is ActionKind.DELETE)
            if not self.confirm(
                "PERMANENT DELETE",
                f"{deletes} item(s) will be erased for good. They do not go to the Recycle Bin "
                "and cannot be undone.\n\nReally delete them permanently?",
            ):
                self._set_status("Apply cancelled; nothing changed.")
                return None
        if self._closing:
            return None
        self._close_compare()
        tool = self.tool
        self.applied_plan = plan

        def job(ctx: Any) -> RunResult:
            return tool.apply(plan, ctx)

        worker = tool_worker(self.state, job, with_journal=True)
        worker.succeeded.connect(self._apply_done)
        self._start(worker, "Applying…")
        return worker

    def _apply_done(self, value: object) -> None:
        assert isinstance(value, RunResult)
        self.result = value
        self.last_run_id = value.run_id
        self.undo_info = undo_info_from_result(value)
        self._applied = True
        text = result_summary(value)
        failures = [o for o in value.outcomes if o.status == "failed"][:10]
        if failures:
            text += "\n\nFailed:\n" + "\n".join(f"  {o.src}: {o.detail}" for o in failures)
        self._set_status(text.splitlines()[0] + " Scan again to see what is left.")
        self.runApplied.emit(value.run_id)
        self._update_buttons()
        if self._closing:
            return  # no message box while the window waits to close
        applied = self.applied_plan
        if applied is not None and wants_clipboard(applied):
            self._copy_to_clipboard(value)
        self.inform("Apply finished", text)

    # ----- copy to clipboard ------------------------------------------------------------------

    def _copy_to_clipboard(self, result: RunResult) -> Worker | None:
        """``copy_to_clipboard``: read the written file in a worker, then copy it (UI thread)."""
        path = written_file(result)
        if path is None:
            self._set_status("Nothing was copied to the clipboard: the run wrote no file.")
            return None

        def job(_worker: Worker) -> ClipboardText:
            return read_for_clipboard(path)

        worker = Worker(job)
        worker.succeeded.connect(self._clipboard_ready)
        worker.failed.connect(
            lambda exc: self._set_status(f"Not copied to the clipboard: {exc}", error=True)
        )
        worker.start()
        return worker

    def _clipboard_ready(self, value: object) -> None:
        if not shiboken6.isValid(self) or not isinstance(value, ClipboardText):
            return
        if value.text is None:
            self._set_status(value.message, error=True)
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            self._set_status("Not copied: there is no clipboard.", error=True)
            return
        clipboard.setText(value.text)
        self._set_status(value.message)
        self.clipboardCopied.emit(value.message)

    # ----- undo -------------------------------------------------------------------------------

    def can_undo(self) -> bool:
        """The shared rule (``undo_rules``) for the run this page applied last."""
        info = self.undo_info
        return self.last_run_id is not None and info is not None and info.undoable

    def undo_last(self) -> Worker | None:
        """Undo the run this page applied last (the same as Undo in History)."""
        run_id = self.last_run_id
        if self.busy or run_id is None or self._apply_pending or self._closing:
            return None
        if not self.can_undo():
            return None
        if not self.confirm(
            "Undo",
            f"Undo run {run_id}?\n\nMoves and renames are put back, and items sent to the "
            "Recycle Bin are restored while they are still there. Anything that cannot be put "
            "back is listed afterwards.",
        ):
            return None
        journal_path = self.state.settings.journal_path()
        protection = self.state.settings.protected_paths()
        limit = stale_after(self.state.settings.transcode_timeout_s)

        def job(_worker: Worker) -> tuple[UndoReport, UndoInfo]:
            # Checked again here, off the UI thread: never undo a run that is still applying.
            before = read_undo_info(journal_path, run_id, limit=limit)
            if before.in_progress:
                raise ValueError(in_progress_message(run_id, limit))
            with Journal(journal_path) as journal:
                report = undo_run(journal, run_id, protection=protection)
            return report, read_undo_info(journal_path, run_id, limit=limit)

        worker = Worker(job, writes=True)
        worker.succeeded.connect(self._undo_done)
        self._start(worker, "Undoing…")
        return worker

    def _undo_done(self, value: object) -> None:
        report, info = cast("tuple[UndoReport, UndoInfo]", value)
        lines = [f"Run {report.run_id}: {report.status} ({report.reverted} reverted)."]
        lines += [f"  skipped: {s}" for s in report.skipped[:10]]
        lines += [f"  failed: {f}" for f in report.failed[:10]]
        self._set_undo_info(info)
        self._set_status(lines[0])
        self.runUndone.emit(report.run_id)  # History and the other pages refresh
        if not self._closing:
            self.inform("Undo finished", "\n".join(lines))

    def refresh_undo(self, run_id: int) -> Worker | None:
        """Another page undid ``run_id``: read the journal again (in a worker) for the button."""
        if run_id != self.last_run_id or self._closing:
            return None
        journal_path = self.state.settings.journal_path()
        limit = stale_after(self.state.settings.transcode_timeout_s)
        worker = Worker(lambda _worker: read_undo_info(journal_path, run_id, limit=limit))
        worker.succeeded.connect(self._undo_info_read)
        worker.start()
        return worker

    def _undo_info_read(self, value: object) -> None:
        if shiboken6.isValid(self) and isinstance(value, UndoInfo):
            self._set_undo_info(value)

    def _set_undo_info(self, info: UndoInfo) -> None:
        if info.run_id != self.last_run_id:
            return
        self.undo_info = info
        if not info.undoable:
            self.last_run_id = None
        self._update_buttons()

    # ----- export -----------------------------------------------------------------------------

    def export(self, path: str | None = None, fmt: str | None = None) -> Worker | None:
        """Export the plan, or the applied result, to CSV, Excel, HTML or a legacy format."""
        from various_tools.core.report import (
            export_plan,
            export_result,
            format_for,
            format_from_filter,
        )

        plan, result = self.plan, self.result
        if self.busy or plan is None:
            return None
        if path is None:
            chosen = self.choose_export()
            if chosen is None:
                return None
            path, selected_filter = chosen
            fmt = format_from_filter(selected_filter)
        target = Path(path)
        try:
            fmt = format_for(target, fmt)
        except ValueError as exc:
            self.inform("Export", str(exc))
            return None
        sizes = self.table.sizes()
        name = self.tool.name

        def job(_worker: Worker) -> Path:
            if result is not None:
                return export_result(plan, result, target, fmt, tool_name=name)
            return export_plan(plan, target, fmt, sizes=sizes, tool_name=name)

        worker = Worker(job)
        worker.succeeded.connect(lambda written: self._set_status(f"Exported to {written}."))
        self._start(worker, "Exporting…")
        return worker

    # ----- duplicates compare view ------------------------------------------------------------

    def open_compare(self, group_id: str | None = None) -> QWidget | None:
        from various_tools.gui.widgets.compare_view import CompareDialog

        plan = self.plan
        group = group_id or self.table.current_group_id()
        if plan is None or group is None or self._applied or self._applying() or self._closing:
            return None
        self._close_compare()
        dialog = CompareDialog(plan, group, self)
        dialog.keeperChanged.connect(self._keeper_changed)
        dialog.setModal(False)
        dialog.show()
        self.compare_dialog = dialog
        return dialog

    def _close_compare(self) -> None:
        dialog = self.compare_dialog
        self.compare_dialog = None
        if dialog is not None and shiboken6.isValid(dialog):
            dialog.close()

    def _keeper_changed(self, new_group_id: str) -> Worker | None:
        """The compare dialog swapped a group's keeper: show it now, read the new sizes in a
        worker (``file_facts`` may block on a network path), then update the size column."""
        plan = self.plan
        if plan is None:
            return None
        self.table.refresh()
        self._set_status(f"Now keeping {Path(new_group_id).name}.")
        targets = [
            (index, action.src)
            for index, action in enumerate(plan.actions)
            if action.group_id == new_group_id and action.src is not None
        ]
        if not targets:
            return None

        def job(_worker: Worker) -> list[tuple[int, int | None]]:
            from various_tools.core.media.thumbs import file_facts

            return [(index, file_facts(src).size) for index, src in targets]

        worker = Worker(job)
        worker.succeeded.connect(lambda value, p=plan: self._keeper_sizes(p, value))
        worker.start()
        return worker

    def _keeper_sizes(self, plan: Plan, value: object) -> None:
        if not shiboken6.isValid(self) or self.plan is not plan:
            return  # a new scan replaced the plan meanwhile
        sizes = self.table.sizes()
        for index, size in cast("list[tuple[int, int | None]]", value):
            if index < len(sizes):
                sizes[index] = size
        self.table.refresh(sizes)

    # ----- run-time choices (editable drop-downs) ------------------------------------------------

    def _choices_worker(self, source: str) -> Worker | None:
        """A worker that lists the choices of ``source`` (for example the Ollama models)."""
        if self._closing:
            return None
        from various_tools.core.choices import dynamic_choices

        return tool_worker(self.state, lambda ctx: dynamic_choices(source, ctx), with_journal=False)

    # ----- size map (any tool with a ``size_tree(plan)`` function) --------------------------------

    def _show_size_map(self, plan: Plan) -> None:
        if self.size_map is None:
            return
        builder = getattr(self.tool, "size_tree", None)
        tree: Any = None
        if callable(builder):
            try:
                tree = builder(plan)  # pure: reads the plan's notes, never the disk
            except Exception:  # a broken size tree must not break the page
                log.exception("%s: could not build the size map", self.tool.id)
        self.size_map.set_tree(tree)

    def _size_map_chose(self, folder: object, recursive: bool) -> int:
        """A size-map tile was clicked: select the plan rows in that folder."""
        plan = self.plan
        if plan is None or not (folder is None or isinstance(folder, Path)):
            return 0
        indices = actions_under(plan, folder, recursive=recursive)
        count = self.table.select_actions(indices)
        where = folder.name if isinstance(folder, Path) else "the chosen folders"
        self._set_status(f"{count} listed item(s) in {where or folder}. See the Plan tab.")
        return count

    def tick_folder(self, folder: object) -> int:
        """Check the plan's Recycle-Bin rows inside ``folder`` (None: every root).

        Only checks rows; Apply, with its confirmation, is still what sends them to the Recycle
        Bin. Refused while busy and after the plan was applied. Returns how many were checked.
        """
        plan = self.plan
        if plan is None or self._applied or self.busy or self._apply_pending:
            return 0
        if not (folder is None or isinstance(folder, Path)):
            return 0
        indices = actions_under(plan, folder, kinds=frozenset({ActionKind.TRASH}))
        changed = 0
        for index in indices:
            if not plan.actions[index].selected:
                self.table.set_action_checked(index, True)
                changed += 1
        self.table.select_actions(indices)
        self._update_buttons()
        where = folder.name if isinstance(folder, Path) else "the chosen folders"
        if indices:
            self._set_status(
                f"Ticked {changed} more file(s) in {where or folder} ({len(indices)} listed "
                "there). Apply sends the ticked files to the Recycle Bin and asks first."
            )
        else:
            self._set_status(f"No listed file is in {where or folder}.")
        return changed

    # ----- tool extras ---------------------------------------------------------------------------

    def run_extra(self, extra: Any, path: str | None = None) -> Worker | None:
        """Run one of the tool's extra actions (``tool.extras``) on a file the person picks."""
        if self.busy or self._apply_pending or self._closing:
            return None
        chosen = path if path is not None else self.choose_extra_path(extra)
        if not chosen:
            return None
        target = Path(chosen)
        run = extra.run
        label = str(extra.label)

        def job(ctx: Any) -> str:
            return str(run(target, ctx))

        worker = tool_worker(self.state, job, with_journal=False)
        worker.succeeded.connect(lambda text, t=label: self._extra_done(t, str(text)))
        self._start(worker, f"{label}…")
        # After _start's own handler, so a ValueError's plain message is what stays shown.
        # Signals from the worker thread are queued, so nothing is missed by connecting now.
        worker.failed.connect(lambda exc, t=label: self._extra_failed(t, exc))
        return worker

    def _extra_done(self, label: str, text: str) -> None:
        self._set_status(f"{label}: {text}")
        if not self._closing:
            self.inform(label, text)

    def _extra_failed(self, label: str, exc: object) -> None:
        if isinstance(exc, ValueError):
            self._set_status(f"{label}: {exc}", error=True)
            if not self._closing:
                self.inform(label, str(exc))

    # ----- cloud-only files (AGENTS.md rule 3) -------------------------------------------------

    def _show_cloud_only(self, plan: Plan) -> None:
        count = len(plan.cloud_only)
        if not count:
            self.cloud_banner.setVisible(False)
            return
        files = "file is" if count == 1 else "files are"
        self.cloud_label.setText(
            f"{count} {files} cloud-only and were skipped: they are in OneDrive but not on this "
            "device, so they were not read."
        )
        self.download_button.setText(f"Download these {count} files…")
        self.cloud_banner.setVisible(True)

    def download_cloud_only(self) -> Worker | None:
        """Ask, then have OneDrive download the files the scan skipped (cancellable)."""
        plan = self.plan
        if plan is None or not plan.cloud_only or self.busy or self._apply_pending:
            return None
        if self._closing:
            return None
        # Files only: downloading a folder would pin and download everything inside it, far
        # more than the count the question shows. Reading the attributes downloads nothing.
        paths = [p for p in plan.cloud_only if not os.path.isdir(p)]
        folders = len(plan.cloud_only) - len(paths)
        skipped = (
            f"\n\n{folders} folder(s) in the list are not downloaded: download files only."
            if folders
            else ""
        )
        count = len(paths)
        if not count:
            self._set_status(
                f"Nothing was downloaded: the list holds only {folders} folder(s), and only "
                "files are downloaded."
            )
            return None
        if not self.confirm(
            "Download cloud-only files",
            f"Download {count} cloud-only file(s) from OneDrive to this device?\n\n"
            "They take space on this disk afterwards. Nothing else is changed, and you can "
            f"cancel at any time.{skipped}",
        ):
            self._set_status("Nothing was downloaded.")
            return None
        wait = getattr(self.state.settings, "download_wait_seconds", None)

        def job(worker: Worker) -> list[tuple[Path, bool, str]]:
            results: list[tuple[Path, bool, str]] = []
            for done, path in enumerate(paths):
                worker.check_cancelled()
                worker.report(done, count, f"Downloading {path.name}")
                if isinstance(wait, int):
                    ok, detail = onedrive.force_download(path, wait_seconds=wait)
                else:
                    ok, detail = onedrive.force_download(path)
                results.append((path, ok, detail))
            worker.report(count, count, "Download finished")
            return results

        worker = Worker(job, writes=True)
        worker.succeeded.connect(self._download_done)
        self._start(worker, f"Downloading {count} file(s)…")
        return worker

    def _download_done(self, value: object) -> None:
        results = cast("list[tuple[Path, bool, str]]", value)
        ok = sum(1 for _p, good, _d in results if good)
        failed = [(p, d) for p, good, d in results if not good]
        log.info("%s: downloaded %d of %d cloud-only file(s)", self.tool.id, ok, len(results))
        for path, detail in failed:
            log.warning("%s: not downloaded: %s (%s)", self.tool.id, path, detail)
        summary = f"Downloaded {ok} of {len(results)} file(s)."
        if failed:
            summary += f" {len(failed)} could not be downloaded; see the log."
        self._set_status(summary)
        if self._closing:
            return
        if self.confirm("Download finished", f"{summary}\n\nScan again to include them?"):
            # The download worker is still winding down; scan once it has finished.
            QTimer.singleShot(0, self._scan_after_download)

    def _scan_after_download(self) -> None:
        if self.busy:
            QTimer.singleShot(50, self._scan_after_download)
            return
        self.scan()

    # ----- missing programs -------------------------------------------------------------------

    def _show_missing(self, programs: list[str]) -> None:
        for button in self.locate_buttons.values():
            self._missing_layout.removeWidget(button)
            button.deleteLater()
        self.locate_buttons = {}
        if not programs:
            self.missing_banner.setVisible(False)
            return
        hints = ", ".join(f"{p} ({install_hint(p)})" for p in programs)
        self.missing_label.setText(
            f"Some features were skipped because a program is missing: {hints}. "
            "Locate it below or install it, then scan again."
        )
        for program in programs:
            if program == "ollama":
                continue  # an address, set in Settings
            button = QPushButton(f"Locate {program}…")
            button.clicked.connect(lambda _c=False, p=program: self.locate(p))
            self._missing_layout.addWidget(button)
            self.locate_buttons[program] = button
        self.missing_banner.setVisible(True)

    def locate(self, program: str, path: str | None = None) -> bool:
        from various_tools.gui.widgets.external_tools import locate_program

        if locate_program(self, self.state, program, path=path):
            self._set_status(f"Saved the {program} location. Scan again to use it.")
            button = self.locate_buttons.get(program)
            if button is not None:
                button.setEnabled(False)
            return True
        return False

    # ----- worker plumbing --------------------------------------------------------------------

    def _start(self, worker: Worker, message: str) -> None:
        self.worker = worker
        self.progress_bar.setRange(0, 0)
        self._set_status(message)
        worker.progress.connect(self._progress)
        worker.failed.connect(self._failed)
        worker.cancelled.connect(lambda: self._set_status("Cancelled. Nothing more was changed."))
        worker.finished.connect(self._finished)
        self.busyChanged.emit(True)
        self._update_buttons()
        worker.start()

    def _progress(self, done: int, total: int, message: str) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
        else:
            self.progress_bar.setRange(0, 0)
        if message:
            self.progress_bar.setFormat(f"%v / %m  {message[:80]}" if total > 0 else message[:80])

    def _failed(self, exc: object) -> None:
        self._set_status(f"Failed: {type(exc).__name__}: {exc}", error=True)

    def _finished(self) -> None:
        # The worker object is released by the worker registry once its thread is really done.
        self.worker = None
        self._live = False
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1 if self.plan is not None else 0)
        self.progress_bar.setFormat("%p%")
        self.busyChanged.emit(False)
        self._update_buttons()

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self._set_status("Cancelling: waiting for the current step to finish…")

    @property
    def applying(self) -> bool:
        """An apply or an undo (a job that changes files and the journal) is running."""
        return self.worker is not None and self.worker.writes

    def shutdown(self) -> None:
        """The window is closing: stop the live preview, start nothing new, cancel the job.

        Does not wait; the main window keeps itself open until every worker has finished.
        """
        self._closing = True
        self._preview_timer.stop()
        self._close_compare()
        if self.worker is not None:
            self.worker.cancel()
            self._set_status("Stopping: waiting for the current step to finish…")
        self._update_buttons()

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #c0392b;" if error else "")
        (log.warning if error else log.info)("%s: %s", self.tool.id, text.splitlines()[0])

    def _update_buttons(self) -> None:
        # "busy" also covers an Apply confirmation that is open, and a window that is closing.
        running = self.busy
        busy = running or self._apply_pending or self._closing
        has_plan = self.plan is not None
        selected = bool(self.plan.selected_actions()) if self.plan is not None else False
        self.scan_button.setEnabled(not busy)
        self.cancel_button.setEnabled(running)
        self.apply_button.setEnabled(not busy and has_plan and selected and not self._applied)
        self.export_button.setEnabled(not busy and has_plan)
        can_select = not busy and has_plan and not self._applied
        self.select_all_button.setEnabled(can_select)
        self.select_none_button.setEnabled(can_select)
        if self.size_map is not None:
            self.size_map.set_tick_enabled(can_select)
        for button in self.extra_buttons.values():
            button.setEnabled(not busy)
        self.download_button.setEnabled(
            not busy and self.plan is not None and bool(self.plan.cloud_only)
        )
        # No check-box changes while an apply is confirmed or runs (or any job runs).
        self.table.set_editable(not busy)
        self.compare_button.setEnabled(
            not busy
            and has_plan
            and not self._applied
            and self.table.current_group_id() is not None
        )
        can_undo = self.can_undo()
        self.undo_button.setVisible(can_undo)
        self.undo_button.setEnabled(not busy and can_undo)
        self.form.setEnabled(not busy or (self._live and not self._closing))
        self.preset_combo.setEnabled(not busy)
        self.save_preset_button.setEnabled(not busy)
        self.delete_preset_button.setEnabled(
            not busy and self.preset_combo.currentText() != NO_PRESET
        )
