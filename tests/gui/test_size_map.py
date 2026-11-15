"""The size map (plan 7.1): squarified layout, zoom, tooltips, keyboard and plan helpers."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from pytestqt.qtbot import QtBot

from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.gui.widgets.size_map import (
    SizeMap,
    SizeMapView,
    actions_under,
    share_text,
    squarify,
)
from various_tools.tools.disk_usage import SizeNode


def tree(base: Path) -> SizeNode:
    """``base`` (1000 bytes): big (600, with one sub-folder of 400), small (300), 100 own."""
    inner = SizeNode("inner", base / "big" / "inner", 400, 2)
    big = SizeNode("big", base / "big", 600, 1, (inner,))
    small = SizeNode("small", base / "small", 300, 1)
    return SizeNode("base", base, 1000, 0, (big, small))


# ----- layout ----------------------------------------------------------------------------------


@pytest.mark.parametrize("values", [[6, 6, 4, 3, 2, 2, 1], [1], [10, 1], [5, 5, 5, 5], [100, 0, 3]])
def test_squarify_fills_the_box_in_proportion(values: list[float]) -> None:
    box = (0.0, 0.0, 600.0, 400.0)
    rects = squarify(values, box)
    assert len(rects) == len(values)
    total = sum(values)
    for value, (x, y, w, h) in zip(values, rects, strict=True):
        assert math.isclose(w * h, 600 * 400 * value / total, rel_tol=1e-6, abs_tol=1e-6)
        assert x >= -1e-6 and y >= -1e-6 and x + w <= 600 + 1e-6 and y + h <= 400 + 1e-6
    assert math.isclose(sum(w * h for _x, _y, w, h in rects), 600 * 400, rel_tol=1e-6)


def test_squarify_without_room_or_values_gives_empty_rectangles() -> None:
    assert squarify([1, 2], (0, 0, 0, 10)) == [(0, 0, 0.0, 0.0)] * 2
    assert squarify([0, 0], (0, 0, 10, 10)) == [(0, 0, 0.0, 0.0)] * 2
    assert squarify([], (0, 0, 10, 10)) == []


def test_share_text() -> None:
    assert share_text(1, 30) == "3.3%"
    assert share_text(1, 3) == "33%"
    assert share_text(1, 2) == "50%"
    assert share_text(1, 0) == "0%"


def test_actions_under_folder_files_and_roots(tmp_path: Path) -> None:
    plan = Plan(
        tool="t",
        roots=[tmp_path],
        actions=[
            PlannedAction(ActionKind.TRASH, tmp_path / "big" / "a.bin"),
            PlannedAction(ActionKind.TRASH, tmp_path / "big" / "inner" / "b.bin"),
            PlannedAction(ActionKind.TRASH, tmp_path / "small" / "c.bin"),
            PlannedAction(ActionKind.MOVE, tmp_path / "big" / "d.bin", tmp_path / "x"),
            PlannedAction(ActionKind.TRASH, tmp_path / "BIG"),  # the folder itself is not "in" it
        ],
    )
    trash = frozenset({ActionKind.TRASH})
    assert actions_under(plan, tmp_path / "big", kinds=trash) == [0, 1]
    assert actions_under(plan, tmp_path / "Big") == [0, 1, 3]
    assert actions_under(plan, tmp_path / "big", recursive=False) == [0, 3]
    assert actions_under(plan, None, kinds=trash) == [0, 1, 2, 4]


# ----- the canvas -------------------------------------------------------------------------------


def shown_map(qtbot: QtBot, root: SizeNode) -> SizeMap:
    canvas = SizeMap()
    qtbot.addWidget(canvas)
    canvas.resize(600, 400)
    canvas.show()
    canvas.set_tree(root)
    return canvas


def test_tiles_are_the_folders_and_the_files_here(qtbot: QtBot, tmp_path: Path) -> None:
    canvas = shown_map(qtbot, tree(tmp_path))
    names = [t.name for t in canvas.tiles()]
    assert names == ["big", "small", "Files in base"]
    areas = [t.rect.width() * t.rect.height() for t in canvas.tiles()]
    assert areas[0] > areas[1] > areas[2]
    tip = canvas.tooltip_for(0)
    assert "big" in tip and "600 B" in tip and "60%" in tip and str(tmp_path / "big") in tip
    assert "Files in base" in canvas.tooltip_for(2) and "10%" in canvas.tooltip_for(2)


def test_click_zooms_in_and_tells_the_page_up_goes_back(qtbot: QtBot, tmp_path: Path) -> None:
    canvas = shown_map(qtbot, tree(tmp_path))
    chosen: list[tuple[object, bool]] = []
    canvas.folderChosen.connect(lambda path, recursive: chosen.append((path, recursive)))
    centre = canvas.tiles()[0].rect.center()
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=centre.toPoint())
    assert chosen == [(tmp_path / "big", True)]
    assert canvas.current() is not None and canvas.current().name == "big"  # type: ignore[union-attr]
    assert [t.name for t in canvas.tiles()] == ["inner", "Files in big"]
    files = canvas.tiles()[1].rect.center()
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=files.toPoint())
    assert chosen[-1] == (tmp_path / "big", False)  # the files directly in "big"
    assert canvas.go_up()
    assert canvas.current().name == "base"  # type: ignore[union-attr]
    assert not canvas.go_up()


def test_keyboard_moves_zooms_and_goes_up(qtbot: QtBot, tmp_path: Path) -> None:
    canvas = shown_map(qtbot, tree(tmp_path))
    canvas.setFocus()
    assert canvas.focused_index() == 0
    qtbot.keyClick(canvas, Qt.Key.Key_Right)
    moved = canvas.focused_index()
    assert moved != 0
    qtbot.keyClick(canvas, Qt.Key.Key_Left)
    assert canvas.focused_index() == 0
    qtbot.keyClick(canvas, Qt.Key.Key_Return)
    assert canvas.current().name == "big"  # type: ignore[union-attr]
    qtbot.keyClick(canvas, Qt.Key.Key_Backspace)
    assert canvas.current().name == "base"  # type: ignore[union-attr]
    assert canvas.focused_index() == 0  # back on the folder we came from


def test_tile_at_and_empty_map(qtbot: QtBot) -> None:
    canvas = SizeMap()
    qtbot.addWidget(canvas)
    canvas.resize(300, 200)
    canvas.set_tree(None)
    assert canvas.tiles() == [] and canvas.tile_at(QPointF(10, 10)) == -1
    canvas.grab()  # paints the "scan first" text without error


def test_view_trail_up_button_and_tick(qtbot: QtBot, tmp_path: Path) -> None:
    view = SizeMapView()
    qtbot.addWidget(view)
    view.resize(700, 500)
    view.show()
    view.set_tree(tree(tmp_path))
    assert "base" in view.trail_label.text()
    assert not view.up_button.isEnabled()
    ticks: list[object] = []
    view.tickRequested.connect(ticks.append)
    view.canvas.choose(0)
    assert "base › big" in view.trail_label.text()
    assert view.up_button.isEnabled()
    view.tick_button.click()
    assert ticks == [tmp_path / "big"]
    view.up_button.click()
    assert view.current_path() == tmp_path
    view.set_tick_enabled(False)
    assert not view.tick_button.isEnabled()
    pixmap = view.grab()
    assert not pixmap.isNull()
