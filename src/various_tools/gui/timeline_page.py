"""The Timeline page: photos and videos by year and month.

It never changes the user's files, so there is no Scan or Apply. "Update the timeline" brings
the media library (a cache) up to date in a background worker; only new or changed files are
read, and OneDrive cloud-only files are never read (AGENTS.md rule 3). On open, the page shows
what the library already has, so browsing needs no update.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.context import RunContext
from various_tools.core.media.library import MediaItem, MediaLibrary, SyncReport, library_path
from various_tools.gui.state import AppState
from various_tools.gui.widgets.media_grid import MediaGrid
from various_tools.gui.widgets.path_picker import PathListEdit
from various_tools.gui.workers import Worker, tool_worker

log = logging.getLogger(__name__)

STEP_TEXTS = ("1  Choose your photo folders", "2  Update the timeline")
UPDATE_TEXT = "Update the timeline"
NO_DATE = "No date"

_KEY = Qt.ItemDataRole.UserRole + 1


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n:,} {singular if n == 1 else plural}"


def summary_text(photos: int, videos: int, report: SyncReport | None = None) -> str:
    """For example "1,234 photos and 56 videos. Updated: 12 new, 3 removed."."""
    text = f"{_count(photos, 'photo', 'photos')} and {_count(videos, 'video', 'videos')}."
    if report is None:
        return text
    text += f" Updated: {report.added:,} new, {report.removed:,} removed."
    if report.cloud_only:
        text += f" {report.cloud_only:,} are in OneDrive only."
    if report.errors:
        notes = _count(len(report.errors), "note", "notes")
        text += f" {notes} (point here to read it)."
    return text


def month_key(item: MediaItem) -> tuple[int, int] | None:
    return (item.taken.year, item.taken.month) if item.taken is not None else None


class TimelinePage(QWidget):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.worker: Worker | None = None
        self._items: list[MediaItem] = []
        self._closing = False

        title = QLabel("<h2>Timeline</h2>")
        description = QLabel(
            "Your photos and videos by the date they were taken. Double-click one to open it. "
            "Nothing is changed or moved."
        )
        description.setWordWrap(True)
        steps: list[QLabel] = []
        for text in STEP_TEXTS:
            label = QLabel(text)
            font = label.font()
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() * 1.1)
            label.setFont(font)
            steps.append(label)

        self.folders = PathListEdit(self)
        self.folders.set_paths(str(p) for p in state.settings.library_folders)
        self.folders.changed.connect(self._save_folders)

        self.update_button = QPushButton(UPDATE_TEXT)
        self.update_button.setDefault(True)
        self.update_button.clicked.connect(lambda _c=False: self.update())
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(lambda _c=False: self.cancel())
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        self.months = QTreeWidget()
        self.months.setHeaderHidden(True)
        self.months.setColumnCount(1)
        self.months.currentItemChanged.connect(lambda _now, _before: self._show_selected())
        self.grid = MediaGrid(self)

        buttons = QHBoxLayout()
        buttons.addWidget(self.update_button)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.progress_bar, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.months)
        splitter.addWidget(self.grid)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([200, 800])

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(steps[0])
        layout.addWidget(self.folders)
        layout.addWidget(steps[1])
        layout.addLayout(buttons)
        layout.addWidget(self.status_label)
        layout.addWidget(splitter, 1)

        self.refresh()
        self.status_label.setText(self._summary())

    # ----- folders ---------------------------------------------------------------------------

    def roots(self) -> list[Path]:
        return [Path(p) for p in self.folders.paths()]

    def _save_folders(self) -> None:
        try:
            self.state.update_settings(library_folders=[str(p) for p in self.roots()])
        except Exception as exc:  # noqa: BLE001 - a bad path must not crash the page
            self._set_status(f"Could not save the folders: {exc}", error=True)
            return
        self.refresh()
        self._set_status(self._summary())

    # ----- library -----------------------------------------------------------------------------

    def refresh(self) -> None:
        """Read the library for the chosen folders and rebuild the month tree."""
        roots = self.roots()
        path = library_path(self.state.settings)
        items: list[MediaItem] = []
        if roots and path.is_file():
            try:
                with MediaLibrary(path) as lib:
                    items = lib.items(roots)
            except Exception as exc:  # noqa: BLE001 - a broken cache shows as empty
                log.warning("Could not read the media library %s: %s", path, exc)
        self._items = items
        self._build_tree()

    def _build_tree(self) -> None:
        previous = self._selected_key()
        self.months.blockSignals(True)
        self.months.clear()
        years: dict[int, dict[int, int]] = {}
        undated = 0
        for item in self._items:
            key = month_key(item)
            if key is None:
                undated += 1
                continue
            months = years.setdefault(key[0], {})
            months[key[1]] = months.get(key[1], 0) + 1
        select: QTreeWidgetItem | None = None
        first: QTreeWidgetItem | None = None
        for year in sorted(years, reverse=True):
            months = years[year]
            node = QTreeWidgetItem([f"{year} ({sum(months.values()):,})"])
            node.setData(0, _KEY, (year, 0))
            self.months.addTopLevelItem(node)
            for month in sorted(months, reverse=True):
                child = QTreeWidgetItem([f"{calendar.month_name[month]} ({months[month]:,})"])
                child.setData(0, _KEY, (year, month))
                node.addChild(child)
                first = first or child
                if previous == (year, month):
                    select = child
            if previous == (year, 0):
                select = node
            node.setExpanded(True)
        if undated:
            node = QTreeWidgetItem([f"{NO_DATE} ({undated:,})"])
            node.setData(0, _KEY, (0, 0))
            self.months.addTopLevelItem(node)
            first = first or node
        self.months.blockSignals(False)
        target = select or first
        if target is not None:
            self.months.setCurrentItem(target)
        self._show_selected()

    def _selected_key(self) -> tuple[int, int] | None:
        current = self.months.currentItem()
        if current is None:
            return None
        value = current.data(0, _KEY)
        return value if isinstance(value, tuple) else None  # pyright: ignore[reportUnknownVariableType]

    def _show_selected(self) -> None:
        key = self._selected_key()
        self.grid.set_items(self._items_for(key) if key is not None else [])

    def _items_for(self, key: tuple[int, int]) -> list[MediaItem]:
        year, month = key
        if year == 0:
            return [i for i in self._items if i.taken is None]
        return [
            i
            for i in self._items
            if i.taken is not None
            and i.taken.year == year
            and (month == 0 or i.taken.month == month)
        ]

    def _summary(self, report: SyncReport | None = None) -> str:
        videos = sum(1 for i in self._items if i.kind == "video")
        return summary_text(len(self._items) - videos, videos, report)

    # ----- update ------------------------------------------------------------------------------

    def update(self) -> Worker | None:  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        """Bring the library up to date for the chosen folders, in the background.

        None when a job already runs, the window is closing or no folder is chosen. (This
        replaces ``QWidget.update``; the widget is repainted with ``repaint`` if ever needed.)
        """
        if self.worker is not None or self._closing:
            return None
        roots = self.roots()
        if not roots:
            self._set_status("Add a folder with photos or videos first.", error=True)
            return None
        db = library_path(self.state.settings)

        def job(ctx: RunContext) -> SyncReport:
            with MediaLibrary(db) as lib:
                return lib.sync(roots, ctx)

        worker = tool_worker(self.state, job, with_journal=False)
        worker.succeeded.connect(self._update_done)
        worker.failed.connect(
            lambda exc: self._set_status(f"Could not update: {type(exc).__name__}: {exc}", True)
        )
        worker.cancelled.connect(self._update_cancelled)
        worker.progress.connect(self._progress)
        worker.finished.connect(self._finished)
        self.worker = worker
        self.progress_bar.setRange(0, 0)
        self._set_status("Updating: looking for new and changed photos and videos…")
        self._update_buttons()
        worker.start()
        return worker

    def _progress(self, done: int, total: int, message: str) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
            self.progress_bar.setFormat(f"%v / %m  {message[:60]}")
        else:
            self.progress_bar.setRange(0, 0)

    def _update_done(self, report: object) -> None:
        self.refresh()
        if isinstance(report, SyncReport):
            self._set_status(self._summary(report))
            self.status_label.setToolTip("\n".join(report.errors[:50]))
            for note in report.errors:
                log.info("Timeline update: %s", note)

    def _update_cancelled(self) -> None:
        self.refresh()
        self._set_status("Stopped. What was read so far is kept; update again to finish.")

    def _finished(self) -> None:
        self.worker = None
        self._update_buttons()

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self._set_status("Stopping: waiting for the current file to finish…")

    def shutdown(self) -> None:
        """The window is closing: start nothing new and stop what runs. Does not wait."""
        self._closing = True
        self.grid.shutdown()
        self.cancel()
        self._update_buttons()

    def _update_buttons(self) -> None:
        running = self.worker is not None
        self.update_button.setEnabled(not running and not self._closing)
        self.cancel_button.setVisible(running)
        self.cancel_button.setEnabled(running)
        self.progress_bar.setVisible(running)
        self.folders.setEnabled(not running)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #c0392b;" if error else "")
        if not error:
            self.status_label.setToolTip("")

    def items(self) -> Sequence[MediaItem]:
        """Every item shown on the page (all months)."""
        return list(self._items)
