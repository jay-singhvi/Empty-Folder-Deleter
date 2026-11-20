"""The large picture and the details of the selected photo or video, and the full-window viewer.

Pictures are made in a background worker with
:func:`~various_tools.core.media.thumbs.thumbnail_jpeg` (so HEIC and RAW work too), one at a time,
and only the newest request is shown. A OneDrive cloud-only item gets a cloud picture and is
never read (AGENTS.md rule 3). The ``load_picture`` hooks are attributes so tests can replace them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.media import thumbs
from various_tools.core.media.library import MediaItem
from various_tools.gui.plan_edit import human_size
from various_tools.gui.workers import Worker

PREVIEW_PX = 900
VIEWER_PX = 2200
CLOUD_TEXT = "In OneDrive only: it is not on this device, so it is not shown."
NO_PICTURE_TEXT = "There is no picture to show."


def default_load_picture(path: Path, max_px: int) -> bytes | None:
    return thumbs.thumbnail_jpeg(path, max_px, quality=88)


class PictureLoader(QObject):
    """Loads one big picture at a time in the background; only the newest request is delivered."""

    loaded = Signal(object, object)  # the Path asked for, a QPixmap or None

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.load_picture: Callable[[Path, int], bytes | None] = default_load_picture
        self._worker: Worker | None = None
        self._waiting: tuple[Path, int] | None = None
        self._latest: Path | None = None
        self._closed = False

    def request(self, path: Path, max_px: int) -> None:
        self._latest = path
        if self._worker is not None:
            self._waiting = (path, max_px)
            return
        self._start(path, max_px)

    def _start(self, path: Path, max_px: int) -> None:
        loader = self.load_picture

        def job(_worker: Worker) -> bytes | None:
            return loader(path, max_px)

        worker = Worker(job)
        worker.succeeded.connect(lambda data, p=path: self._done(p, data))
        worker.failed.connect(lambda _exc, p=path: self._done(p, None))
        worker.finished.connect(self._finished)
        self._worker = worker
        worker.start()

    def _done(self, path: Path, data: object) -> None:
        if self._closed or path != self._latest:
            return
        pixmap: QPixmap | None = None
        if isinstance(data, bytes) and data:
            candidate = QPixmap()
            if candidate.loadFromData(data):
                pixmap = candidate
        self.loaded.emit(path, pixmap)

    def _finished(self) -> None:
        self._worker = None
        if self._waiting is not None and not self._closed:
            path, px = self._waiting
            self._waiting = None
            self._start(path, px)

    def shutdown(self) -> None:
        self._closed = True
        self._waiting = None
        if self._worker is not None:
            self._worker.cancel()


class _PictureLabel(QLabel):
    """Shows a pixmap scaled to fit, keeping its shape."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(QSize(120, 120))

    def set_picture(self, pixmap: QPixmap | None, fallback: str = "") -> None:
        self._pixmap = pixmap
        if pixmap is None:
            super().setPixmap(QPixmap())
            self.setText(fallback)
        else:
            self.setText("")
            self._fit()

    def has_picture(self) -> bool:
        return self._pixmap is not None

    def _fit(self) -> None:
        if self._pixmap is None:
            return
        size = self.size()
        scaled = self._pixmap.scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        super().setPixmap(scaled)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._fit()


def details_text(item: MediaItem) -> str:
    """The plain lines shown next to the big picture."""
    lines = [item.path.name]
    if item.taken is not None:
        lines.append(f"Date: {item.taken:%A %d %B %Y, %H:%M} (from {item.date_source})")
    else:
        lines.append("Date: unknown")
    lines.append(f"Size: {human_size(item.size)}")
    if item.width and item.height:
        lines.append(f"Pixels: {item.width} × {item.height}")
    if item.lat is not None and item.lon is not None:
        lines.append(f"Place: {item.lat:.4f}, {item.lon:.4f}")
    else:
        lines.append("Place: none in the file")
    lines.append(f"Folder: {item.path.parent}")
    return "\n".join(lines)


