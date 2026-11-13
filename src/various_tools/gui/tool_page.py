"""One page per tool: options form, presets, Scan, the plan table, Apply, Export and Undo.

Everything here is generic. The page is built from the tool's options model through
:class:`~various_tools.gui.widgets.schema_form.SchemaForm`. The only per-tool touches are the
duplicates compare view (``COMPARE_TOOLS``) and the rename live preview (``LIVE_PREVIEW_TOOLS``).

Safety (AGENTS.md): Scan never changes the disk. Apply is a separate button that always asks for
confirmation, and asks a second time when a selected action is a permanent delete. Every apply
runs through the tool's ``apply()``, which journals each action, so History can undo it.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError
from PySide6.QtCore import Qt, QTimer, Signal
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
    QVBoxLayout,
    QWidget,
)

from various_tools.core.deps import TOOL_SPECS, install_hint
from various_tools.core.journal import Journal, UndoReport, undo_run
from various_tools.core.plan import ActionKind, Plan, RunResult
from various_tools.core.report import action_sizes
from various_tools.gui.state import AppState
from various_tools.gui.widgets.plan_table import PlanTable
from various_tools.gui.widgets.schema_form import SchemaForm
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
    counts = Counter(str(a.kind) for a in selected)
    lines = [f"Apply {len(selected)} selected action(s)?", ""]
    lines += [f"  {kind}: {count}" for kind, count in sorted(counts.items())]
    lines.append("")
    if counts.get(str(ActionKind.TRASH)):
        lines.append("Trashed items go to the Recycle Bin, where you can restore them.")
    if permanent_delete:
        lines.append("Some items will be PERMANENTLY deleted. You will be asked again.")
    lines.append("Moves and renames can be undone from History.")
    return "\n".join(lines)


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


def _default_choose_export(parent: QWidget) -> ChooseExport:
    def choose() -> tuple[str, str] | None:
        from various_tools.core.report import EXPORT_FILTER

        path, selected_filter = QFileDialog.getSaveFileName(parent, "Export", "", EXPORT_FILTER)
        return (path, selected_filter) if path else None

    return choose


class ToolPage(QWidget):
    """The page for one tool. Emits ``runApplied(run_id)`` after an apply was journaled."""

    runApplied = Signal(int)
    busyChanged = Signal(bool)

    def __init__(self, state: AppState, tool: Tool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.tool = tool
        self.plan: Plan | None = None
        self.result: RunResult | None = None
        self.last_run_id: int | None = None
        self.worker: Worker | None = None
        self.compare_dialog: QWidget | None = None
        self._applied = False
        self._live = False  # the running job is a live-preview scan; the form stays editable

        # Hooks that tests replace so no modal dialog blocks.
        self.confirm: Confirm = _default_confirm(self)
        self.inform: Inform = _default_inform(self)
        self.ask_text: AskText = _default_ask_text(self)
        self.choose_export: ChooseExport = _default_choose_export(self)

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
        self.form = SchemaForm(tool.Options, self._initial_values())
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

        # ----- results -----------------------------------------------------------------
        self.notes_view = QPlainTextEdit()
        self.notes_view.setReadOnly(True)
        self.notes_view.setMaximumHeight(90)
        self.notes_view.setPlaceholderText("Scan notes appear here.")
        self.table = PlanTable()

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
        bottom_layout.addWidget(self.progress_bar)
        bottom_layout.addWidget(self.status_label)
        bottom_layout.addWidget(self.missing_banner)
        bottom_layout.addWidget(self.notes_view)
        bottom_layout.addWidget(self.table, 1)
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
        self.save_preset_button.clicked.connect(lambda _c=False: self.save_preset())
        self.delete_preset_button.clicked.connect(lambda _c=False: self.delete_preset())
        self.preset_combo.activated.connect(self._preset_chosen)
        self.form.changed.connect(self._form_changed)
        self.table.selectionChanged.connect(self._update_buttons)
        self.table.currentActionChanged.connect(lambda _a: self._update_buttons())

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
        """Scan in a worker and show the plan. Never changes the disk."""
        if self.busy:
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

        worker = tool_worker(self.state, job, with_journal=False, parent=self)
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
        count = len(plan.actions)
        if count:
            self._set_status(
                f"Preview ready: {count} action(s). Uncheck anything you want to keep, then Apply."
            )
        else:
            self._set_status("Nothing to do: the scan found no actions.")

    def _form_changed(self) -> None:
        if self.tool.id in LIVE_PREVIEW_TOOLS:
            self._preview_timer.start()

    def _live_preview(self) -> None:
        """Rename live preview: re-scan (read-only) after the options stop changing."""
        if self.busy:
            self._preview_timer.start()
            return
        values = self.form.raw_values()
        if not any(values.get(name) for name in self.form.folder_fields()):
            return
        self.scan(quiet=True)

    def select_all(self, selected: bool = True) -> int:
        """Check (or uncheck) every action of the plan. Returns how many changed."""
        if self.plan is None or self._applied or self.busy:
            return 0
        changed = self.plan.select_all(selected)
        self.table.refresh()
        self._update_buttons()
        return changed

    # ----- apply ------------------------------------------------------------------------------

    def apply(self) -> Worker | None:
        """Ask for confirmation, then apply the checked actions in a worker (journaled)."""
        if self.busy or self.plan is None or self._applied:
            return None
        plan = self.plan
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
        tool = self.tool

        def job(ctx: Any) -> RunResult:
            return tool.apply(plan, ctx)

        worker = tool_worker(self.state, job, with_journal=True, parent=self)
        worker.succeeded.connect(self._apply_done)
        self._start(worker, "Applying…")
        return worker

    def _apply_done(self, value: object) -> None:
        assert isinstance(value, RunResult)
        self.result = value
        self.last_run_id = value.run_id
        self._applied = True
        text = result_summary(value)
        failures = [o for o in value.outcomes if o.status == "failed"][:10]
        if failures:
            text += "\n\nFailed:\n" + "\n".join(f"  {o.src}: {o.detail}" for o in failures)
        self._set_status(text.splitlines()[0] + " Scan again to see what is left.")
        self.runApplied.emit(value.run_id)
        self._update_buttons()
        self.inform("Apply finished", text)

    # ----- undo -------------------------------------------------------------------------------

    def undo_last(self) -> Worker | None:
        """Undo the run this page applied last (the same as Undo in History)."""
        run_id = self.last_run_id
        if self.busy or run_id is None:
            return None
        if not self.confirm(
            "Undo",
            f"Move and rename back everything run {run_id} changed?\n\n"
            "Items sent to the Recycle Bin must be restored from the Recycle Bin.",
        ):
            return None
        journal_path = self.state.settings.journal_path()

        def job(_worker: Worker) -> UndoReport:
            with Journal(journal_path) as journal:
                return undo_run(journal, run_id)

        worker = Worker(job, self)
        worker.succeeded.connect(self._undo_done)
        self._start(worker, "Undoing…")
        return worker

    def _undo_done(self, value: object) -> None:
        assert isinstance(value, UndoReport)
        lines = [f"Run {value.run_id}: {value.status} ({value.reverted} reverted)."]
        lines += [f"  skipped: {s}" for s in value.skipped[:10]]
        lines += [f"  failed: {f}" for f in value.failed[:10]]
        self.last_run_id = None
        self._set_status(lines[0])
        self.runApplied.emit(value.run_id)  # History reloads
        self.inform("Undo finished", "\n".join(lines))

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

        worker = Worker(job, self)
        worker.succeeded.connect(lambda written: self._set_status(f"Exported to {written}."))
        self._start(worker, "Exporting…")
        return worker

    # ----- duplicates compare view ------------------------------------------------------------

    def open_compare(self, group_id: str | None = None) -> QWidget | None:
        from various_tools.gui.widgets.compare_view import CompareDialog

        plan = self.plan
        group = group_id or self.table.current_group_id()
        if plan is None or group is None or self._applied:
            return None
        dialog = CompareDialog(plan, group, self)
        dialog.keeperChanged.connect(self._keeper_changed)
        dialog.setModal(False)
        dialog.show()
        self.compare_dialog = dialog
        return dialog

    def _keeper_changed(self, new_group_id: str) -> None:
        from various_tools.core.media.thumbs import file_facts

        plan = self.plan
        if plan is None:
            return
        sizes = self.table.sizes()
        for index, action in enumerate(plan.actions):
            if action.group_id == new_group_id and action.src is not None and index < len(sizes):
                sizes[index] = file_facts(action.src).size
        self.table.refresh(sizes)
        self._set_status(f"Now keeping {Path(new_group_id).name}.")

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
        worker = self.worker
        self.worker = None
        self._live = False
        if worker is not None:
            worker.deleteLater()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1 if self.plan is not None else 0)
        self.progress_bar.setFormat("%p%")
        self.busyChanged.emit(False)
        self._update_buttons()

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self._set_status("Cancelling: waiting for the current step to finish…")

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Cancel any running job and wait for it (window close)."""
        self._preview_timer.stop()
        worker = self.worker
        if worker is not None:
            worker.cancel()
            worker.wait(timeout_ms)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #c0392b;" if error else "")
        (log.warning if error else log.info)("%s: %s", self.tool.id, text.splitlines()[0])

    def _update_buttons(self) -> None:
        busy = self.busy
        has_plan = self.plan is not None
        selected = bool(self.plan.selected_actions()) if self.plan is not None else False
        self.scan_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.apply_button.setEnabled(not busy and has_plan and selected and not self._applied)
        self.export_button.setEnabled(not busy and has_plan)
        can_select = not busy and has_plan and not self._applied
        self.select_all_button.setEnabled(can_select)
        self.select_none_button.setEnabled(can_select)
        self.compare_button.setEnabled(
            not busy
            and has_plan
            and not self._applied
            and self.table.current_group_id() is not None
        )
        self.undo_button.setVisible(self.last_run_id is not None)
        self.undo_button.setEnabled(not busy and self.last_run_id is not None)
        self.form.setEnabled(not busy or self._live)
        self.preset_combo.setEnabled(not busy)
        self.save_preset_button.setEnabled(not busy)
        self.delete_preset_button.setEnabled(
            not busy and self.preset_combo.currentText() != NO_PRESET
        )
