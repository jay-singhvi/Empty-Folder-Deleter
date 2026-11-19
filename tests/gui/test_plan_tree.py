"""The plan table's "Folder tree" view (plan 3.1): folders as nodes, tri-state check boxes.

Every plan can be shown either grouped (the default) or as a real folder hierarchy built from the
actions' paths under the plan roots. Check boxes in the tree go through the same selection hook as
the flat table, so for empty_folders unchecking a folder still unchecks its parents.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, Qt
from pytestqt.qtbot import QtBot

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.core.settings import Settings
from various_tools.gui.widgets.plan_table import FOLDER_TREE, GROUPED, PlanTable
from various_tools.tools.empty_folders import TOOL as EMPTY_FOLDERS
from various_tools.tools.empty_folders import EmptyFoldersOptions

CHECK = Qt.ItemDataRole.CheckStateRole


def empty_folders_plan(root: Path) -> Plan:
    (root / "a" / "b").mkdir(parents=True, exist_ok=True)
    (root / "c").mkdir(exist_ok=True)
    (root / "c" / "Thumbs.db").write_text("junk")
    (root / "keep").mkdir(exist_ok=True)
    (root / "keep" / "file.txt").write_text("data")
    ctx = RunContext(
        settings=Settings(onedrive_root=root),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=None,
    )
    return EMPTY_FOLDERS.scan(EmptyFoldersOptions(roots=[root]), ctx)


def tree_table(qtbot: QtBot, plan: Plan, tool_id: str = "empty_folders") -> PlanTable:
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_view_mode(FOLDER_TREE)
    table.set_plan(plan, tool_id)
    return table


def names(table: PlanTable, parent: QModelIndex | None = None) -> list[str]:
    model = table.view.model()
    parent = parent or QModelIndex()
    return [
        str(model.data(model.index(row, 0, parent), Qt.ItemDataRole.DisplayRole))
        for row in range(model.rowCount(parent))
    ]


def find(table: PlanTable, *path: str) -> QModelIndex:
    """The proxy index reached by following display names from the top."""
    model = table.view.model()
    index = QModelIndex()
    for name in path:
        rows = [model.index(r, 0, index) for r in range(model.rowCount(index))]
        matches = [i for i in rows if model.data(i, Qt.ItemDataRole.DisplayRole) == name]
        assert matches, f"{name!r} not under {names(table, index)}"
        index = matches[0]
    return index


def state(table: PlanTable, index: QModelIndex) -> Qt.CheckState:
    return table.view.model().data(index, CHECK)


def test_the_default_view_is_grouped_and_the_switch_offers_both(qtbot: QtBot) -> None:
    table = PlanTable()
    qtbot.addWidget(table)
    assert table.view_mode() == GROUPED
    items = [table.view_combo.itemText(i) for i in range(table.view_combo.count())]
    assert items == ["Grouped", "Folder tree"]
    table.view_combo.setCurrentIndex(1)
    assert table.view_mode() == FOLDER_TREE


def test_folder_tree_builds_the_hierarchy_under_the_root(qtbot: QtBot, tmp_path: Path) -> None:
    plan = empty_folders_plan(tmp_path)
    table = tree_table(qtbot, plan)
    model = table.view.model()
    headers = [model.headerData(c, Qt.Orientation.Horizontal) for c in range(model.columnCount())]
    assert headers == ["Name", "What happens", "Goes to", "Why", "Size"]
    assert names(table) == [str(tmp_path)]
    assert names(table, find(table, str(tmp_path))) == ["a", "c"]  # "keep" is not in the plan
    assert names(table, find(table, str(tmp_path), "a")) == ["b"]
    assert names(table, find(table, str(tmp_path), "c")) == ["Thumbs.db"]
    kind = model.index(0, 1, find(table, str(tmp_path), "a"))
    assert model.data(kind, Qt.ItemDataRole.DisplayRole) == "Move to Recycle Bin"
    assert table.current_group_id() is None


def test_unchecking_a_nested_folder_uses_the_hook_and_shows_tri_state(
    qtbot: QtBot, tmp_path: Path
) -> None:
    plan = empty_folders_plan(tmp_path)
    table = tree_table(qtbot, plan)
    model = table.view.model()
    root = find(table, str(tmp_path))
    b = find(table, str(tmp_path), "a", "b")
    assert state(table, root) == Qt.CheckState.Checked
    assert model.setData(b, Qt.CheckState.Unchecked, CHECK)
    selected = {a.src.name: a.selected for a in plan.actions if a.src is not None}
    assert selected == {"a": False, "b": False, "c": True, "Thumbs.db": True}  # parent unchecked
    assert state(table, find(table, str(tmp_path), "a")) == Qt.CheckState.Unchecked
    assert state(table, find(table, str(tmp_path), "c")) == Qt.CheckState.Checked
    assert state(table, root) == Qt.CheckState.PartiallyChecked
    assert table.totals_text() == "4 actions, 2 selected"


def test_checking_a_folder_node_checks_everything_under_it(qtbot: QtBot, tmp_path: Path) -> None:
    plan = empty_folders_plan(tmp_path)
    plan.select_all(False)
    table = tree_table(qtbot, plan)
    model = table.view.model()
    root = find(table, str(tmp_path))
    assert state(table, root) == Qt.CheckState.Unchecked
    assert model.setData(root, Qt.CheckState.Checked, CHECK)
    assert all(a.selected for a in plan.actions)
    assert model.setData(find(table, str(tmp_path), "a"), Qt.CheckState.Unchecked, CHECK)
    selected = {a.src.name: a.selected for a in plan.actions if a.src is not None}
    assert selected == {"a": False, "b": False, "c": True, "Thumbs.db": True}


def test_filter_and_sizes_work_in_the_tree(qtbot: QtBot, tmp_path: Path) -> None:
    plan = empty_folders_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_view_mode(FOLDER_TREE)
    sizes: list[int | None] = [None] * len(plan.actions)
    junk = next(i for i, a in enumerate(plan.actions) if a.src and a.src.name == "Thumbs.db")
    sizes[junk] = 4
    table.set_plan(plan, "empty_folders", sizes)
    model = table.view.model()
    root = find(table, str(tmp_path))
    size_column = model.columnCount() - 1
    assert model.data(model.index(0, size_column), Qt.ItemDataRole.DisplayRole) == "4 B"
    assert model.data(root.siblingAtColumn(size_column), Qt.ItemDataRole.DisplayRole) == "4 B"
    table.filter_edit.setText("Thumbs")
    assert names(table, find(table, str(tmp_path))) == ["c"]
    table.filter_edit.setText("")
    assert names(table, find(table, str(tmp_path))) == ["a", "c"]


def test_switching_views_keeps_the_plan_and_its_checks(qtbot: QtBot, tmp_path: Path) -> None:
    plan = empty_folders_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "empty_folders")
    assert names(table) == sorted(names(table))  # grouped: one row per top folder
    assert str(tmp_path) not in names(table)
    table.set_action_checked(0, False)
    table.set_view_mode(FOLDER_TREE)
    assert names(table) == [str(tmp_path)]
    assert table.plan() is plan
    assert not plan.actions[0].selected
    table.set_view_mode(GROUPED)
    assert str(tmp_path) not in names(table)


def test_actions_outside_the_roots_and_without_a_path_sit_at_the_top(
    qtbot: QtBot, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    plan = Plan(
        tool="x",
        roots=[root],
        actions=[
            PlannedAction(ActionKind.MOVE, root / "deep" / "er" / "f.txt", root / "g.txt", "r"),
            PlannedAction(ActionKind.WRITE, None, tmp_path / "out.txt", "report"),
            PlannedAction(ActionKind.TRASH, tmp_path / "elsewhere.txt", None, "outside"),
        ],
    )
    table = tree_table(qtbot, plan, "x")
    assert names(table) == [str(root), "out.txt", "elsewhere.txt"]
    assert names(table, find(table, str(root), "deep", "er")) == ["f.txt"]
    deep = find(table, str(root), "deep")
    assert table.view.model().data(deep, CHECK) == Qt.CheckState.Checked
    assert table.view.model().setData(deep, Qt.CheckState.Unchecked, CHECK)
    assert [a.selected for a in plan.actions] == [False, True, True]  # no hook: just that subtree


def test_select_actions_selects_and_reveals_the_rows(qtbot: QtBot, tmp_path: Path) -> None:
    plan = empty_folders_plan(tmp_path)
    table = tree_table(qtbot, plan)
    junk = next(i for i, a in enumerate(plan.actions) if a.src and a.src.name == "Thumbs.db")
    folder_b = next(i for i, a in enumerate(plan.actions) if a.src and a.src.name == "b")
    assert table.select_actions([junk, folder_b]) == 2
    assert sorted(table.selected_action_indices()) == sorted([junk, folder_b])
    table.set_view_mode(GROUPED)
    assert table.select_actions([junk]) == 1
    assert table.selected_action_indices() == [junk]
