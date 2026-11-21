"""Results list: tick first N, the Show filter, Ctrl+A, Space, the menu, the status line."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.gui.widgets.plan_table import PlanTable

ROWS = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows


def _flat_plan(tmp_path: Path, count: int, *, selected: bool = False) -> Plan:
    actions = [
        PlannedAction(
            ActionKind.TRASH, tmp_path / f"f{i:05}.txt", None, f"why{i}", selected=selected
        )
        for i in range(count)
    ]
    return Plan(tool="t", roots=[tmp_path], actions=actions)


def _table(
    qtbot: QtBot,
    plan: Plan,
    tool_id: str = "t",
    sizes: list[int | None] | None = None,
) -> PlanTable:
    table = PlanTable()
    qtbot.addWidget(table)
    table.resize(900, 500)
    table.set_plan(plan, tool_id, sizes)
    table.show()
    return table


def _ticked(plan: Plan) -> list[int]:
    return [i for i, a in enumerate(plan.actions) if a.selected]


def _menu_labels(menu: QMenu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def _entry(menu: QMenu, text: str):  # type: ignore[no-untyped-def]
    return next(a for a in menu.actions() if a.text() == text)


def _row_point(table: PlanTable, row: int) -> QPoint:
    return table.view.visualRect(table.view.model().index(row, 0)).center()


def test_tick_only_first_n_follows_the_list_order(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 10, selected=True)
    table = _table(qtbot, plan)
    assert table.tick_only_first(3) == 3
    assert _ticked(plan) == [0, 1, 2]
    table.view.sortByColumn(1, Qt.SortOrder.DescendingOrder)  # File column, reversed
    table.tick_only_first(2)
    assert _ticked(plan) == [8, 9]


def test_tick_only_first_uses_the_spin_box_and_button(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 10)
    table = _table(qtbot, plan)
    assert table.tick_first_spin.value() == 100
    table.tick_first_spin.setValue(4)
    table.tick_first_button.click()
    assert _ticked(plan) == [0, 1, 2, 3]
    assert table.status_text() == "10 found · 4 ticked"


def test_tick_only_first_touches_only_shown_rows(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 10, selected=True)
    table = _table(qtbot, plan)
    table.filter_edit.setText("why1")
    assert table.shown_action_indices() == [1]
    table.tick_only_first(1)
    assert _ticked(plan) == list(range(10))  # hidden rows keep their state
    table.tick_only_first(0)
    assert 1 not in _ticked(plan) and 0 in _ticked(plan)


def test_show_filter_ticked_and_not_ticked(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 6)
    for i in (1, 3):
        plan.actions[i].selected = True
    table = _table(qtbot, plan)
    events: list[bool] = []
    table.filterChanged.connect(events.append)
    table.show_combo.setCurrentIndex(table.show_combo.findData("ticked"))
    assert table.shown_action_indices() == [1, 3]
    assert table.filter_active() and events[-1] is True
    assert "2 shown" in table.totals_label.text()
    table.show_combo.setCurrentIndex(table.show_combo.findData("unticked"))
    assert table.shown_action_indices() == [0, 2, 4, 5]
    # Unticking a row in the Ticked view makes it leave the view.
    table.show_combo.setCurrentIndex(table.show_combo.findData("ticked"))
    table.set_action_checked(1, False)
    assert table.shown_action_indices() == [1, 3]  # a single tick does not re-filter the list
    table.refresh_button.click()
    assert table.shown_action_indices() == [3]
    table.show_combo.setCurrentIndex(0)
    assert not table.filter_active()
    assert len(table.shown_action_indices()) == 6


def test_status_line_has_counts_and_size(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 3)
    plan.actions[0].selected = True
    table = _table(qtbot, plan, sizes=[1024, 2048, None])
    assert table.totals_label.text() == "3 found · 1 ticked (1.0 KB)"
    table = _table(qtbot, _flat_plan(tmp_path, 2500))
    assert table.totals_label.text() == "2,500 found · 0 ticked"


def test_ctrl_a_highlights_every_shown_row(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 8)
    table = _table(qtbot, plan)
    table.filter_edit.setText("why5")
    table.view.setFocus()
    QTest.keyClick(table.view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert table.highlighted_action_indices() == [5]
    table.filter_edit.clear()
    QTest.keyClick(table.view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert sorted(table.highlighted_action_indices()) == list(range(8))


def test_ctrl_a_reaches_rows_inside_folded_groups(qtbot: QtBot, tmp_path: Path) -> None:
    keeper = tmp_path / "k.jpg"
    actions = [
        PlannedAction(ActionKind.TRASH, tmp_path / f"d{i}.jpg", None, "dup", str(keeper))
        for i in range(4)
    ]
    plan = Plan(tool="duplicates", roots=[tmp_path], actions=actions)
    table = _table(qtbot, plan, "duplicates")
    table.view.collapseAll()
    table.highlight_all_shown()
    assert sorted(table.highlighted_action_indices()) == [0, 1, 2, 3]


def test_space_toggles_to_the_opposite_of_the_first_row(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 5)
    plan.actions[0].selected = True  # the first highlighted row is ticked
    table = _table(qtbot, plan)
    table.view.setFocus()
    QTest.keyClick(table.view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(table.view, Qt.Key.Key_Space)
    assert _ticked(plan) == []  # all to the opposite of row 0 (ticked, so unticked)
    QTest.keyClick(table.view, Qt.Key.Key_Space)
    assert _ticked(plan) == [0, 1, 2, 3, 4]


def test_space_does_nothing_while_not_editable(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 3)
    table = _table(qtbot, plan)
    table.view.setFocus()
    table.highlight_all_shown()
    table.set_editable(False)
    QTest.keyClick(table.view, Qt.Key.Key_Space)
    assert _ticked(plan) == []


def test_ctrl_f_focuses_the_filter(qtbot: QtBot, tmp_path: Path) -> None:
    table = _table(qtbot, _flat_plan(tmp_path, 2))
    table.view.setFocus()
    table.find_shortcut.activated.emit()
    assert table.filter_edit.hasFocus() or table.window().focusWidget() is table.filter_edit


def test_context_menu_acts_on_the_highlighted_rows(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 6)
    table = _table(qtbot, plan)
    model = table.view.model()
    sel = table.view.selectionModel()
    sel.select(model.index(1, 0), ROWS)
    sel.select(model.index(2, 0), ROWS)
    menu = table.context_menu_at(_row_point(table, 1))
    assert menu is not None
    assert _menu_labels(menu) == [
        "Tick",
        "Untick",
        "Tick only these",
        "Open",
        "Show in File Explorer",
        "Properties",
        "Copy paths",
    ]
    assert not _entry(menu, "Properties").isEnabled()  # several rows
    _entry(menu, "Tick").trigger()
    assert _ticked(plan) == [1, 2]
    _entry(menu, "Untick").trigger()
    assert _ticked(plan) == []
    table.tick_highlighted(True)
    sel.clearSelection()
    sel.select(model.index(4, 0), ROWS)
    menu = table.context_menu_at(_row_point(table, 4))
    assert menu is not None
    _entry(menu, "Tick only these").trigger()
    assert _ticked(plan) == [4]
    sel.clearSelection()
    sel.select(model.index(0, 0), ROWS)
    sel.select(model.index(5, 0), ROWS)
    menu = table.context_menu_at(_row_point(table, 0))
    assert menu is not None
    _entry(menu, "Copy paths").trigger()
    text = QGuiApplication.clipboard().text()
    assert text == "\n".join(str(tmp_path / f"f{i:05}.txt") for i in (0, 5))


def test_right_click_outside_the_highlight_selects_that_row(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 4)
    table = _table(qtbot, plan)
    model = table.view.model()
    table.view.selectionModel().select(model.index(0, 0), ROWS)
    menu = table.context_menu_at(_row_point(table, 2))
    assert menu is not None
    assert table.highlighted_action_indices() == [2]


def test_context_menu_single_row_open_show_properties_copy(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 3)
    target = tmp_path / "f00001.txt"
    target.write_text("x")
    table = _table(qtbot, plan)
    shown: list[Path] = []
    props: list[Path] = []
    opened: list[Path] = []
    table.reveal = lambda p: shown.append(p) or True  # type: ignore[method-assign]
    table.show_properties = lambda p: props.append(p) or True  # type: ignore[method-assign]
    table.open_file = lambda p: opened.append(p) or True  # type: ignore[method-assign]
    menu = table.context_menu_at(_row_point(table, 1))
    assert menu is not None
    assert _menu_labels(menu) == [
        "Tick",
        "Untick",
        "Tick only these",
        "Show in File Explorer",
        "Open",
        "Properties",
        "Copy path",
    ]
    _entry(menu, "Show in File Explorer").trigger()
    _entry(menu, "Properties").trigger()
    _entry(menu, "Open").trigger()
    assert shown == props == opened == [target]
    _entry(menu, "Copy path").trigger()
    assert QGuiApplication.clipboard().text() == str(target)


def test_selection_row_is_always_visible(qtbot: QtBot, tmp_path: Path) -> None:
    table = _table(qtbot, _flat_plan(tmp_path, 2))
    table.set_full_controls(False)
    for widget in (
        table.tick_all_button,
        table.untick_all_button,
        table.show_combo,
        table.tick_first_button,
        table.tick_first_spin,
    ):
        assert widget.isVisibleTo(table)


def test_ticking_100000_rows_is_fast(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 100_000)
    table = _table(qtbot, plan)
    start = time.perf_counter()
    table.set_shown_checked(True)
    table.tick_only_first(500)
    table.highlight_all_shown()
    elapsed = time.perf_counter() - start
    assert len(_ticked(plan)) == 500
    assert elapsed < 10, elapsed


@pytest.mark.parametrize("count", [0, 1])
def test_tick_only_first_edge_counts(qtbot: QtBot, tmp_path: Path, count: int) -> None:
    plan = _flat_plan(tmp_path, 3, selected=True)
    table = _table(qtbot, plan)
    assert table.tick_only_first(count) == count
    assert len(_ticked(plan)) == count


def _hint_visible(table: PlanTable) -> bool:
    return table.out_of_date_label.isVisibleTo(table) and table.refresh_button.isVisibleTo(table)


def test_a_single_tick_in_a_ticked_view_keeps_the_row_and_says_the_list_is_out_of_date(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _flat_plan(tmp_path, 6)
    for i in (1, 3):
        plan.actions[i].selected = True
    table = _table(qtbot, plan)
    table.show_combo.setCurrentIndex(table.show_combo.findData("ticked"))
    assert not _hint_visible(table)
    refilters: list[int] = []
    real = table._proxy.set_filters  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(
        table._proxy,  # pyright: ignore[reportPrivateUsage]
        "set_filters",
        lambda *a, **k: (refilters.append(1), real(*a, **k))[1],
    )
    # The user's click on the check box of row 1 (through the model, as the view does).
    model = table.view.model()
    index = model.index(0, 0)
    assert table.shown_action_indices() == [1, 3]
    model.setData(index, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    assert plan.actions[1].selected is False
    assert refilters == []  # not re-filtered
    assert table.shown_action_indices() == [1, 3]  # the row stays
    assert _hint_visible(table)
    assert table.status_text() == "6 found · 1 ticked"  # the counts are always current
    table.refresh_button.click()
    assert refilters == [1]
    assert table.shown_action_indices() == [3]
    assert not _hint_visible(table)


def test_changing_the_filter_clears_the_out_of_date_hint(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 4)
    plan.actions[0].selected = True
    table = _table(qtbot, plan)
    table.show_combo.setCurrentIndex(table.show_combo.findData("ticked"))
    table.set_action_checked(0, False)
    assert _hint_visible(table)
    table.show_combo.setCurrentIndex(table.show_combo.findData("unticked"))
    assert not _hint_visible(table)
    assert table.shown_action_indices() == [0, 1, 2, 3]


def test_bulk_operations_still_re_filter_at_once(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _flat_plan(tmp_path, 6)
    for i in (1, 3):
        plan.actions[i].selected = True
    table = _table(qtbot, plan)
    table.show_combo.setCurrentIndex(table.show_combo.findData("ticked"))
    table.set_action_checked(1, False)  # a single tick: stale
    assert _hint_visible(table)
    table.set_shown_checked(False)  # Untick all shown: re-filters, clears the hint
    assert table.shown_action_indices() == []
    assert not _hint_visible(table)
    table.show_combo.setCurrentIndex(table.show_combo.findData("unticked"))
    assert len(table.shown_action_indices()) == 6
    assert table.tick_only_first(2) == 2
    assert table.shown_action_indices() == [2, 3, 4, 5]
    assert not _hint_visible(table)


def test_no_hint_without_a_ticked_filter(qtbot: QtBot, tmp_path: Path) -> None:
    table = _table(qtbot, _flat_plan(tmp_path, 3))
    table.set_action_checked(0, True)
    assert not _hint_visible(table)
    assert table.shown_action_indices() == [0, 1, 2]
