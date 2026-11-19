"""MediaGrid: items, captions, right-click menu, double-click and lazy thumbnails."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from pytestqt.qtbot import QtBot

from various_tools.core.media.library import MediaItem
from various_tools.gui.widgets.media_grid import MediaGrid


def item(
    path: Path,
    *,
    kind: str = "photo",
    cloud: bool = False,
    taken: datetime | None = datetime(2024, 5, 6, 7, 8, 9),
) -> MediaItem:
    source = "modified (in OneDrive only)" if cloud else "camera"
    return MediaItem(path, kind, 10, 0.0, taken, source, None, None, None, None, cloud)


def photo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), (10, 200, 30)).save(path, "JPEG")
    return path


@pytest.fixture
def grid(qtbot: QtBot) -> MediaGrid:
    widget = MediaGrid()
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.reveal = lambda _p: True
    widget.open_file = lambda _p: True
    widget.edit_file = lambda _p: True
    widget.confirm = lambda _t, _x: False
    widget.load_thumbnail = lambda _p: None
    return widget


def test_set_items_shows_them_with_name_and_date(grid: MediaGrid, tmp_path: Path) -> None:
    counts: list[int] = []
    grid.countChanged.connect(counts.append)
    items = [item(tmp_path / "a.jpg"), item(tmp_path / "b.mp4", kind="video", taken=None)]

    grid.set_items(items)

    assert grid.items() == items
    assert counts == [2]
    assert grid.caption(0) == "a.jpg\n6 May 2024"
    assert grid.caption(1) == "b.mp4"
    grid.set_items([])
    assert grid.items() == [] and counts == [2, 0]


def test_context_menu_labels_and_edit_only_for_photos(grid: MediaGrid, tmp_path: Path) -> None:
    pic, vid = photo(tmp_path / "a.jpg"), tmp_path / "b.mp4"
    vid.write_bytes(b"x")
    grid.set_items([item(pic), item(vid, kind="video")])

    menu = grid.menu_for(0)
    labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert labels == ["Open", "Edit", "Show in File Explorer", "Copy path"]
    assert all(a.isEnabled() for a in menu.actions())

    video_menu = grid.menu_for(1)
    edit = next(a for a in video_menu.actions() if a.text() == "Edit")
    assert not edit.isEnabled()


def test_menu_entries_call_the_hooks(grid: MediaGrid, tmp_path: Path) -> None:
    pic = photo(tmp_path / "a.jpg")
    calls: list[tuple[str, Path]] = []
    grid.reveal = lambda p: calls.append(("reveal", p)) is None
    grid.open_file = lambda p: calls.append(("open", p)) is None
    grid.edit_file = lambda p: calls.append(("edit", p)) is None
    grid.set_items([item(pic)])

    for action in grid.menu_for(0).actions():
        if action.text() in ("Open", "Edit", "Show in File Explorer"):
            action.trigger()

    assert calls == [("open", pic), ("edit", pic), ("reveal", pic)]


def test_edit_that_fails_is_disabled_for_that_kind_of_file(grid: MediaGrid, tmp_path: Path) -> None:
    a, b = photo(tmp_path / "a.png"), photo(tmp_path / "b.png")
    grid.edit_file = lambda _p: False
    grid.set_items([item(a), item(b)])
    next(x for x in grid.menu_for(0).actions() if x.text() == "Edit").trigger()
    assert not next(x for x in grid.menu_for(1).actions() if x.text() == "Edit").isEnabled()


def test_context_menu_at_a_position(grid: MediaGrid, qtbot: QtBot, tmp_path: Path) -> None:
    grid.show()
    qtbot.waitExposed(grid)
    grid.set_items([item(photo(tmp_path / "a.jpg"))])
    rect = grid.view.visualRect(grid.view.model().index(0, 0))
    assert grid.context_menu_at(rect.center()) is not None
    assert grid.context_menu_at(QPoint(590, 390)) is None


def test_double_click_calls_the_open_hook(grid: MediaGrid, qtbot: QtBot, tmp_path: Path) -> None:
    grid.show()
    qtbot.waitExposed(grid)
    pic = photo(tmp_path / "a.jpg")
    opened: list[Path] = []
    grid.open_file = lambda p: opened.append(p) is None
    grid.set_items([item(pic)])
    rect = grid.view.visualRect(grid.view.model().index(0, 0))
    qtbot.mouseClick(grid.view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qtbot.mouseDClick(grid.view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert opened == [pic]


def test_opening_a_cloud_only_item_asks_first(
    grid: MediaGrid, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.gui import reveal

    pic = photo(tmp_path / "a.jpg")
    monkeypatch.setattr(reveal, "needs_download", lambda _p: True)
    asked: list[str] = []
    opened: list[Path] = []
    grid.confirm = lambda title, _t: asked.append(title) is None and False
    grid.open_file = lambda p: opened.append(p) is None
    grid.set_items([item(pic, cloud=True)])
    grid.open_row(0)
    assert asked and opened == []


def test_thumbnails_load_lazily_and_never_for_cloud_only_items(
    grid: MediaGrid, qtbot: QtBot, tmp_path: Path
) -> None:
    grid.show()
    qtbot.waitExposed(grid)
    local = photo(tmp_path / "local.jpg")
    remote = photo(tmp_path / "remote.jpg")
    asked: list[Path] = []

    def load(path: Path) -> bytes | None:
        asked.append(path)
        return (tmp_path / "local.jpg").read_bytes()

    grid.load_thumbnail = load
    grid.set_items([item(remote, cloud=True), item(local)])

    qtbot.waitUntil(lambda: grid.has_thumbnail(1), timeout=10000)
    assert asked == [local]
    assert not grid.has_thumbnail(0)


def test_only_visible_items_get_thumbnails(grid: MediaGrid, qtbot: QtBot, tmp_path: Path) -> None:
    grid.show()
    qtbot.waitExposed(grid)
    asked: list[Path] = []

    def load(path: Path) -> bytes | None:
        asked.append(path)
        return None

    grid.load_thumbnail = load
    many = [item(tmp_path / f"{n:04}.jpg") for n in range(2000)]
    grid.set_items(many)
    qtbot.waitUntil(lambda: bool(asked), timeout=10000)
    qtbot.wait(200)
    assert 0 < len(asked) < 500
