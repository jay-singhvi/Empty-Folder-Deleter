"""The "Change date" dialog: choose a new date for the selected photos and videos.

It uses the ``set_date`` tool, so nothing is different from running that tool by hand:

1. every choice is checked by a *scan* in the background, and the dialog lists each file with its
   old and new date (nothing is changed yet; preview first);
2. "Change dates" asks once more, then runs the plan with the journal, so History can undo it;
3. afterwards the media library reads the changed files again and ``datesChanged`` tells the
   Timeline and the Map to refresh.

The ``confirm`` hook is an attribute so tests can replace it.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDateTime, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.context import RunContext
from various_tools.core.media.library import MediaLibrary, library_path
from various_tools.core.plan import Plan, RunResult
from various_tools.gui.state import AppState
from various_tools.gui.workers import Worker, tool_worker
from various_tools.tools.set_date import TOOL, SetDateOptions
from various_tools.tools.set_date.tool import ARROW, MAX_YEAR, MIN_YEAR

log = logging.getLogger(__name__)

MODES: tuple[tuple[str, str], ...] = (
    ("exact", "Exact date and time"),
    ("month_year", "Month and year only (keep the day and time)"),
    ("shift", "Shift by days, hours and minutes (the camera clock was wrong)"),
)
DATE_LENGTH = len("2021-03-04 10:12")
PREVIEW_DELAY_MS = 350


class ChangeDateDialog(QDialog):
    datesChanged = Signal(list)  # noqa: N815  the paths that were changed

    def __init__(self, state: AppState, paths: list[Path], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.paths = list(paths)
        self.plan: Plan | None = None
        self.result: RunResult | None = None
        self.worker: Worker | None = None
        self._plan_options: str | None = None
        self._stale = True
        self.confirm: Callable[[str, str], bool] = self._default_confirm
        self.setWindowTitle("Change date")
        self.resize(720, 560)

        count = len(self.paths)
        intro = QLabel(
            f"Change the date of {count:,} selected {'file' if count == 1 else 'files'}. "
            "The list below shows each file's old and new date. Nothing changes until you "
            "press Change dates, and the originals go to the Recycle Bin, so History can undo it."
        )
        intro.setWordWrap(True)

        self.mode_box = QComboBox()
        for value, label in MODES:
            self.mode_box.addItem(label, value)
        self.exact_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.exact_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.exact_edit.setCalendarPopup(True)
        self.month_box = QComboBox()
        for number in range(1, 13):
            self.month_box.addItem(calendar.month_name[number], number)
        self.year_spin = QSpinBox()
        self.year_spin.setRange(MIN_YEAR, MAX_YEAR)
        self.year_spin.setValue(datetime.now().year)
        self.days_spin = QSpinBox()
        self.days_spin.setRange(-36500, 36500)
        self.days_spin.setSuffix(" days")
        self.hours_spin = QSpinBox()
        self.hours_spin.setRange(-8760, 8760)
        self.hours_spin.setSuffix(" hours")
        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(-525600, 525600)
        self.minutes_spin.setSuffix(" minutes")

        self.form = QFormLayout()
        self.form.addRow("What to change", self.mode_box)
        self.form.addRow("New date and time", self.exact_edit)
        month_row = QHBoxLayout()
        month_row.addWidget(self.month_box)
        month_row.addWidget(self.year_spin)
        self._month_row = QWidget()
        self._month_row.setLayout(month_row)
        self.form.addRow("New month and year", self._month_row)
        shift_row = QHBoxLayout()
        for spin in (self.days_spin, self.hours_spin, self.minutes_spin):
            shift_row.addWidget(spin)
        self._shift_row = QWidget()
        self._shift_row.setLayout(shift_row)
        self.form.addRow("Move every date by", self._shift_row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["File", "Now", "Will become", "Note"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 230)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 130)
        self.notes_label = QLabel("")
        self.notes_label.setWordWrap(True)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        self.apply_button = QPushButton("Change dates")
        self.apply_button.setDefault(True)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(lambda _c=False: self.apply())
        self.close_button = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button.rejected.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(self.form)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.notes_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(PREVIEW_DELAY_MS)
        self._timer.timeout.connect(self.preview)
        self.mode_box.currentIndexChanged.connect(lambda _i: self._inputs_changed())
        self.exact_edit.dateTimeChanged.connect(lambda _v: self._inputs_changed())
        self.month_box.currentIndexChanged.connect(lambda _i: self._inputs_changed())
        for spin in (self.year_spin, self.days_spin, self.hours_spin, self.minutes_spin):
            spin.valueChanged.connect(lambda _v: self._inputs_changed())
        self._show_mode()
        self.preview()

    # ----- the options -------------------------------------------------------------------------

    def mode(self) -> str:
        return str(self.mode_box.currentData())

    def options(self) -> SetDateOptions:
        mode = self.mode()
        if mode == "exact":
            return SetDateOptions(
                paths=self.paths,
                mode="exact",
                new_date=self.exact_edit.dateTime().toString("yyyy-MM-dd HH:mm"),
            )
        if mode == "month_year":
            return SetDateOptions(
                paths=self.paths,
                mode="month_year",
                year=self.year_spin.value(),
                month=int(self.month_box.currentData()),
            )
        return SetDateOptions(
            paths=self.paths,
            mode="shift",
            shift_days=self.days_spin.value(),
            shift_hours=self.hours_spin.value(),
            shift_minutes=self.minutes_spin.value(),
        )

    def _show_mode(self) -> None:
        mode = self.mode()
        self._set_row_visible(self.exact_edit, mode == "exact")
        self._set_row_visible(self._month_row, mode == "month_year")
        self._set_row_visible(self._shift_row, mode == "shift")

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        self.form.setRowVisible(field, visible)

    def _inputs_changed(self) -> None:
        self._show_mode()
        self._stale = True
        self.apply_button.setEnabled(False)
        self.status_label.setText("Checking the new dates…")
        self._timer.start()

    # ----- preview -----------------------------------------------------------------------------

    def preview(self) -> Worker | None:
        """Scan with the current choices and list old and new for each file. Changes nothing."""
        self._timer.stop()
        if self.worker is not None:
            self._timer.start()  # try again when the running job is done
            return None
        options = self.options()
        self._stale = False
        job = self._scan_job(options)
        worker = tool_worker(self.state, job, with_journal=False)
        worker.succeeded.connect(lambda plan, o=options: self._previewed(o, plan))
        worker.failed.connect(lambda exc: self._failed(exc))
        worker.finished.connect(self._worker_finished)
        self.worker = worker
        self._update_buttons()
        worker.start()
        return worker

    @staticmethod
    def _scan_job(options: SetDateOptions) -> Callable[[RunContext], Plan]:
        def job(ctx: RunContext) -> Plan:
            return TOOL.scan(options, ctx)

        return job

    def _previewed(self, options: SetDateOptions, plan: object) -> None:
        if not isinstance(plan, Plan):
            return
        self.plan = plan
        self._plan_options = options.model_dump_json()
        self._fill_table(plan)

    def _fill_table(self, plan: Plan) -> None:
        self.table.setRowCount(len(plan.actions))
        for row, action in enumerate(plan.actions):
            old, _arrow, rest = action.reason.partition(f" {ARROW} ")
            new, note = rest[:DATE_LENGTH], rest[DATE_LENGTH:].strip(" ()")
            name = action.src.name if action.src is not None else ""
            for col, text in enumerate((name, old, new, note)):
                self.table.setItem(row, col, QTableWidgetItem(text))
            first = self.table.item(row, 0)
            if first is not None and action.src is not None:
                first.setToolTip(str(action.src))
        skipped = [n for n in plan.notes if n.startswith("skipped")]
        extra = [n for n in plan.notes if not n.startswith("skipped")]
        text = " ".join(extra)
        if skipped:
            text += f" {len(skipped)} skipped (for example cloud-only files): {skipped[0]}"
        self.notes_label.setText(text.strip())
        count = len(plan.actions)
        self.status_label.setText(
            f"{count:,} {'file' if count == 1 else 'files'} will change."
            if count
            else "No file would change with these choices."
        )

    def preview_rows(self) -> list[tuple[str, str, str]]:
        """``(file, old, new)`` for each row of the list."""
        rows: list[tuple[str, str, str]] = []
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, c) for c in range(3)]
            rows.append(tuple(c.text() if c is not None else "" for c in cells))  # type: ignore[arg-type]
        return rows

    # ----- apply -------------------------------------------------------------------------------

    def can_apply(self) -> bool:
        return (
            self.worker is None
            and not self._stale
            and self.plan is not None
            and bool(self.plan.actions)
            and self._plan_options == self.options().model_dump_json()
        )

    def apply(self) -> Worker | None:
        """Ask once more, then change the dates (journaled) and read the files again."""
        plan = self.plan
        if plan is None or not self.can_apply():
            return None
        count = len(plan.actions)
        text = (
            f"Change the date of {count:,} {'file' if count == 1 else 'files'}?\n\n"
            "The originals go to the Recycle Bin and can be restored from History."
        )
        if any("ExifTool not found" in a.reason for a in plan.actions):
            text += (
                "\n\nExifTool was not found, so only the modified time changes, not the date "
                "inside the file."
            )
        if not self.confirm("Change dates?", text):
            return None
        changed = [a.src for a in plan.actions if a.src is not None]
        db = library_path(self.state.settings)

        def job(ctx: RunContext) -> RunResult:
            result = TOOL.apply(plan, ctx)
            done = {o.src for o in result.outcomes if o.status == "done" and o.src}
            with MediaLibrary(db) as lib:
                lib.refresh_paths([p for p in changed if str(p) in done], ctx)
            return result

        worker = tool_worker(self.state, job, with_journal=True)
        worker.succeeded.connect(lambda result, c=changed: self._applied(result, c))
        worker.failed.connect(lambda exc: self._failed(exc))
        worker.finished.connect(self._worker_finished)
        self.worker = worker
        self._update_buttons()
        self.status_label.setText("Changing the dates…")
        worker.start()
        return worker

    def _applied(self, result: object, changed: list[Path]) -> None:
        if not isinstance(result, RunResult):
            return
        self.result = result
        done = [Path(o.src) for o in result.outcomes if o.status == "done" and o.src]
        failed = result.count("failed") + result.count("skipped")
        text = f"Changed {len(done):,} {'file' if len(done) == 1 else 'files'}."
        if failed:
            text += f" {failed:,} could not be changed (see History)."
        self.status_label.setText(text + " You can undo this from History.")
        self.datesChanged.emit(done)
        self.plan = None
        self.table.setRowCount(0)
        del changed

    def _failed(self, exc: object) -> None:
        self.status_label.setText(f"Something went wrong: {type(exc).__name__}: {exc}")

    def _worker_finished(self) -> None:
        self.worker = None
        self._update_buttons()
        if self._stale:
            self._timer.start()

    def _update_buttons(self) -> None:
        applying = self.worker is not None and self.worker.writes
        self.apply_button.setEnabled(self.can_apply())
        for widget in (
            self.mode_box,
            self.exact_edit,
            self.month_box,
            self.year_spin,
            self.days_spin,
            self.hours_spin,
            self.minutes_spin,
        ):
            widget.setEnabled(not applying)

    def reject(self) -> None:
        if self.worker is not None and self.worker.writes:
            return  # never close in the middle of changing files
        if self.worker is not None:
            self.worker.cancel()
        super().reject()

    def _default_confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes
