"""One page per tool: options form, Scan, the plan table, Apply, Export and Undo.

The page reads as three numbered steps in plain words (gui/wording.py): 1 choose where to look
(the options form), 2 look (the scan button, named by the tool's ``scan_label``), 3 check the
list, then confirm (the results, hidden until the first scan). One app-wide "Show all settings"
switch (``AppState.show_all_settings``) opens every page's extra options and shows the table's
extra controls; nothing is ever removed, only folded away.

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
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import shiboken6
from pydantic import ValidationError
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QToolButton,
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
from various_tools.core.context import Pace
from various_tools.core.deps import TOOL_SPECS, install_hint
from various_tools.core.journal import Journal, UndoReport, undo_run
from various_tools.core.plan import WAIT_PAUSED_MESSAGE, ActionKind, Plan, RunResult
from various_tools.core.report import action_sizes
from various_tools.core.schema import describe, format_value
from various_tools.gui.apply_dialog import (
    PACE_KEY,
    ApplyAsk,
    ApplyDialog,
    onedrive_suggestion,
)
from various_tools.gui.state import AppState
from various_tools.gui.undo_rules import (
    UndoInfo,
    in_progress_message,
    read_undo_info,
    stale_after,
)
from various_tools.gui.widgets.plan_table import FOLDER_TREE, PlanTable
from various_tools.gui.widgets.schema_form import SchemaForm, folder_field_names
from various_tools.gui.widgets.size_map import SizeMapView, actions_under
from various_tools.gui.wording import (
    APPLY_TEXT,
    CHANGE_SETTINGS,
    CHOOSE_WORKSPACE_FIRST,
    HIDE_DETAILS,
    HIDE_SETTINGS,
    PAUSE_TEXT,
    RESUME_TEXT,
    SHOW_DETAILS,
    SKIP_WAIT_TEXT,
    STOP_TEXT,
    TICK_ALL,
    TICK_ALL_SHOWN,
    UNTICK_ALL,
    UNTICK_ALL_SHOWN,
    USE_OTHER_FOLDERS,
    WORKSPACE_CHANGED,
    apply_label,
    found_nouns,
    looks_in_text,
    plain_value,
    scan_label,
    summary_text,
)
from various_tools.gui.workers import Worker, applying_run_ids, tool_worker
from various_tools.tools.base import Tool, ToolOptions

log = logging.getLogger(__name__)

STEP_TEXTS = ("1  Choose where to look", "2  {scan}", "3  Check the list, then confirm")
# An option value never remembered between sessions (a permanent delete).
PERMANENT = "permanent"
LIVE_PREVIEW_TOOLS = frozenset({"rename"})
COMPARE_TOOLS = frozenset({"duplicates"})
LIVE_PREVIEW_DELAY_MS = 700
# The remembered flag "this tool uses its own folders, not the working folder".
OTHER_FOLDERS_KEY = "_use_other_folders"
WORKSPACE_FIELD_NAMES = frozenset({"roots", "sources", "root"})
MAX_SUMMARY_ITEMS = 8
# A note kind ("skipped: <reason>: <path>") is shown as a count once it repeats this often.
GROUP_NOTES_FROM = 3
MAX_LOOSE_NOTES = 20
_NOTE = re.compile(r"^(?P<label>[a-z][a-z ]*): (?P<reason>[^:]+?): (?P<path>.+)$")

Confirm = Callable[[str, str], bool]
Inform = Callable[[str, str], None]
ChooseExport = Callable[[], tuple[str, str] | None]
ChooseExtraPath = Callable[[Any], str | None]  # a tool extra -> the chosen file, or None
ChoosePace = Callable[[ApplyAsk], tuple[bool, Pace | None]]  # -> (confirmed, pace)


def rememberable_fields(model: type[ToolOptions]) -> list[str]:
    """The options a page may remember for next time.

    Left out, so they always start from their safe default: 🔒 locked options and options
    marked ``replaces_originals`` (they replace, remove or download originals). A
    permanent-delete value is also never remembered (see :data:`PERMANENT`).
    """
    return [s.name for s in describe(model) if not s.locked and not s.replaces_originals]


def workspace_field_name(tool: Tool) -> str | None:
    """The option that receives the working folder, or None when the tool opts out.

    The tool's ``workspace_field`` attribute wins (``None`` opts out); otherwise the first
    folder option, when it is named ``roots``, ``sources`` or ``root``.
    """
    folders = folder_field_names(tool.Options)
    if hasattr(tool, "workspace_field"):
        name = getattr(tool, "workspace_field")  # noqa: B009 (an optional capability)
        return str(name) if name is not None and str(name) in folders else None
    if folders and folders[0] in WORKSPACE_FIELD_NAMES:
        return folders[0]
    return None


def group_notes(notes: list[str]) -> tuple[list[str], bool]:
    """Short lines for the notes box: repeated ``label: reason: path`` notes become counts
    ("86,691 skipped: already smaller than the target"), biggest kind first. Returns the lines
    and whether anything was grouped."""
    counts: Counter[tuple[str, str]] = Counter()
    parsed: list[tuple[str, str] | None] = []
    for note in notes:
        match = _NOTE.match(note)
        key = (match["label"], match["reason"]) if match else None
        parsed.append(key)
        if key is not None:
            counts[key] += 1
    grouped = {key for key, n in counts.items() if n >= GROUP_NOTES_FROM}
    if not grouped:
        return list(notes), False
    lines = [
        f"{n:,} {label}: {reason}"
        for (label, reason), n in counts.most_common()
        if (label, reason) in grouped
    ]
    loose = [n for n, key in zip(notes, parsed, strict=True) if key not in grouped]
    lines += loose[:MAX_LOOSE_NOTES]
    if len(loose) > MAX_LOOSE_NOTES:
        lines.append(f"…and {len(loose) - MAX_LOOSE_NOTES:,} more notes")
    return lines, True


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
        self.choose_export: ChooseExport = _default_choose_export(self)
        self.choose_extra_path: ChooseExtraPath = _default_choose_extra_path(self)
        # The Apply window. Tests set ``choose_pace`` (or, as before, ``confirm``).
        self.choose_pace: ChoosePace = self._open_apply_dialog
        self._default_confirm_hook = self.confirm

        # ----- header ------------------------------------------------------------------
        title = QLabel(f"<h2>{tool.name}</h2>")
        description = QLabel(tool.description)
        description.setWordWrap(True)
        self.step_labels: list[QLabel] = []
        for text in STEP_TEXTS:
            label = QLabel(text.format(scan=scan_label(tool)))
            font = label.font()
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() * 1.1)
            label.setFont(font)
            self.step_labels.append(label)

        # ----- options form ------------------------------------------------------------
        # The working folder: the tool's folder option is folded into one line unless the
        # person chose other folders for this tool (remembered with the other options).
        self._ws_name = workspace_field_name(tool)
        self._override = bool(
            self._ws_name and self.state.last_options(tool.id).get(OTHER_FOLDERS_KEY) is True
        )
        self.form = SchemaForm(
            tool.Options, self._initial_values(), load_choices=self._choices_worker
        )
        self.workspace_label = QLabel()
        self.workspace_label.setWordWrap(True)
        self.other_folders_check = QCheckBox(USE_OTHER_FOLDERS)
        self.other_folders_check.setToolTip(
            "Look in folders of your own for this tool instead of the working folder."
        )
        self.workspace_row = QWidget()
        workspace_layout = QHBoxLayout(self.workspace_row)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.addWidget(self.workspace_label, 1)
        workspace_layout.addWidget(self.other_folders_check)
        self.workspace_row.setVisible(self._ws_name is not None)
        self.other_folders_check.setChecked(self._override)
        # Step 1 folds into one line after a scan.
        self.options_summary = QLabel()
        self.options_summary.setWordWrap(True)
        self.change_settings_button = QPushButton(CHANGE_SETTINGS)
        self.change_settings_button.setFlat(True)
        self.change_settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.options_summary_row = QWidget()
        summary_layout = QHBoxLayout(self.options_summary_row)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.addWidget(self.options_summary, 1)
        summary_layout.addWidget(self.change_settings_button)
        self.options_summary_row.setVisible(False)
        self.options_collapsed = False
        self.workspace_banner = QLabel(WORKSPACE_CHANGED)
        self.workspace_banner.setWordWrap(True)
        self.workspace_banner.setFrameShape(QFrame.Shape.StyledPanel)
        self.workspace_banner.setVisible(False)
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_scroll.setWidget(self.form)

        # ----- actions -----------------------------------------------------------------
        self.scan_button = QPushButton(scan_label(tool))
        self.scan_button.setToolTip("Preview what would change. Nothing on disk changes.")
        self.scan_button.setDefault(True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)  # shown only while a job runs
        self.apply_button = QPushButton(APPLY_TEXT)
        self.apply_button.setToolTip("Carry out the checked actions. Asks for confirmation first.")
        # "More": the actions that are needed now and then, out of the main row.
        self.more_button = QToolButton()
        self.more_button.setText("More")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        more_menu = QMenu(self.more_button)
        more_menu.setToolTipsVisible(True)
        self.export_button = more_menu.addAction("Export…")
        self.export_button.setToolTip(
            "Save the plan (or the applied result) as CSV, Excel or HTML."
        )
        more_menu.addSeparator()
        self.reset_options_action = more_menu.addAction("Reset options to defaults")
        self.reset_options_action.setToolTip(
            "Put every option back to its default. The folders stay as they are."
        )
        self.more_button.setMenu(more_menu)
        self.compare_button = QPushButton("Compare group…")
        self.compare_button.setToolTip(
            "Show the selected duplicate group side by side, change which copy is kept, "
            "or explain a match."
        )
        self.compare_button.setVisible(tool.id in COMPARE_TOOLS)
        self.pause_button = QPushButton(PAUSE_TEXT)
        self.pause_button.setToolTip("Hold after the current file. Resume to carry on.")
        self.resume_button = QPushButton(RESUME_TEXT)
        self.skip_wait_button = QPushButton(SKIP_WAIT_TEXT)
        self.skip_wait_button.setToolTip("Do not wait for the next batch: start it now.")
        for button in (self.pause_button, self.resume_button, self.skip_wait_button):
            button.setVisible(False)  # shown only while an apply runs
        self.show_all_check = QCheckBox("Show all settings")
        self.show_all_check.setToolTip(
            "Show every setting and control on every page: extra options, the kind filter, the "
            "view switch, Select all and the log. Remembered for next time."
        )
        self.undo_button = QPushButton("Undo this run")
        self.undo_button.setToolTip("Move and rename everything this run changed back.")
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)  # shown only while a job runs
        self.status_label = QLabel("Choose options, then Scan to preview.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Step 2: look. Step 3 (under the results): confirm.
        buttons = QHBoxLayout()
        buttons.addWidget(self.scan_button)
        buttons.addWidget(self.cancel_button)
        for button in (self.pause_button, self.resume_button, self.skip_wait_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.show_all_check)
        confirm_row = QHBoxLayout()
        for button in (self.apply_button, self.compare_button, self.undo_button):
            confirm_row.addWidget(button)
        confirm_row.addStretch(1)

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
        for extra in self.extras:
            button = QPushButton(f"{extra.label}…")
            button.setToolTip(extra.description)
            button.clicked.connect(lambda _c=False, e=extra: self.run_extra(e))
            buttons.addWidget(button)
            self.extra_buttons[extra.id] = button
        buttons.addWidget(self.more_button)

        # ----- results -----------------------------------------------------------------
        self.notes_view = QPlainTextEdit()
        self.notes_view.setReadOnly(True)
        self.notes_view.setVisible(False)  # shown only when a scan left notes
        self._notes_full: list[str] = []
        self.notes_toggle = QPushButton(SHOW_DETAILS)
        self.notes_toggle.setFlat(True)
        self.notes_toggle.setCheckable(True)
        self.notes_toggle.setVisible(False)
        self.notes_details = QPlainTextEdit()  # filled the first time it is opened
        self.notes_details.setReadOnly(True)
        self.notes_details.setVisible(False)
        self._notes_details_built = False
        self.table = PlanTable()
        # Selecting rows belongs to the results, so the buttons sit in the table's toolbar.
        self.select_all_button = self.table.tick_all_button
        self.select_none_button = self.table.untick_all_button
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
        top_layout.addWidget(self.step_labels[0])
        top_layout.addWidget(self.options_summary_row)
        top_layout.addWidget(self.workspace_row)
        top_layout.addWidget(form_scroll, 1)
        self._form_scroll = form_scroll
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self.step_labels[1])
        bottom_layout.addLayout(buttons)
        bottom_layout.addWidget(self.progress_bar)
        bottom_layout.addWidget(self.status_label)
        bottom_layout.addWidget(self.workspace_banner)
        bottom_layout.addWidget(self.missing_banner)
        bottom_layout.addWidget(self.cloud_banner)
        # Step 3, hidden until the first scan so a new page is short.
        self.results = QWidget()
        results_layout = QVBoxLayout(self.results)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.addWidget(self.step_labels[2])
        results_layout.addWidget(self.notes_view)
        results_layout.addWidget(self.notes_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        results_layout.addWidget(self.notes_details)
        results_layout.addWidget(results, 1)
        results_layout.addLayout(confirm_row)
        self.results.setVisible(False)
        bottom_layout.addWidget(self.results, 1)
        bottom_layout.addStretch(0)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        self.splitter = splitter
        self._top = top
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
        self.pause_button.clicked.connect(lambda _c=False: self.pause_apply())
        self.resume_button.clicked.connect(lambda _c=False: self.resume_apply())
        self.skip_wait_button.clicked.connect(lambda _c=False: self.skip_wait())
        self.apply_button.clicked.connect(lambda _c=False: self.apply())
        self.export_button.triggered.connect(lambda _c=False: self.export())
        self.reset_options_action.triggered.connect(lambda _c=False: self.reset_options())
        self.compare_button.clicked.connect(lambda _c=False: self.open_compare())
        self.undo_button.clicked.connect(lambda _c=False: self.undo_last())
        self.select_all_button.clicked.connect(lambda _c=False: self.select_all(True))
        self.select_none_button.clicked.connect(lambda _c=False: self.select_all(False))
        self.download_button.clicked.connect(lambda _c=False: self.download_cloud_only())
        self.show_all_check.toggled.connect(self._show_all_toggled)
        self.form.changed.connect(self._form_changed)
        self.other_folders_check.toggled.connect(self._other_folders_toggled)
        self.change_settings_button.clicked.connect(lambda _c=False: self.toggle_options())
        self.notes_toggle.toggled.connect(self._notes_toggled)
        state.on_workspace_changed(self._workspace_changed)
        self.table.selectionChanged.connect(self._update_buttons)
        self.table.currentActionChanged.connect(lambda _a: self._update_buttons())
        self.table.filterChanged.connect(self._label_select_buttons)

        self._apply_workspace_ui()
        self.set_show_all(state.show_all_settings())
        self._update_buttons()

    # ----- "Show all settings" ------------------------------------------------------------------

    def set_show_all(self, on: bool) -> None:
        """Show (or fold away) the extra options and the table's extra controls."""
        if self.show_all_check.isChecked() != on:
            self.show_all_check.blockSignals(True)
            self.show_all_check.setChecked(on)
            self.show_all_check.blockSignals(False)
        self.form.set_advanced_visible(on)
        self.table.set_full_controls(on)

    def _show_all_toggled(self, on: bool) -> None:
        self.set_show_all(on)
        self.state.set_show_all_settings(on)  # remembered, and every other page follows

    # ----- options ----------------------------------------------------------------------------

    def _initial_values(self) -> dict[str, Any]:
        remembered = self.state.last_options(self.tool.id)
        keep = set(rememberable_fields(self.tool.Options))
        values = {name: value for name, value in remembered.items() if name in keep}
        if self._ws_name and not self._override:
            values.pop(self._ws_name, None)  # the working folder decides, not an old value
        return values

    # ----- the working folder -----------------------------------------------------------------

    @property
    def uses_workspace(self) -> bool:
        """The tool has a workspace field and the person has not chosen other folders."""
        return self._ws_name is not None and not self._override

    def _apply_workspace_ui(self) -> None:
        """Show the one-line "Looks in" text or the real folder field, and fill the field."""
        name = self._ws_name
        if name is None:
            return
        paths = self.state.workspace()
        self.workspace_label.setText(
            "Looks in: folders chosen for this tool"
            if self._override
            else looks_in_text(paths, first_only=self._workspace_is_single())
        )
        self.form.hide_field(name, not self._override)
        if not self._override:
            self._put_workspace_in_field(paths)

    def _workspace_is_single(self) -> bool:
        """True when the working-folder option takes one folder (it then uses the first)."""
        name = self._ws_name
        return name is not None and self.form.field_spec(name).kind != "list"

    def _put_workspace_in_field(self, paths: list[Path]) -> None:
        name = self._ws_name
        if name is None:
            return
        is_list = self.form.field_spec(name).kind == "list"
        value: Any = [str(p) for p in paths] if is_list else (str(paths[0]) if paths else "")
        self.form.set_field_value(name, value)

    def _other_folders_toggled(self, on: bool) -> None:
        self._override = on
        self._override_session_only = False  # a tick by the person is remembered
        self._apply_workspace_ui()
        self.workspace_banner.setVisible(False)

    def _workspace_changed(self, paths: list[Path]) -> None:
        """The working folder changed. An idle page follows it; a page that already shows a
        list keeps that list (Apply stays tied to it) and says to look again."""
        if not shiboken6.isValid(self) or not self.uses_workspace:
            return
        self._apply_workspace_ui()
        if self.plan is not None:
            self.workspace_banner.setVisible(True)

    def _workspace_missing(self) -> bool:
        """No working folder, and nothing in the (hidden) folder field either."""
        name = self._ws_name
        if not self.uses_workspace or self.state.workspace() or name is None:
            return False
        return not self.form.raw_values().get(name)

    # ----- folding the options after a scan -----------------------------------------------------

    def options_summary_text(self) -> str:
        """The non-default option values in plain words (field titles, not raw names)."""
        values = self.form.raw_values()
        parts: list[str] = []
        if self.uses_workspace:
            where = looks_in_text(
                self.state.workspace(), first_only=self._workspace_is_single()
            ).removeprefix("Looks in: ")
            parts.append(f"Looking in: {where}")
        for spec in describe(self.tool.Options):
            if spec.name == self._ws_name and not self._override:
                continue
            if spec.name not in values:
                continue
            value = values[spec.name]
            if format_value(value) == format_value(spec.default):
                continue
            parts.append(f"{spec.title}: {plain_value(value)}")
        if len(parts) > MAX_SUMMARY_ITEMS:
            extra = len(parts) - MAX_SUMMARY_ITEMS
            parts = [*parts[:MAX_SUMMARY_ITEMS], f"and {extra} more"]
        return "  ·  ".join(parts) if parts else "Default settings."

    def collapse_options(self) -> None:
        """Fold step 1 into one summary line (the list gets the room)."""
        self.options_summary.setText(self.options_summary_text())
        self.change_settings_button.setText(CHANGE_SETTINGS)
        self._set_options_folded(True)
        QTimer.singleShot(0, self._give_results_room)

    def expand_options(self) -> None:
        """Unfold step 1; the summary line stays, with a link to fold it again."""
        self._set_options_folded(False)
        self.options_summary_row.setVisible(True)
        self.change_settings_button.setText(HIDE_SETTINGS)
        QTimer.singleShot(0, self._give_results_room)

    def toggle_options(self) -> None:
        if self.options_collapsed:
            self.expand_options()
        else:
            self.collapse_options()

    def _set_options_folded(self, folded: bool) -> None:
        self.options_collapsed = folded
        self.options_summary_row.setVisible(folded)
        self._form_scroll.setVisible(not folded)
        self.workspace_row.setVisible(self._ws_name is not None and not folded)

    def reset_options(self) -> None:
        """Every option back to its default; the folder fields keep what they hold."""
        values = self.form.raw_values()
        folders = {name: values[name] for name in self.form.folder_fields() if name in values}
        self.form.set_values(folders)
        self._set_status("Options reset to the defaults.")

    def set_folder(self, path: str) -> bool:
        """Put a folder into the first folder field (used by ``--folder`` and the shell menu).

        On a page that uses the working folder this chooses other folders for this tool.
        """
        if self._ws_name is not None and not self._override:
            self.other_folders_check.setChecked(True)  # shows the real field
            self._override_session_only = True  # this launch only: not remembered
        return self.form.set_first_folder(path)

    def _build_options(self) -> ToolOptions | None:
        try:
            return cast(ToolOptions, self.form.build())
        except ValidationError:
            self._set_status("Some options are not valid; see the messages in the form.", True)
            return None

    def _remember_options(self, options: ToolOptions) -> None:
        """Remember what this scan used, except options that must start from the default."""
        values = options.model_dump(mode="json")
        remembered = {
            name: values[name]
            for name in rememberable_fields(self.tool.Options)
            if name in values and values[name] != PERMANENT
        }
        if self._ws_name:
            if not self._override or self._override_session_only:
                remembered.pop(self._ws_name, None)  # the working folder is not a tool option
            if self._override_session_only:  # set by --folder or the shell menu: keep what was
                remembered[OTHER_FOLDERS_KEY] = (
                    self.state.last_options(self.tool.id).get(OTHER_FOLDERS_KEY) is True
                )
            else:
                remembered[OTHER_FOLDERS_KEY] = self._override
        self.state.remember_options(self.tool.id, remembered)

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
        if self._workspace_missing():
            self._set_status(CHOOSE_WORKSPACE_FIRST, True)
            return None
        options = self._build_options()
        if options is None:
            return None
        self._remember_options(options)
        self._scan_workspace = list(self.state.workspace())
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
        self._show_notes(plan.notes)
        # A scan that started under another working folder shows that folder's list: say so.
        self.workspace_banner.setVisible(
            self.uses_workspace and self.state.workspace() != self._scan_workspace
        )
        self._show_missing(missing_programs(plan.notes))
        self._show_cloud_only(plan)
        self._show_size_map(plan)
        if not self.results.isVisible():
            self.results.setVisible(True)
            QTimer.singleShot(0, self._give_results_room)
        self._set_status(summary_text(plan, sizes, found_nouns(self.tool)))
        if self.tool.id not in LIVE_PREVIEW_TOOLS:
            self.collapse_options()

    def _give_results_room(self) -> None:
        """After the first scan, the options keep only the height they need (at most 40% of
        the page) and the results get the rest. The splitter can still be dragged."""
        if not shiboken6.isValid(self):
            return
        total = sum(self.splitter.sizes())
        if total <= 0:
            return
        if self.options_collapsed:
            top = min(self._top.sizeHint().height(), total * 2 // 5)
        else:
            top = min(
                self._top.minimumSizeHint().height() + self.form.sizeHint().height(),
                total * 2 // 5,
            )
        self.splitter.setSizes([top, total - top])

    def _show_notes(self, notes: list[str]) -> None:
        """The scan notes, in a box only as tall as they need (up to 5 lines), or hidden.

        Repeated "skipped: <reason>: <path>" notes are shown as counts; "Show details" opens
        the full text, built only when asked (there can be 100,000 lines).
        """
        shown, grouped = group_notes(notes)
        self._notes_full = notes
        self._notes_details_built = False
        self.notes_toggle.blockSignals(True)
        self.notes_toggle.setChecked(False)
        self.notes_toggle.setText(SHOW_DETAILS)
        self.notes_toggle.blockSignals(False)
        self.notes_toggle.setVisible(grouped)
        self.notes_details.clear()
        self.notes_details.setVisible(False)
        self.notes_view.setPlainText("\n".join(shown))
        self.notes_view.setVisible(bool(notes))
        lines = min(max(len(shown), 1), 5)
        line = self.notes_view.fontMetrics().lineSpacing()
        margins = 2 * self.notes_view.frameWidth() + 12
        self.notes_view.setFixedHeight(lines * line + margins)

    def _notes_toggled(self, on: bool) -> None:
        self.notes_toggle.setText(HIDE_DETAILS if on else SHOW_DETAILS)
        if on and not self._notes_details_built:
            self.notes_details.setPlainText("\n".join(self._notes_full))
            self._notes_details_built = True
        self.notes_details.setVisible(on)

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
            self.select_all_button.setText(TICK_ALL_SHOWN)
            self.select_all_button.setToolTip(
                "Tick every row the filter shows, including ones the tool left unticked. "
                "Hidden rows are not changed. Apply still asks first."
            )
            self.select_none_button.setText(UNTICK_ALL_SHOWN)
            self.select_none_button.setToolTip(
                "Untick every row the filter shows. Hidden rows are not changed."
            )
        else:
            self.select_all_button.setText(TICK_ALL)
            self.select_all_button.setToolTip(
                "Tick every row, including ones the tool left unticked. Apply still asks first."
            )
            self.select_none_button.setText(UNTICK_ALL)
            self.select_none_button.setToolTip("Untick every row.")

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
        ask = ApplyAsk(
            f"Apply: {self.tool.name}",
            apply_summary(plan, permanent),
            len(selected),
            onedrive_suggestion(plan, self.table.sizes()),
        )
        confirmed, pace = self._ask_to_apply(ask)
        if not confirmed:
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

        pause_event, skip_event = threading.Event(), threading.Event()
        self._pause_event, self._skip_wait_event = pause_event, skip_event
        self._pace = pace
        self._paused = self._waiting = False

        def job(ctx: Any) -> RunResult:
            ctx.pace = pace
            ctx.pause_event = pause_event
            ctx.skip_wait_event = skip_event
            return tool.apply(plan, ctx)

        worker = tool_worker(self.state, job, with_journal=True)
        self._apply_worker = worker
        worker.succeeded.connect(self._apply_done)
        self._start(worker, "Applying…")
        return worker

    def _ask_to_apply(self, ask: ApplyAsk) -> tuple[bool, Pace | None]:
        """The Apply question. A test that replaced ``confirm`` (and not ``choose_pace``) is
        asked through it, all at once, so older hooks keep working."""
        if self.confirm is not self._default_confirm_hook and (
            self.choose_pace == self._open_apply_dialog
        ):
            return self.confirm(ask.title, ask.summary), None
        return self.choose_pace(ask)

    def _open_apply_dialog(self, ask: ApplyAsk) -> tuple[bool, Pace | None]:
        dialog = ApplyDialog(ask, self.state.last_options(PACE_KEY), self)
        if dialog.exec() != ApplyDialog.DialogCode.Accepted:
            return False, None
        self.state.remember_options(PACE_KEY, dialog.values())
        return True, dialog.pace()

    # ----- pause, resume, skip the wait ------------------------------------------------------

    _pause_event: threading.Event | None = None
    _skip_wait_event: threading.Event | None = None
    _override_session_only = False  # "Use other folders" was set by set_folder, not ticked
    _scan_workspace: list[Path] = []  # the working folder when the running scan started
    _apply_worker: Worker | None = None
    _pace: Pace | None = None
    _paused = False
    _waiting = False

    def _apply_running(self) -> bool:
        return self.worker is not None and self.worker is self._apply_worker

    def pause_apply(self) -> None:
        """Hold the running apply after the file it is on."""
        if self._apply_running() and self._pause_event is not None:
            self._pause_event.set()
            self._paused = True
            self._set_status("Pausing: the current file finishes first. Press Resume to go on.")
            self._update_buttons()

    def resume_apply(self) -> None:
        if self._apply_running() and self._pause_event is not None:
            self._pause_event.clear()
            self._paused = False
            self._set_status("Applying…")
            self._update_buttons()

    def skip_wait(self) -> None:
        """Start the next batch now instead of waiting."""
        if self._apply_running() and self._skip_wait_event is not None:
            self._skip_wait_event.set()

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
        if run_id in applying_run_ids():
            self.inform("Undo", in_progress_message(run_id))
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
            if before.in_progress or run_id in applying_run_ids():
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
        waiting = "Next batch in" in message or message == WAIT_PAUSED_MESSAGE
        if waiting != self._waiting:
            self._waiting = waiting
            self._update_buttons()

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
        self.cancel_button.setVisible(running)
        applying_now = running and self._apply_running()
        self.cancel_button.setText(STOP_TEXT if applying_now else "Cancel")
        self.pause_button.setVisible(applying_now and not self._paused)
        self.resume_button.setVisible(applying_now and self._paused)
        self.skip_wait_button.setVisible(applying_now and self._pace is not None)
        self.skip_wait_button.setEnabled(self._waiting)
        self.progress_bar.setVisible(running)
        self.apply_button.setEnabled(not busy and has_plan and selected and not self._applied)
        self.apply_button.setText(APPLY_TEXT if self._applied else apply_label(self.plan))
        self.more_button.setEnabled(not busy)
        self.reset_options_action.setEnabled(not busy)
        self.export_button.setEnabled(not busy and has_plan)
        can_select = not busy and has_plan and not self._applied
        self.select_all_button.setEnabled(can_select)
        self.select_none_button.setEnabled(can_select)
        self.table.set_selection_enabled(can_select)
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
