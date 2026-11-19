"""A small world map: land outlines, clustered points and optional online tiles.

* ``lonlat_to_world`` / ``world_to_lonlat`` are Web Mercator on the unit square (0..1), the same
  layout the usual slippy-map tile servers use, so tile ``(z, x, y)`` covers the world square
  ``[x / 2**z, (x + 1) / 2**z]``.
* ``cluster`` groups points that fall in the same ``cell_px`` square of the screen at a zoom.
* ``WorldMap`` draws the land from ``data/world_land.json`` (offline, always available). Raster
  tiles are off until ``set_tile_source`` is called with a URL template; they are fetched with a
  ``QNetworkAccessManager``, cached on disk as ``cache_dir/z/x/y.png`` and drawn under the
  clusters together with the "© OpenStreetMap contributors" attribution.
* Drag pans, the wheel and double-click zoom (1..18), clicking a cluster emits
  ``clusterClicked`` with the payloads of its members.
* Colours come from the widget palette, so light and dark themes both read well.

The widget never touches the user's files; the only disk writes are tile cache files.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache, partial
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPixmap,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import QWidget

MIN_ZOOM = 1
MAX_ZOOM = 18
TILE_PX = 256
MAX_LAT = 85.0511287798  # Web Mercator cuts the poles here
ATTRIBUTION = "© OpenStreetMap contributors"
_DRAG_THRESHOLD_PX = 4
_MAX_TILES_IN_MEMORY = 256


def lonlat_to_world(lon: float, lat: float) -> tuple[float, float]:
    """Web Mercator on the unit square: (0, 0) is the north-west corner, (1, 1) the south-east."""
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    x = (lon + 180.0) / 360.0
    y = 0.5 - math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) / (2 * math.pi)
    return x, y


def world_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Inverse of ``lonlat_to_world``; returns ``(lon, lat)``."""
    lon = x * 360.0 - 180.0
    lat = math.degrees(2 * math.atan(math.exp((0.5 - y) * 2 * math.pi)) - math.pi / 2)
    return lon, lat


def tile_url(template: str, z: int, x: int, y: int) -> str:
    return template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))


@dataclass
class Cluster:
    lon: float
    lat: float
    members: list[object] = field(default_factory=list)


def _world_arrays(points: Sequence[tuple[float, float, object]]) -> tuple[Any, Any]:
    n = len(points)
    xs = np.empty(n, dtype=np.float64)
    ys = np.empty(n, dtype=np.float64)
    for i, (lat, lon, _payload) in enumerate(points):
        xs[i], ys[i] = lonlat_to_world(lon, lat)
    return xs, ys


def _cluster_world(
    xs: Any, ys: Any, payloads: Sequence[object], zoom: int, cell_px: int
) -> list[Cluster]:
    """Group by screen cell. ``xs``/``ys`` are world coordinates (numpy arrays)."""
    n = len(payloads)
    if n == 0:
        return []
    scale = TILE_PX * (2**zoom)
    cell = max(1, cell_px)
    cx = np.floor(xs * scale / cell).astype(np.int64)
    cy = np.floor(ys * scale / cell).astype(np.int64)
    key = cx * 4_000_000_007 + cy
    order = np.argsort(key, kind="stable")
    sorted_key = key[order]
    starts = np.concatenate(([0], np.flatnonzero(np.diff(sorted_key)) + 1))
    counts = np.diff(np.concatenate((starts, [n])))
    sx = np.add.reduceat(xs[order], starts) / counts
    sy = np.add.reduceat(ys[order], starts) / counts
    order_list = order.tolist()
    result: list[Cluster] = []
    for start, count, mx, my in zip(
        starts.tolist(), counts.tolist(), sx.tolist(), sy.tolist(), strict=True
    ):
        lon, lat = world_to_lonlat(mx, my)
        members = [payloads[i] for i in order_list[start : start + count]]
        result.append(Cluster(lon, lat, members))
    return result


