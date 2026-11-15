"""History page: the runs that were applied, what each one did, and Undo.

Every read of the journal happens in a worker thread, with its own connection that is opened and
closed there, so the page never blocks the window and never holds a connection an apply running
in the background needs:

* the run list comes with each run's action count from one ``COUNT ... GROUP BY`` query;
* the selected run's actions are loaded a page at a time (:data:`PAGE_SIZE` rows, then
  "Show more"), because a run can have tens of thousands; its status counts and the undo rule's
  numbers come from count queries, not from the loaded rows;
* Export reads every action of the run in its own worker.

Undo runs in a worker too. Whether a run can be undone is decided by the shared rule in
:mod:`various_tools.gui.undo_rules`, the same one the tool pages use.

Tests replace :attr:`HistoryPage.confirm` and :attr:`HistoryPage.inform` so no message box opens,
and wait for :attr:`HistoryPage.loading` to become false.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
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

from various_tools.core.journal import ActionRecord, Journal, RunRecord, UndoReport, undo_run
from various_tools.gui.state import AppState
from various_tools.gui.undo_rules import UndoInfo, read_undo_info
from various_tools.gui.workers import Worker

log = logging.getLogger(__name__)

NO_HISTORY_TEXT = "No history yet: nothing has been applied."
MAX_RUNS = 500
MAX_LISTED = 20
PAGE_SIZE = 500

RUN_COLUMNS = ("Run", "Tool", "Started", "Finished", "Status", "Actions")
ACTION_COLUMNS = ("#", "Kind", "Status", "Source", "Destination", "Reason", "Error")

# Workers that are running. A QThread must not be garbage collected while it runs.
_RUNNING: set[Worker] = set()


def _start(
    fn: Callable[[Worker], object],
    on_success: Callable[[object], None],
    on_failure: Callable[[BaseException], None],
    *,
    writes: bool = False,
) -> Worker:
    worker = Worker(fn, writes=writes)
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


def undo_question(run_id: int, tool_name: str, info: UndoInfo) -> str:
    """The confirmation text for undoing a run, from the undo rule's counts."""
    moves = info.count("move", "rename")
    trashed = info.count("trash")
    outputs = info.count("write", "convert")
    other = info.done - info.reversible
    lines = [f"Undo run {run_id} ({tool_name})?", ""]
    if moves:
        lines.append(
            f"{moves} move or rename action(s) will be reversed: each item is moved back to "
            "where it came from."
        )
    if trashed:
        lines.append(f"{trashed} item(s) sent to the Recycle Bin are restored if still there.")
    if outputs:
        lines.append(
            f"{outputs} written or converted file(s) go to the Recycle Bin if they are unchanged."
        )
    if other:
        lines.append(f"{other} other action(s), such as permanent deletes, cannot be undone.")
    lines += ["", "Anything that cannot be put back is listed afterwards."]
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


@dataclass(frozen=True)
class RunList:
    """What the run-list worker read."""

    runs: list[RunRecord] = field(default_factory=list[RunRecord])
    counts: dict[int, int] = field(default_factory=dict[int, int])
    error: str | None = None


@dataclass(frozen=True)
class ActionPage:
    """One page of a run's actions, with the run's totals (only read with the first page)."""

    run_id: int
    offset: int
    actions: list[ActionRecord]
    total: int | None = None
    status_counts: dict[str, int] | None = None
    info: UndoInfo | None = None


def _read_runs(path: Path) -> RunList:
    if not path.is_file():
        return RunList()
    try:
        with Journal(path) as journal:
            runs = journal.runs(MAX_RUNS)
    except (sqlite3.Error, OSError) as exc:
        return RunList(error=str(exc))
    return RunList(runs, _action_counts(path))