class PreviewPane(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.picture = _PictureLabel()
        self.details = QLabel("Select a photo or video to see it here.")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.loader = PictureLoader(self)
        self.loader.loaded.connect(self._loaded)
        self._shown: Path | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._request)
        self._pending: MediaItem | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.addWidget(self.picture, 3)
        layout.addWidget(self.details, 2)
        self.setMinimumWidth(220)

    def show_items(self, items: Sequence[MediaItem]) -> None:
        """Show the one selected item, or a count when several are selected."""
        self._pending = None
        if not items:
            self._shown = None
            self.picture.set_picture(None, "")
            self.details.setText("Select a photo or video to see it here.")
        elif len(items) > 1:
            self._shown = None
            self.picture.set_picture(None, "")
            self.details.setText(f"{len(items):,} selected")
        else:
            self._show_one(items[0])

    def _show_one(self, item: MediaItem) -> None:
        self._shown = item.path
        self.details.setText(details_text(item))
        if item.cloud_only:  # rule 3: never read
            self.picture.set_picture(None, f"☁\n{CLOUD_TEXT}")
        elif item.kind == "video":
            self.picture.set_picture(None, "▶\nA video. Double-click it to play it.")
        else:
            self.picture.set_picture(None, "…")
            self._pending = item
            self._timer.start()

    def _request(self) -> None:
        item = self._pending
        if item is not None:
            self.loader.request(item.path, PREVIEW_PX)

    def _loaded(self, path: object, pixmap: object) -> None:
        if path != self._shown:
            return
        if isinstance(pixmap, QPixmap):
            self.picture.set_picture(pixmap)
        else:
            self.picture.set_picture(None, NO_PICTURE_TEXT)

    def shutdown(self) -> None:
        self._timer.stop()
        self.loader.shutdown()


class PhotoViewer(QDialog):
    """A full-window viewer for photos. Left and Right change photo, Esc closes it."""

    rowChanged = Signal(int)  # noqa: N815  the index in the list given at the start

    def __init__(
        self,
        items: Sequence[MediaItem],
        index: int,
        parent: QWidget | None = None,
        *,
        load_picture: Callable[[Path, int], bytes | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Photo")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.items = list(items)
        self.index = max(0, min(index, len(self.items) - 1))
        self.setStyleSheet("QDialog { background: #101010; } QLabel { color: #e8e8e8; }")
        self.picture = _PictureLabel()
        self.caption = QLabel("")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loader = PictureLoader(self)
        if load_picture is not None:
            self.loader.load_picture = load_picture
        self.loader.loaded.connect(self._loaded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.picture, 1)
        layout.addWidget(self.caption)
        self.resize(1000, 700)
        self._show_current()

    def current_item(self) -> MediaItem:
        return self.items[self.index]

    def step(self, delta: int) -> None:
        new = self.index + delta
        if 0 <= new < len(self.items):
            self.index = new
            self._show_current()
            self.rowChanged.emit(new)

    def _show_current(self) -> None:
        item = self.current_item()
        date = f" · {item.taken:%d %B %Y, %H:%M}" if item.taken is not None else ""
        self.caption.setText(f"{item.path.name}{date} · {self.index + 1} of {len(self.items)}")
        self.setWindowTitle(item.path.name)
        if item.cloud_only:  # rule 3: never read
            self.picture.set_picture(None, f"☁\n{CLOUD_TEXT}")
            return
        self.picture.set_picture(None, "…")
        self.loader.request(item.path, VIEWER_PX)

    def _loaded(self, path: object, pixmap: object) -> None:
        if path != self.current_item().path:
            return
        if isinstance(pixmap, QPixmap):
            self.picture.set_picture(pixmap)
        else:
            self.picture.set_picture(None, NO_PICTURE_TEXT)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        if event.key() == Qt.Key.Key_Left:
            self.step(-1)
        elif event.key() == Qt.Key.Key_Right:
            self.step(1)
        else:
            super().keyPressEvent(event)

    def done(self, result: int) -> None:
        self.loader.shutdown()
        super().done(result)
