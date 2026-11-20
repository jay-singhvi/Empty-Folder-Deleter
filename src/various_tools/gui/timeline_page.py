"""The Timeline page: photos and videos by month, in one scrolling grid.

Browsing never changes the user's files, so there is no Scan or Apply here. "Update the
timeline" brings the media library (a cache) up to date in a background worker; only new or
changed files are read, and OneDrive cloud-only files are never read (AGENTS.md rule 3). On
open, the page shows what the library already has. The one thing that changes files is
"Change date…" in the right-click menu, which runs the ``set_date`` tool (preview first,
journaled, undone from History).

The Timeline and the Map share one library list and one filter bar (``library_model``).
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.context import RunContext
from various_tools.core.media.library import MediaItem, MediaLibrary, SyncReport, library_path
from various_tools.gui.change_date_dialog import ChangeDateDialog
from various_tools.gui.library_folders_dialog import LibraryFoldersDialog
from various_tools.gui.library_model import LibraryModel, library_model
from various_tools.gui.state import AppState
from various_tools.gui.widgets.filter_bar import FilterBar
from various_tools.gui.widgets.media_grid import (
    DEFAULT_THUMB,
    MAX_THUMB,
    MIN_THUMB,
    MediaGrid,
)
from various_tools.gui.widgets.preview_pane import PhotoViewer, PreviewPane
from various_tools.gui.workers import Worker, tool_worker

log = logging.getLogger(__name__)

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


def timeline_order(items: Sequence[MediaItem]) -> list[MediaItem]:
    """Newest month and day first; inside a day oldest first; items without a date last."""

    def key(item: MediaItem) -> tuple[int, float, float, str]:
        if item.taken is None:
            return (1, 0.0, 0.0, str(item.path).casefold())
        day = item.taken.replace(hour=0, minute=0, second=0, microsecond=0)
        return (0, -day.timestamp(), item.taken.timestamp(), str(item.path).casefold())

    return sorted(items, key=key)


class TimelinePage(QWidget):
    showOnMap = Signal(object)  # noqa: N815 (Qt name) a Path: "Show on map" was chosen

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.model: LibraryModel = library_model(state)
        self.worker: Worker | None = None
        self._closing = False
        self._shown: list[MediaItem] = []
        self.confirm: Callable[[str, str], bool] | None = None
        self.change_dialog: ChangeDateDialog | None = None
        self.viewer: PhotoViewer | None = None

        title = QLabel("<h2>Timeline</h2>")
        description = QLabel(
            "Your photos and videos by the date they were taken. Double-click a photo to see it "
            "large. Right-click for more, including Change date. Nothing is changed unless you "
            "choose Change date."
        )
        description.setWordWrap(True)

        self.update_button = QPushButton(UPDATE_TEXT)
        self.update_button.setDefault(True)
        self.update_button.clicked.connect(lambda _c=False: self.update_library())
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(lambda _c=False: self.cancel())
        self.folders_button = QPushButton("Library folders…")
        self.folders_button.clicked.connect(lambda _c=False: self.choose_folders())
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        self.filter_bar = FilterBar(self.model)

        self.months = QTreeWidget()
        self.months.setHeaderHidden(True)
        self.months.setColumnCount(1)
        self.months.currentItemChanged.connect(self._jump_selected)
        self.grid = MediaGrid(self, grouped=True)
        self.grid.activate = self.activate_row
        self.grid.selectionChanged.connect(self._selection_changed)
        self.grid.visibleMonthChanged.connect(self._follow_month)
        self.grid.showOnMapRequested.connect(self.showOnMap)
        self.grid.changeDateRequested.connect(self.change_dates)
        self.preview = PreviewPane()
        self.preview.setVisible(False)

        self.years_button = QToolButton()
        self.years_button.setText("Years")
        self.years_button.setCheckable(True)
        self.years_button.setChecked(True)
        self.years_button.setToolTip("Show or hide the list of years and months")
        self.years_button.toggled.connect(self.months.setVisible)
        self.preview_button = QToolButton()
        self.preview_button.setText("i")
        self.preview_button.setCheckable(True)
        self.preview_button.setToolTip("Show or hide the preview (press I)")
        self.preview_button.toggled.connect(self.set_preview_visible)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(MIN_THUMB, MAX_THUMB)
        self.size_slider.setValue(DEFAULT_THUMB)
        self.size_slider.setFixedWidth(150)
        self.size_slider.setToolTip("Picture size (or hold Ctrl and use the mouse wheel)")
        self.size_slider.valueChanged.connect(self.grid.set_thumb_size)
        self.grid.thumbSizeChanged.connect(self._size_changed)
        self.selection_label = QLabel("")

        buttons = QHBoxLayout()
        buttons.addWidget(self.update_button)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.folders_button)
        buttons.addWidget(self.progress_bar, 1)
        tools = QHBoxLayout()
        tools.addWidget(self.years_button)
        tools.addWidget(self.selection_label, 1)
        tools.addWidget(QLabel("Size"))
        tools.addWidget(self.size_slider)
        tools.addWidget(self.preview_button)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.months)
        self.splitter.addWidget(self.grid)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([160, 640, 280])

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addLayout(buttons)
        layout.addWidget(self.filter_bar)
        layout.addWidget(self.status_label)
        layout.addLayout(tools)
        layout.addWidget(self.splitter, 1)

        self.grid.previewToggleRequested.connect(self.toggle_preview)
        self.model.changed.connect(self._model_changed)
        self._rebuild()
        self.status_label.setText(self._summary())

    # ----- folders -----------------------------------------------------------------------------

    def roots(self) -> list[Path]:
        return self.model.roots()

    def choose_folders(self) -> LibraryFoldersDialog:
        """Open the "Library folders" dialog (not modal, so tests can drive it)."""
        dialog = LibraryFoldersDialog(self.state, self)
        dialog.on_saved = self._folders_saved
        dialog.open()
        return dialog

    def _folders_saved(self) -> None:
        self.refresh()
        self._set_status(self._summary())

    # ----- the list ----------------------------------------------------------------------------

    def refresh(self) -> None:
        """Read the library again for the chosen folders."""
        self.model.reload()

    def _model_changed(self) -> None:
        self._rebuild()
        if self.worker is None:
            self._set_status(self._summary())

    def _rebuild(self) -> None:
        shown = timeline_order(self.model.shown)
        self._shown = shown
        self.grid.set_items(shown)
        self._build_tree()
        self._selection_changed()

    def _build_tree(self) -> None:
        self.months.blockSignals(True)
        self.months.clear()
        years: dict[int, dict[int, int]] = {}
        undated = 0
        for item in self._shown:
            if item.taken is None:
                undated += 1
                continue
            months = years.setdefault(item.taken.year, {})
            months[item.taken.month] = months.get(item.taken.month, 0) + 1
        for year in sorted(years, reverse=True):
            months = years[year]
            node = QTreeWidgetItem([f"{year} ({sum(months.values()):,})"])
            node.setData(0, _KEY, (year, 0))
            self.months.addTopLevelItem(node)
            for month in sorted(months, reverse=True):
                child = QTreeWidgetItem([f"{calendar.month_name[month]} ({months[month]:,})"])
                child.setData(0, _KEY, (year, month))
                node.addChild(child)
            node.setExpanded(True)
        if undated:
            node = QTreeWidgetItem([f"{NO_DATE} ({undated:,})"])
            node.setData(0, _KEY, (0, 0))
            self.months.addTopLevelItem(node)
        self.months.blockSignals(False)

    def _jump_selected(self, current: QTreeWidgetItem | None, _before: object) -> None:
        if current is None:
            return
        value = current.data(0, _KEY)
        if not isinstance(value, tuple):
            return
        year, month = int(value[0]), int(value[1])  # pyright: ignore[reportUnknownArgumentType]
        if month == 0 and year != 0:
            child = current.child(0)  # the newest month of that year
            if child is not None:
                inner = child.data(0, _KEY)
                year, month = int(inner[0]), int(inner[1])
        self.grid.scroll_to_month(year, month)

    def jump_to(self, year: int, month: int) -> bool:
        """Scroll to the header of that month (the list on the left does the same)."""
        return self.grid.scroll_to_month(year, month)

    def _follow_month(self, year: int, month: int) -> None:
        for i in range(self.months.topLevelItemCount()):
            node = self.months.topLevelItem(i)
            if node is None:
                continue
            for j in range(node.childCount()):
                child = node.child(j)
                if child is not None and child.data(0, _KEY) == (year, month):
                    self.months.blockSignals(True)
                    self.months.setCurrentItem(child)
                    self.months.blockSignals(False)
                    return

    def _summary(self, report: SyncReport | None = None) -> str:
        shown = self.model.shown
        videos = sum(1 for i in shown if i.kind == "video")
        text = summary_text(len(shown) - videos, videos, report)
        total = len(self.model.all_items)
        if total != len(shown):
            text += f" (filtered from {total:,})"
        return text

    def items(self) -> Sequence[MediaItem]:
        """Every item shown on the page (all months)."""
        return list(self._shown)

    # ----- selection, preview, viewer ------------------------------------------------------------

    def _selection_changed(self) -> None:
        chosen = self.grid.selected_items()
        self.selection_label.setText(f"{len(chosen):,} selected" if chosen else "")
        if self.preview_button.isChecked():
            self.preview.show_items(chosen)

    def set_preview_visible(self, visible: bool) -> None:
        self.preview.setVisible(visible)
        if self.preview_button.isChecked() != visible:
            self.preview_button.setChecked(visible)
        if visible:
            self.preview.show_items(self.grid.selected_items())

    def toggle_preview(self) -> None:
        self.set_preview_visible(not self.preview_button.isChecked())

    def preview_visible(self) -> bool:
        return self.preview_button.isChecked()

    def _size_changed(self, size: int) -> None:
        if self.size_slider.value() != size:
            self.size_slider.blockSignals(True)
            self.size_slider.setValue(size)
            self.size_slider.blockSignals(False)

    def activate_row(self, row: int) -> bool:
        """Enter or double-click: a photo opens in the viewer, a video in the default app."""
        items = self.grid.items()
        if not 0 <= row < len(items):
            return False
        item = items[row]
        if item.kind != "photo":
            return self.grid.open_row(row)
        photos = [i for i in items if i.kind == "photo"]
        index = next((n for n, p in enumerate(photos) if p.path == item.path), 0)
        self.open_viewer(photos, index)
        return True

    def open_viewer(self, photos: list[MediaItem], index: int) -> PhotoViewer:
        viewer = PhotoViewer(photos, index, self)
        self.viewer = viewer
        viewer.rowChanged.connect(
            lambda i, p=photos: self.grid.set_current(self.grid.row_of_path(p[i].path))
        )
        viewer.finished.connect(lambda _r: setattr(self, "viewer", None))
        viewer.open()
        return viewer

    # ----- change date -------------------------------------------------------------------------

    def change_dates(self, paths: object) -> ChangeDateDialog | None:
        """Open the "Change date" dialog for these files (preview first)."""
        if not isinstance(paths, list) or not paths:
            return None
        files = [Path(str(p)) for p in paths]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        dialog = ChangeDateDialog(self.state, files, self)
        if self.confirm is not None:
            dialog.confirm = self.confirm
        dialog.datesChanged.connect(lambda _p: self.refresh())
        self.change_dialog = dialog
        dialog.open()
        return dialog

    # ----- update ------------------------------------------------------------------------------

    def update_library(self) -> Worker | None:
        """Bring the library up to date for the chosen folders, in the background.

        None when a job already runs, the window is closing or no folder is chosen. (This
        replaces ``QWidget.update``; the widget is repainted with ``repaint`` if ever needed.)
        """
        if self.worker is not None or self._closing:
            return None
        roots = self.roots()
        if not roots:
            self._set_status("Add a folder with photos or videos first (Library folders…).", True)
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
        self.preview.shutdown()
        self.cancel()
        self._update_buttons()

    def _update_buttons(self) -> None:
        running = self.worker is not None
        self.update_button.setEnabled(not running and not self._closing)
        self.cancel_button.setVisible(running)
        self.cancel_button.setEnabled(running)
        self.progress_bar.setVisible(running)
        self.folders_button.setEnabled(not running)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #c0392b;" if error else "")
        if not error:
            self.status_label.setToolTip("")
