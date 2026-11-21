"""MediaGrid: grouping, selection, the right-click menu, lazy thumbnails and 50,000 items."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction
from pytestqt.qtbot import QtBot

from various_tools.core.media.library import MediaItem
from various_tools.gui.timeline_page import timeline_order
from various_tools.gui.widgets.media_grid import MediaGrid


def item(
    path: Path,
    *,
    kind: str = "photo",
    cloud: bool = False,
    taken: datetime | None = datetime(2024, 5, 6, 7, 8, 9),
    place: tuple[float, float] | None = None,
) -> MediaItem:
    source = "modified (in OneDrive only)" if cloud else "camera"
    lat, lon = place if place else (None, None)
    return MediaItem(path, kind, 10, 0.0, taken, source, lat, lon, None, None, cloud)


def photo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), (10, 200, 30)).save(path, "JPEG")
    return path


@pytest.fixture
def grid(qtbot: QtBot) -> MediaGrid:
    widget = MediaGrid(grouped=True)
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.reveal = lambda _p: True
    widget.open_file = lambda _p: True
    widget.edit_file = lambda _p: True
    widget.show_properties = lambda _p: True
    widget.confirm = lambda _t, _x: False
    widget.load_thumbnail = lambda _p: None
    return widget


def labels(menu_actions: list[QAction]) -> list[str]:
    return [a.text() for a in menu_actions if not a.isSeparator()]


def test_items_are_grouped_by_month_and_day(grid: MediaGrid, tmp_path: Path) -> None:
    counts: list[int] = []
    grid.countChanged.connect(counts.append)
    items = timeline_order(
        [
            item(tmp_path / "a.jpg", taken=datetime(2022, 11, 22, 14, 0)),
            item(tmp_path / "b.jpg", taken=datetime(2022, 11, 22, 9, 0)),
            item(tmp_path / "c.jpg", taken=datetime(2022, 11, 3, 9, 0)),
            item(tmp_path / "d.jpg", taken=datetime(2022, 10, 3, 9, 0)),
            item(tmp_path / "e.jpg", taken=None),
        ]
    )
    grid.set_items(items)
    assert counts == [5]
    assert grid.header_texts() == [
        "November 2022 · 3",
        "Tuesday 22 November 2022 · 2",
        "Thursday 3 November 2022 · 1",
        "October 2022 · 1",
        "Monday 3 October 2022 · 1",
        "No date · 1",
    ]
    # inside a day the oldest comes first
    assert [i.path.name for i in grid.items()][:2] == ["b.jpg", "a.jpg"]
    grid.set_items([])
    assert grid.items() == [] and counts == [5, 0]


def test_caption_has_name_and_date(grid: MediaGrid, tmp_path: Path) -> None:
    grid.set_items([item(tmp_path / "a.jpg"), item(tmp_path / "b.mp4", kind="video", taken=None)])
    assert grid.caption(0) == "a.jpg\n6 May 2024"
    assert grid.caption(1) == "b.mp4"


def test_scroll_to_month(grid: MediaGrid, tmp_path: Path) -> None:
    many = [
        item(tmp_path / f"{n}.jpg", taken=datetime(2022, 12, 1) - timedelta(days=n))
        for n in range(400)
    ]
    grid.show()
    grid.set_items(timeline_order(many))
    assert grid.scroll_to_month(2022, 3)
    assert grid.verticalScrollBar().value() > 0
    assert not grid.scroll_to_month(1999, 1)


def test_selection_ctrl_shift_and_select_all(grid: MediaGrid, qtbot: QtBot, tmp_path: Path) -> None:
    grid.show()
    qtbot.waitExposed(grid)
    grid.set_items([item(tmp_path / f"{n}.jpg", taken=datetime(2024, 5, 1, n)) for n in range(6)])
    viewport = grid.viewport()
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=grid.rect_of(1).center())
    assert grid.selected_rows() == [1]
    qtbot.mouseClick(
        viewport,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        grid.rect_of(3).center(),
    )
    assert grid.selected_rows() == [1, 3]
    qtbot.mouseClick(
        viewport,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        grid.rect_of(5).center(),
    )
    assert grid.selected_rows() == [3, 4, 5]  # from the last clicked item, like Explorer
    qtbot.keyClick(grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert grid.selected_rows() == [0, 1, 2, 3, 4, 5]
    qtbot.keyClick(grid, Qt.Key.Key_Left)
    assert grid.selected_rows() == [grid.current_row]


def test_enter_and_double_click_activate(grid: MediaGrid, qtbot: QtBot, tmp_path: Path) -> None:
    grid.show()
    qtbot.waitExposed(grid)
    grid.set_items([item(tmp_path / "a.jpg"), item(tmp_path / "b.jpg")])
    seen: list[int] = []
    grid.activate = lambda row: seen.append(row)
    qtbot.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=grid.rect_of(1).center())
    grid.set_current(0)
    qtbot.keyClick(grid, Qt.Key.Key_Return)
    assert seen == [1, 0]


def test_default_activation_opens_the_file(grid: MediaGrid, qtbot: QtBot, tmp_path: Path) -> None:
    grid.show()
    qtbot.waitExposed(grid)
    pic = photo(tmp_path / "a.jpg")
    opened: list[Path] = []
    grid.open_file = lambda p: opened.append(p) is None
    grid.set_items([item(pic)])
    qtbot.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=grid.rect_of(0).center())
    assert opened == [pic]


def test_context_menu_for_one_photo_with_a_place(grid: MediaGrid, tmp_path: Path) -> None:
    pic = photo(tmp_path / "a.jpg")
    grid.set_items([item(pic, place=(48.0, 2.0))])
    menu = grid.menu_for(0)
    assert labels(menu.actions()) == [
        "Open",
        "Edit",
        "Show in File Explorer",
        "Properties",
        "Copy path",
        "Show on map",
        "Change date…",
    ]
    assert all(a.isEnabled() for a in menu.actions())


def test_context_menu_without_a_place_or_for_a_video(grid: MediaGrid, tmp_path: Path) -> None:
    pic, vid = photo(tmp_path / "a.jpg"), tmp_path / "b.mp4"
    vid.write_bytes(b"x")
    grid.set_items([item(pic), item(vid, kind="video")])
    assert "Show on map" not in labels(grid.menu_for(0).actions())
    edit = next(a for a in grid.menu_for(1).actions() if a.text() == "Edit")
    assert not edit.isEnabled()


def test_context_menu_for_a_selection(grid: MediaGrid, tmp_path: Path) -> None:
    a, b = photo(tmp_path / "a.jpg"), photo(tmp_path / "b.jpg")
    grid.set_items([item(a, place=(1.0, 1.0)), item(b)])
    grid.select_all()
    menu = grid.menu_for_rows(grid.selected_rows())
    assert labels(menu.actions()) == [
        "Open",
        "Edit",
        "Show in File Explorer",
        "Properties",
        "Copy paths",
        "Change date…",
    ]
    assert not next(x for x in menu.actions() if x.text() == "Properties").isEnabled()


def test_menu_entries_call_the_hooks_and_signals(grid: MediaGrid, tmp_path: Path) -> None:
    pic = photo(tmp_path / "a.jpg")
    calls: list[tuple[str, object]] = []
    grid.reveal = lambda p: calls.append(("reveal", p)) is None
    grid.open_file = lambda p: calls.append(("open", p)) is None
    grid.edit_file = lambda p: calls.append(("edit", p)) is None
    grid.show_properties = lambda p: calls.append(("props", p)) is None
    grid.showOnMapRequested.connect(lambda p: calls.append(("map", p)))
    grid.changeDateRequested.connect(lambda p: calls.append(("date", p)))
    grid.set_items([item(pic, place=(1.0, 2.0))])

    for action in grid.menu_for(0).actions():
        if not action.isSeparator() and action.text() != "Copy path":
            action.trigger()

    assert calls == [
        ("open", pic),
        ("edit", pic),
        ("reveal", pic),
        ("props", pic),
        ("map", pic),
        ("date", [pic]),
    ]


def test_copy_paths_goes_to_the_clipboard(grid: MediaGrid, tmp_path: Path) -> None:
    from PySide6.QtGui import QGuiApplication

    a, b = photo(tmp_path / "a.jpg"), photo(tmp_path / "b.jpg")
    grid.set_items([item(a), item(b)])
    next(x for x in grid.menu_for_rows([0, 1]).actions() if x.text() == "Copy paths").trigger()
    clipboard = QGuiApplication.clipboard()
    assert clipboard is not None and clipboard.text() == f"{a}\n{b}"


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
    assert grid.context_menu_at(grid.rect_of(0).center()) is not None
    assert grid.context_menu_at(QPoint(590, 390)) is None


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
        return local.read_bytes()

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
    many = [
        item(tmp_path / f"{n:04}.jpg", taken=datetime(2024, 1, 1) + timedelta(minutes=n))
        for n in range(2000)
    ]
    grid.set_items(timeline_order(many))
    qtbot.waitUntil(lambda: bool(asked), timeout=10000)
    qtbot.wait(200)
    assert 0 < len(asked) < 500


def test_thumbnail_size_changes_the_layout(grid: MediaGrid, tmp_path: Path) -> None:
    grid.show()
    grid.set_items(
        [item(tmp_path / f"{n}.jpg", taken=datetime(2024, 1, 1, 0, n)) for n in range(40)]
    )
    sizes: list[int] = []
    grid.thumbSizeChanged.connect(sizes.append)
    before = grid.rect_of(0).width()
    grid.set_thumb_size(200)
    assert grid.rect_of(0).width() == 200 > before and sizes == [200]
    grid.set_thumb_size(9999)
    assert grid.thumb_size == 256


def test_ctrl_wheel_changes_the_size(grid: MediaGrid, qtbot: QtBot, tmp_path: Path) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    grid.show()
    qtbot.waitExposed(grid)
    grid.set_items([item(tmp_path / "a.jpg")])
    start = grid.thumb_size
    event = QWheelEvent(
        QPointF(50, 50),
        QPointF(50, 50),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(grid.viewport(), event)
    assert grid.thumb_size > start


def test_fifty_thousand_items_build_and_scroll_quickly(grid: MediaGrid, tmp_path: Path) -> None:
    base = datetime(2000, 1, 1)
    many = [
        item(tmp_path / f"p{n}.jpg", taken=base + timedelta(minutes=37 * n)) for n in range(50_000)
    ]
    grid.show()
    started = time.perf_counter()
    grid.set_items(timeline_order(many))
    built = time.perf_counter() - started
    assert built < 10, built
    assert len(grid.month_headers()) > 40
    started = time.perf_counter()
    grid.verticalScrollBar().setValue(grid.verticalScrollBar().maximum() // 2)
    grid.set_selection([25_000])
    assert grid.visible_rows()
    assert time.perf_counter() - started < 2


def test_a_big_selection_menu_does_not_stat_every_file(
    grid: MediaGrid, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    pics = [photo(tmp_path / f"{i}.jpg") for i in range(30)]
    grid.set_items([item(p) for p in pics])
    grid.select_all()
    calls: list[object] = []
    real = os.path.lexists
    monkeypatch.setattr(os.path, "lexists", lambda p: (calls.append(p), real(p))[1])
    grid.menu_for_rows(grid.selected_rows())
    assert len(calls) <= 1


def test_row_of_path_uses_an_index_that_follows_the_items(grid: MediaGrid, tmp_path: Path) -> None:
    a, b = photo(tmp_path / "a.jpg"), photo(tmp_path / "b.jpg")
    grid.set_items([item(a), item(b)])
    assert grid.row_of_path(b) == 1 and grid.row_of_path(tmp_path / "x.jpg") == -1
    grid.set_items([item(b)])
    assert grid.row_of_path(b) == 0 and grid.row_of_path(a) == -1


def test_a_resize_that_keeps_the_column_count_does_not_lay_out_again(
    qtbot: QtBot, grid: MediaGrid, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grid.show()
    qtbot.waitExposed(grid)
    grid.set_items([item(photo(tmp_path / f"{i}.jpg")) for i in range(20)])
    calls: list[int] = []
    real = grid._relayout  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(grid, "_relayout", lambda: (calls.append(1), real())[1])
    cols = grid._cols  # pyright: ignore[reportPrivateUsage]
    grid.resize(grid.width(), grid.height() + 120)  # taller only
    qtbot.wait(50)
    assert calls == [] and grid._cols == cols  # pyright: ignore[reportPrivateUsage]
    qtbot.wait(50)
    grid.resize(300, grid.height())  # fewer columns
    qtbot.wait(50)
    assert calls and grid._cols < cols  # pyright: ignore[reportPrivateUsage]
