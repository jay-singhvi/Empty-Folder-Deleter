"""A squarified treemap of a size tree (plan 7.1: "a treemap … items can be sent to Trash").

Generic: it draws any tree whose nodes have ``name``, ``path`` (or None), ``bytes``,
``children`` (largest first) and ``own_bytes``, such as ``tools.disk_usage.SizeNode``. The tool
page shows it for any tool that offers a ``size_tree(plan)`` function; nothing here knows a tool.

* each tile is a folder at the current level, plus one grey "files here" tile for the bytes that
  are not in a listed sub-folder. Folders inside a tile are outlined when there is room;
* labels are drawn where they fit; the tooltip gives the name, the size and the share;
* clicking a folder tile zooms into it and emits ``folderChosen``; **Up** goes back;
* keyboard: arrow keys move between tiles, Enter zooms in, Backspace (or Alt+Up) goes up,
  Space chooses the tile without zooming;
* colours come from the widget palette, so light and dark themes both read well.

The view never touches the disk. "Tick files in this folder" only asks the page to check rows of
the plan; Apply (with its normal confirmation) is still what sends anything to the Recycle Bin.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QHelpEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.plan import ActionKind, Plan
from various_tools.gui.plan_edit import human_size

Rect = tuple[float, float, float, float]  # x, y, width, height

MIN_LABEL_WIDTH = 46.0
MIN_LABEL_HEIGHT = 18.0
MIN_NESTED_SIDE = 40.0
PADDING = 4.0


class SizeTreeNode(Protocol):
    """What the map needs from a node (``tools.disk_usage.SizeNode`` fits)."""

    @property
    def name(self) -> str: ...
    @property
    def path(self) -> Path | None: ...
    @property
    def bytes(self) -> int: ...
    @property
    def children(self) -> Sequence[SizeTreeNode]: ...
    @property
    def own_bytes(self) -> int: ...


# ----- layout (pure) ------------------------------------------------------------------------------


def _worst(row: Sequence[float], side: float) -> float:
    total = sum(row)
    if total <= 0 or side <= 0:
        return float("inf")
    biggest, smallest = max(row), min(row)
    side2, total2 = side * side, total * total
    return max(side2 * biggest / total2, total2 / (side2 * smallest))


def squarify(values: Sequence[float], rect: Rect) -> list[Rect]:
    """Squarified treemap layout (Bruls, Huizing and van Wijk): one rectangle per value.

    ``values`` should be sorted largest first. Each rectangle's area is proportional to its value
    and together they fill ``rect``. A value of zero or less gets an empty rectangle.
    """
    x, y, width, height = rect
    total = sum(v for v in values if v > 0)
    result: list[Rect] = [(x, y, 0.0, 0.0)] * len(values)
    if total <= 0 or width <= 0 or height <= 0:
        return result
    scale = width * height / total
    pending = [(i, v * scale) for i, v in enumerate(values) if v > 0]
    position = 0
    while position < len(pending):
        side = min(width, height)
        row = [pending[position]]
        position += 1
        while position < len(pending):
            candidate = [*row, pending[position]]
            if _worst([a for _i, a in candidate], side) > _worst([a for _i, a in row], side):
                break
            row = candidate
            position += 1
        area = sum(a for _i, a in row)
        last = position >= len(pending)
        if width >= height:
            column = width if last else area / height
            top = y
            for index, (item, a) in enumerate(row):
                tall = (y + height - top) if index == len(row) - 1 else a / column
                result[item] = (x, top, column, tall)
                top += tall
            x += column
            width -= column
        else:
            band = height if last else area / width
            left = x
            for index, (item, a) in enumerate(row):
                wide = (x + width - left) if index == len(row) - 1 else a / band
                result[item] = (left, y, wide, band)
                left += wide
            y += band
            height -= band
    return result


def share_text(part: int, whole: int) -> str:
    if whole <= 0:
        return "0%"
    share = 100.0 * part / whole
    return f"{share:.1f}%" if share < 10 else f"{share:.0f}%"


def _key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path))).casefold()


def _under(path: Path, folder: Path) -> bool:
    key, base = _key(path), _key(folder)
    return key == base or key.startswith(base.rstrip("\\/") + os.sep)


def actions_under(
    plan: Plan,
    folder: Path | None,
    *,
    recursive: bool = True,
    kinds: frozenset[ActionKind] | None = None,
) -> list[int]:
    """Positions of the plan actions whose source is in ``folder`` (any depth, or directly in it
    when not ``recursive``). ``folder=None`` means every plan root. ``kinds`` limits the kinds."""
    result: list[int] = []
    for index, action in enumerate(plan.actions):
        src = action.src
        if src is None or (kinds is not None and action.kind not in kinds):
            continue
        if folder is None:
            inside = any(_under(src, root) for root in plan.roots)
        elif recursive:
            inside = _under(src, folder) and _key(src) != _key(folder)
        else:
            inside = _key(src.parent) == _key(folder)
        if inside:
            result.append(index)
    return result


# ----- the canvas -------------------------------------------------------------------------------


@dataclass
class Tile:
    """One drawn rectangle: a folder (``node``) or the files directly in the current folder."""

    rect: QRectF
    node: SizeTreeNode | None  # None: the "files here" tile
    name: str
    bytes: int
    hue: int


class SizeMap(QWidget):
    """The treemap canvas. ``folderChosen(path_or_None, recursive)`` when a tile is chosen;
    ``zoomChanged()`` after the current folder changed."""

    folderChosen = Signal(object, bool)
    zoomChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._root: SizeTreeNode | None = None
        self._stack: list[SizeTreeNode] = []  # the path from the root to the current node
        self._tiles: list[Tile] = []
        self._focus = -1
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(240, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Size map")

    # ----- state --------------------------------------------------------------------------

    def set_tree(self, root: SizeTreeNode | None) -> None:
        self._root = root
        self._stack = [root] if root is not None else []
        self._focus = 0
        self._relayout()
        self.zoomChanged.emit()

    def root(self) -> SizeTreeNode | None:
        return self._root

    def current(self) -> SizeTreeNode | None:
        return self._stack[-1] if self._stack else None

    def trail(self) -> list[SizeTreeNode]:
        """The nodes from the top to the current one."""
        return list(self._stack)

    def tiles(self) -> list[Tile]:
        return list(self._tiles)

    def focused_index(self) -> int:
        return self._focus

    def can_go_up(self) -> bool:
        return len(self._stack) > 1

    def zoom_into(self, node: SizeTreeNode) -> None:
        self._stack.append(node)
        self._focus = 0
        self._relayout()
        self.zoomChanged.emit()

    def go_up(self) -> bool:
        if not self.can_go_up():
            return False
        left = self._stack.pop()
        self._relayout()
        self._focus = next((i for i, t in enumerate(self._tiles) if t.node is left), 0)
        self.update()
        self.zoomChanged.emit()
        return True

    def choose(self, index: int, *, zoom: bool = True) -> None:
        """What a click on tile ``index`` does: tell the page, and zoom into a folder."""
        if not 0 <= index < len(self._tiles):
            return
        tile = self._tiles[index]
        self._focus = index
        current = self.current()
        if tile.node is None:
            self.folderChosen.emit(current.path if current is not None else None, False)
            self.update()
            return
        self.folderChosen.emit(tile.node.path, True)
        if zoom:
            self.zoom_into(tile.node)
        else:
            self.update()

    def tile_at(self, point: QPointF) -> int:
        for index, tile in enumerate(self._tiles):
            if tile.rect.contains(point):
                return index
        return -1

    def tooltip_for(self, index: int) -> str:
        if not 0 <= index < len(self._tiles):
            return ""
        tile = self._tiles[index]
        current = self.current()
        whole = current.bytes if current is not None else 0
        lines = [tile.name, f"{human_size(tile.bytes)} ({share_text(tile.bytes, whole)})"]
        if tile.node is not None and tile.node.path is not None:
            lines.append(str(tile.node.path))
        if tile.node is not None and tile.node.children:
            lines.append("Click to look inside.")
        return "\n".join(lines)

    # ----- layout -------------------------------------------------------------------------

    def _items(self) -> list[tuple[SizeTreeNode | None, str, int]]:
        current = self.current()
        if current is None:
            return []
        items: list[tuple[SizeTreeNode | None, str, int]] = [
            (child, child.name, child.bytes) for child in current.children if child.bytes > 0
        ]
        if current.own_bytes > 0 and current.path is not None:
            items.append((None, f"Files in {current.name}", current.own_bytes))
        items.sort(key=lambda item: -item[2])
        return items

    def _relayout(self) -> None:
        items = self._items()
        area = (1.0, 1.0, max(0.0, self.width() - 2.0), max(0.0, self.height() - 2.0))
        rects = squarify([float(size) for _n, _name, size in items], area)
        self._tiles = [
            Tile(QRectF(*rect), node, name, size, (index * 47 + 205) % 360)
            for index, ((node, name, size), rect) in enumerate(zip(items, rects, strict=True))
        ]
        if self._tiles:
            self._focus = min(max(self._focus, 0), len(self._tiles) - 1)
        else:
            self._focus = -1
        current = self.current()
        if current is not None:
            self.setAccessibleDescription(
                f"{current.name}: {human_size(current.bytes)} in {len(self._tiles)} parts"
            )
        self.update()

    def resizeEvent(self, event: object) -> None:  # noqa: N802 (Qt name)
        self._relayout()

    # ----- painting -----------------------------------------------------------------------

    def _dark(self) -> bool:
        return self.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _fill(self, tile: Tile, nested: bool = False) -> QColor:
        dark = self._dark()
        if tile.node is None:
            return QColor.fromHsl(0, 0, 88 if dark else 205)
        lightness = (78 if dark else 196) + (14 if nested else 0) * (1 if dark else -1)
        return QColor.fromHsl(tile.hue, 95 if dark else 120, lightness)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt name)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            palette = self.palette()
            painter.fillRect(self.rect(), palette.color(QPalette.ColorRole.Base))
            if not self._tiles:
                painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
                painter.drawText(
                    self.rect(), Qt.AlignmentFlag.AlignCenter, "Scan to see the size map."
                )
                return
            border = QPen(palette.color(QPalette.ColorRole.Base), 2)
            text_colour = QColor("#f2f2f2") if self._dark() else QColor("#1d1d1f")
            metrics = QFontMetricsF(self.font())
            for index, tile in enumerate(self._tiles):
                painter.setPen(border)
                painter.setBrush(self._fill(tile))
                painter.drawRect(tile.rect)
                header = self._draw_label(painter, metrics, tile, text_colour)
                self._draw_nested(painter, tile, header)
                if index == self._focus and self.hasFocus():
                    focus = QPen(palette.color(QPalette.ColorRole.Highlight), 3)
                    painter.setPen(focus)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(tile.rect.adjusted(2, 2, -2, -2))
        finally:
            painter.end()

    def _draw_label(
        self, painter: QPainter, metrics: QFontMetricsF, tile: Tile, colour: QColor
    ) -> float:
        """Draw the name and size where they fit. Returns the height used at the top."""
        rect = tile.rect.adjusted(PADDING, PADDING / 2, -PADDING, -PADDING / 2)
        line = metrics.height()
        if rect.width() < MIN_LABEL_WIDTH or rect.height() < max(MIN_LABEL_HEIGHT, line):
            return 0.0
        painter.setPen(colour)
        name = metrics.elidedText(tile.name, Qt.TextElideMode.ElideMiddle, rect.width())
        painter.drawText(QRectF(rect.left(), rect.top(), rect.width(), line), name)
        used = line
        if rect.height() >= 2 * line + 2:
            size = human_size(tile.bytes)
            if metrics.horizontalAdvance(size) <= rect.width():
                painter.drawText(QRectF(rect.left(), rect.top() + line, rect.width(), line), size)
                used += line
        return used + PADDING

    def _draw_nested(self, painter: QPainter, tile: Tile, header: float) -> None:
        """Outline the folders inside a tile when there is room (one level deeper)."""
        node = tile.node
        if node is None or not node.children:
            return
        inner = tile.rect.adjusted(PADDING, header + PADDING / 2, -PADDING, -PADDING)
        if inner.width() < MIN_NESTED_SIDE or inner.height() < MIN_NESTED_SIDE:
            return
        sizes = [float(c.bytes) for c in node.children if c.bytes > 0]
        if node.own_bytes > 0:
            sizes.append(float(node.own_bytes))
        sizes.sort(reverse=True)
        rects = squarify(sizes, (inner.x(), inner.y(), inner.width(), inner.height()))
        pen = QPen(self.palette().color(QPalette.ColorRole.Base), 1)
        painter.setPen(pen)
        painter.setBrush(self._fill(tile, nested=True))
        for x, y, w, h in rects:
            if w >= 3 and h >= 3:
                painter.drawRect(QRectF(x, y, w, h))

    # ----- input --------------------------------------------------------------------------

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            text = self.tooltip_for(self.tile_at(QPointF(event.pos())))
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        if event.button() == Qt.MouseButton.LeftButton:
            self.choose(self.tile_at(event.position()))
        elif event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.BackButton):
            self.go_up()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        key = event.key()
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Escape) or (alt and key == Qt.Key.Key_Up):
            if not self.go_up():
                super().keyPressEvent(event)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.choose(self._focus)
            return
        if key == Qt.Key.Key_Space:
            self.choose(self._focus, zoom=False)
            return
        moves: dict[int, tuple[int, int]] = {
            int(Qt.Key.Key_Right): (1, 0),
            int(Qt.Key.Key_Left): (-1, 0),
            int(Qt.Key.Key_Down): (0, 1),
            int(Qt.Key.Key_Up): (0, -1),
        }
        if key in moves and self._tiles:
            self._focus = self._neighbour(*moves[key])
            self.update()
            tip = self.tooltip_for(self._focus)
            self.setAccessibleDescription(tip.replace("\n", ", "))
            return
        super().keyPressEvent(event)

    def _neighbour(self, dx: int, dy: int) -> int:
        """The tile nearest to the focused one in a direction (or the next/previous one)."""
        if not 0 <= self._focus < len(self._tiles):
            return 0
        here = self._tiles[self._focus].rect.center()
        best, best_score = self._focus, float("inf")
        for index, tile in enumerate(self._tiles):
            if index == self._focus:
                continue
            centre = tile.rect.center()
            ddx, ddy = centre.x() - here.x(), centre.y() - here.y()
            along = ddx * dx + ddy * dy
            if along <= 0:
                continue
            across = abs(ddx * dy) + abs(ddy * dx)
            score = along + 2 * across
            if score < best_score:
                best, best_score = index, score
        if best == self._focus:  # nothing that way: step through the list instead
            step = 1 if dx + dy > 0 else -1
            best = min(max(self._focus + step, 0), len(self._tiles) - 1)
        return best


# ----- the view with its toolbar ------------------------------------------------------------------


class SizeMapView(QWidget):
    """The map with an Up button, the current folder and "Tick files in this folder".

    ``tickRequested(path_or_None)`` asks the page to check the plan's Recycle-Bin rows under that
    folder (None: every chosen folder). ``folderChosen`` is passed on from the canvas.
    """

    folderChosen = Signal(object, bool)
    tickRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tick_allowed = True
        self.canvas = SizeMap(self)
        self.up_button = QToolButton(self)
        self.up_button.setText("Up")
        self.up_button.setArrowType(Qt.ArrowType.UpArrow)
        self.up_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.up_button.setToolTip("Back to the folder above (Backspace).")
        self.trail_label = QLabel(self)
        self.trail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tick_button = QPushButton("Tick files in this folder", self)
        self.tick_button.setToolTip(
            "Check the listed files inside the folder shown, so Apply sends them to the Recycle "
            "Bin. Apply still asks first."
        )
        bar = QHBoxLayout()
        bar.addWidget(self.up_button)
        bar.addWidget(self.trail_label, 1)
        bar.addWidget(self.tick_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.canvas, 1)
        self.up_button.clicked.connect(lambda _c=False: self.canvas.go_up())
        self.tick_button.clicked.connect(lambda _c=False: self._tick())
        self.canvas.zoomChanged.connect(self._zoom_changed)
        self.canvas.folderChosen.connect(self.folderChosen)
        self._zoom_changed()

    def set_tree(self, root: SizeTreeNode | None) -> None:
        self.canvas.set_tree(root)

    def set_tick_enabled(self, enabled: bool) -> None:
        self._tick_allowed = enabled
        self._zoom_changed()

    def current_path(self) -> Path | None:
        current = self.canvas.current()
        return current.path if current is not None else None

    def _tick(self) -> None:
        if self.canvas.current() is not None:
            self.tickRequested.emit(self.current_path())

    def _zoom_changed(self) -> None:
        trail = self.canvas.trail()
        if trail:
            names = " › ".join(node.name for node in trail)
            current = trail[-1]
            self.trail_label.setText(f"{names}   ({human_size(current.bytes)})")
        else:
            self.trail_label.setText("No size map yet.")
        self.up_button.setEnabled(self.canvas.can_go_up())
        self.tick_button.setEnabled(self._tick_allowed and bool(trail))
