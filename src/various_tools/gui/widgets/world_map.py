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

import contextlib
import hashlib
import json
import math
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cache, partial
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QHelpEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPixmap,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import QToolTip, QWidget

MIN_ZOOM = 1
MAX_ZOOM = 18
TILE_PX = 256
MAX_LAT = 85.0511287798  # Web Mercator cuts the poles here
ATTRIBUTION = "© OpenStreetMap contributors"
_DRAG_THRESHOLD_PX = 4
_MAX_TILES_IN_MEMORY = 256
_TILE_RETRY_S = 5 * 60  # a failed tile is not asked for again for this long
_MAX_TILE_FAILURES = 5  # this many failures in a row stop all requests until the source is reset
_PRUNE_EVERY_BYTES = 5 * 1024 * 1024  # look at the cache size again after this much was written


def _now() -> float:
    """Monotonic seconds. A function so tests can replace the clock."""
    return time.monotonic()


def tile_source_folder(root: Path, template: str) -> Path:
    """The folder for the pictures of one map address: a short hash of it, inside ``root``.

    The name starts with a letter so it can never be taken for an old ``<zoom>`` folder.
    """
    digest = hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]
    return root / f"s{digest}"


def prune_tile_cache(root: Path, max_bytes: int) -> int:
    """Delete the oldest files under ``root`` until it holds at most ``max_bytes``.

    Age is the file's modified time (when the picture was downloaded). Empty folders are
    removed. A missing folder or a file that cannot be removed is ignored. Returns how many
    files were deleted.
    """
    files: list[tuple[float, int, str]] = []
    total = 0
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
            total += st.st_size
    if total <= max_bytes:
        return 0
    files.sort()
    removed = 0
    for _mtime, size, path in files:
        if total <= max_bytes:
            break
        try:
            os.unlink(path)
        except OSError:
            continue
        total -= size
        removed += 1
    for folder, _dirs, _names in os.walk(root, topdown=False):
        if folder != str(root):
            with contextlib.suppress(OSError):
                os.rmdir(folder)  # only succeeds when empty
    return removed


def remove_legacy_tile_folders(root: Path) -> None:
    """Delete ``<zoom>`` folders that older versions put straight under ``root``.

    Those pictures were not tied to a map address, so they could belong to another source. It is
    only a cache; the pictures are downloaded again when needed.
    """
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if entry.name.isdigit() and entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path, ignore_errors=True)


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
    if n == 0:
        return np.empty(0), np.empty(0)
    lats = np.fromiter((p[0] for p in points), dtype=np.float64, count=n)
    lats = np.clip(lats, -MAX_LAT, MAX_LAT)
    lons = np.fromiter((p[1] for p in points), dtype=np.float64, count=n)
    xs = (lons + 180.0) / 360.0
    ys = 0.5 - np.log(np.tan(np.pi / 4 + np.radians(lats) / 2)) / (2 * np.pi)
    return xs, ys


