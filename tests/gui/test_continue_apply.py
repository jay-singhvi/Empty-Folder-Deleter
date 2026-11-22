"""Continue a stopped apply, and change the pace while it runs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from pytestqt.qtbot import QtBot

from various_tools.core import plan as plan_module
from various_tools.core.context import Pace
from various_tools.core.journal import Journal
from various_tools.core.plan import ActionKind, ActionOutcome, Plan, PlannedAction, RunResult
from various_tools.gui.apply_dialog import ApplyAsk, ApplyDialog, pace_to_values
from various_tools.gui.continue_run import can_continue, drop_done
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.wording import continue_label


@pytest.fixture
def folders(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    for name in "abcd":
        (root / name).mkdir(parents=True)
    return root


def _stop_after_first(qtbot: QtBot, page: ToolPage, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply in batches of one with tiny real sleeps, and press Stop during the first wait."""
    monkeypatch.setattr(plan_module, "_sleep", lambda _s: time.sleep(0.01))
    page.choose_pace = lambda _ask: (True, Pace(1, 60.0))
    page.window().show()
    assert page.apply() is not None
    qtbot.waitUntil(lambda: page.skip_wait_button.isEnabled(), timeout=10000)
    page.cancel_button.click()
    qtbot.waitUntil(lambda: page.worker is None, timeout=60000)


