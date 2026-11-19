"""A grid of photo and video thumbnails, used by the Timeline and the Map pages.

Thumbnails are made only for the items that are on screen, in a background worker, with
:func:`~various_tools.core.media.thumbs.thumbnail_jpeg`, and kept in a small in-memory cache.
A OneDrive cloud-only item gets a cloud picture and is never read (AGENTS.md rule 3); opening
one asks first, because that downloads it.

Double-click opens an item in its default app. The right-click menu has Open, Edit (photos
only), Show in File Explorer and Copy path. The hooks (``reveal``, ``open_file``, ``edit_file``,
``confirm`` and ``load_thumbnail``) are attributes so tests can replace them, like
``PlanTable.reveal``.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QListView,
    QMenu,
    QMessageBox,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.media import thumbs
from various_tools.core.media.library import MediaItem
from various_tools.gui import reveal
from various_tools.gui.workers import Worker

log = logging.getLogger(__name__)

THUMB_PX = 160
CACHE_SIZE = 1500  # thumbnails kept in memory
BATCH = 24  # thumbnails made per background job

_ROW = Qt.ItemDataRole.UserRole + 1


def edit_with_windows(path: Path) -> bool:
    """Open ``path`` in the app Windows registered for "Edit". False when there is none."""
    startfile: Any = getattr(os, "startfile", None)
    if startfile is None:
        return False
    try:
        startfile(str(path), "edit")
    except OSError as exc:
        log.info("No edit app for %s: %s", path, exc)
        return False
    return True


def caption_for(item: MediaItem) -> str:
    """File name, and the date on a second line when there is one."""
    if item.taken is None:
        return item.path.name
    return f"{item.path.name}\n{item.taken.day} {item.taken:%b %Y}"


def _badge(text: str, size: int = THUMB_PX) -> QIcon:
    """A plain grey tile with a symbol, for items that have no picture (yet)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(128, 128, 128, 60))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRect(4, 4, size - 8, size - 8), 12, 12)
    font = QFont()
    font.setPointSize(size // 5)
    painter.setFont(font)
    painter.setPen(QColor(128, 128, 128))
    painter.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return QIcon(pixmap)


class MediaGrid(QWidget):
    countChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[MediaItem] = []
        self._model = QStandardItemModel(self)
        self._cache: OrderedDict[str, QIcon] = OrderedDict()
        self._tried: set[str] = set()  # asked once (a None result is not asked again)
        self._worker: Worker | None = None
        self._generation = 0
        self._edit_failed: set[str] = set()  # extensions with no working "Edit" app

        # Hooks that tests replace, so nothing opens Explorer, an app or a message box.
        self.reveal: Callable[[Path], bool] = reveal.show_in_explorer
        self.open_file: Callable[[Path], bool] = reveal.open_path
        self.edit_file: Callable[[Path], bool] = edit_with_windows
        self.confirm: Callable[[str, str], bool] = self._default_confirm
        self.load_thumbnail: Callable[[Path], bytes | None] = lambda p: thumbs.thumbnail_jpeg(
            p, THUMB_PX
        )

        self.view = QListView(self)
        self.view.setModel(self._model)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setWordWrap(True)
        self.view.setIconSize(QSize(THUMB_PX, THUMB_PX))
        self.view.setGridSize(QSize(THUMB_PX + 24, THUMB_PX + 48))
        self.view.setSpacing(4)
        self.view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._show_context_menu)
        self.view.doubleClicked.connect(lambda index: self.open_row(self._row_of(index)))
        self.view.verticalScrollBar().valueChanged.connect(lambda _v: self._schedule())

        style = self.style()
        self._photo_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self._video_icon = _badge("▶")
        self._cloud_icon = _badge("☁")

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self.load_visible)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

    # ----- items -------------------------------------------------------------------------------

    def set_items(self, items: Sequence[MediaItem]) -> None:
        """Show ``items`` (in the given order), replacing what was shown."""
        self._generation += 1
        self._items = list(items)
        self._model.clear()
        for row, entry in enumerate(self._items):
            cell = QStandardItem(self._icon_for(entry), caption_for(entry))
            cell.setData(row, _ROW)
            tip = f"{entry.path}\nDate from: {entry.date_source}"
            if entry.cloud_only:
                tip += "\nIn OneDrive only: not on this device"
            cell.setToolTip(tip)
            cell.setEditable(False)
            self._model.appendRow(cell)
        self.countChanged.emit(len(self._items))
        self._schedule()

    def items(self) -> list[MediaItem]:
        return list(self._items)

    def caption(self, row: int) -> str:
        return self._model.item(row).text()

    def has_thumbnail(self, row: int) -> bool:
        return _key(self._items[row]) in self._cache

    def _icon_for(self, entry: MediaItem) -> QIcon:
        if entry.cloud_only:
            return self._cloud_icon
        cached = self._cache.get(_key(entry))
        if cached is not None:
            self._cache.move_to_end(_key(entry))
            return cached
        return self._video_icon if entry.kind == "video" else self._photo_icon

    def _row_of(self, index: QModelIndex) -> int:
        if not index.isValid():
            return -1
        value = index.data(_ROW)
        return int(value) if isinstance(value, int) else -1

    # ----- thumbnails --------------------------------------------------------------------------

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._schedule()

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        self._schedule()

    def _schedule(self) -> None:
        self._timer.start()

    def visible_rows(self) -> list[int]:
        """Rows whose tile is at least partly on screen."""
        if not self._items or not self.view.isVisible():
            return []
        viewport = self.view.viewport().rect()
        first = -1
        step = max(8, THUMB_PX // 4)
        for y in range(0, max(viewport.height(), 1), step):
            for x in range(0, max(viewport.width(), 1), step):
                row = self._row_of(self.view.indexAt(QPoint(x, y)))
                if row >= 0:
                    first = row
                    break
            if first >= 0:
                break
        if first < 0:
            return []
        rows: list[int] = []
        for row in range(first, len(self._items)):
            rect = self.view.visualRect(self._model.index(row, 0))
            if rect.top() > viewport.bottom():
                break
            if rect.intersects(viewport):
                rows.append(row)
        return rows

    def load_visible(self) -> None:
        """Start making thumbnails for on-screen photos that have none yet."""
        if self._worker is not None:
            return
        wanted: list[tuple[str, Path]] = []
        for row in self.visible_rows():
            entry = self._items[row]
            key = _key(entry)
            # Rule 3: a cloud-only item is never read. Videos have no still preview here.
            if entry.cloud_only or entry.kind != "photo":
                continue
            if key in self._cache or key in self._tried:
                continue
            wanted.append((key, entry.path))
            if len(wanted) >= BATCH:
                break
        if not wanted:
            return
        loader = self.load_thumbnail
        generation = self._generation

        def job(worker: Worker) -> dict[str, bytes | None]:
            made: dict[str, bytes | None] = {}
            for key, path in wanted:
                if worker.cancel_event.is_set():
                    break
                made[key] = loader(path)
            return made

        worker = Worker(job, self)
        worker.succeeded.connect(lambda made, g=generation: self._thumbnails_ready(g, made))
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        worker.start()

    def _worker_finished(self) -> None:
        self._worker = None
        self._schedule()  # more may have scrolled into view meanwhile

    def _thumbnails_ready(self, generation: int, made: dict[str, bytes | None]) -> None:
        for key, data in made.items():
            self._tried.add(key)
            if not data:
                continue
            pixmap = QPixmap()
            if not pixmap.loadFromData(data, "JPEG"):
                continue
            self._cache[key] = QIcon(pixmap)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        if generation != self._generation:
            return
        for row, entry in enumerate(self._items):
            key = _key(entry)
            if key in made and key in self._cache:
                self._model.item(row).setIcon(self._cache[key])

    def shutdown(self) -> None:
        """Stop the thumbnail job (the window is closing)."""
        self._timer.stop()
        if self._worker is not None:
            self._worker.cancel()

    # ----- open, edit, show --------------------------------------------------------------------

    def open_row(self, row: int) -> bool:
        if not 0 <= row < len(self._items):
            return False
        return self._open(self._items[row].path)

    def _open(self, path: Path) -> bool:
        """Open ``path`` in its default app; a cloud-only file is downloaded only if asked."""
        if reveal.needs_download(path) and not self.confirm(
            "Download from OneDrive?",
            f"{path.name} is only in OneDrive, not on this device. Opening it downloads it "
            "to this device.\n\nDownload and open it?",
        ):
            return False
        return self.open_file(path)

    def _edit(self, path: Path) -> bool:
        if reveal.needs_download(path) and not self.confirm(
            "Download from OneDrive?",
            f"{path.name} is only in OneDrive, not on this device. Editing it downloads it "
            "to this device.\n\nDownload and edit it?",
        ):
            return False
        ok = self.edit_file(path)
        if not ok:
            self._edit_failed.add(path.suffix.lower())
        return ok

    def menu_for(self, row: int) -> QMenu:
        """The right-click menu for one item, not shown yet."""
        entry = self._items[row]
        path = entry.path
        exists = os.path.lexists(path)
        menu = QMenu(self.view)
        open_entry = menu.addAction("Open")
        open_entry.setEnabled(exists)
        open_entry.triggered.connect(lambda _c=False, p=path: self._open(p))
        edit_entry = menu.addAction("Edit")
        can_edit = entry.kind == "photo" and path.suffix.lower() not in self._edit_failed
        edit_entry.setEnabled(exists and can_edit)
        edit_entry.triggered.connect(lambda _c=False, p=path: self._edit(p))
        menu.addSeparator()
        show_entry = menu.addAction(reveal.SHOW)
        show_entry.setEnabled(exists)
        show_entry.triggered.connect(lambda _c=False, p=path: self.reveal(p))
        copy_entry = menu.addAction("Copy path")
        copy_entry.triggered.connect(lambda _c=False, p=path: _copy_path(p))
        return menu

    def context_menu_at(self, pos: QPoint) -> QMenu | None:
        """The menu for the item at ``pos`` (viewport coordinates), or None when there is none."""
        row = self._row_of(self.view.indexAt(pos))
        if row < 0:
            return None
        return self.menu_for(row)

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = self.context_menu_at(pos)
        if menu is not None:
            menu.exec(self.view.viewport().mapToGlobal(pos))
            menu.deleteLater()

    def _default_confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes


def _copy_path(path: Path) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(str(path))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _key(entry: MediaItem) -> str:
    """Cache key: the path plus size and time, so a changed file gets a new thumbnail."""
    return f"{_path_key(entry.path)}|{entry.size}|{entry.mtime}"
