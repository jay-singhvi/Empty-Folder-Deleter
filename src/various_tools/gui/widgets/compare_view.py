"""Side-by-side view of one duplicate group: the kept file and each duplicate.

All file reading happens in background workers: thumbnails, sizes and "Explain match". A
cloud-only OneDrive file is never opened (rule 3). Its card says so, and Explain match refuses.
"Keep this one" only edits the plan in memory (``plan_edit.change_keeper``); nothing on disk
changes until the user applies the plan.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.media.thumbs import FileFacts, file_facts, image_size, thumbnail_jpeg
from various_tools.core.plan import Plan, PlannedAction
from various_tools.gui import plan_edit
from various_tools.gui.workers import Worker

if TYPE_CHECKING:
    from various_tools.tools.duplicates.explain import ExplainReport
    from various_tools.tools.duplicates.images import ImageRules

CLOUD_ONLY_TEXT = "Cloud-only: not downloaded"
NO_PREVIEW_TEXT = "No preview"
THUMB_BOX = 260
CARD_WIDTH = 300

# Running workers stay referenced here until their thread has really finished, even when the
# dialog that started them is already closed. Muted workers have been told to stop and their
# results are dropped.
_LIVE: set[Worker] = set()
_MUTED: set[Worker] = set()


def _forget(worker: Worker) -> None:
    worker.wait(2000)
    _LIVE.discard(worker)
    _MUTED.discard(worker)


def _start(worker: Worker) -> Worker:
    _LIVE.add(worker)
    worker.finished.connect(lambda w=worker: _forget(w))
    worker.start()
    return worker


def _unplug(worker: Worker) -> None:
    """Stop a worker from reaching the UI: ask it to stop and drop its result."""
    worker.cancel()
    _MUTED.add(worker)


def _on_result(worker: Worker, handler: Callable[[Any], None]) -> None:
    """Call ``handler`` with the worker's result or exception, unless it was unplugged."""

    def deliver(value: Any) -> None:
        if worker not in _MUTED:
            handler(value)

    worker.succeeded.connect(deliver)


def _on_failure(worker: Worker, handler: Callable[[BaseException], None]) -> None:
    def deliver(exc: object) -> None:
        if worker not in _MUTED and isinstance(exc, BaseException):
            handler(exc)

    worker.failed.connect(deliver)


@dataclass(frozen=True)
class CardInfo:
    """What the background worker found out about one file."""

    facts: FileFacts
    resolution: tuple[int, int] | None
    thumbnail: bytes | None


@dataclass(frozen=True)
class ExplainOutcome:
    """The result of Explain match: a report, or the reason there is none."""

    report: ExplainReport | None
    message: str = ""


def _card_job(path: Path) -> Callable[[Worker], CardInfo]:
    def job(worker: Worker) -> CardInfo:
        facts = file_facts(path)
        if facts.cloud_only or not facts.exists:
            return CardInfo(facts, None, None)
        worker.check_cancelled()
        resolution = image_size(path)
        worker.check_cancelled()
        return CardInfo(facts, resolution, thumbnail_jpeg(path, THUMB_BOX))

    return job


def rules_for_plan(plan: Plan) -> ImageRules:
    """The image rules the plan was scanned with, or the tuned defaults if they can't be read."""
    from various_tools.tools.duplicates.images import ImageRules

    try:
        from various_tools.tools.duplicates import DuplicatesOptions

        return DuplicatesOptions.model_validate_json(plan.options_json).image_rules()
    except Exception:  # noqa: BLE001 - any bad or missing options just mean "use the defaults"
        return ImageRules()