def test_stop_keeps_the_rest_and_continue_applies_only_that(
    qtbot: QtBot,
    open_tool: Any,
    folders: Path,
    scan: Any,
    wait_idle: Any,
    gui_state: AppState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    assert page.plan is not None
    total = len(page.plan.actions)
    assert total == 4
    _stop_after_first(qtbot, page, monkeypatch)

    assert page.result is not None and page.result.status == "cancelled"
    done = page.result.count("done")
    assert 1 <= done < total
    left = total - done
    assert len(page.plan.actions) == left  # the done rows are gone from the list
    assert page.apply_button.text() == continue_label(left)
    assert page.apply_button.isEnabled()
    assert "Continue" in page.status_label.text()
    first_run = page.last_run_id
    remaining = {str(a.src) for a in page.plan.actions}
    for name in "abcd":
        # What was done is gone from the disk; what is left is still there.
        assert (folders / name).exists() == (str(folders / name) in remaining)

    page.choose_pace = lambda _ask: (True, None)
    assert page.apply() is not None
    wait_idle(page)
    assert page.result.status == "done" and page.result.count("done") == left
    assert page.last_run_id != first_run
    for name in "abcd":
        assert not (folders / name).exists()
    assert page.apply_button.text() == "Apply…"  # finished for good
    with Journal(gui_state.settings.journal_path()) as journal:
        assert journal.action_count(first_run or 0) == done
        assert journal.action_count(page.last_run_id or 0) == left


def test_a_new_scan_drops_the_continue_state(
    qtbot: QtBot, open_tool: Any, folders: Path, scan: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    _stop_after_first(qtbot, page, monkeypatch)
    assert page.apply_button.text().startswith("Continue (")
    scan(page)
    assert page.plan is not None
    assert not page.apply_button.text().startswith("Continue")
    assert len(page.plan.actions) == len([n for n in "abcd" if (folders / n).exists()])


def test_changed_options_drop_the_continue_state(
    qtbot: QtBot, open_tool: Any, folders: Path, scan: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    _stop_after_first(qtbot, page, monkeypatch)
    assert page.apply_button.text().startswith("Continue (")
    page.form.changed.emit()  # an option was edited
    assert not page.apply_button.isEnabled()
    assert "Scan again" in page.status_label.text()


def _synthetic(page: ToolPage, status: str, outcomes: list[ActionOutcome]) -> None:
    page._apply_done(RunResult(run_id=1, status=status, outcomes=outcomes))  # pyright: ignore[reportPrivateUsage]


def test_partial_run_keeps_the_failed_rows_for_a_retry(
    open_tool: Any, folders: Path, scan: Any, dialogs: Any
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    assert page.plan is not None
    srcs = [str(a.src) for a in page.plan.actions]
    outcomes = [
        ActionOutcome(1, "trash", "done", srcs[0], None),
        ActionOutcome(2, "trash", "failed", srcs[1], None, "locked"),
        ActionOutcome(3, "trash", "done", srcs[2], None),
        ActionOutcome(4, "trash", "failed", srcs[3], None, "locked"),
    ]
    _synthetic(page, "partial", outcomes)
    assert [str(a.src) for a in page.plan.actions] == [srcs[1], srcs[3]]
    assert page.apply_button.text() == continue_label(2)
    assert "try the failed ones again" in page.status_label.text()


def test_a_finished_run_still_says_scan_again(
    open_tool: Any, folders: Path, scan: Any, apply: Any
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    apply(page)
    assert page.result is not None and page.result.status == "done"
    assert "Scan again" in page.status_label.text()
    assert not page.apply_button.isEnabled()


@pytest.mark.parametrize("tool_id", ["organize_by_date", "wrong_extension"])
def test_tools_whose_rows_depend_on_each_other_fall_back_to_scan_again(tool_id: str) -> None:
    assert not can_continue(tool_id, "cancelled")
    assert not can_continue(tool_id, "partial")
    assert can_continue("empty_folders", "cancelled")
    assert can_continue("duplicates", "partial")
    assert not can_continue("duplicates", "done")


def test_organize_continues_when_its_list_holds_only_moves(tmp_path: Path) -> None:
    def plan_of(*kinds: ActionKind) -> Plan:
        actions = [
            PlannedAction(k, tmp_path / f"{i}.jpg", tmp_path / "d", "x")
            for i, k in enumerate(kinds)
        ]
        return Plan(tool="organize_by_date", roots=[tmp_path], actions=actions)

    assert can_continue("organize_by_date", "cancelled", plan_of(ActionKind.MOVE, ActionKind.MOVE))
    assert not can_continue(
        "organize_by_date", "cancelled", plan_of(ActionKind.CONVERT, ActionKind.MOVE)
    )
    assert not can_continue("organize_by_date", "done", plan_of(ActionKind.MOVE))


def test_drop_done_matches_by_kind_and_source_and_keeps_sizes_in_step(tmp_path: Path) -> None:
    def row(name: str, kind: ActionKind = ActionKind.TRASH, selected: bool = True) -> PlannedAction:
        return PlannedAction(kind, tmp_path / name, None, "x", selected=selected)

    plan = Plan(
        tool="t",
        roots=[tmp_path],
        actions=[row("a"), row("b"), row("c"), row("d", selected=False), row("e")],
    )
    outcomes = [
        ActionOutcome(1, "trash", "done", str(tmp_path / "a"), None),
        ActionOutcome(2, "trash", "failed", str(tmp_path / "b"), None, "x"),
        ActionOutcome(3, "move", "done", str(tmp_path / "c"), None),  # another kind: no match
        ActionOutcome(4, "trash", "done", str(tmp_path / "d"), None),  # not ticked: kept
    ]
    sizes = drop_done(plan, outcomes, [1, 2, 3, 4, 5])
    assert [a.src.name for a in plan.actions if a.src] == ["b", "c", "d", "e"]
    assert sizes == [2, 3, 4, 5]


def test_a_write_row_is_matched_by_kind_when_the_file_got_another_name(tmp_path: Path) -> None:
    writes = [
        PlannedAction(ActionKind.WRITE, None, tmp_path / "m1.txt", "x"),
        PlannedAction(ActionKind.WRITE, None, tmp_path / "m2.txt", "x"),
    ]
    plan = Plan(tool="t", roots=[tmp_path], actions=writes)
    # The first manifest was written under a free name: the outcome has that name, not m1.txt.
    done = [ActionOutcome(1, "write", "done", None, str(tmp_path / "m1 (2).txt"))]
    drop_done(plan, done, [None, None])
    assert [a.dst.name for a in plan.actions if a.dst] == ["m2.txt"]


def test_change_pace_swaps_the_pace_of_the_running_apply(
    qtbot: QtBot,
    open_tool: Any,
    folders: Path,
    scan: Any,
    wait_idle: Any,
    gui_state: AppState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    monkeypatch.setattr(plan_module, "_sleep", lambda _s: time.sleep(0.01))
    page.choose_pace = lambda _ask: (True, Pace(1, 60.0))
    page.window().show()
    worker = page.apply()
    assert worker is not None
    qtbot.waitUntil(lambda: page.skip_wait_button.isEnabled(), timeout=10000)
    assert not page.change_pace_button.isHidden()
    asked: list[ApplyAsk] = []

    def choose(ask: ApplyAsk) -> tuple[bool, Pace | None]:
        asked.append(ask)
        return True, None

    page.choose_new_pace = choose
    assert page.change_pace() is True
    assert asked and asked[0].count == 4
    assert worker.ctx is not None and worker.ctx.pace is None
    # The wait that was running is still there to skip, even though the new pace is "all at once".
    assert page._pace is None  # pyright: ignore[reportPrivateUsage]
    page.skip_wait_button.click()
    wait_idle(page)
    assert page.result is not None and page.result.status == "done"
    assert page.result.count("done") == 4
    assert page.change_pace_button.isHidden()
    assert page.change_pace() is False  # nothing is running now


def test_declining_change_pace_keeps_the_pace(
    qtbot: QtBot,
    open_tool: Any,
    folders: Path,
    scan: Any,
    wait_idle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    monkeypatch.setattr(plan_module, "_sleep", lambda _s: time.sleep(0.01))
    first = Pace(1, 60.0)
    page.choose_pace = lambda _ask: (True, first)
    page.window().show()
    worker = page.apply()
    assert worker is not None
    qtbot.waitUntil(lambda: page.skip_wait_button.isEnabled(), timeout=10000)
    page.choose_new_pace = lambda _ask: (False, None)
    assert page.change_pace() is False
    assert worker.ctx is not None and worker.ctx.pace == first
    page.cancel_button.click()
    wait_idle(page)


def test_change_pace_dialog_shows_only_the_pace_and_the_current_choice(
    qtbot: QtBot, gui_state: AppState
) -> None:
    pace = Pace(25, 180.0)
    dialog = ApplyDialog(ApplyAsk("t", "a long summary", 100), pace_to_values(pace), pace_only=True)
    qtbot.addWidget(dialog)
    assert dialog.batch_radio.isChecked() and dialog.pace() == pace
    assert dialog.windowTitle() == "Change the pace"
    assert pace_to_values(None) == {"mode": "all"}
    none = ApplyDialog(ApplyAsk("t", "s", 3), pace_to_values(None), pace_only=True)
    qtbot.addWidget(none)
    assert none.all_radio.isChecked() and none.pace() is None


def test_the_default_change_pace_dialog_is_remembered(
    qtbot: QtBot,
    open_tool: Any,
    folders: Path,
    scan: Any,
    gui_state: AppState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)

    def fake_exec(self: ApplyDialog) -> int:
        self.preset_combo.setCurrentIndex(1)
        return ApplyDialog.DialogCode.Accepted

    monkeypatch.setattr(ApplyDialog, "exec", fake_exec)
    assert page._open_change_pace_dialog(ApplyAsk("t", "", 4)) == (  # pyright: ignore[reportPrivateUsage]
        True,
        Pace(100, 60),
    )
    assert gui_state.apply_pace()["batch_size"] == 100
