"""Safety review fixes on the GUI side: filtered ticking, download wording, folder downloads.

* With a filter active, ticking a group or folder row changes only the rows the filter shows, and
  "Select all"/"Select none" change only the shown rows (and say so). The parent's tri-state
  still reflects every row below it.
* The Apply confirmation says plainly how many cloud-only files the ticked rows download.
* "Download these N files" never downloads a folder.

Plans are built in the test; files live under ``tmp_path``; ``force_download`` is a fake.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import Field
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from various_tools.core import onedrive as onedrive_module
from various_tools.core.context import RunContext
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage, apply_summary, download_actions
from various_tools.gui.widgets.plan_table import FOLDER_TREE, PlanTable
from various_tools.tools.base import Category, ToolOptions

if TYPE_CHECKING:
    from conftest import Dialogs


def _dup_plan(tmp_path: Path) -> Plan:
    gid = str(tmp_path / "a.jpg")
    dup = tmp_path / "duplicate"
    return Plan(
        tool="duplicates",
        roots=[tmp_path],
        actions=[
            PlannedAction(ActionKind.MOVE, tmp_path / "b.jpg", dup / "b.jpg", "exact", gid),
            PlannedAction(ActionKind.MOVE, tmp_path / "c.jpg", dup / "c.jpg", "visual", gid),
            PlannedAction(ActionKind.MOVE, tmp_path / "d.jpg", dup / "d.jpg", "visual", gid),
        ],
    )


def _check(table: PlanTable, index: object, state: Qt.CheckState) -> bool:
    model = table.view.model()
    return bool(model.setData(index, state, Qt.ItemDataRole.CheckStateRole))  # type: ignore[arg-type]


def _state(table: PlanTable, index: object) -> object:
    return table.view.model().data(index, Qt.ItemDataRole.CheckStateRole)  # type: ignore[arg-type]


# ----- item 2: ticking a parent with a filter active -------------------------------------------


def test_unticking_a_group_with_a_filter_changes_only_the_shown_rows(
    qtbot: QtBot, tmp_path: Path
) -> None:
    plan = _dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    table.filter_edit.setText("c.jpg")
    group = table.view.model().index(0, 0)
    assert _check(table, group, Qt.CheckState.Unchecked)
    assert [a.selected for a in plan.actions] == [True, False, True]
    # The group's tri-state covers every row below it, shown or not.
    assert _state(table, group) == Qt.CheckState.PartiallyChecked
    table.filter_edit.setText("")
    assert _state(table, table.view.model().index(0, 0)) == Qt.CheckState.PartiallyChecked


def test_ticking_a_group_with_a_filter_ticks_only_the_shown_rows(
    qtbot: QtBot, tmp_path: Path
) -> None:
    plan = _dup_plan(tmp_path)
    for action in plan.actions:
        action.selected = False
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    table.filter_edit.setText("visual")
    assert _check(table, table.view.model().index(0, 0), Qt.CheckState.Checked)
    assert [a.selected for a in plan.actions] == [False, True, True]
    table.kind_combo.setCurrentText("Move")
    table.filter_edit.setText("")
    # Without a text filter but with a kind filter, every move row is shown: all change.
    assert _check(table, table.view.model().index(0, 0), Qt.CheckState.Unchecked)
    assert [a.selected for a in plan.actions] == [False, False, False]


def test_without_a_filter_a_group_click_still_sets_every_row(qtbot: QtBot, tmp_path: Path) -> None:
    plan = _dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    assert not table.filter_active()
    assert _check(table, table.view.model().index(0, 0), Qt.CheckState.Unchecked)
    assert not any(a.selected for a in plan.actions)


def test_folder_tree_tick_with_a_filter_skips_hidden_files(qtbot: QtBot, tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    plan = Plan(
        tool="mixed",
        roots=[tmp_path],
        actions=[
            PlannedAction(ActionKind.TRASH, sub / "keep-me.txt", None, "old", selected=False),
            PlannedAction(ActionKind.TRASH, sub / "junk.tmp", None, "old", selected=False),
        ],
    )
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "mixed")
    table.set_view_mode(FOLDER_TREE)
    table.filter_edit.setText("junk")
    model = table.view.model()
    root_row = model.index(0, 0)
    assert _check(table, root_row, Qt.CheckState.Checked)
    assert [a.selected for a in plan.actions] == [False, True]


def test_the_empty_folders_hook_is_used_for_each_shown_row(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.gui.widgets import plan_table as plan_table_module

    calls: list[tuple[str, bool]] = []

    def hook(plan: Plan, action: PlannedAction, checked: bool) -> None:
        calls.append((action.src.name if action.src else "", checked))
        action.selected = checked

    monkeypatch.setitem(plan_table_module.SELECTION_HOOKS, "duplicates", hook)
    plan = _dup_plan(tmp_path)
    table = PlanTable()
    qtbot.addWidget(table)
    table.set_plan(plan, "duplicates")
    table.filter_edit.setText("visual")
    _check(table, table.view.model().index(0, 0), Qt.CheckState.Unchecked)
    assert calls == [("c.jpg", False), ("d.jpg", False)]
    calls.clear()
    assert table.set_shown_checked(True) == 2
    assert calls == [("c.jpg", True), ("d.jpg", True)]


# ----- item 2: Select all / Select none on the tool page --------------------------------------


class FakeOptions(ToolOptions):
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders",
        description="x",
        json_schema_extra={"widget": "folders"},
    )


@dataclass
class FakeTool:
    id: str = "fake_review"
    name: str = "Fake"
    description: str = "A fake tool."
    category: Category = "Clean up"
    Options: type[ToolOptions] = FakeOptions
    cloud_only: list[Path] = field(default_factory=list[Path])
    downloads: list[Path] = field(default_factory=list[Path])

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        assert isinstance(opts, FakeOptions)
        plan = Plan(tool=self.id, roots=list(opts.roots), cloud_only=list(self.cloud_only))
        for root in opts.roots:
            for name in ("kiwi.txt", "pear.txt", "peach.txt"):
                plan.actions.append(
                    PlannedAction(ActionKind.TRASH, root / name, reason="fake", selected=False)
                )
            for path in self.downloads:
                plan.actions.append(
                    PlannedAction(
                        ActionKind.CONVERT,
                        path,
                        None,
                        "cloud-only: will be downloaded from OneDrive first",
                        selected=True,
                    )
                )
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        return apply_plan(plan, ctx)


def _page(qtbot: QtBot, state: AppState, dialogs: Dialogs, tool: FakeTool, root: Path) -> ToolPage:
    page = ToolPage(state, tool)  # type: ignore[arg-type]
    qtbot.addWidget(page)
    dialogs.attach(page)
    page.form.set_values({"roots": [str(root)]})
    return page


def test_select_all_with_a_filter_ticks_only_the_shown_rows_and_says_so(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    tmp_path: Path,
    scan: Callable[[ToolPage], None],
) -> None:
    page = _page(qtbot, gui_state, dialogs, FakeTool(), tmp_path)
    scan(page)
    assert page.plan is not None
    assert page.select_all_button.text() == "Select all"
    page.table.filter_edit.setText("pea")  # pear and peach
    assert page.select_all_button.text() == "Select all shown"
    assert page.select_none_button.text() == "Select none shown"
    assert "Hidden rows are not changed" in page.select_all_button.toolTip()
    assert page.select_all(True) == 2
    assert [a.selected for a in page.plan.actions] == [False, True, True]
    page.table.filter_edit.setText("")
    assert page.select_all_button.text() == "Select all"
    assert page.select_all(True) == 1
    page.table.filter_edit.setText("peach")
    assert page.select_all(False) == 1
    assert [a.selected for a in page.plan.actions] == [True, True, False]


# ----- item 6a: download wording ----------------------------------------------------------------


def test_apply_summary_says_how_many_cloud_only_files_will_be_downloaded(tmp_path: Path) -> None:
    plan = Plan(
        tool="organize_by_date",
        roots=[tmp_path],
        actions=[
            PlannedAction(ActionKind.CONVERT, tmp_path / "a.heic", None, "cloud-only: download"),
            PlannedAction(ActionKind.CONVERT, tmp_path / "b.heic", None, "cloud-only: download"),
            PlannedAction(ActionKind.CONVERT, tmp_path / "c.jpg", tmp_path / "c.jpg", "resize"),
            PlannedAction(
                ActionKind.CONVERT, tmp_path / "d.heic", None, "cloud-only", selected=False
            ),
            PlannedAction(ActionKind.MOVE, tmp_path / "e.jpg", tmp_path / "2024" / "e.jpg", "m"),
        ],
    )
    assert [a.src.name for a in download_actions(plan) if a.src] == ["a.heic", "b.heic"]
    text = apply_summary(plan, permanent_delete=False)
    assert "download 2 cloud-only file(s) from OneDrive" in text
    assert "convert: 1" in text  # the resize, counted on its own
    assert "convert: 3" not in text


def test_apply_summary_without_downloads_does_not_mention_them(tmp_path: Path) -> None:
    plan = Plan(
        tool="t",
        roots=[tmp_path],
        actions=[PlannedAction(ActionKind.TRASH, tmp_path / "x", None, "junk")],
    )
    assert "download" not in apply_summary(plan, permanent_delete=False).casefold()


# ----- item 6b: never download a folder ----------------------------------------------------------


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def fake(path: Path, wait_seconds: int | None = None) -> tuple[bool, str]:
        calls.append(path)
        return True, "downloaded"

    monkeypatch.setattr(onedrive_module, "force_download", fake)
    return calls


def test_download_banner_never_downloads_a_folder(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    downloads: list[Path],
    tmp_path: Path,
    scan: Callable[[ToolPage], None],
    wait_idle: Callable[[ToolPage], None],
) -> None:
    folder = tmp_path / "cloud folder"
    folder.mkdir()
    tool = FakeTool(cloud_only=[tmp_path / "a.jpg", folder])
    page = _page(qtbot, gui_state, dialogs, tool, tmp_path)
    scan(page)
    dialogs.answers = [True, False]
    assert page.download_cloud_only() is not None
    wait_idle(page)
    assert downloads == [tmp_path / "a.jpg"]
    question = dialogs.confirms[0][1]
    assert "1 cloud-only file(s)" in question and "folder" in question


def test_download_banner_with_only_folders_downloads_nothing(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    downloads: list[Path],
    tmp_path: Path,
    scan: Callable[[ToolPage], None],
) -> None:
    folder = tmp_path / "cloud folder"
    folder.mkdir()
    page = _page(qtbot, gui_state, dialogs, FakeTool(cloud_only=[folder]), tmp_path)
    scan(page)
    assert page.download_cloud_only() is None
    assert downloads == [] and dialogs.confirms == []
    assert "folder" in page.status_label.text()
