"""World map widget: projection, clustering, interaction and the (faked) tile source."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtNetwork import QNetworkRequest
from pytestqt.qtbot import QtBot

from various_tools.gui.widgets import world_map as wm
from various_tools.gui.widgets.world_map import (
    Cluster,
    WorldMap,
    cluster,
    lonlat_to_world,
    tile_url,
    world_to_lonlat,
)


def test_projection_known_values() -> None:
    assert lonlat_to_world(0, 0) == pytest.approx((0.5, 0.5))
    assert lonlat_to_world(-180, 0)[0] == pytest.approx(0.0)
    assert lonlat_to_world(180, 0)[0] == pytest.approx(1.0)
    _, y = lonlat_to_world(0, 85.0511287798)
    assert y == pytest.approx(0.0, abs=1e-6)
    assert lonlat_to_world(0, 89.9)[1] >= 0.0
    assert lonlat_to_world(0, -90)[1] <= 1.0


@pytest.mark.parametrize("lon,lat", [(0, 0), (12.5, 41.9), (-122.4, 37.8), (151.2, -33.9)])
def test_projection_round_trip(lon: float, lat: float) -> None:
    x, y = lonlat_to_world(lon, lat)
    lon2, lat2 = world_to_lonlat(x, y)
    assert lon2 == pytest.approx(lon, abs=1e-9)
    assert lat2 == pytest.approx(lat, abs=1e-9)
    assert not math.isnan(x + y)


def test_tile_url_formatting() -> None:
    assert (
        tile_url("https://tile.example/{z}/{x}/{y}.png", 3, 4, 5)
        == "https://tile.example/3/4/5.png"
    )


def test_cluster_empty() -> None:
    assert cluster([], 5, 60) == []


def test_cluster_merges_nearby_at_low_zoom_and_splits_at_high() -> None:
    points = [(48.8566, 2.3522, "a"), (48.8570, 2.3530, "b"), (-33.9, 151.2, "c")]
    low = cluster(points, 3, 60)
    assert sorted(len(c.members) for c in low) == [1, 2]
    high = cluster(points, 18, 60)
    assert sorted(len(c.members) for c in high) == [1, 1, 1]


def test_cluster_preserves_payloads_and_position() -> None:
    payloads = [object(), object(), object()]
    pts = [(10.0, 20.0, payloads[0]), (10.0, 20.0, payloads[1]), (-40.0, -70.0, payloads[2])]
    result = cluster(pts, 2, 60)
    members = [m for c in result for m in c.members]
    assert sorted(map(id, members)) == sorted(map(id, payloads))
    big = max(result, key=lambda c: len(c.members))
    assert isinstance(big, Cluster)
    assert big.lon == pytest.approx(20.0)
    assert big.lat == pytest.approx(10.0)


def test_land_data_loads() -> None:
    land = wm.load_land()
    assert len(land) > 100
    lon, lat = land[0][0]
    assert -180 <= lon <= 180
    assert -90 <= lat <= 90


def _screen_point(widget: WorldMap, lat: float, lon: float) -> QPoint:
    p = widget.lonlat_to_screen(lon, lat)
    return QPoint(round(p.x()), round(p.y()))


def test_click_on_cluster_emits_payloads(qtbot: QtBot) -> None:
    widget = WorldMap()
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.show()
    widget.set_points([(10.0, 20.0, "a"), (10.0, 20.0, "b"), (-40.0, -70.0, "c")])
    widget.set_view(15.0, 0.0, 2)
    with qtbot.waitSignal(widget.clusterClicked, timeout=1000) as sig:
        qtbot.mouseClick(widget, Qt.MouseButton.LeftButton, pos=_screen_point(widget, 10.0, 20.0))
    assert sig.args is not None
    assert sig.args is not None
    assert sorted(sig.args[0]) == ["a", "b"]


def test_click_on_empty_space_emits_nothing(qtbot: QtBot) -> None:
    widget = WorldMap()
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.set_points([(10.0, 20.0, "a")])
    widget.set_view(15.0, 0.0, 2)
    with qtbot.assertNotEmitted(widget.clusterClicked):
        qtbot.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))


def test_zoom_bounds(qtbot: QtBot) -> None:
    widget = WorldMap()
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    for _ in range(30):
        widget.zoom_in()
    assert widget.zoom == 18
    for _ in range(30):
        widget.zoom_out()
    assert widget.zoom == 1


def test_drag_pans(qtbot: QtBot) -> None:
    widget = WorldMap()
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.set_view(0.0, 0.0, 3)
    before = widget.lonlat_to_screen(0.0, 0.0)
    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(300, 200))
    qtbot.mouseMove(widget, QPoint(340, 200))
    qtbot.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(340, 200))
    after = widget.lonlat_to_screen(0.0, 0.0)
    assert after.x() - before.x() == pytest.approx(40, abs=1)


class _FakeReply(QObject):
    finished = Signal()

    def __init__(self, data: bytes, error: int = 0) -> None:
        super().__init__()
        self._data = data
        self._error = error

    def readAll(self) -> QByteArray:  # noqa: N802
        return QByteArray(self._data)

    def error(self) -> int:
        return self._error

    def deleteLater(self) -> None:  # noqa: N802
        pass


class _FakeManager(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[QNetworkRequest] = []
        self.replies: list[_FakeReply] = []
        self.fail = False

    def get(self, request: QNetworkRequest) -> _FakeReply:
        self.requests.append(request)
        reply = _FakeReply(b"", error=1) if self.fail else _FakeReply(_png_bytes())
        self.replies.append(reply)
        return reply


def _png_bytes() -> bytes:
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")  # pyright: ignore[reportCallIssue, reportArgumentType]
    return bytes(buf.data().data())


def test_tiles_off_by_default_and_no_request(qtbot: QtBot) -> None:
    manager = _FakeManager()
    widget = WorldMap(network_manager=manager)
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.show()
    widget.grab()
    widget.zoom_in()
    widget.grab()
    assert widget.tiles_enabled is False
    assert manager.requests == []


def test_tile_request_has_user_agent_and_is_cached(qtbot: QtBot, tmp_path: Path) -> None:
    manager = _FakeManager()
    widget = WorldMap(network_manager=manager)
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.set_tile_source("https://tile.example/{z}/{x}/{y}.png", tmp_path, "VariousTools/test")
    assert widget.tiles_enabled is True
    widget.set_view(0.0, 0.0, 2)
    widget.grab()
    assert manager.requests, "painting should have requested tiles"
    req = manager.requests[0]
    assert req.rawHeader("User-Agent").toStdString() == "VariousTools/test"
    url = req.url().toString()
    assert url.startswith("https://tile.example/2/")
    z, x, y = url.removeprefix("https://tile.example/").removesuffix(".png").split("/")
    manager.replies[0].finished.emit()
    assert (tmp_path / z / x / f"{y}.png").is_file()
    # A second paint uses the cache and the tile is not requested again.
    count = len(manager.requests)
    widget.grab()
    assert all(r.url().toString() != url for r in manager.requests[count:])


def test_tile_source_none_turns_tiles_off(qtbot: QtBot, tmp_path: Path) -> None:
    widget = WorldMap(network_manager=_FakeManager())
    qtbot.addWidget(widget)
    widget.set_tile_source("https://tile.example/{z}/{x}/{y}.png", tmp_path, "UA")
    widget.set_tile_source(None, None, "UA")
    assert widget.tiles_enabled is False


def _failing_widget(qtbot: QtBot, tmp_path: Path) -> tuple[WorldMap, _FakeManager]:
    manager = _FakeManager()
    manager.fail = True
    widget = WorldMap(network_manager=manager)
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.set_tile_source("https://tile.example/{z}/{x}/{y}.png", tmp_path, "UA")
    widget.set_view(0.0, 0.0, 2)
    return widget, manager


def _fail_all_pending(manager: _FakeManager, done: int) -> int:
    for reply in manager.replies[done:]:
        reply.finished.emit()
    return len(manager.replies)


def test_failed_tile_is_not_requested_again_until_the_backoff_is_over(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(wm, "_now", lambda: now[0])
    widget, manager = _failing_widget(qtbot, tmp_path)
    widget.grab()
    first = len(manager.requests)
    assert first > 0
    for reply in manager.replies[:2]:  # two failures: well under the give-up limit
        reply.finished.emit()

    widget.grab()
    assert len(manager.requests) == first  # failed tiles wait

    now[0] += 4 * 60
    widget.grab()
    assert len(manager.requests) == first  # still inside the 5 minutes

    now[0] += 2 * 60
    widget.grab()
    assert len(manager.requests) == first + 2  # backoff over: only the failed two are asked again


def test_tiles_stop_after_five_failures_in_a_row_until_the_source_is_set_again(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(wm, "_now", lambda: now[0])
    widget, manager = _failing_widget(qtbot, tmp_path)
    done = 0
    for _ in range(10):
        widget.grab()
        done = _fail_all_pending(manager, done)
        now[0] += 6 * 60
    assert done >= 5
    total = len(manager.requests)
    for _ in range(3):
        widget.grab()
        now[0] += 6 * 60
    assert len(manager.requests) == total  # gave up

    widget.set_tile_source("https://tile.example/{z}/{x}/{y}.png", tmp_path, "UA")
    widget.grab()
    assert len(manager.requests) > total


# ----- tile cache: one folder per source, capped (part 1, item 5) --------------------------------


def _fill(folder: Path, names: list[str], size: int, start: float) -> None:
    import os

    for n, name in enumerate(names):
        path = folder / "1" / "0" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        os.utime(path, (start + n, start + n))


def test_each_tile_source_gets_its_own_folder(tmp_path: Path) -> None:
    a = wm.tile_source_folder(tmp_path, "https://a.example/{z}/{x}/{y}.png")
    b = wm.tile_source_folder(tmp_path, "https://b.example/{z}/{x}/{y}.png")
    assert a != b
    assert a.parent == tmp_path == b.parent
    assert a == wm.tile_source_folder(tmp_path, "https://a.example/{z}/{x}/{y}.png")
    assert a.name.isalnum()


def test_prune_removes_the_oldest_files_first(tmp_path: Path) -> None:
    _fill(tmp_path / "src1", ["old.png", "mid.png"], 400, start=1000)
    _fill(tmp_path / "src2", ["new.png"], 400, start=5000)

    removed = wm.prune_tile_cache(tmp_path, max_bytes=800)

    assert removed == 1
    assert not (tmp_path / "src1" / "1" / "0" / "old.png").exists()
    assert (tmp_path / "src1" / "1" / "0" / "mid.png").exists()
    assert (tmp_path / "src2" / "1" / "0" / "new.png").exists()
    assert wm.prune_tile_cache(tmp_path, max_bytes=800) == 0


def test_prune_leaves_a_cache_under_the_limit_alone_and_survives_a_missing_folder(
    tmp_path: Path,
) -> None:
    _fill(tmp_path / "s", ["a.png"], 100, start=1)
    assert wm.prune_tile_cache(tmp_path, max_bytes=10_000) == 0
    assert wm.prune_tile_cache(tmp_path / "nothing", max_bytes=1) == 0


def test_old_layout_folders_are_removed_but_source_folders_stay(tmp_path: Path) -> None:
    _fill(tmp_path / "3", ["1.png"], 10, start=1)  # old: <z>/<x>/<y>.png straight under the root
    (tmp_path / "7").mkdir()
    keep = wm.tile_source_folder(tmp_path, "https://a.example/{z}/{x}/{y}.png")
    _fill(keep, ["k.png"], 10, start=1)

    wm.remove_legacy_tile_folders(tmp_path)

    assert not (tmp_path / "3").exists() and not (tmp_path / "7").exists()
    assert (keep / "1" / "0" / "k.png").exists()


def test_the_widget_prunes_the_cache_when_the_source_is_set(qtbot: QtBot, tmp_path: Path) -> None:
    root = tmp_path / "map_tiles"
    folder = wm.tile_source_folder(root, "https://tile.example/{z}/{x}/{y}.png")
    _fill(folder, ["a.png", "b.png", "c.png"], 1024 * 1024, start=100)
    widget = WorldMap()
    qtbot.addWidget(widget)

    widget.set_tile_source(
        "https://tile.example/{z}/{x}/{y}.png", folder, "UA", cache_root=root, cache_limit_mb=2
    )

    left = sorted(p.name for p in folder.rglob("*.png"))
    assert left == ["b.png", "c.png"]