def cluster(
    points: Sequence[tuple[float, float, object]], zoom: int, cell_px: int
) -> list[Cluster]:
    """Cluster ``(lat, lon, payload)`` points that share a ``cell_px`` screen cell at ``zoom``."""
    xs, ys = _world_arrays(points)
    return _cluster_world(xs, ys, [p[2] for p in points], zoom, cell_px)


@cache
def load_land() -> list[list[tuple[float, float]]]:
    """The land rings as lists of ``(lon, lat)``, from the bundled Natural Earth data."""
    text = (
        resources.files("various_tools.gui")
        .joinpath("data", "world_land.json")
        .read_text(encoding="utf-8")
    )
    rings = json.loads(text)
    return [[(float(lon), float(lat)) for lon, lat in ring] for ring in rings]


@cache
def _land_world() -> list[Any]:
    out = []
    for ring in load_land():
        out.append(np.array([lonlat_to_world(lon, lat) for lon, lat in ring], dtype=np.float64))
    return out


class WorldMap(QWidget):
    """Pan, zoom and click a clustered world map."""

    clusterClicked = Signal(list)  # noqa: N815

    CELL_PX = 56

    def __init__(
        self, parent: QWidget | None = None, *, network_manager: Any | None = None
    ) -> None:
        super().__init__(parent)
        self.setMinimumSize(240, 160)
        self.setMouseTracking(False)
        self._payloads: list[object] = []
        self._xs: Any = np.empty(0)
        self._ys: Any = np.empty(0)
        self._cluster_cache: dict[int, list[Cluster]] = {}
        self._zoom = 1
        self._cx = 0.5
        self._cy = 0.5
        self._press_pos: QPointF | None = None
        self._press_center = (0.5, 0.5)
        self._dragging = False
        # Tiles: off until a source is set.
        self._manager: Any | None = network_manager
        self._template: str | None = None
        self._cache_dir: Path | None = None
        self._user_agent = ""
        self._tiles: dict[tuple[int, int, int], QPixmap] = {}
        self._pending: set[tuple[int, int, int]] = set()

    # ---- public API --------------------------------------------------------------------------

    @property
    def zoom(self) -> int:
        return self._zoom

    @property
    def tiles_enabled(self) -> bool:
        return self._template is not None

    def set_points(self, points: Sequence[tuple[float, float, object]]) -> None:
        """``(lat, lon, payload)`` for each point."""
        self._payloads = [p[2] for p in points]
        self._xs, self._ys = _world_arrays(points)
        self._cluster_cache.clear()
        self.update()

    def set_view(self, lat: float, lon: float, zoom: int) -> None:
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self._cx, self._cy = lonlat_to_world(lon, lat)
        self._clamp_center()
        self.update()

    def zoom_in(self, anchor: QPointF | None = None) -> None:
        self._zoom_by(1, anchor)

    def zoom_out(self, anchor: QPointF | None = None) -> None:
        self._zoom_by(-1, anchor)

    def lonlat_to_screen(self, lon: float, lat: float) -> QPointF:
        x, y = lonlat_to_world(lon, lat)
        return self._world_to_screen(x, y)

    def set_tile_source(
        self, url_template: str | None, cache_dir: Path | None, user_agent: str
    ) -> None:
        """Turn raster tiles on (a URL template with ``{z}/{x}/{y}``) or off (``None``)."""
        self._template = url_template
        self._cache_dir = cache_dir
        self._user_agent = user_agent
        self._tiles.clear()
        self._pending.clear()
        if url_template is not None and self._manager is None:
            self._manager = QNetworkAccessManager(self)
        self.update()

    # ---- geometry ----------------------------------------------------------------------------

    def _scale(self) -> float:
        return float(TILE_PX * (2**self._zoom))

    def _world_to_screen(self, x: float, y: float) -> QPointF:
        s = self._scale()
        return QPointF(
            self.width() / 2 + (x - self._cx) * s, self.height() / 2 + (y - self._cy) * s
        )

    def _screen_to_world(self, p: QPointF) -> tuple[float, float]:
        s = self._scale()
        return (
            self._cx + (p.x() - self.width() / 2) / s,
            self._cy + (p.y() - self.height() / 2) / s,
        )

    def _clamp_center(self) -> None:
        self._cx = min(1.0, max(0.0, self._cx))
        self._cy = min(1.0, max(0.0, self._cy))

    def _zoom_by(self, delta: int, anchor: QPointF | None) -> None:
        new = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom + delta))
        if new == self._zoom:
            return
        if anchor is None:
            anchor = QPointF(self.width() / 2, self.height() / 2)
        wx, wy = self._screen_to_world(anchor)
        self._zoom = new
        s = self._scale()
        self._cx = wx - (anchor.x() - self.width() / 2) / s
        self._cy = wy - (anchor.y() - self.height() / 2) / s
        self._clamp_center()
        self.update()

    def _clusters(self) -> list[Cluster]:
        clusters = self._cluster_cache.get(self._zoom)
        if clusters is None:
            clusters = _cluster_world(self._xs, self._ys, self._payloads, self._zoom, self.CELL_PX)
            self._cluster_cache[self._zoom] = clusters
        return clusters

    @staticmethod
    def _radius(count: int) -> float:
        return 10.0 + 4.0 * math.log10(max(1, count)) * 2

    # ---- interaction -------------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position()
            self._press_center = (self._cx, self._cy)
            self._dragging = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press_pos is None:
            return
        delta = event.position() - self._press_pos
        if not self._dragging and max(abs(delta.x()), abs(delta.y())) < _DRAG_THRESHOLD_PX:
            return
        self._dragging = True
        s = self._scale()
        self._cx = self._press_center[0] - delta.x() / s
        self._cy = self._press_center[1] - delta.y() / s
        self._clamp_center()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            return
        was_drag = self._dragging
        self._press_pos = None
        self._dragging = False
        if was_drag:
            return
        hit = self._cluster_at(event.position())
        if hit is not None:
            self.clusterClicked.emit(list(hit.members))

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = None
            self.zoom_in(event.position())

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        dy = event.angleDelta().y()
        if dy > 0:
            self.zoom_in(event.position())
        elif dy < 0:
            self.zoom_out(event.position())
        event.accept()

    def _cluster_at(self, pos: QPointF) -> Cluster | None:
        best: Cluster | None = None
        best_d = math.inf
        for c in self._clusters():
            p = self.lonlat_to_screen(c.lon, c.lat)
            d = math.hypot(p.x() - pos.x(), p.y() - pos.y())
            if d <= self._radius(len(c.members)) and d < best_d:
                best, best_d = c, d
        return best

    # ---- tiles -------------------------------------------------------------------------------

    def _tile_path(self, z: int, x: int, y: int) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / str(z) / str(x) / f"{y}.png"

    def _tile(self, z: int, x: int, y: int) -> QPixmap | None:
        key = (z, x, y)
        pix = self._tiles.get(key)
        if pix is not None:
            return pix
        path = self._tile_path(z, x, y)
        if path is not None and path.is_file():
            loaded = QPixmap(str(path))
            if not loaded.isNull():
                self._remember(key, loaded)
                return loaded
        if key not in self._pending and self._template is not None and self._manager is not None:
            self._request_tile(z, x, y)
        return None

    def _remember(self, key: tuple[int, int, int], pix: QPixmap) -> None:
        if len(self._tiles) >= _MAX_TILES_IN_MEMORY:
            self._tiles.pop(next(iter(self._tiles)))
        self._tiles[key] = pix

    def _request_tile(self, z: int, x: int, y: int) -> None:
        assert self._template is not None
        assert self._manager is not None
        key = (z, x, y)
        request = QNetworkRequest(QUrl(tile_url(self._template, z, x, y)))
        request.setRawHeader(b"User-Agent", self._user_agent.encode("utf-8"))
        reply = self._manager.get(request)
        self._pending.add(key)
        reply.finished.connect(partial(self._tile_finished, reply, key))

    def _tile_finished(self, reply: Any, key: tuple[int, int, int]) -> None:
        self._pending.discard(key)
        try:
            err = reply.error()
            failed = getattr(err, "value", err) != 0
            data = bytes(reply.readAll()) if not failed else b""
        finally:
            reply.deleteLater()
        if not data:
            return
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        path = self._tile_path(*key)
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except OSError:
                pass  # the cache is only a convenience
        if self._template is not None:
            self._remember(key, pix)
            self.update()

    # ---- painting ----------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        pal = self.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = pal.color(QPalette.ColorRole.Base)
        text = pal.color(QPalette.ColorRole.Text)
        ocean = _mix(base, text, 0.06)
        land = _mix(base, text, 0.22)
        painter.fillRect(self.rect(), ocean)
        if self._template is None:
            self._paint_land(painter, land, _mix(base, text, 0.35))
        else:
            self._paint_tiles(painter)
        self._paint_clusters(painter, pal)
        if self._template is not None:
            self._paint_attribution(painter, pal)
        painter.end()

    def _paint_land(self, painter: QPainter, fill: QColor, outline: QColor) -> None:
        painter.setBrush(fill)
        painter.setPen(outline)
        s = self._scale()
        ox = self.width() / 2 - self._cx * s
        oy = self.height() / 2 - self._cy * s
        view = QRectF(self.rect()).adjusted(-50, -50, 50, 50)
        for ring in _land_world():
            xs = ring[:, 0] * s + ox
            ys = ring[:, 1] * s + oy
            if xs.max() < view.left() or xs.min() > view.right():
                continue
            if ys.max() < view.top() or ys.min() > view.bottom():
                continue
            painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in zip(xs, ys, strict=True)]))

    def _paint_tiles(self, painter: QPainter) -> None:
        z = self._zoom
        n = 2**z
        s = self._scale()
        left, top = self._screen_to_world(QPointF(0, 0))
        right, bottom = self._screen_to_world(QPointF(self.width(), self.height()))
        x0 = max(0, math.floor(left * n))
        x1 = min(n - 1, math.floor(right * n))
        y0 = max(0, math.floor(top * n))
        y1 = min(n - 1, math.floor(bottom * n))
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                pix = self._tile(z, tx, ty)
                if pix is None:
                    continue
                p = self._world_to_screen(tx / n, ty / n)
                size = s / n
                painter.drawPixmap(QRectF(p.x(), p.y(), size, size), pix, QRectF(pix.rect()))

    def _paint_clusters(self, painter: QPainter, pal: QPalette) -> None:
        fill = pal.color(QPalette.ColorRole.Highlight)
        label = pal.color(QPalette.ColorRole.HighlightedText)
        ring = pal.color(QPalette.ColorRole.Base)
        fill.setAlpha(220)
        painter.setBrush(fill)
        view = QRectF(self.rect()).adjusted(-40, -40, 40, 40)
        s = self._scale()
        ox = self.width() / 2 - self._cx * s
        oy = self.height() / 2 - self._cy * s
        for c in self._clusters():
            wx, wy = lonlat_to_world(c.lon, c.lat)
            px, py = wx * s + ox, wy * s + oy
            if not view.contains(px, py):
                continue
            count = len(c.members)
            r = self._radius(count)
            painter.setPen(ring)
            painter.drawEllipse(QPointF(px, py), r, r)
            if count > 1:
                painter.setPen(label)
                painter.drawText(
                    QRectF(px - r, py - r, 2 * r, 2 * r),
                    Qt.AlignmentFlag.AlignCenter,
                    _short_count(count),
                )

    def _paint_attribution(self, painter: QPainter, pal: QPalette) -> None:
        metrics = QFontMetricsF(painter.font())
        w = metrics.horizontalAdvance(ATTRIBUTION) + 10
        h = metrics.height() + 4
        box = QRectF(self.width() - w - 2, self.height() - h - 2, w, h)
        bg = pal.color(QPalette.ColorRole.Base)
        bg.setAlpha(200)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRect(box)
        painter.setPen(pal.color(QPalette.ColorRole.Text))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, ATTRIBUTION)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def _short_count(count: int) -> str:
    if count >= 10_000:
        return f"{count // 1000}k"
    return str(count)
