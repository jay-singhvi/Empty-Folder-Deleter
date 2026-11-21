"""Plan table (grouping, checks, filter, totals, columns) and the pure plan edits behind it."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QPushButton
from pytestqt.qtbot import QtBot

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.core.settings import Settings
from various_tools.gui import plan_edit
from various_tools.gui.widgets.plan_table import ALL_KINDS, EM_DASH, PlanTable
from various_tools.tools.empty_folders import TOOL as EMPTY_FOLDERS
from various_tools.tools.empty_folders import EmptyFoldersOptions

# ----- plan_edit ------------------------------------------------------------------------------


def dup_plan(tmp_path: Path) -> tuple[Plan, Path, Path, Path]:
    """One group: keeper ``a.jpg`` and two duplicates moved into ``duplicate/``."""
    keeper, b, c = tmp_path / "a.jpg", tmp_path / "b.jpg", tmp_path / "c.jpg"
    dup = tmp_path / "duplicate"
    gid = str(keeper)
    plan = Plan(
        tool="duplicates",
        roots=[tmp_path],
        actions=[
            PlannedAction(ActionKind.MOVE, b, dup / "b.jpg", "exact copy; keeps a.jpg", gid),
            PlannedAction(
                ActionKind.MOVE, c, dup / "c.jpg", "visual copy: phash 1; keeps a.jpg", gid
            ),
        ],
    )
    return plan, keeper, b, c


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (None, ""),
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024 * 1024, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
        (2 * 1024**4, "2.0 TB"),
        (5000 * 1024**4, "5000.0 TB"),
    ],
)
def test_human_size(size: int | None, text: str) -> None:
    assert plan_edit.human_size(size) == text


def test_group_helpers(tmp_path: Path) -> None:
    plan, keeper, b, _c = dup_plan(tmp_path)
    plan.actions.append(PlannedAction(ActionKind.TRASH, tmp_path / "x", reason="alone"))
    other = PlannedAction(ActionKind.MOVE, tmp_path / "y", None, "", "other-group")
    plan.actions.append(other)
    assert plan_edit.group_ids(plan) == [str(keeper), "other-group"]
    assert [a.src for a in plan_edit.group_actions(plan, str(keeper))][0] == b
    assert plan_edit.group_actions(plan, "missing") == []


def test_change_keeper_swaps_the_kept_file_and_rewrites_reasons(tmp_path: Path) -> None:
    plan, keeper, b, c = dup_plan(tmp_path)
    new_id = plan_edit.change_keeper(plan, str(keeper), b)
    assert new_id == str(b)
    first, second = plan.actions
    # The old keeper takes over b's action, with b's place in the duplicate folder.
    assert first.src == keeper
    assert first.dst == tmp_path / "duplicate" / "a.jpg"
    assert second.src == c  # untouched
    assert second.dst == tmp_path / "duplicate" / "c.jpg"
    assert {a.group_id for a in plan.actions} == {str(b)}
    assert first.reason == "exact copy; keeps b.jpg"
    assert second.reason == "visual copy: phash 1; keeps b.jpg"
    # A second change works from the new group id.
    again = plan_edit.change_keeper(plan, new_id, c)
    assert again == str(c)
    assert plan.actions[1].src == b


def test_change_keeper_keeps_the_selection_state_of_the_action(tmp_path: Path) -> None:
    plan, keeper, b, _c = dup_plan(tmp_path)
    plan.actions[0].selected = False
    plan_edit.change_keeper(plan, str(keeper), b)
    assert plan.actions[0].selected is False
    assert plan.actions[1].selected is True


def test_change_keeper_rejects_a_file_outside_the_group(tmp_path: Path) -> None:
    plan, keeper, _b, _c = dup_plan(tmp_path)
    before = [(a.src, a.dst, a.reason, a.group_id) for a in plan.actions]
    with pytest.raises(ValueError, match="not part of this group"):
        plan_edit.change_keeper(plan, str(keeper), tmp_path / "zzz.jpg")
    assert [(a.src, a.dst, a.reason, a.group_id) for a in plan.actions] == before


def test_change_keeper_accepts_an_equivalent_spelling_of_the_path(tmp_path: Path) -> None:
    plan, keeper, b, _c = dup_plan(tmp_path)
    odd = Path(str(b).upper())  # Windows paths compare without regard to case
    assert plan_edit.change_keeper(plan, str(keeper), odd) == str(b)


def test_change_keeper_never_touches_the_disk(tmp_path: Path) -> None:
    plan, keeper, b, _c = dup_plan(tmp_path)
    for path in (keeper, b):
        path.write_text(path.name)
    plan_edit.change_keeper(plan, str(keeper), b)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg", "b.jpg"]
    assert not (tmp_path / "duplicate").exists()


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("exact copy; keeps a.jpg", "exact copy; keeps b.jpg"),
        ("keeps a.jpg", "keeps b.jpg"),
        ("no marker at all", "no marker at all; keeps b.jpg"),
        ("", "keeps b.jpg"),
        ("keeps something else; keeps a.jpg", "keeps something else; keeps b.jpg"),
    ],
)
def test_rewrite_reason(reason: str, expected: str) -> None:
    assert plan_edit._rewrite_reason(reason, "a.jpg", "b.jpg") == expected  # pyright: ignore[reportPrivateUsage]


# ----- the table ------------------------------------------------------------------------------


def top_rows(table: PlanTable) -> int:
    return table.view.model().rowCount()


def child_rows(table: PlanTable, row: int) -> int:
    model = table.view.model()
    return model.rowCount(model.index(row, 0))


def header_labels(table: PlanTable) -> list[str]:
    model = table.view.model()
    return [
        str(model.headerData(i, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole))
        for i in range(model.columnCount())
    ]


def cell(table: PlanTable, row: int, column: int, parent: QModelIndex | None = None) -> str:
    model = table.view.model()
    index = model.index(row, column, parent) if parent is not None else model.index(row, column)
    return str(model.data(index, Qt.ItemDataRole.DisplayRole))


def test_empty_table_says_no_plan(qtbot: QtBot) -> None:
    table = PlanTable()
    qtbot.addWidget(table)
    assert table.totals_text() == "No plan"
    assert top_rows(table) == 0
    assert table.plan() is None
    assert table.current_group_id() is None
    assert table.sizes() == []


def test_duplicate_plan_groups_under_a_keeps_row(qtbot: QtBot, tmp_path: Path) -> None:
    plan, keeper, _b, _c = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates", [100, 2000])
    assert top_rows(table) == 1
    assert child_rows(table, 0) == 2
    assert cell(table, 0, 0) == f"Keeps {keeper}"
    assert header_labels(table) == ["What happens", "File", "Goes to", "Kept copy", "Why", "Size"]
    group = table.view.model().index(0, 0)
    assert cell(table, 0, 3, group) == str(keeper)
    assert cell(table, 0, 0, group) == "Move"
    assert table.sizes() == [100, 2000]
    assert table.plan() is plan
    assert str(keeper) in plan.actions[0].group_id  # type: ignore[operator]


def test_totals_and_checks_update_together(qtbot: QtBot, tmp_path: Path) -> None:
    plan, *_ = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates", [1024, 2048])
    assert table.totals_text() == "2 actions, 2 selected, 3.0 KB selected"
    changes: list[int] = []
    table.selectionChanged.connect(lambda: changes.append(1))
    table.set_action_checked(0, False)
    assert plan.actions[0].selected is False
    assert plan.actions[1].selected is True
    assert table.totals_text() == "2 actions, 1 selected, 2.0 KB selected"
    assert table.totals_label.text() == table.status_text() == "2 found · 1 ticked (2.0 KB)"
    assert changes == [1]
    table.set_action_checked(1, False)
    assert table.totals_text() == "2 actions, 0 selected, 0 B selected"
    table.set_action_checked(0, True)
    assert plan.selected_actions() == [plan.actions[0]]


def test_totals_omit_sizes_when_none_are_known(qtbot: QtBot, tmp_path: Path) -> None:
    plan, *_ = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    assert table.totals_text() == "2 actions, 2 selected"


def test_group_check_state_follows_its_children(qtbot: QtBot, tmp_path: Path) -> None:
    plan, *_ = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    model = table.view.model()
    group = model.index(0, 0)
    assert model.data(group, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    table.set_action_checked(0, False)
    assert model.data(group, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.PartiallyChecked
    table.set_action_checked(1, False)
    assert model.data(group, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked


def test_clicking_a_group_check_box_sets_all_children(qtbot: QtBot, tmp_path: Path) -> None:
    plan, *_ = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    model = table.view.model()
    assert model.setData(model.index(0, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    assert [a.selected for a in plan.actions] == [False, False]
    assert model.setData(model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert [a.selected for a in plan.actions] == [True, True]


def test_plain_plan_is_a_flat_list(qtbot: QtBot, tmp_path: Path) -> None:
    plan = Plan(
        tool="write",
        roots=[tmp_path],
        actions=[
            PlannedAction(ActionKind.WRITE, None, tmp_path / "out.txt", "export"),
            PlannedAction(ActionKind.TRASH, tmp_path / "x", None, "junk"),
        ],
    )
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "project_export")
    assert top_rows(table) == 2
    assert child_rows(table, 0) == 0
    # An absent source or destination shows a dash.
    assert EM_DASH in (cell(table, 0, 1), cell(table, 0, 2))
    assert cell(table, 0, 0) != ""


def test_filter_by_text_and_kind(qtbot: QtBot, tmp_path: Path) -> None:
    plan = Plan(
        tool="mixed",
        roots=[tmp_path],
        actions=[
            PlannedAction(
                ActionKind.MOVE, tmp_path / "apple.jpg", tmp_path / "d" / "apple.jpg", "x"
            ),
            PlannedAction(ActionKind.TRASH, tmp_path / "pear.jpg", None, "junk file"),
            PlannedAction(ActionKind.TRASH, tmp_path / "plum.jpg", None, "empty folder"),
        ],
    )
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "mixed")
    assert top_rows(table) == 3
    assert [table.kind_combo.itemText(i) for i in range(table.kind_combo.count())] == [
        ALL_KINDS,
        "Move",
        "Move to Recycle Bin",
    ]
    table.filter_edit.setText("PEAR")  # case-insensitive, on the path
    assert top_rows(table) == 1
    table.filter_edit.setText("empty folder")  # on the reason
    assert top_rows(table) == 1
    table.filter_edit.setText("")
    assert top_rows(table) == 3
    table.kind_combo.setCurrentText("Move to Recycle Bin")
    assert top_rows(table) == 2
    table.filter_edit.setText("plum")
    assert top_rows(table) == 1
    table.filter_edit.setText("nothing matches this")
    assert top_rows(table) == 0
    table.kind_combo.setCurrentIndex(0)
    table.filter_edit.setText("")
    assert top_rows(table) == 3
    # Filtering only hides rows: the plan and the totals do not change.
    assert table.totals_text() == "3 actions, 3 selected"


def test_filter_keeps_a_group_with_a_matching_child(qtbot: QtBot, tmp_path: Path) -> None:
    plan, *_ = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    table.filter_edit.setText("c.jpg")
    assert top_rows(table) == 1
    assert child_rows(table, 0) == 1
    table.filter_edit.setText("zzz")
    assert top_rows(table) == 0
    # The group row also matches by the keeper's name.
    table.filter_edit.setText("keeps a.jpg")
    assert top_rows(table) == 1


def test_rename_plans_get_folder_old_and_new_name_columns(qtbot: QtBot, tmp_path: Path) -> None:
    sub = tmp_path / "pics"
    plan = Plan(
        tool="rename",
        roots=[tmp_path],
        actions=[
            PlannedAction(ActionKind.RENAME, sub / "IMG_1.jpg", sub / "2024-01-01-001.jpg", "date"),
            PlannedAction(ActionKind.RENAME, sub / "IMG_2.jpg", sub / "2024-01-01-002.jpg", "date"),
        ],
    )
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "rename", [10, 20])
    assert header_labels(table) == ["What happens", "Folder", "Old name", "New name", "Why", "Size"]
    assert cell(table, 0, 1) == str(sub)
    assert cell(table, 0, 2) == "IMG_1.jpg"
    assert cell(table, 0, 3) == "2024-01-01-001.jpg"
    assert cell(table, 1, 2) == "IMG_2.jpg"


def test_a_plan_with_a_move_is_not_a_rename_layout(qtbot: QtBot, tmp_path: Path) -> None:
    plan = Plan(
        tool="rename",
        roots=[tmp_path],
        actions=[
            PlannedAction(ActionKind.RENAME, tmp_path / "a", tmp_path / "b"),
            PlannedAction(ActionKind.MOVE, tmp_path / "c", tmp_path / "d" / "c"),
        ],
    )
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "rename")
    assert header_labels(table)[:3] == ["What happens", "File", "Goes to"]


def empty_folders_plan(tmp_path: Path) -> Plan:
    (tmp_path / "a" / "b").mkdir(parents=True, exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    (tmp_path / "c" / "Thumbs.db").write_text("junk")
    ctx = RunContext(
        settings=Settings(onedrive_root=tmp_path),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=None,
    )
    return EMPTY_FOLDERS.scan(EmptyFoldersOptions(roots=[tmp_path]), ctx)


def test_empty_folders_unchecking_a_nested_folder_unchecks_its_parent(
    qtbot: QtBot, tmp_path: Path
) -> None:
    plan = empty_folders_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "empty_folders")
    by_name = {a.src.name: i for i, a in enumerate(plan.actions) if a.src is not None}
    assert set(by_name) == {"a", "b", "c", "Thumbs.db"}
    table.set_action_checked(by_name["b"], False)
    state = {a.src.name: a.selected for a in plan.actions if a.src is not None}
    assert state["b"] is False
    assert state["a"] is False  # its parent would no longer be empty
    assert state["c"] is True
    assert state["Thumbs.db"] is True
    assert table.totals_text() == "4 actions, 2 selected"
    # Without the hook (another tool id) the same click only changes one action.
    plain = empty_folders_plan(tmp_path)
    other = PlanTable()
    qtbot.addWidget(other)
    other.set_plan(plain, "something_else")
    index = next(i for i, a in enumerate(plain.actions) if a.src is not None and a.src.name == "b")
    other.set_action_checked(index, False)
    assert sum(1 for a in plain.actions if not a.selected) == 1


def test_empty_folders_groups_show_the_folder_path(qtbot: QtBot, tmp_path: Path) -> None:
    plan = empty_folders_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "empty_folders")
    assert top_rows(table) == 2
    labels = {cell(table, 0, 0), cell(table, 1, 0)}
    assert labels == {str(tmp_path / "a"), str(tmp_path / "c")}


def test_refresh_keeps_the_filter_and_replaces_sizes(qtbot: QtBot, tmp_path: Path) -> None:
    plan, *_ = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates", [1, 2])
    table.filter_edit.setText("b.jpg")
    assert child_rows(table, 0) == 1
    plan.actions[0].selected = False
    table.refresh([1000, 2000])
    assert table.sizes() == [1000, 2000]
    assert child_rows(table, 0) == 1  # the filter survived
    table.filter_edit.setText("")
    model = table.view.model()
    assert (
        model.data(model.index(0, 0, model.index(0, 0)), Qt.ItemDataRole.CheckStateRole)
        == Qt.CheckState.Unchecked
    )


def test_current_group_id_follows_the_selected_row(qtbot: QtBot, tmp_path: Path) -> None:
    plan, keeper, *_ = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    seen: list[object] = []
    table.currentActionChanged.connect(seen.append)
    model = table.view.model()
    table.view.setCurrentIndex(model.index(0, 0))
    assert table.current_group_id() == str(keeper)
    assert seen[-1] is None  # a group row has no action
    table.view.expandAll()
    table.view.setCurrentIndex(model.index(1, 0, model.index(0, 0)))
    assert table.current_group_id() == str(keeper)
    assert seen[-1] is plan.actions[1]


def test_set_plan_none_clears_the_table(qtbot: QtBot, tmp_path: Path) -> None:
    plan, *_ = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    table.set_plan(None)
    assert top_rows(table) == 0
    assert table.totals_text() == "No plan"


def test_filtering_raises_no_deprecation_warning(qtbot: QtBot, tmp_path: Path) -> None:
    plan, *_ = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        table.set_plan(plan, "duplicates")
        table.filter_edit.setText("a")


def test_duplicates_show_the_new_keeper_after_a_swap(qtbot: QtBot, tmp_path: Path) -> None:
    plan, keeper, b, _c = dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    plan_edit.change_keeper(plan, str(keeper), b)
    table.refresh()
    model = table.view.model()
    assert cell(table, 0, 0) == f"Keeps {b}"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole) == str(b)
    assert cell(table, 0, 3, model.index(0, 0)) == str(b)


def test_full_controls_switch_hides_the_extra_filters(qtbot: QtBot, tmp_path: Path) -> None:
    table = PlanTable()
    qtbot.addWidget(table)
    extra = QPushButton("Select all")
    table.add_toolbar_widget(extra, full_only=True)
    table.show()
    table.set_full_controls(False)
    assert not table.kind_combo.isVisibleTo(table)
    assert not table.view_combo.isVisibleTo(table)
    assert not extra.isVisibleTo(table)
    assert table.filter_edit.isVisibleTo(table)
    table.set_full_controls(True)
    assert table.kind_combo.isVisibleTo(table)
    assert table.view_combo.isVisibleTo(table)
    assert extra.isVisibleTo(table)


def test_switching_the_full_controls_off_clears_the_kind_filter(
    qtbot: QtBot, tmp_path: Path
) -> None:
    plan = Plan(
        tool="mixed",
        roots=[tmp_path],
        actions=[
            PlannedAction(ActionKind.MOVE, tmp_path / "a", tmp_path / "d" / "a", "x"),
            PlannedAction(ActionKind.TRASH, tmp_path / "b", None, "junk"),
        ],
    )
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "mixed")
    table.kind_combo.setCurrentText("Move")
    assert top_rows(table) == 1
    table.set_full_controls(False)
    # A filter nobody can see must not hide rows that Apply would still act on.
    assert top_rows(table) == 2
    assert not table.filter_active()


def _big_empty_folders_plan(root: Path, count: int) -> Plan:
    """``count`` nested folders (each with a child and a junk file) as an empty-folders plan."""
    from various_tools.tools.empty_folders.tool import JUNK_REASON

    actions: list[PlannedAction] = []
    for i in range(count // 2):
        top = root / f"g{i}"
        child = top / "c"
        actions.append(PlannedAction(ActionKind.TRASH, top, None, "empty folder"))
        actions.append(PlannedAction(ActionKind.TRASH, child, None, "empty folder"))
        actions.append(
            PlannedAction(ActionKind.TRASH, child / "Thumbs.db", None, f"{JUNK_REASON}: Thumbs.db")
        )
    return Plan(tool="empty_folders", roots=[root], actions=actions)


def test_empty_folders_batch_form_equals_the_per_row_form(tmp_path: Path) -> None:
    from various_tools.tools.empty_folders import set_folder_selected, set_folders_selected

    one = _big_empty_folders_plan(tmp_path, 60)
    two = _big_empty_folders_plan(tmp_path, 60)
    picks = [i for i in range(0, len(one.actions), 4)]
    for value in (True, False, True):
        for i in picks:
            src = one.actions[i].src
            assert src is not None
            set_folder_selected(one, src, value)
        set_folders_selected(
            two, [a.src for i, a in enumerate(two.actions) if i in picks and a.src], value
        )
        assert [a.selected for a in one.actions] == [a.selected for a in two.actions]
    # Unticking a child unticks its parent and the junk file inside, in both forms.
    child = tmp_path / "g1" / "c"
    set_folder_selected(one, child, False)
    set_folders_selected(two, [child], False)
    assert [a.selected for a in one.actions] == [a.selected for a in two.actions]
    assert not any(a.selected for a in two.actions if a.src and "g1" in a.src.parts)


def test_bulk_ticking_thousands_of_empty_folders_is_quick(qtbot: QtBot, tmp_path: Path) -> None:
    import time

    plan = _big_empty_folders_plan(tmp_path, 5000)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "empty_folders")
    start = time.monotonic()
    table.set_shown_checked(False)
    assert not any(a.selected for a in plan.actions)
    table.set_shown_checked(True)
    assert all(a.selected for a in plan.actions)
    table.tick_only_first(10)
    assert sum(1 for a in plan.actions if a.selected) >= 10
    assert time.monotonic() - start < 20  # per-row passes took minutes; one pass takes a second