def _read_page(path: Path, run_id: int, offset: int, *, first: bool) -> ActionPage:
    with Journal(path) as journal:
        actions = journal.actions(run_id, limit=PAGE_SIZE, offset=offset)
        if not first:
            return ActionPage(run_id, offset, actions)
        total = journal.action_count(run_id)
        statuses = journal.status_counts(run_id)
    return ActionPage(run_id, offset, actions, total, statuses, read_undo_info(path, run_id))


class HistoryPage(QWidget):
    """Past runs from the journal, the actions of the selected run, Undo and Export."""

    runUndone = Signal(int)
    loaded = Signal()

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._runs: list[RunRecord] = []
        self._actions: list[ActionRecord] = []
        self._detail: ActionPage | None = None  # the first page: totals and the undo numbers
        self._busy = False  # an undo is running
        self._closing = False
        self._runs_worker: Worker | None = None
        self._page_worker: Worker | None = None
        self._reload_again = False
        self._generation = 0  # bumped on every selection change, to drop late pages
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
        self.more_button = QPushButton("Show more")
        self.refresh_button.setToolTip("Read the history again.")
        self.undo_button.setToolTip(
            "Undo the selected run: moves and renames are put back, Recycle Bin items are "
            "restored while they are still there, and unchanged written files are recycled."
        )
        self.export_button.setToolTip("Save the selected run to a CSV, Excel or HTML file.")
        self.more_button.setToolTip(f"Show the next {PAGE_SIZE} actions of this run.")
        self.undo_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.more_button.setVisible(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self.undo_button)
        buttons.addWidget(self.export_button)

        more_row = QHBoxLayout()
        more_row.addStretch(1)
        more_row.addWidget(self.more_button)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.runs_view)
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addWidget(self.summary_label)
        lower_layout.addWidget(self.actions_view, 1)
        lower_layout.addLayout(more_row)
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
        self.more_button.clicked.connect(lambda _checked=False: self.load_more())
        self.runs_view.itemSelectionChanged.connect(self._selection_changed)
        self.reload()

    # ----- state -----------------------------------------------------------------------------

    @property
    def loading(self) -> bool:
        """The run list or a page of actions is being read."""
        return self._runs_worker is not None or self._page_worker is not None or self._reload_again

    @property
    def actions_shown(self) -> int:
        return len(self._actions)

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

    def _detail_for(self, run: RunRecord | None) -> ActionPage | None:
        detail = self._detail
        if run is None or detail is None or detail.run_id != run.id:
            return None
        return detail

    # ----- reading the journal (in workers) --------------------------------------------------

    def reload(self) -> Worker | None:
        """Read the runs again in a worker, keeping the selected run selected if it still exists."""
        if self._closing:
            return None
        if self._runs_worker is not None:
            self._reload_again = True  # once the current read is done
            return None
        self._reload_again = False
        path = self._state.settings.journal_path()
        worker = _start(lambda _w: _read_runs(path), self._runs_read, self._runs_failed)
        self._runs_worker = worker
        self._update_buttons()
        return worker

    def _runs_failed(self, exc: BaseException) -> None:
        self._runs_read(RunList(error=str(exc)))

    def _runs_read(self, value: object) -> None:
        if not shiboken6.isValid(self) or not isinstance(value, RunList):
            return
        self._runs_worker = None
        if self._reload_again and not self._closing:
            self.reload()
            return
        keep = self._selected_run()
        runs = value.runs
        if value.error is not None:
            log.warning("Could not read the history: %s", value.error)
            self.empty_label.setText(f"The history could not be read: {value.error}")
        else:
            self.empty_label.setText(NO_HISTORY_TEXT)
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
                str(value.counts.get(run.id, 0)),
            )
            for column, text in enumerate(values):
                item = _readonly_item(text, right=column in (0, 5))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, run.id)
                self.runs_view.setItem(row, column, item)
        self.runs_view.resizeColumnsToContents()
        self.runs_view.clearSelection()
        if keep is not None:
            self._select_row(keep.id)
        self.runs_view.blockSignals(False)
        # The selection may be the same run as before, but its actions may have changed.
        self._selection_changed()

    def _select_row(self, run_id: int) -> bool:
        for row in range(self.runs_view.rowCount()):
            if self._run_id_at(row) == run_id:
                self.runs_view.selectRow(row)
                return True
        return False

    def select_run(self, run_id: int) -> None:
        """Select a run in the table (its actions are then read in a worker)."""
        self._select_row(run_id)

    def _selection_changed(self) -> None:
        run = self._selected_run()
        self._generation += 1
        self._actions = []
        self._detail = None
        self._page_worker = None  # a page still on its way is dropped (wrong generation)
        self.actions_view.setRowCount(0)
        self.more_button.setVisible(False)
        if run is None:
            self.summary_label.setText("Select a run to see what it did.")
        else:
            self.summary_label.setText(f"Run {run.id}: reading its actions…")
            self._read_page(run, 0)
        self._update_buttons()
        if not self.loading:
            self.loaded.emit()

    def load_more(self) -> Worker | None:
        """Read the next page of the selected run's actions."""
        run = self._selected_run()
        detail = self._detail_for(run)
        if run is None or detail is None or self._page_worker is not None:
            return None
        if detail.total is not None and len(self._actions) >= detail.total:
            return None
        return self._read_page(run, len(self._actions))

    def _read_page(self, run: RunRecord, offset: int) -> Worker | None:
        if self._closing:
            return None
        path = self._state.settings.journal_path()
        generation = self._generation
        run_id = run.id
        first = offset == 0
        worker = _start(
            lambda _w: _read_page(path, run_id, offset, first=first),
            lambda value: self._page_read(generation, value),
            lambda exc: self._page_failed(generation, run_id, exc),
        )
        self._page_worker = worker
        return worker

    def _page_failed(self, generation: int, run_id: int, exc: BaseException) -> None:
        if not shiboken6.isValid(self) or generation != self._generation:
            return
        self._page_worker = None
        log.warning("Could not read the actions of run %s: %s", run_id, exc)
        self.summary_label.setText(f"Run {run_id}: its actions could not be read: {exc}")
        self._update_buttons()
        self.loaded.emit()

    def _page_read(self, generation: int, value: object) -> None:
        if not shiboken6.isValid(self) or generation != self._generation:
            return  # the selection changed meanwhile
        if not isinstance(value, ActionPage):
            return
        self._page_worker = None
        if value.offset == 0:
            self._detail = value
        self._append_actions(value.actions)
        run = self._selected_run()
        self._show_summary(run)
        self._update_buttons()
        if not self.loading:
            self.loaded.emit()

    def _append_actions(self, actions: list[ActionRecord]) -> None:
        start = len(self._actions)
        self._actions.extend(actions)
        self.actions_view.setRowCount(len(self._actions))
        for offset, action in enumerate(actions):
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
                self.actions_view.setItem(
                    start + offset, column, _readonly_item(text, right=column == 0)
                )
        if start == 0:
            self.actions_view.resizeColumnsToContents()

    def _show_summary(self, run: RunRecord | None) -> None:
        detail = self._detail_for(run)
        if run is None:
            self.summary_label.setText("Select a run to see what it did.")
            return
        finished = f", finished {run.finished}" if run.finished else ""
        text = (
            f"Run {run.id}: {self._tool_name(run.tool)}, {run.status}, "
            f"started {run.started}{finished}"
        )
        if detail is not None and detail.total is not None:
            statuses = ", ".join(
                f"{count} {status}"
                for status, count in sorted((detail.status_counts or {}).items())
            )
            text += f". {detail.total} action(s)" + (f": {statuses}" if statuses else "")
            if detail.total > len(self._actions):
                text += f". Showing the first {len(self._actions)}."
        self.summary_label.setText(text)
        remaining = (detail.total or 0) - len(self._actions) if detail is not None else 0
        self.more_button.setVisible(remaining > 0)
        self.more_button.setText(f"Show {min(remaining, PAGE_SIZE)} more")

    # ----- buttons ---------------------------------------------------------------------------

    def undo_info(self) -> UndoInfo | None:
        """The undo rule's numbers for the selected run, once they have been read."""
        detail = self._detail_for(self._selected_run())
        return detail.info if detail is not None else None

    def _update_buttons(self) -> None:
        run = self._selected_run()
        info = self.undo_info()
        undoable = (
            run is not None
            and info is not None
            and info.undoable
            and not self._busy
            and not self._closing
        )
        self.undo_button.setEnabled(undoable)
        self.export_button.setEnabled(run is not None and not self._busy and not self._closing)
        self.refresh_button.setEnabled(not self._busy and not self._closing)
        self.more_button.setEnabled(self._page_worker is None and not self._closing)

    def shutdown(self) -> None:
        """The window is closing: start nothing new (running workers are cancelled for us)."""
        self._closing = True
        self._reload_again = False
        self._update_buttons()

    # ----- undo ------------------------------------------------------------------------------

    def undo_selected(self) -> Worker | None:
        """Ask for confirmation, then undo the selected run in a worker. None if nothing to do."""
        run = self._selected_run()
        info = self.undo_info()
        if run is None or info is None or self._busy or self._closing or not info.undoable:
            return None
        if not self.confirm(
            f"Undo run {run.id}", undo_question(run.id, self._tool_name(run.tool), info)
        ):
            return None

        run_id = run.id
        settings = self._state.settings
        path = settings.journal_path()
        protection = settings.protected_paths()

        def job(_worker: Worker) -> UndoReport:
            # Its own connection, opened and closed on this thread.
            with Journal(path) as journal:
                return undo_run(journal, run_id, protection=protection)

        self._busy = True
        self._update_buttons()
        return _start(job, self._undo_done, self._undo_failed, writes=True)

    def _undo_done(self, result: object) -> None:
        if not shiboken6.isValid(self) or not isinstance(result, UndoReport):
            return
        self._busy = False
        if not self._closing:
            self.inform(f"Undo run {result.run_id}", undo_summary(result))
        self.reload()
        self.runUndone.emit(result.run_id)

    def _undo_failed(self, exc: BaseException) -> None:
        if not shiboken6.isValid(self):
            return
        self._busy = False
        if not self._closing:
            self.inform("Undo failed", f"The run could not be undone: {exc}")
        self.reload()

    # ----- export ----------------------------------------------------------------------------

    def export_selected(self, path: str | None = None) -> Worker | None:
        """Write the selected run to a file chosen in a dialog (or ``path``, which tests pass).

        The dialog asks before replacing a file. When a suffix is added to the name it chose,
        the page asks itself, so an existing file is never replaced silently.
        """
        # Imported here so this page loads even if the report module is unavailable.
        from various_tools.core.report import EXPORT_FILTER

        run = self._selected_run()
        if run is None or self._busy or self._closing:
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
                    if Path(file_name).exists() and not self.confirm(
                        "Export run", f"{file_name} already exists. Replace it?"
                    ):
                        return None
        target = Path(file_name)
        tool_name = self._tool_name(run.tool)
        journal_path = self._state.settings.journal_path()

        def job(_worker: Worker) -> Path:
            from various_tools.core.report import export_run

            with Journal(journal_path) as journal:
                actions = journal.actions(run.id)
            return export_run(run, actions, target, tool_name=tool_name)

        return _start(job, self._export_done, self._export_failed)

    def _export_done(self, result: object) -> None:
        if shiboken6.isValid(self):
            self.inform("Export", f"Saved {result}")

    def _export_failed(self, exc: BaseException) -> None:
        if shiboken6.isValid(self):
            self.inform("Export failed", f"The run could not be exported: {exc}")
