"""A grid of photo and video thumbnails, used by the Timeline and the Map pages.

It is one scroll area that paints only what is on screen: there is no widget and no model row per
item, so 50,000 items open at once and scroll smoothly. Items are laid out in rows. With
``grouped=True`` (the Timeline) the items, which must be in date order, get a header for each
month ("November 2022 · 214") and a smaller one for each day ("Saturday 22 November 2022 · 14").

Thumbnails are made only for the items that are on screen, in a background worker, with
:func:`~various_tools.core.media.thumbs.thumbnail_jpeg`, and kept in a small in-memory cache
shared by every grid. A OneDrive cloud-only item gets a cloud picture and is never read
(AGENTS.md rule 3); opening one asks first, because that downloads it.

Selection works like File Explorer: click, Ctrl+click, Shift+click, Ctrl+A and the arrow keys.
Enter or double-click calls ``activate`` (by default: open the item in its default app). The
right-click menu works on the whole selection. The hooks (``reveal``, ``open_file``,
``edit_file``, ``show_properties``, ``confirm``, ``activate`` and ``load_thumbnail``) are
attributes so tests can replace them, like ``PlanTable.reveal``.
"""

from __future__ import annotations

import calendar
import logging
import os
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QHelpEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import QAbstractScrollArea, QMenu, QMessageBox, QToolTip, QWidget

from various_tools.core.media import thumbs
from various_tools.core.media.library import MediaItem
from various_tools.gui import reveal
from various_tools.gui.workers import Worker

log = logging.getLogger(__name__)

THUMB_PX = 192  # the size thumbnails are made at
MIN_THUMB = 64
MAX_THUMB = 256
DEFAULT_THUMB = 120
CACHE_SIZE = 1000  # thumbnails kept in memory, shared by every grid
BATCH = 24  # thumbnails made per background job
OPEN_MANY = 10  # opening more than this many files at once asks first
NO_DATE = "No date"

_PAD = 8
_GAP = 6
_MONTH_H = 34
_DAY_H = 26
_CAPTION_H = 34

_SHARED_CACHE: OrderedDict[str, QPixmap] = OrderedDict()

_MONTH, _DAY, _ROW = 0, 1, 2


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


def month_title(year: int, month: int, count: int) -> str:
    if year == 0:
        return f"{NO_DATE} · {count:,}"
    return f"{calendar.month_name[month]} {year} · {count:,}"


def day_title(item: MediaItem, count: int) -> str:
    taken = item.taken
    assert taken is not None
    return f"{taken:%A} {taken.day} {calendar.month_name[taken.month]} {taken.year} · {count:,}"


