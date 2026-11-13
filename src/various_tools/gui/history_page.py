"""History page: the runs that were applied, what each one did, and Undo.

The page reads the journal to show runs and opens it again only for the moment it needs it, so it
never holds a connection that an apply running in the background needs. Undo runs in a worker with
its own journal connection. Moves and renames are put back. Items that went to the Recycle Bin must
be restored from there by hand, and the confirmation says so.

Tests replace :attr:`HistoryPage.confirm` and :attr:`HistoryPage.inform` so no message box opens.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.journal import (
    REVERSIBLE_KINDS,
    ActionRecord,
    Journal,
    RunRecord,
    UndoReport,
    undo_run,
)
from various_tools.gui.state import AppState
from various_tools.gui.workers import Worker

log = logging.getLogger(__name__)

NO_HISTORY_TEXT = "No history yet: nothing has been applied."
MAX_RUNS = 500
MAX_LISTED = 20

RUN_COLUMNS = ("Run", "Tool", "Started", "Finished", "Status", "Actions")
ACTION_COLUMNS = ("#", "Kind", "Status", "Source", "Destination", "Reason", "Error")

# Workers that are running. A QThread must not be garbage collected while it runs.
_RUNNING: set[Worker] = set()


def _start(
    fn: Callable[[Worker], object],
    on_success: Callable[[object], None],
    on_failure: Callable[[BaseException], None],
) -> Worker:
    worker = Worker(fn)
    _RUNNING.add(worker)
    worker.succeeded.connect(on_success)
    worker.failed.connect(on_failure)
    worker.finished.connect(lambda: _RUNNING.discard(worker))
    worker.start()
    return worker


def _default_confirm(title: str, text: str) -> bool:
    answer = QMessageBox.question(
        None,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def _default_inform(title: str, text: str) -> None:
    QMessageBox.information(None, title, text)


def _action_counts(path: Path) -> dict[int, int]:
    """How many actions each run has, from one read-only query (not one query per run)."""
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute("SELECT run_id, COUNT(*) FROM actions GROUP BY run_id").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {int(run_id): int(count) for run_id, count in rows}


def undo_summary(report: UndoReport) -> str:
    """Plain-English result of an undo, for the message box."""
    lines = [
        f"Run {report.run_id} is now: {report.status}.",
        f"{report.reverted} action(s) were put back.",
    ]
    for label, items in (("Skipped", report.skipped), ("Failed", report.failed)):
        if items:
            lines.append("")
            lines.append(f"{label} ({len(items)}):")
            lines.extend(f"  - {item}" for item in items[:MAX_LISTED])
            if len(items) > MAX_LISTED:
                lines.append(f"  ... and {len(items) - MAX_LISTED} more")
    return "\n".join(lines)


def _readonly_item(text: str, *, right: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


def _make_table(columns: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(list(columns))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    return table


class HistoryPage(QWidget):
    """Past runs from the journal, the actions of the selected run, Undo and Export."""

    runUndone = Signal(int)

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._runs: list[RunRecord] = []
        self._actions: list[ActionRecord] = []
        self._busy = False
        self.confirm: Callable[[str, str], bool] = _default_confirm
        self.inform: Callable[[str, str], None] = _default_inform

        self.runs_view = _make_table(RUN_COLUMNS)
        self.actions_view = _make_table(ACTION_COLUMNS)
        self.empty_label = QLabel(NO_HISTORY_TEXT)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.refresh_button = QPushButton("Refresh")
        self.undo_button = QPushButton("Undo run")
        self.export_button = QPushButton("Export...")
        self.refresh_button.setToolTip("Read the history again.")
        self.undo_button.setToolTip(
            "Move the files of the selected run back. Items sent to the Recycle Bin are not "
            "restored by this; restore them from the Recycle Bin."
        )
        self.export_button.setToolTip("Save the selected run to a CSV, Excel or HTML file.")
        self.undo_button.setEnabled(False)
        self.export_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self.undo_button)
        buttons.addWidget(self.export_button)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.runs_view)
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addWidget(self.summary_label)
        lower_layout.addWidget(self.actions_view, 1)
        splitter.addWidget(lower)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        self._splitter = splitter

        layout = QVBoxLayout(self)
        layout.addWidget(self.empty_label, 1)
        layout.addWidget(splitter, 1)
        layout.addLayout(buttons)

        self.refresh_button.clicked.connect(lambda _checked=False: self.reload())
        self.undo_button.clicked.connect(lambda _checked=False: self.undo_selected())
        self.export_button.clicked.connect(lambda _checked=False: self.export_selected())
        self.runs_view.itemSelectionChanged.connect(self._selection_changed)
        self.reload()

    # ----- reading the journal ---------------------------------------------------------------

    def _tool_name(self, tool_id: str) -> str:
        tool = self._state.tools.get(tool_id)
        return tool.name if tool is not None else tool_id

    def _run_id_at(self, row: int) -> object:
        item = self.runs_view.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _selected_run(self) -> RunRecord | None:
        rows = self.runs_view.selectionModel().selectedRows()
        if not rows:
            return None
        run_id = self._run_id_at(rows[0].row())
        return next((run for run in self._runs if run.id == run_id), None)

    def reload(self) -> None:
        """Read the runs again, keeping the selected run selected when it still exists."""
        keep = self._selected_run()
        runs: list[RunRecord] = []
        counts: dict[int, int] = {}
        self.empty_label.setText(NO_HISTORY_TEXT)
        try:
            journal = self._state.open_journal()
            if journal is not None:
                try:
                    runs = journal.runs(MAX_RUNS)
                    path = journal.path
                finally:
                    journal.close()
                counts = _action_counts(path)
        except (sqlite3.Error, OSError) as exc:
            log.warning("Could not read the history: %s", exc)
            self.empty_label.setText(f"The history could not be read: {exc}")
            runs, counts = [], {}
        self._runs = runs

        self.empty_label.setVisible(not runs)
        self._splitter.setVisible(bool(runs))
        self.runs_view.blockSignals(True)
        self.runs_view.setRowCount(len(runs))
        for row, run in enumerate(runs):
            values = (
                str(run.id),
                self._tool_name(run.tool),
                run.started,
                run.finished or "",
                run.status,
                str(counts.get(run.id, 0)),
            )
            for column, text in enumerate(values):
                item = _readonly_item(text, right=column in (0, 5))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, run.id)
                self.runs_view.setItem(row, column, item)
        self.runs_view.blockSignals(False)
        self.runs_view.resizeColumnsToContents()
        self.runs_view.blockSignals(True)
        self.runs_view.clearSelection()
        if keep is not None:
            self.select_run(keep.id)
        self.runs_view.blockSignals(False)
        # The selection may be the same run as before, but its actions may have changed.
        self._selection_changed()

    def select_run(self, run_id: int) -> None:
        """Select a run in the table (this also shows its actions)."""
        for row in range(self.runs_view.rowCount()):
            if self._run_id_at(row) == run_id:
                self.runs_view.selectRow(row)
                return

    def _selection_changed(self) -> None:
        run = self._selected_run()
        self._actions = []
        if run is not None:
            try:
                journal = self._state.open_journal()
                if journal is not None:
                    try:
                        self._actions = journal.actions(run.id)
                    finally:
                        journal.close()
            except (sqlite3.Error, OSError) as exc:
                log.warning("Could not read the actions of run %s: %s", run.id, exc)
        self._show_actions(run)
        self._update_buttons()

    def _show_actions(self, run: RunRecord | None) -> None:
        if run is None:
            self.summary_label.setText("Select a run to see what it did.")
        else:
            finished = f", finished {run.finished}" if run.finished else ""
            self.summary_label.setText(
                f"Run {run.id}: {self._tool_name(run.tool)}, {run.status}, "
                f"started {run.started}{finished}"
            )
        self.actions_view.setRowCount(len(self._actions))
        for row, action in enumerate(self._actions):
            values = (
                str(action.seq),
                action.kind,
                action.status,
                action.src or "",
                action.dst or "",
                action.reason,
                action.error or "",
            )
            for column, text in enumerate(values):
                self.actions_view.setItem(row, column, _readonly_item(text, right=column == 0))
        self.actions_view.resizeColumnsToContents()

    def _reversible_count(self) -> int:
        return sum(1 for a in self._actions if a.status == "done" and a.kind in REVERSIBLE_KINDS)

    def _update_buttons(self) -> None:
        run = self._selected_run()
        undoable = (
            run is not None
            and run.status != "undone"
            and self._reversible_count() > 0
            and not self._busy
        )
        self.undo_button.setEnabled(undoable)
        self.export_button.setEnabled(run is not None and not self._busy)
        self.refresh_button.setEnabled(not self._busy)

    # ----- undo ------------------------------------------------------------------------------

    def undo_selected(self) -> Worker | None:
        """Ask for confirmation, then undo the selected run in a worker. None if nothing to do."""
        run = self._selected_run()
        if run is None or self._busy or run.status == "undone":
            return None
        reversible = self._reversible_count()
        if reversible == 0:
            return None
        other = sum(1 for a in self._actions if a.status == "done") - reversible
        text = (
            f"Undo run {run.id} ({self._tool_name(run.tool)})?\n\n"
            f"{reversible} move or rename action(s) will be reversed: each item is moved back "
            "to where it came from."
        )
        if other:
            text += (
                f"\n\n{other} other action(s), such as items sent to the Recycle Bin, cannot be "
                "reversed here. Restore those from the Recycle Bin yourself."
            )
        if not self.confirm(f"Undo run {run.id}", text):
            return None

        run_id = run.id
        path = self._state.settings.journal_path()

        def job(_worker: Worker) -> UndoReport:
            # Its own connection, opened and closed on this thread.
            journal = Journal(path)
            try:
                return undo_run(journal, run_id)
            finally:
                journal.close()

        self._busy = True
        self._update_buttons()
        return _start(job, self._undo_done, self._undo_failed)

    def _undo_done(self, result: object) -> None:
        if not shiboken6.isValid(self) or not isinstance(result, UndoReport):
            return
        self._busy = False
        self.inform(f"Undo run {result.run_id}", undo_summary(result))
        self.reload()
        self.runUndone.emit(result.run_id)

    def _undo_failed(self, exc: BaseException) -> None:
        if not shiboken6.isValid(self):
            return
        self._busy = False
        self.inform("Undo failed", f"The run could not be undone: {exc}")
        self.reload()

    # ----- export ----------------------------------------------------------------------------

    def export_selected(self, path: str | None = None) -> Worker | None:
        """Write the selected run to a file chosen in a dialog (or ``path``, which tests pass)."""
        # Imported here so this page loads even if the report module is unavailable.
        from various_tools.core.report import EXPORT_FILTER

        run = self._selected_run()
        if run is None or self._busy:
            return None
        file_name = path
        if file_name is None:
            chosen, selected_filter = QFileDialog.getSaveFileName(
                self, "Export run", f"run-{run.id}", EXPORT_FILTER
            )
            if not chosen:
                return None
            file_name = chosen
            if not Path(file_name).suffix:
                match = re.search(r"\(\*(\.\w+)", selected_filter)
                if match:
                    file_name += match.group(1)
        target = Path(file_name)
        actions = list(self._actions)
        tool_name = self._tool_name(run.tool)

        def job(_worker: Worker) -> Path:
            from various_tools.core.report import export_run

            return export_run(run, actions, target, tool_name=tool_name)

        return _start(job, self._export_done, self._export_failed)

    def _export_done(self, result: object) -> None:
        if shiboken6.isValid(self):
            self.inform("Export", f"Saved {result}")

    def _export_failed(self, exc: BaseException) -> None:
        if shiboken6.isValid(self):
            self.inform("Export failed", f"The run could not be exported: {exc}")
