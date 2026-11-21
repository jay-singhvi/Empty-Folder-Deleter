"""The Apply window (pace choice, OneDrive hint, remembered choice) and Pause / Resume / Skip."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pytestqt.qtbot import QtBot

from various_tools.core import plan as plan_module
from various_tools.core.context import Pace
from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.gui.apply_dialog import (
    ApplyAsk,
    ApplyDialog,
    estimate_wait,
    onedrive_suggestion,
    pace_to_preselect,
)
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.wording import format_wait

if TYPE_CHECKING:
    from conftest import Dialogs


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0 seconds"),
        (1, "1 second"),
        (90, "1 minute 30 seconds"),
        (3600, "1 hour"),
        (13500, "3 hours 45 minutes"),
        (300, "5 minutes"),
    ],
)
def test_format_wait(seconds: float, text: str) -> None:
    assert format_wait(seconds) == text


def test_estimate_wait() -> None:
    assert estimate_wait(100, None) == 0
    assert estimate_wait(50, Pace(50, 300)) == 0
    assert estimate_wait(51, Pace(50, 300)) == 300
    assert estimate_wait(2113, Pace(50, 300)) == 42 * 300


def _plan(tmp_path: Path, count: int, *, where: Path | None = None) -> Plan:
    base = where or tmp_path
    actions = [PlannedAction(ActionKind.TRASH, base / f"f{i}.txt", None, "x") for i in range(count)]
    return Plan(tool="t", roots=[base], actions=actions)


def test_suggestion_needs_onedrive_and_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "OneDrive"
    monkeypatch.setenv("OneDrive", str(root))
    inside = _plan(tmp_path, 600, where=root / "pics")
    assert onedrive_suggestion(inside, []) is not None  # over 500 files
    small = _plan(tmp_path, 3, where=root)
    assert onedrive_suggestion(small, [None] * 3) is None
    assert onedrive_suggestion(small, [2 * 1024**3, None, None]) is not None  # over 1 GB
    outside = _plan(tmp_path, 600, where=tmp_path / "plain")
    assert onedrive_suggestion(outside, [2 * 1024**3] * 600) is None
    inside.select_all(False)
    assert onedrive_suggestion(inside, []) is None


def test_dialog_defaults_to_all_at_once(qtbot: QtBot) -> None:
    dialog = ApplyDialog(ApplyAsk("t", "summary", 10))
    qtbot.addWidget(dialog)
    assert dialog.all_radio.isChecked()
    assert dialog.pace() is None
    assert "No extra waiting" in dialog.estimate_label.text()
    assert dialog.reason_label.isHidden()


def test_dialog_preselects_batches_with_a_reason(qtbot: QtBot) -> None:
    dialog = ApplyDialog(ApplyAsk("t", "summary", 2113, "Because OneDrive."))
    qtbot.addWidget(dialog)
    assert dialog.batch_radio.isChecked()
    assert not dialog.reason_label.isHidden() and "Because OneDrive." in dialog.reason_label.text()
    assert dialog.pace() == Pace(50, 300)
    assert dialog.preset_combo.currentIndex() == 0
    assert "3 hours 30 minutes" in dialog.estimate_label.text()


def test_dialog_presets_and_custom(qtbot: QtBot) -> None:
    dialog = ApplyDialog(ApplyAsk("t", "s", 10))
    qtbot.addWidget(dialog)
    dialog.preset_combo.setCurrentIndex(2)
    assert dialog.batch_radio.isChecked() and dialog.pace() == Pace(1, 2)
    dialog.preset_combo.setCurrentIndex(1)
    assert dialog.pace() == Pace(100, 60)
    dialog.batch_spin.setValue(7)
    assert dialog.preset_combo.currentText() == "Custom"
    dialog.unit_combo.setCurrentIndex(2)
    dialog.wait_spin.setValue(2)
    assert dialog.pace() == Pace(7, 7200)
    dialog.all_radio.setChecked(True)
    assert dialog.pace() is None


def test_dialog_restores_the_remembered_choice(qtbot: QtBot) -> None:
    saved = {"mode": "batches", "batch_size": 25, "wait": 3, "unit": "minutes"}
    dialog = ApplyDialog(ApplyAsk("t", "s", 100), saved)
    qtbot.addWidget(dialog)
    assert dialog.pace() == Pace(25, 180)
    assert dialog.values() == saved | {"wait_for_upload": True, "upload_minutes": 30}
    junk = ApplyDialog(ApplyAsk("t", "s", 100), {"batch_size": "x"})
    qtbot.addWidget(junk)
    assert junk.pace() is None


# ----- the tool page --------------------------------------------------------------------------


@pytest.fixture
def folders(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    for name in "abcd":
        (root / name).mkdir(parents=True)
    (root / "keep").mkdir()
    (root / "keep" / "f.txt").write_text("x")
    return root


def test_open_dialog_remembers_the_choice(
    qtbot: QtBot,
    open_tool: Any,
    folders: Path,
    scan: Any,
    gui_state: AppState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    seen: list[ApplyDialog] = []

    def fake_exec(self: ApplyDialog) -> int:
        seen.append(self)
        self.preset_combo.setCurrentIndex(1)
        return ApplyDialog.DialogCode.Accepted

    monkeypatch.setattr(ApplyDialog, "exec", fake_exec)
    # Reset the hook so the real dialog path (with the fake exec) is used.
    page.confirm = page._default_confirm_hook  # pyright: ignore[reportPrivateUsage]
    ask = ApplyAsk("t", "s", 4)
    assert page.choose_pace(ask) == (True, Pace(100, 60))
    assert gui_state.apply_pace()["batch_size"] == 100
    assert seen


def test_choose_pace_hook_is_used_and_reaches_the_run(
    qtbot: QtBot,
    open_tool: Any,
    dialogs: Dialogs,
    folders: Path,
    scan: Any,
    wait_idle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    waits: list[float] = []
    monkeypatch.setattr(plan_module, "_sleep", waits.append)
    asked: list[ApplyAsk] = []

    def choose(ask: ApplyAsk) -> tuple[bool, Pace | None]:
        asked.append(ask)
        return True, Pace(2, 1.0)

    page.choose_pace = choose
    assert page.apply() is not None
    wait_idle(page)
    assert asked and asked[0].count == len(page.plan.actions)  # type: ignore[union-attr]
    assert waits  # the run really paused between batches
    assert page.result is not None and page.result.status == "done"


def test_declining_the_apply_window_changes_nothing(
    open_tool: Any, folders: Path, scan: Any
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    page.choose_pace = lambda _ask: (False, None)
    assert page.apply() is None
    assert (folders / "a").exists()


def test_pause_resume_and_skip_the_wait(
    qtbot: QtBot,
    open_tool: Any,
    folders: Path,
    scan: Any,
    wait_idle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page: ToolPage = open_tool("empty_folders", str(folders))
    scan(page)
    # Real but tiny sleeps: a 60 s wait then lasts about a second unless skipped.
    monkeypatch.setattr(plan_module, "_sleep", lambda _s: time.sleep(0.01))
    page.choose_pace = lambda _ask: (True, Pace(1, 60.0))
    window = page.window()
    window.show()
    worker = page.apply()
    assert worker is not None
    qtbot.waitUntil(lambda: page.skip_wait_button.isEnabled(), timeout=10000)
    assert page.cancel_button.text() == "Stop"
    assert not page.pause_button.isHidden() and page.resume_button.isHidden()
    page.pause_button.click()
    assert page.pause_button.isHidden() and not page.resume_button.isHidden()
    page.resume_button.click()
    assert not page.pause_button.isHidden()
    # Skip every wait until the run ends.
    deadline = time.time() + 60
    while page.worker is not None and time.time() < deadline:
        if page.skip_wait_button.isEnabled():
            page.skip_wait_button.click()
        qtbot.wait(20)
    assert page.worker is None
    assert page.result is not None and page.result.status == "done"
    assert page.pause_button.isHidden() and page.skip_wait_button.isHidden()
    assert page.cancel_button.text() == "Cancel"


def test_stop_while_paused_ends_the_run(
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
    page.choose_pace = lambda _ask: (True, Pace(1, 60.0))
    page.window().show()
    assert page.apply() is not None
    qtbot.waitUntil(lambda: page.skip_wait_button.isEnabled(), timeout=10000)
    page.pause_button.click()
    page.cancel_button.click()
    wait_idle(page)
    assert page.result is None or page.result.status == "cancelled"


def test_the_pace_choice_is_kept_even_when_remembering_options_is_off(
    make_state: Any, tmp_path: Path
) -> None:
    state: AppState = make_state(remember_last_folders=False)
    state.set_apply_pace({"mode": "batches", "batch_size": 25, "wait": 3, "unit": "minutes"})
    again = AppState.load(state.settings_path)
    assert again.apply_pace()["batch_size"] == 25
    assert state.pace_picked_this_session and not again.pace_picked_this_session


def test_a_remembered_batches_choice_is_preselected_only_when_it_applies() -> None:
    saved = {"mode": "batches", "batch_size": 25, "wait": 3, "unit": "minutes"}
    plain = pace_to_preselect(saved, touches_onedrive=False, picked_this_session=False)
    assert plain["mode"] == "all" and plain["batch_size"] == 25
    assert pace_to_preselect(saved, touches_onedrive=True, picked_this_session=False) == saved
    assert pace_to_preselect(saved, touches_onedrive=False, picked_this_session=True) == saved
    all_at_once = {"mode": "all"}
    assert pace_to_preselect(all_at_once, touches_onedrive=True, picked_this_session=True) == (
        all_at_once
    )


# ----- wait for the OneDrive upload --------------------------------------------------------------


def _ask(touches: bool) -> ApplyAsk:
    return ApplyAsk("t", "s", 100, touches_onedrive=touches)


def test_upload_checkbox_is_hidden_unless_the_plan_touches_onedrive(qtbot: QtBot) -> None:
    plain = ApplyDialog(_ask(False))
    qtbot.addWidget(plain)
    assert plain.upload_check.isHidden()
    assert plain.pace() is None
    plain.batch_radio.setChecked(True)
    pace = plain.pace()
    assert pace is not None and pace.wait_for_upload is False


def test_upload_checkbox_is_on_by_default_with_batches_on_onedrive(qtbot: QtBot) -> None:
    dialog = ApplyDialog(_ask(True))
    qtbot.addWidget(dialog)
    assert not dialog.upload_check.isHidden() and dialog.upload_check.isChecked()
    assert not dialog.upload_check.isEnabled()  # all at once: nothing to wait between
    dialog.batch_radio.setChecked(True)
    assert dialog.upload_check.isEnabled()
    assert dialog.pace() == Pace(50, 300, wait_for_upload=True, upload_timeout_s=1800)
    dialog.upload_spin.setValue(5)
    dialog.upload_check.setChecked(False)
    assert dialog.pace() == Pace(50, 300, upload_timeout_s=300.0)


def test_upload_choice_is_remembered(qtbot: QtBot) -> None:
    dialog = ApplyDialog(_ask(True))
    qtbot.addWidget(dialog)
    dialog.batch_radio.setChecked(True)
    dialog.upload_spin.setValue(12)
    values = dialog.values()
    assert values["wait_for_upload"] is True and values["upload_minutes"] == 12
    again = ApplyDialog(_ask(True), values)
    qtbot.addWidget(again)
    assert again.upload_spin.value() == 12 and again.upload_check.isChecked()
    off = ApplyDialog(_ask(True), values | {"wait_for_upload": False})
    qtbot.addWidget(off)
    assert not off.upload_check.isChecked()


def test_an_old_remembered_choice_without_the_upload_keys_still_loads(qtbot: QtBot) -> None:
    old = {"mode": "batches", "batch_size": 25, "wait": 3, "unit": "minutes"}
    dialog = ApplyDialog(_ask(True), old)
    qtbot.addWidget(dialog)
    assert dialog.upload_check.isChecked() and dialog.upload_spin.value() == 30
    junk = ApplyDialog(_ask(True), old | {"upload_minutes": "x", "wait_for_upload": "maybe"})
    qtbot.addWidget(junk)
    assert junk.upload_spin.value() == 30
