"""Right-click on a results row: show its files in File Explorer, open them, copy the path."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from various_tools.core import deps
from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.gui import reveal
from various_tools.gui.reveal import reveal_targets
from various_tools.gui.widgets.plan_table import FOLDER_TREE, PlanTable


def _labels(menu: QMenu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def _action(menu: QMenu, text: str):  # type: ignore[no-untyped-def]
    return next(a for a in menu.actions() if a.text() == text)


def test_one_place_gives_one_target(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tmp"
    junk.write_text("x")
    targets = reveal_targets(PlannedAction(ActionKind.TRASH, junk), None, is_duplicates=False)
    assert [(t.label, t.path, t.exists) for t in targets] == [("Show in File Explorer", junk, True)]


def test_duplicates_give_this_copy_and_the_kept_copy(tmp_path: Path) -> None:
    keeper, copy = tmp_path / "a.jpg", tmp_path / "sub" / "a.jpg"
    keeper.write_text("k")
    copy.parent.mkdir()
    copy.write_text("k")
    action = PlannedAction(ActionKind.MOVE, copy, tmp_path / "duplicate" / "a.jpg", "", str(keeper))
    targets = reveal_targets(action, None, is_duplicates=True)
    assert [(t.label, t.path) for t in targets] == [
        ("Show this copy", copy),
        ("Show the kept copy", keeper),
        ("Show where it will go", tmp_path),  # duplicate/ does not exist yet
    ]


def test_a_move_shows_the_nearest_existing_destination_folder(tmp_path: Path) -> None:
    src = tmp_path / "a.jpg"
    src.write_text("x")
    action = PlannedAction(ActionKind.MOVE, src, tmp_path / "2024" / "01" / "a.jpg")
    now, later = reveal_targets(action, None, is_duplicates=False)
    assert (now.label, now.path, now.is_destination) == ("Show where it is now", src, False)
    assert (later.label, later.path) == ("Show where it will go", tmp_path)
    assert later.is_destination and later.exists


def test_a_missing_source_is_marked_missing(tmp_path: Path) -> None:
    gone = tmp_path / "gone.txt"
    (target,) = reveal_targets(PlannedAction(ActionKind.TRASH, gone), None, is_duplicates=False)
    assert target.exists is False


def test_a_folder_row_without_an_action_shows_its_folder(tmp_path: Path) -> None:
    (target,) = reveal_targets(None, tmp_path, is_duplicates=False)
    assert (target.label, target.path, target.exists) == ("Show in File Explorer", tmp_path, True)


def test_explorer_is_named_only_by_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path))
    assert deps.system_tool("explorer") == "explorer.exe"
    (tmp_path / "explorer.exe").write_text("")
    assert deps.system_tool("explorer") == str(tmp_path / "explorer.exe")


# ----- the menu in the plan table -------------------------------------------------------------


def _dup_table(qtbot: QtBot, tmp_path: Path) -> tuple[PlanTable, Path, Path]:
    keeper, copy = tmp_path / "a.jpg", tmp_path / "b.jpg"
    keeper.write_text("k")
    copy.write_text("k")
    plan = Plan(
        tool="duplicates",
        roots=[tmp_path],
        actions=[PlannedAction(ActionKind.TRASH, copy, None, "exact", str(keeper))],
    )
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    table.view.expandAll()
    table.show()
    return table, keeper, copy


def _row_point(table: PlanTable, row: int, parent_row: int | None = None) -> QPoint:
    model = table.view.model()
    parent = model.index(parent_row, 0) if parent_row is not None else None
    index = model.index(row, 0, parent) if parent is not None else model.index(row, 0)
    return table.view.visualRect(index).center()


def test_menu_reveals_each_file_of_a_duplicate_row(qtbot: QtBot, tmp_path: Path) -> None:
    table, keeper, copy = _dup_table(qtbot, tmp_path)
    shown: list[Path] = []
    table.reveal = lambda p: shown.append(p) or True  # type: ignore[method-assign]
    menu = table.context_menu_at(_row_point(table, 0, parent_row=0))
    assert menu is not None
    assert _labels(menu) == ["Show this copy", "Show the kept copy", "Open", "Copy path"]
    _action(menu, "Show this copy").trigger()
    _action(menu, "Show the kept copy").trigger()
    assert shown == [copy, keeper]


def test_menu_disables_a_file_that_is_gone(qtbot: QtBot, tmp_path: Path) -> None:
    table, _keeper, copy = _dup_table(qtbot, tmp_path)
    copy.unlink()
    menu = table.context_menu_at(_row_point(table, 0, parent_row=0))
    assert menu is not None
    assert not _action(menu, "Show this copy").isEnabled()
    assert not _action(menu, "Open").isEnabled()
    assert _action(menu, "Show the kept copy").isEnabled()


def test_open_asks_before_downloading_a_cloud_only_file(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table, _keeper, copy = _dup_table(qtbot, tmp_path)
    monkeypatch.setattr(reveal, "needs_download", lambda _p: True)
    opened: list[Path] = []
    questions: list[str] = []
    table.open_file = lambda p: opened.append(p) or True  # type: ignore[method-assign]
    table.confirm = lambda title, _text: questions.append(title) or False  # type: ignore[method-assign]
    menu = table.context_menu_at(_row_point(table, 0, parent_row=0))
    assert menu is not None
    _action(menu, "Open").trigger()
    assert questions and opened == []
    table.confirm = lambda _t, _x: True  # type: ignore[method-assign]
    _action(menu, "Open").trigger()
    assert opened == [copy]


def test_folder_tree_folder_row_has_show_in_explorer(qtbot: QtBot, tmp_path: Path) -> None:
    folder = tmp_path / "a"
    (folder / "b").mkdir(parents=True)
    plan = Plan(
        tool="empty_folders",
        roots=[tmp_path],
        actions=[PlannedAction(ActionKind.TRASH, folder / "b", None, "empty")],
    )
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_view_mode(FOLDER_TREE)
    table.set_plan(plan, "empty_folders")
    table.view.expandAll()
    table.show()
    shown: list[Path] = []
    table.reveal = lambda p: shown.append(p) or True  # type: ignore[method-assign]
    menu = table.context_menu_at(_row_point(table, 0))  # the root row
    assert menu is not None
    _action(menu, "Show in File Explorer").trigger()
    assert shown == [tmp_path]


def test_no_menu_on_empty_space(qtbot: QtBot, tmp_path: Path) -> None:
    table, *_ = _dup_table(qtbot, tmp_path)
    assert table.context_menu_at(QPoint(5, table.view.viewport().height() - 2)) is None


# ----- review fixes ---------------------------------------------------------------------------


def test_explorer_command_quotes_spaces_and_commas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(reveal.subprocess, "Popen", lambda args, **_k: calls.append(args))
    monkeypatch.setattr(reveal, "system_tool", lambda _name: r"C:\Windows\explorer.exe")
    target = Path(r"C:\Pics\Summer, 2024\a,b c.jpg")
    assert reveal.show_in_explorer(target)
    assert calls == [r'"C:\Windows\explorer.exe" /select,"C:\Pics\Summer, 2024\a,b c.jpg"']


def test_a_moved_duplicate_also_shows_where_it_will_go(tmp_path: Path) -> None:
    keeper, copy = tmp_path / "a.jpg", tmp_path / "b.jpg"
    keeper.write_text("k")
    copy.write_text("k")
    review = tmp_path / "review" / "b.jpg"
    action = PlannedAction(ActionKind.MOVE, copy, review, "", str(keeper))
    labels = [t.label for t in reveal_targets(action, None, is_duplicates=True)]
    assert labels == ["Show this copy", "Show the kept copy", "Show where it will go"]


def test_a_new_file_row_copies_its_own_path(qtbot: QtBot, tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    plan = Plan(
        tool="project_export",
        roots=[tmp_path],
        actions=[PlannedAction(ActionKind.WRITE, None, out, "export")],
    )
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "project_export")
    table.show()
    menu = table.context_menu_at(_row_point(table, 0))
    assert menu is not None
    assert not _action(menu, "Open").isEnabled()  # not written yet
    _action(menu, "Copy path").trigger()
    from PySide6.QtGui import QGuiApplication

    assert QGuiApplication.clipboard().text() == str(out)
    out.write_text("done")  # after Apply the file exists
    menu = table.context_menu_at(_row_point(table, 0))
    assert menu is not None and _action(menu, "Open").isEnabled()