def _copy_path(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _key(entry: MediaItem) -> str:
    """Cache key: the path plus size and time, so a changed file gets a new thumbnail."""
    return f"{_path_key(entry.path)}|{entry.size}|{entry.mtime}"


class _Band:
    """One horizontal strip of the layout: a header or one row of tiles."""

    __slots__ = ("end", "height", "kind", "month", "start", "text", "top")

    def __init__(
        self,
        top: int,
        height: int,
        kind: int,
        text: str = "",
        start: int = 0,
        end: int = 0,
        month: tuple[int, int] = (0, 0),
    ) -> None:
        self.top = top
        self.height = height
        self.kind = kind
        self.text = text
        self.start = start
        self.end = end
        self.month = month


class MediaGrid(QAbstractScrollArea):
    countChanged = Signal(int)  # noqa: N815 (Qt name)
    currentChanged = Signal(int)  # noqa: N815  the row with the focus, or -1
    selectionChanged = Signal()  # noqa: N815
    thumbSizeChanged = Signal(int)  # noqa: N815
    showOnMapRequested = Signal(object)  # noqa: N815  a Path
    changeDateRequested = Signal(object)  # noqa: N815  a list of Paths
    previewToggleRequested = Signal()  # noqa: N815  the I key was pressed
    visibleMonthChanged = Signal(int, int)  # noqa: N815  the month at the top

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        grouped: bool = False,
        captions: bool = False,
        thumb_size: int = DEFAULT_THUMB,
    ) -> None:
        super().__init__(parent)
        self.grouped = grouped
        self.captions = captions
        self._thumb = max(MIN_THUMB, min(MAX_THUMB, thumb_size))
        self._items: list[MediaItem] = []
        self._row_index: dict[str, int] | None = None
        self._bands: list[_Band] = []
        self._tops: list[int] = []
        self._row_starts: list[int] = []
        self._row_bands: list[int] = []
        self._months: dict[tuple[int, int], int] = {}
        self._cols = 1
        self._content_h = 0
        self._selected: set[int] = set()
        self._current = -1
        self._anchor = -1
        self._tried: set[str] = set()  # asked once (a None result is not asked again)
        self._worker: Worker | None = None
        self._generation = 0
        self._last_month: tuple[int, int] | None = None
        self._edit_failed: set[str] = set()  # extensions with no working "Edit" app

        # Hooks that tests replace, so nothing opens Explorer, an app or a message box.
        self.reveal: Callable[[Path], bool] = reveal.show_in_explorer
        self.open_file: Callable[[Path], bool] = reveal.open_path
        self.edit_file: Callable[[Path], bool] = edit_with_windows
        self.show_properties: Callable[[Path], bool] = reveal.show_properties
        self.confirm: Callable[[str, str], bool] = self._default_confirm
        self.activate: Callable[[int], object] = self.open_row
        self.load_thumbnail: Callable[[Path], bytes | None] = lambda p: thumbs.thumbnail_jpeg(
            p, THUMB_PX
        )

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self.load_visible)

    # ----- items -------------------------------------------------------------------------------

    def set_items(self, items: Sequence[MediaItem]) -> None:
        """Show ``items`` (in the given order), replacing what was shown."""
        self._generation += 1
        self._items = list(items)
        self._row_index = None
        self._selected.clear()
        self._current = -1
        self._anchor = -1
        self._relayout()
        self.verticalScrollBar().setValue(0)
        self.viewport().update()
        self.countChanged.emit(len(self._items))
        self.selectionChanged.emit()
        self.currentChanged.emit(-1)
        self._schedule()

    def items(self) -> list[MediaItem]:
        return list(self._items)

    def row_of_path(self, path: Path) -> int:
        index = self._row_index
        if index is None:  # built on first use, dropped when the items change
            index = {}
            for row, item in enumerate(self._items):
                index.setdefault(_path_key(item.path), row)
            self._row_index = index
        return index.get(_path_key(path), -1)

    def caption(self, row: int) -> str:
        return caption_for(self._items[row])

    def header_texts(self) -> list[str]:
        """The month and day headers, top to bottom (for tests and the jump list)."""
        return [b.text for b in self._bands if b.kind in (_MONTH, _DAY)]

    def month_headers(self) -> list[str]:
        return [b.text for b in self._bands if b.kind == _MONTH]

    def has_thumbnail(self, row: int) -> bool:
        return _key(self._items[row]) in _SHARED_CACHE

    # ----- thumbnail size ----------------------------------------------------------------------

    @property
    def thumb_size(self) -> int:
        return self._thumb

    def set_thumb_size(self, size: int) -> None:
        size = max(MIN_THUMB, min(MAX_THUMB, int(size)))
        if size == self._thumb:
            return
        anchor = self._first_visible_row()
        self._thumb = size
        self._relayout()
        if anchor >= 0:
            self._scroll_row_to_top(anchor)
        self.viewport().update()
        self.thumbSizeChanged.emit(size)
        self._schedule()

    # ----- layout ------------------------------------------------------------------------------

    def _tile_h(self) -> int:
        return self._thumb + (_CAPTION_H if self.captions else 0)

    def _columns_for_width(self) -> int:
        width = max(1, self.viewport().width())
        return max(1, (width - 2 * _PAD + _GAP) // (self._thumb + _GAP))

    def _update_scrollbar(self) -> None:
        bar = self.verticalScrollBar()
        bar.setSingleStep(max(16, self._thumb // 2))
        bar.setPageStep(self.viewport().height())
        bar.setRange(0, max(0, self._content_h - self.viewport().height()))

    def _relayout(self) -> None:
        self._cols = self._columns_for_width()
        bands: list[_Band] = []
        row_starts: list[int] = []
        row_bands: list[int] = []
        months: dict[tuple[int, int], int] = {}
        items = self._items
        month_counts: dict[tuple[int, int], int] = {}
        day_counts: dict[object, int] = {}
        if self.grouped:
            for item in items:
                taken = item.taken
                mk = (taken.year, taken.month) if taken is not None else (0, 0)
                month_counts[mk] = month_counts.get(mk, 0) + 1
                if taken is not None:
                    day_counts[taken.date()] = day_counts.get(taken.date(), 0) + 1
        y = _PAD
        last_month: tuple[int, int] | None = None
        last_day: object = None
        row_start = -1
        row_h = self._tile_h() + _GAP

        def close_row(end: int, at: int, month: tuple[int, int]) -> int:
            row_starts.append(row_start)
            row_bands.append(len(bands))
            bands.append(_Band(at, row_h, _ROW, start=row_start, end=end, month=month))
            return at + row_h

        for i, item in enumerate(items):
            taken = item.taken
            mk = (taken.year, taken.month) if taken is not None and self.grouped else (0, 0)
            if self.grouped:
                day = taken.date() if taken is not None else None
                new_month = mk != last_month
                if new_month or (day != last_day and day is not None):
                    if row_start >= 0:
                        y = close_row(i, y, last_month or (0, 0))
                        row_start = -1
                    if new_month:
                        months[mk] = len(bands)
                        title = month_title(mk[0], mk[1], month_counts[mk])
                        bands.append(_Band(y, _MONTH_H, _MONTH, title, month=mk))
                        y += _MONTH_H
                    if day is not None:
                        title = day_title(item, day_counts[day])
                        bands.append(_Band(y, _DAY_H, _DAY, title, month=mk))
                        y += _DAY_H
                    last_month, last_day = mk, day
            if row_start >= 0 and i - row_start >= self._cols:
                y = close_row(i, y, last_month or (0, 0))
                row_start = -1
            if row_start < 0:
                row_start = i
        if row_start >= 0:
            y = close_row(len(items), y, last_month or (0, 0))
        self._bands = bands
        self._tops = [b.top for b in bands]
        self._row_starts = row_starts
        self._row_bands = row_bands
        self._months = months
        self._content_h = y + _PAD
        self._update_scrollbar()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        if self._bands and self._columns_for_width() == self._cols:
            # The same number of columns: the layout is unchanged, only the scroll range moves.
            self._update_scrollbar()
            self._schedule()
            return
        first = self._first_visible_row()
        self._relayout()
        if first >= 0:
            self._scroll_row_to_top(first)
        self._schedule()

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        self._relayout()
        self._schedule()

    def _band_at(self, y: int) -> int:
        """Index of the band that contains content coordinate ``y`` (-1 above the first)."""
        return bisect_right(self._tops, y) - 1

    def _first_visible_row(self) -> int:
        if not self._items:
            return -1
        band = self._band_at(self.verticalScrollBar().value())
        while 0 <= band < len(self._bands):
            if self._bands[band].kind == _ROW:
                return self._bands[band].start
            band += 1
        return len(self._items) - 1

    def _band_of_row(self, row: int) -> _Band | None:
        if not 0 <= row < len(self._items) or not self._row_starts:
            return None
        return self._bands[self._row_bands[bisect_right(self._row_starts, row) - 1]]

    def rect_of(self, row: int) -> QRect:
        """The tile of ``row`` in viewport coordinates (it may be outside the viewport)."""
        band = self._band_of_row(row)
        if band is None:
            return QRect()
        x = _PAD + (row - band.start) * (self._thumb + _GAP)
        return QRect(x, band.top - self.verticalScrollBar().value(), self._thumb, self._tile_h())

    def _row_at(self, pos: QPoint) -> int:
        y = pos.y() + self.verticalScrollBar().value()
        band_index = self._band_at(y)
        if band_index < 0:
            return -1
        band = self._bands[band_index]
        if band.kind != _ROW or y >= band.top + self._tile_h():
            return -1
        if pos.x() < _PAD:
            return -1
        col, rest = divmod(pos.x() - _PAD, self._thumb + _GAP)
        if rest >= self._thumb:
            return -1
        row = band.start + col
        return row if row < band.end else -1

    # ----- scrolling ---------------------------------------------------------------------------

    def _scrolled(self, _value: int) -> None:
        self.viewport().update()
        self._schedule()
        if self.grouped:
            band = self._band_at(self.verticalScrollBar().value())
            if 0 <= band < len(self._bands):
                month = self._bands[band].month
                if month != self._last_month:
                    self._last_month = month
                    self.visibleMonthChanged.emit(*month)

    def _scroll_row_to_top(self, row: int) -> None:
        band = self._band_of_row(row)
        if band is not None:
            self.verticalScrollBar().setValue(band.top)

    def scroll_to_row(self, row: int) -> None:
        """Scroll just enough to show ``row``."""
        band = self._band_of_row(row)
        if band is None:
            return
        bar = self.verticalScrollBar()
        view_h = self.viewport().height()
        if band.top < bar.value():
            bar.setValue(band.top)
        elif band.top + self._tile_h() > bar.value() + view_h:
            bar.setValue(band.top + self._tile_h() - view_h + _GAP)

    def scroll_to_month(self, year: int, month: int) -> bool:
        """Scroll so that the header of that month is at the top. False when it is not shown."""
        index = self._months.get((year, month))
        if index is None:
            return False
        self.verticalScrollBar().setValue(self._bands[index].top)
        return True

    # ----- selection ---------------------------------------------------------------------------

    @property
    def current_row(self) -> int:
        return self._current

    def selected_rows(self) -> list[int]:
        return sorted(self._selected)

    def selected_items(self) -> list[MediaItem]:
        return [self._items[r] for r in sorted(self._selected)]

    def set_selection(self, rows: Sequence[int], *, current: int | None = None) -> None:
        valid = {r for r in rows if 0 <= r < len(self._items)}
        before, before_current = self._selected, self._current
        self._selected = valid
        if current is not None:
            self._current = current if 0 <= current < len(self._items) else -1
        elif self._current not in valid:
            self._current = min(valid) if valid else -1
        self.viewport().update()
        if before != self._selected:
            self.selectionChanged.emit()
        if before_current != self._current:
            self.currentChanged.emit(self._current)

    def select_all(self) -> None:
        self.set_selection(range(len(self._items)), current=max(self._current, 0))

    def set_current(self, row: int, *, select: bool = True) -> None:
        """Give ``row`` the focus (selecting only it) and scroll to it."""
        if not 0 <= row < len(self._items):
            return
        self._anchor = row
        if select:
            self.set_selection([row], current=row)
        else:
            self._current = row
            self.currentChanged.emit(row)
            self.viewport().update()
        self.scroll_to_row(row)

    def _click(self, row: int, modifiers: Qt.KeyboardModifier) -> None:
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if row < 0:
            if not ctrl and not shift:
                self.set_selection([], current=-1)
            return
        if shift and self._anchor >= 0:
            lo, hi = sorted((self._anchor, row))
            chosen = set(range(lo, hi + 1))
            if ctrl:
                chosen |= self._selected
            self.set_selection(sorted(chosen), current=row)
            return
        self._anchor = row
        if ctrl:
            chosen = set(self._selected)
            chosen.symmetric_difference_update({row})
            self.set_selection(sorted(chosen), current=row)
        else:
            self.set_selection([row], current=row)

    # ----- events ------------------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        self.setFocus()
        row = self._row_at(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton:
            if row >= 0 and row not in self._selected:
                self._click(row, Qt.KeyboardModifier.NoModifier)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._click(row, event.modifiers())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        row = self._row_at(event.position().toPoint())
        if row >= 0:
            self.activate(row)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt name)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            step = 16 if event.angleDelta().y() > 0 else -16
            self.set_thumb_size(self._thumb + step)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        key = event.key()
        mods = event.modifiers()
        if key == Qt.Key.Key_A and mods & Qt.KeyboardModifier.ControlModifier:
            self.select_all()
            return
        if key == Qt.Key.Key_I and not mods:
            self.previewToggleRequested.emit()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._current >= 0:
                self.activate(self._current)
            return
        page = self._cols * max(1, self.viewport().height() // max(1, self._tile_h()))
        moves: dict[int, int] = {
            Qt.Key.Key_Left.value: -1,
            Qt.Key.Key_Right.value: 1,
            Qt.Key.Key_Up.value: -self._cols,
            Qt.Key.Key_Down.value: self._cols,
            Qt.Key.Key_PageUp.value: -page,
            Qt.Key.Key_PageDown.value: page,
        }
        if not self._items:
            super().keyPressEvent(event)
            return
        if key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            target = 0 if key == Qt.Key.Key_Home else len(self._items) - 1
        elif key in moves:
            target = 0 if self._current < 0 else self._current + moves[key]
            target = min(len(self._items) - 1, max(0, target))
        else:
            super().keyPressEvent(event)
            return
        if mods & Qt.KeyboardModifier.ShiftModifier:
            anchor = self._anchor if self._anchor >= 0 else target
            lo, hi = sorted((anchor, target))
            self.set_selection(range(lo, hi + 1), current=target)
            self.scroll_to_row(target)
        else:
            self.set_current(target)

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            row = self._row_at(event.pos())
            if row >= 0:
                QToolTip.showText(event.globalPos(), self.tooltip_for(row), self.viewport())
            else:
                QToolTip.hideText()
            return True
        return super().viewportEvent(event)

    def tooltip_for(self, row: int) -> str:
        entry = self._items[row]
        tip = f"{entry.path}\nDate from: {entry.date_source}"
        if entry.cloud_only:
            tip += "\nIn OneDrive only: not on this device"
        return tip

    # ----- painting ----------------------------------------------------------------------------

    def _visible_bands(self) -> range:
        top = self.verticalScrollBar().value()
        first = max(0, self._band_at(top))
        last = first
        bottom = top + self.viewport().height()
        count = len(self._bands)
        while last < count and self._bands[last].top <= bottom:
            last += 1
        return range(first, last)

    def visible_rows(self) -> list[int]:
        """Rows whose tile is at least partly on screen."""
        if not self._items or not self.viewport().isVisible():
            return []
        rows: list[int] = []
        for index in self._visible_bands():
            band = self._bands[index]
            if band.kind == _ROW:
                rows.extend(range(band.start, band.end))
        return rows

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt name)
        painter = QPainter(self.viewport())
        pal = self.palette()
        painter.fillRect(self.viewport().rect(), pal.color(QPalette.ColorRole.Base))
        text = pal.color(QPalette.ColorRole.Text)
        offset = self.verticalScrollBar().value()
        bold = QFont(self.font())
        bold.setBold(True)
        big = QFont(bold)
        big.setPointSizeF(max(1.0, bold.pointSizeF()) * 1.25)
        wide = self.viewport().width() - 2 * _PAD
        for index in self._visible_bands():
            band = self._bands[index]
            y = band.top - offset
            if band.kind == _MONTH:
                painter.setFont(big)
                painter.setPen(text)
                painter.drawText(
                    QRect(_PAD, y, wide, band.height),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    band.text,
                )
            elif band.kind == _DAY:
                painter.setFont(bold)
                dim = QColor(text)
                dim.setAlpha(170)
                painter.setPen(dim)
                painter.drawText(
                    QRect(_PAD, y, wide, band.height),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    band.text,
                )
            else:
                painter.setFont(self.font())
                for row in range(band.start, band.end):
                    self._paint_tile(painter, row, row - band.start, y, pal)
        painter.end()

    def _paint_tile(self, painter: QPainter, row: int, col: int, y: int, pal: QPalette) -> None:
        item = self._items[row]
        size = self._thumb
        x = _PAD + col * (size + _GAP)
        tile = QRect(x, y, size, size)
        if row in self._selected:
            fill = pal.color(QPalette.ColorRole.Highlight)
            fill.setAlpha(110)
            extra = _CAPTION_H if self.captions else 0
            painter.fillRect(tile.adjusted(-3, -3, 3, 3 + extra), fill)
        pixmap = None if item.cloud_only else _SHARED_CACHE.get(_key(item))
        if pixmap is not None and not pixmap.isNull():
            scaled = pixmap.size().scaled(QSize(size, size), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(
                x + (size - scaled.width()) // 2,
                y + (size - scaled.height()) // 2,
                scaled.width(),
                scaled.height(),
            )
            painter.drawPixmap(target, pixmap)
        else:
            self._paint_placeholder(painter, tile, item)
        if row == self._current and self.hasFocus():
            painter.setPen(pal.color(QPalette.ColorRole.Highlight))
            painter.drawRect(tile.adjusted(-2, -2, 1, 1))
        if self.captions:
            painter.setPen(pal.color(QPalette.ColorRole.Text))
            painter.drawText(
                QRect(x, y + size + 2, size, _CAPTION_H - 2),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                painter.fontMetrics().elidedText(
                    self.caption(row).replace("\n", " · "), Qt.TextElideMode.ElideMiddle, size
                ),
            )

    def _paint_placeholder(self, painter: QPainter, tile: QRect, item: MediaItem) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(128, 128, 128, 60))
        painter.drawRoundedRect(tile.adjusted(2, 2, -2, -2), 8, 8)
        glyph = "☁" if item.cloud_only else "▶" if item.kind == "video" else "▣"
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, self._thumb / 4))
        painter.setFont(font)
        painter.setPen(QColor(128, 128, 128))
        painter.drawText(tile, Qt.AlignmentFlag.AlignCenter, glyph)
        painter.setFont(self.font())
        painter.setBrush(Qt.BrushStyle.NoBrush)

    # ----- thumbnails --------------------------------------------------------------------------

    def _schedule(self) -> None:
        self._timer.start()

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
            if key in _SHARED_CACHE or key in self._tried:
                continue
            wanted.append((key, entry.path))
            if len(wanted) >= BATCH:
                break
        if not wanted:
            return
        loader = self.load_thumbnail

        def job(worker: Worker) -> dict[str, bytes | None]:
            made: dict[str, bytes | None] = {}
            for key, path in wanted:
                if worker.cancel_event.is_set():
                    break
                made[key] = loader(path)
            return made

        worker = Worker(job)
        worker.succeeded.connect(self._thumbnails_ready)
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        worker.start()

    def _worker_finished(self) -> None:
        self._worker = None
        self._schedule()  # more may have scrolled into view meanwhile

    def _thumbnails_ready(self, made: dict[str, bytes | None]) -> None:
        for key, data in made.items():
            self._tried.add(key)
            if not data:
                continue
            pixmap = QPixmap()
            if not pixmap.loadFromData(data, "JPEG"):
                continue
            _SHARED_CACHE[key] = pixmap
            _SHARED_CACHE.move_to_end(key)
            while len(_SHARED_CACHE) > CACHE_SIZE:
                _SHARED_CACHE.popitem(last=False)
        self.viewport().update()

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

    def _for_each(self, paths: list[Path], action: Callable[[Path], bool], verb: str) -> None:
        if len(paths) > OPEN_MANY and not self.confirm(
            f"{verb} {len(paths)} files?", f"{verb} all {len(paths)} selected files at once?"
        ):
            return
        for path in paths:
            action(path)

    def menu_for_rows(self, rows: Sequence[int]) -> QMenu:
        """The right-click menu for these items, not shown yet."""
        entries = [self._items[r] for r in rows]
        paths = [e.path for e in entries]
        single = len(entries) == 1
        # One stat for one item; for a long selection the actions deal with a missing file.
        exists = os.path.lexists(paths[0]) if single else True
        menu = QMenu(self.viewport())
        open_entry = menu.addAction("Open")
        open_entry.setEnabled(exists)
        open_entry.triggered.connect(
            lambda _c=False, p=paths: self._for_each(p, self._open, "Open")
        )
        edit_entry = menu.addAction("Edit")
        can_edit = all(
            e.kind == "photo" and e.path.suffix.lower() not in self._edit_failed for e in entries
        )
        edit_entry.setEnabled(exists and can_edit)
        edit_entry.triggered.connect(
            lambda _c=False, p=paths: self._for_each(p, self._edit, "Edit")
        )
        menu.addSeparator()
        show_entry = menu.addAction(reveal.SHOW)
        show_entry.setEnabled(os.path.lexists(paths[0]))
        show_entry.triggered.connect(lambda _c=False, p=paths[0]: self.reveal(p))
        props_entry = menu.addAction("Properties")
        props_entry.setEnabled(single and exists)
        props_entry.triggered.connect(lambda _c=False, p=paths[0]: self.show_properties(p))
        copy_entry = menu.addAction("Copy path" if single else "Copy paths")
        copy_entry.triggered.connect(
            lambda _c=False, p=paths: _copy_path("\n".join(str(x) for x in p))
        )
        menu.addSeparator()
        if single and entries[0].lat is not None and entries[0].lon is not None:
            map_entry = menu.addAction("Show on map")
            map_entry.triggered.connect(
                lambda _c=False, p=paths[0]: self.showOnMapRequested.emit(p)
            )
        date_entry = menu.addAction("Change date…")
        date_entry.triggered.connect(
            lambda _c=False, p=paths: self.changeDateRequested.emit(list(p))
        )
        return menu

    def menu_for(self, row: int) -> QMenu:
        """The menu for one item."""
        return self.menu_for_rows([row])

    def context_menu_at(self, pos: QPoint) -> QMenu | None:
        """The menu for the item at ``pos`` (viewport coordinates), or None when there is none.

        When the item is part of the selection the menu is for the whole selection.
        """
        row = self._row_at(pos)
        if row < 0:
            return None
        rows = self.selected_rows() if row in self._selected else [row]
        return self.menu_for_rows(rows)

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = self.context_menu_at(pos)
        if menu is not None:
            menu.exec(self.viewport().mapToGlobal(pos))
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