class ExplainDialog(QDialog):
    """A small table of every check, then a verdict line (like ``duplicates explain``)."""

    def __init__(self, a: Path, b: Path, outcome: ExplainOutcome, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Explain match")
        self.resize(640, 360)
        layout = QVBoxLayout(self)
        report = outcome.report
        a_text = f"A: {a}"
        b_text = f"B: {b}"
        if report is not None:
            a_text += f" ({report.a.w}x{report.a.h})"
            b_text += f" ({report.b.w}x{report.b.h})"
        for text in (a_text, b_text):
            label = QLabel(text)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            layout.addWidget(label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Check", "Value", "Rule", "Result"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.verdict_label = QLabel()
        self.verdict_label.setWordWrap(True)
        if report is None:
            self.table.hide()
            self.verdict_label.setText(outcome.message)
        else:
            for check in report.checks:
                row = self.table.rowCount()
                self.table.insertRow(row)
                result = QTableWidgetItem("pass" if check.ok else "FAIL")
                result.setForeground(QColor("#1a7f37" if check.ok else "#c62828"))
                for column, text in enumerate((check.name, check.value, check.rule)):
                    self.table.setItem(row, column, QTableWidgetItem(text))
                self.table.setItem(row, 3, result)
            if report.matched:
                self.verdict_label.setText("Verdict: visual duplicate (every check passed).")
            else:
                failed = ", ".join(c.name for c in report.checks if not c.ok)
                self.verdict_label.setText(f"Verdict: not a visual duplicate (failed: {failed}).")
        layout.addWidget(self.table, 1)
        layout.addWidget(self.verdict_label)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row_layout = QHBoxLayout()
        row_layout.addStretch(1)
        row_layout.addWidget(close)
        layout.addLayout(row_layout)


class CompareCard(QFrame):
    """One file of the group: thumbnail, facts and, for a duplicate, the two buttons."""

    def __init__(
        self,
        path: Path,
        action: PlannedAction | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.path = path
        self.action = action
        self.is_keeper = action is None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedWidth(CARD_WIDTH)

        self.role_label = QLabel("Kept" if self.is_keeper else "Duplicate")
        font = self.role_label.font()
        font.setBold(True)
        self.role_label.setFont(font)

        self.thumb_label = QLabel("Loading…")
        self.thumb_label.setFixedSize(THUMB_BOX, THUMB_BOX)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setFrameShape(QFrame.Shape.Box)
        self.thumb_label.setWordWrap(True)

        self.name_label = self._text_label(path.name or str(path), bold=True)
        self.folder_label = self._text_label(str(path.parent))
        self.folder_label.setToolTip(str(path.parent))
        self.resolution_label = self._text_label("Resolution: …")
        self.size_label = self._text_label("Size: …")
        self.modified_label = self._text_label("Modified: …")
        self.action_label = self._text_label("")
        if action is not None:
            self.action_label.setText(self._action_text(action))
            self.action_label.setToolTip(self.action_label.text())
        else:
            self.action_label.hide()

        self.keep_button = QPushButton("Keep this one")
        self.explain_button = QPushButton("Explain match")
        if self.is_keeper:
            self.keep_button.hide()
            self.explain_button.hide()

        layout = QVBoxLayout(self)
        layout.addWidget(self.role_label)
        layout.addWidget(self.thumb_label, 0, Qt.AlignmentFlag.AlignHCenter)
        for label in (
            self.name_label,
            self.folder_label,
            self.resolution_label,
            self.size_label,
            self.modified_label,
            self.action_label,
        ):
            layout.addWidget(label)
        layout.addStretch(1)
        layout.addWidget(self.keep_button)
        layout.addWidget(self.explain_button)

    @staticmethod
    def _text_label(text: str, *, bold: bool = False) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if bold:
            font = label.font()
            font.setBold(True)
            label.setFont(font)
        return label

    @staticmethod
    def _action_text(action: PlannedAction) -> str:
        text = f"Action: {action.kind}"
        if action.dst is not None:
            text += f" to {action.dst.parent}"
        if action.reason:
            text += f"\n{action.reason}"
        if not action.selected:
            text += "\n(unchecked: will be left alone)"
        return text

    def show_info(self, info: CardInfo) -> None:
        """Fill the card from the worker's result (called on the UI thread)."""
        facts = info.facts
        if not facts.exists:
            self.thumb_label.setText(NO_PREVIEW_TEXT)
            self.resolution_label.setText("Resolution: unknown")
            self.size_label.setText("Size: file is missing")
            self.modified_label.setText("Modified: unknown")
            return
        self.size_label.setText(f"Size: {plan_edit.human_size(facts.size)}")
        modified = facts.modified.strftime("%Y-%m-%d %H:%M") if facts.modified else "unknown"
        self.modified_label.setText(f"Modified: {modified}")
        if facts.cloud_only:
            self.thumb_label.setText(CLOUD_ONLY_TEXT)
            self.resolution_label.setText("Resolution: not read (cloud-only)")
            return
        if info.resolution is not None:
            width, height = info.resolution
            self.resolution_label.setText(f"Resolution: {width} x {height}")
        else:
            self.resolution_label.setText("Resolution: unknown")
        pixmap = QPixmap()
        if info.thumbnail is not None and pixmap.loadFromData(info.thumbnail):
            self.thumb_label.setPixmap(
                pixmap.scaled(
                    THUMB_BOX,
                    THUMB_BOX,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.thumb_label.setText(NO_PREVIEW_TEXT)

    def show_failure(self) -> None:
        self.thumb_label.setText(NO_PREVIEW_TEXT)
        self.resolution_label.setText("Resolution: unknown")
        self.size_label.setText("Size: unknown")
        self.modified_label.setText("Modified: unknown")


class CompareDialog(QDialog):
    """The kept file first, then each duplicate of one group, in a horizontal row of cards."""

    keeperChanged = Signal(str)

    def __init__(self, plan: Plan, group_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plan = plan
        self._group_id = group_id
        self._workers: list[Worker] = []
        self._closed = False
        self.cards: list[CompareCard] = []
        self.explain_dialogs: list[ExplainDialog] = []
        self.resize(1000, 640)

        self._title = QLabel()
        self._container = QWidget()
        self._row = QHBoxLayout(self._container)
        self._row.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._container)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self._scroll, 1)
        layout.addLayout(footer)
        self._rebuild()

    # ----- public ---------------------------------------------------------------------------

    @property
    def group_id(self) -> str:
        return self._group_id

    def keep(self, path: Path) -> str:
        """Make ``path`` the kept file ("Keep this one"), rebuild the cards, return the new id."""
        new_id = plan_edit.change_keeper(self._plan, self._group_id, path)
        self._group_id = new_id
        self._rebuild()
        self.keeperChanged.emit(new_id)
        return new_id

    def explain_pair(self, a: Path, b: Path) -> Worker:
        """Start Explain match for two files in a worker and show the result when it is done.

        Cloud-only files are refused before anything is read (rule 3).
        """
        rules = rules_for_plan(self._plan)

        def job(worker: Worker) -> ExplainOutcome:
            for path in (a, b):
                if file_facts(path).cloud_only:
                    return ExplainOutcome(None, f"skipped: cloud-only ({path.name})")
            worker.check_cancelled()
            from various_tools.tools.duplicates.explain import explain

            try:
                return ExplainOutcome(explain(a, b, rules))
            except (ValueError, OSError) as exc:
                return ExplainOutcome(None, str(exc))

        worker = Worker(job)
        _on_result(worker, lambda outcome: self._show_explain(a, b, outcome))
        _on_failure(worker, lambda exc: self._show_explain(a, b, ExplainOutcome(None, str(exc))))
        self._track(worker)
        _start(worker)
        return worker

    def wait_for_workers(self, timeout_ms: int = 10000) -> bool:
        """Wait (keeping the UI alive) until every worker has finished. For tests."""
        from PySide6.QtCore import QCoreApplication, QDeadlineTimer

        deadline = QDeadlineTimer(timeout_ms)
        while any(not w.isFinished() for w in self._workers):
            if deadline.hasExpired():
                return False
            QCoreApplication.processEvents()
        QCoreApplication.processEvents()
        return True

    # ----- internals ------------------------------------------------------------------------

    def _track(self, worker: Worker) -> None:
        self._workers = [w for w in self._workers if not w.isFinished()]
        self._workers.append(worker)

    def _rebuild(self) -> None:
        for worker in self._workers:
            _unplug(worker)
        self._workers = []
        for card in self.cards:
            self._row.removeWidget(card)
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.cards = []
        keeper = Path(self._group_id)
        actions = plan_edit.group_actions(self._plan, self._group_id)
        self.setWindowTitle(f"Compare: {keeper.name}")
        self._title.setText(
            f"{len(actions)} duplicate(s) of {keeper.name}. "
            "Pick “Keep this one” to keep a different file."
        )
        self._add_card(CompareCard(keeper, None))
        for action in actions:
            if action.src is None:
                continue
            card = CompareCard(action.src, action)
            card.keep_button.clicked.connect(lambda _=False, p=action.src: self.keep(p))
            card.explain_button.clicked.connect(
                lambda _=False, p=action.src, c=card: self._explain_clicked(c, p)
            )
            self._add_card(card)

    def _add_card(self, card: CompareCard) -> None:
        self.cards.append(card)
        self._row.addWidget(card)
        worker = Worker(_card_job(card.path))
        _on_result(worker, card.show_info)
        _on_failure(worker, lambda _exc, c=card: c.show_failure())
        self._track(worker)
        _start(worker)

    def _explain_clicked(self, card: CompareCard, duplicate: Path) -> None:
        card.explain_button.setEnabled(False)
        worker = self.explain_pair(Path(self._group_id), duplicate)
        worker.finished.connect(lambda c=card: self._reenable(c))

    @staticmethod
    def _reenable(card: CompareCard) -> None:
        # A RuntimeError means the card was replaced by a rebuild.
        with contextlib.suppress(RuntimeError):
            card.explain_button.setEnabled(True)

    def _show_explain(self, a: Path, b: Path, outcome: ExplainOutcome) -> None:
        if self._closed:
            return
        dialog = ExplainDialog(a, b, outcome, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.explain_dialogs.append(dialog)
        dialog.show()

    def _shutdown(self) -> None:
        """Stop listening to workers so a late result never touches deleted widgets."""
        self._closed = True
        for worker in self._workers:
            _unplug(worker)

    def done(self, result: int) -> None:
        self._shutdown()
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._shutdown()
        super().closeEvent(event)