@dataclass
class _ClusterSet:
    """The circles of one zoom level, with numpy arrays of where they are and how big."""

    clusters: list[Cluster]
    wx: Any
    wy: Any
    counts: Any


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
        self.setMouseTracking(True)
        # Text for the hover tooltip of a circle, from the payloads of its members.
        self.describe: Callable[[list[object]], str] = lambda members: f"{len(members):,} here"
        self._highlight: tuple[float, float] | None = None
        self._payloads: list[object] = []
        self._xs: Any = np.empty(0)
        self._ys: Any = np.empty(0)
        self._cluster_cache: dict[int, _ClusterSet] = {}
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
        self._cache_root: Path | None = None
        self._cache_limit_bytes = 0
        self._written_since_prune = 0
        self._user_agent = ""
        self._tiles: dict[tuple[int, int, int], QPixmap] = {}
        self._pending: set[tuple[int, int, int]] = set()
        self._failed: dict[tuple[int, int, int], float] = {}  # key -> when to retry
        self._failures_in_a_row = 0

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

    def set_highlight(self, lat: float | None, lon: float | None) -> None:
        """Ring one position (``None`` removes the ring)."""
        self._highlight = (lat, lon) if lat is not None and lon is not None else None
        self.update()

    @property
    def highlight(self) -> tuple[float, float] | None:
        return self._highlight

    def fit_all(self) -> None:
        """Zoom and move so that every point is on screen."""
        if len(self._xs) == 0:
            self._zoom, self._cx, self._cy = 1, 0.5, 0.5
            self.update()
            return
        x0, x1 = float(self._xs.min()), float(self._xs.max())
        y0, y1 = float(self._ys.min()), float(self._ys.max())
        span = max(x1 - x0, y1 - y0, 1e-9)
        room = max(1.0, min(self.width(), self.height()) - 80.0)
        zoom = math.floor(math.log2(room / (TILE_PX * span)))
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self._cx, self._cy = (x0 + x1) / 2, (y0 + y1) / 2
        self._clamp_center()
        self.update()

    def members_near(self, lat: float, lon: float) -> list[object]:
        """The payloads of the circle that is drawn at this position (empty when none is)."""
        hit = self._cluster_at(self.lonlat_to_screen(lon, lat))
        return list(hit.members) if hit is not None else []

    def tooltip_at(self, pos: QPointF) -> str | None:
        hit = self._cluster_at(pos)
        return self.describe(list(hit.members)) if hit is not None else None

    def center_lonlat(self) -> tuple[float, float]:
        """``(lon, lat)`` of the middle of the view; the longitude is in -180 to 180."""
        return world_to_lonlat(self._cx, self._cy)

    def screen_to_lonlat(self, pos: QPointF) -> tuple[float, float]:
        """``(lon, lat)`` under a position of the widget (the longitude is wrapped)."""
        wx, wy = self._screen_to_world(pos)
        lon, lat = world_to_lonlat(wx, min(1.0, max(0.0, wy)))
        return (lon + 180.0) % 360.0 - 180.0, lat

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
        """Where a place is drawn: the copy of the world that is nearest to the view's middle."""
        x, y = lonlat_to_world(lon, lat)
        x -= math.floor(x - self._cx + 0.5)
        return self._world_to_screen(x, y)

    def set_tile_source(
        self,
        url_template: str | None,
        cache_dir: Path | None,
        user_agent: str,
        *,
        cache_root: Path | None = None,
        cache_limit_mb: int = 200,
    ) -> None:
        """Turn raster tiles on (a URL template with ``{z}/{x}/{y}``) or off (``None``).

        Pictures are kept in ``cache_dir``. When ``cache_root`` (the folder that holds the
        folders of every source) is given, it is kept under ``cache_limit_mb``: the oldest
        pictures go first, when the source is set and again as new ones are saved.
        """
        self._template = url_template
        self._cache_dir = cache_dir
        self._user_agent = user_agent
        self._cache_root = cache_root if url_template is not None else None
        self._cache_limit_bytes = max(1, cache_limit_mb) * 1024 * 1024
        self._written_since_prune = 0
        if self._cache_root is not None:
            prune_tile_cache(self._cache_root, self._cache_limit_bytes)
        self._tiles.clear()
        self._pending.clear()
        self._failed.clear()
        self._failures_in_a_row = 0
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
        """The map wraps sideways (across the 180 degree line) and stops at the poles."""
        self._cx %= 1.0
        self._cy = min(1.0, max(0.0, self._cy))

    def _copies(self, margin_px: float) -> range:
        """Which sideways copies of the world (shifts of whole world widths) can be on screen."""
        s = self._scale()
        left = self._cx - (self.width() / 2 + margin_px) / s
        right = self._cx + (self.width() / 2 + margin_px) / s
        return range(math.floor(left) - 1, math.floor(right) + 1)

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

    def _cluster_set(self) -> _ClusterSet:
        found = self._cluster_cache.get(self._zoom)
        if found is None:
            clusters = _cluster_world(self._xs, self._ys, self._payloads, self._zoom, self.CELL_PX)
            wx, wy = _world_arrays([(c.lat, c.lon, None) for c in clusters])
            counts = np.fromiter((len(c.members) for c in clusters), dtype=np.int64)
            found = _ClusterSet(clusters, wx, wy, counts)
            self._cluster_cache[self._zoom] = found
        return found

    def _clusters(self) -> list[Cluster]:
        return self._cluster_set().clusters

    @staticmethod
    def _radius(count: int) -> float:
        return 10.0 + 4.0 * math.log10(max(1, count)) * 2

    @staticmethod
    def _radii(counts: Any) -> Any:
        return 10.0 + 8.0 * np.log10(np.maximum(1, counts))

    # ---- interaction -------------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position()
            self._press_center = (self._cx, self._cy)
            self._dragging = False

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            text = self.tooltip_at(QPointF(event.pos()))
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

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
        found = self._cluster_set()
        if not found.clusters:
            return None
        s = self._scale()
        ox = self.width() / 2 - self._cx * s
        oy = self.height() / 2 - self._cy * s
        py = found.wy * s + oy
        radii = self._radii(found.counts)
        best: int | None = None
        best_d = math.inf
        for k in self._copies(0.0):
            px = (found.wx + k) * s + ox
            d = np.hypot(px - pos.x(), py - pos.y())
            d = np.where(d <= radii, d, np.inf)
            i = int(np.argmin(d))
            if d[i] < best_d:
                best, best_d = i, float(d[i])
        return found.clusters[best] if best is not None else None

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
        if (
            key not in self._pending
            and self._template is not None
            and self._manager is not None
            and self._may_request(key)
        ):
            self._request_tile(z, x, y)
        return None

    def _may_request(self, key: tuple[int, int, int]) -> bool:
        if self._failures_in_a_row >= _MAX_TILE_FAILURES:
            return False  # offline or blocked: stay quiet until set_tile_source is called again
        retry_at = self._failed.get(key)
        return retry_at is None or _now() >= retry_at

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
        pix = QPixmap()
        if not data or not pix.loadFromData(data):
            self._failed[key] = _now() + _TILE_RETRY_S
            self._failures_in_a_row += 1
            return
        self._failed.pop(key, None)
        self._failures_in_a_row = 0
        path = self._tile_path(*key)
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except OSError:
                pass  # the cache is only a convenience
            else:
                self._written_since_prune += len(data)
                if self._cache_root is not None and self._written_since_prune >= _PRUNE_EVERY_BYTES:
                    self._written_since_prune = 0
                    prune_tile_cache(self._cache_root, self._cache_limit_bytes)
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
        self._paint_highlight(painter, pal)
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
        for k in self._copies(50.0):
            for ring in _land_world():
                xs = (ring[:, 0] + k) * s + ox
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
        x0 = math.floor(left * n)
        x1 = math.floor(right * n)
        y0 = max(0, math.floor(top * n))
        y1 = min(n - 1, math.floor(bottom * n))
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                pix = self._tile(z, tx % n, ty)  # sideways, the world repeats
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
        found = self._cluster_set()
        if not found.clusters:
            return
        py = found.wy * s + oy
        row = (py >= view.top()) & (py <= view.bottom())
        for k in self._copies(40.0):
            px = (found.wx + k) * s + ox
            for i in np.flatnonzero(row & (px >= view.left()) & (px <= view.right())).tolist():
                count = int(found.counts[i])
                r = self._radius(count)
                cx, cy = float(px[i]), float(py[i])
                painter.setPen(ring)
                painter.drawEllipse(QPointF(cx, cy), r, r)
                if count > 1:
                    painter.setPen(label)
                    painter.drawText(
                        QRectF(cx - r, cy - r, 2 * r, 2 * r),
                        Qt.AlignmentFlag.AlignCenter,
                        _short_count(count),
                    )

    def _paint_highlight(self, painter: QPainter, pal: QPalette) -> None:
        if self._highlight is None:
            return
        p = self.lonlat_to_screen(self._highlight[1], self._highlight[0])
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(230, 60, 40))
        painter.drawEllipse(p, 22.0, 22.0)
        painter.drawEllipse(p, 20.0, 20.0)

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
