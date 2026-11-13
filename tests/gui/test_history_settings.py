# pyright: reportOptionalMemberAccess=false
"""History page (list, undo, export), Settings page (save, revert, defaults, theme, shell menu)
and the external programs panel. Only fakes: a temp journal, a fake registry, fake .bat programs.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from various_tools.core.deps import TOOL_SPECS, ExternalTools
from various_tools.core.journal import UndoReport
from various_tools.core.settings import Settings
from various_tools.gui import shell
from various_tools.gui.history_page import NO_HISTORY_TEXT, HistoryPage, undo_summary
from various_tools.gui.main_window import MainWindow
from various_tools.gui.settings_page import SettingsPage
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.widgets.external_tools import (
    ExternalToolsPanel,
    check_program,
    locate_program,
    status_of,
)
from various_tools.gui.workers import Worker

if TYPE_CHECKING:
    from conftest import Dialogs

ImageMaker = Callable[..., Path]
OpenTool = Callable[..., ToolPage]
PageStep = Callable[[ToolPage], None]


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


@pytest.fixture
def renamed(
    tmp_path: Path,
    make_image: ImageMaker,
    open_tool: OpenTool,
    scan: PageStep,
    apply: PageStep,
    dialogs: Dialogs,
) -> tuple[Path, list[str]]:
    """A folder where a rename run was applied; returns the folder and the original names."""
    root = tmp_path / "snaps"
    for number, second in ((1001, 5), (1002, 40)):
        path = make_image(f"snaps/Snapchat-{number}.jpg", seed=number)
        stamp = datetime(2024, 1, 1, 12, 34, second).timestamp()
        os.utime(path, (stamp, stamp))
    original = names(root)
    page = open_tool("rename", str(root))
    page._preview_timer.stop()  # pyright: ignore[reportPrivateUsage]
    page.form.update_values({"preset": "snapchat"})
    scan(page)
    apply(page)
    dialogs.confirms.clear()
    dialogs.informs.clear()
    return root, original


def first_run_id(history: HistoryPage) -> int:
    return int(history.runs_view.item(0, 0).text())


# ----- history --------------------------------------------------------------------------------


def test_empty_history_shows_the_hint(window: MainWindow) -> None:
    history = window.history_page
    assert history.empty_label.text() == NO_HISTORY_TEXT
    assert not history.empty_label.isHidden()
    assert history.runs_view.rowCount() == 0
    assert not history.undo_button.isEnabled() and not history.export_button.isEnabled()
    assert history.undo_selected() is None
    assert history.export_selected("x.csv") is None


def test_history_lists_the_applied_run(window: MainWindow, renamed: tuple[Path, list[str]]) -> None:
    history = window.history_page
    assert history.empty_label.isHidden()
    assert history.runs_view.rowCount() == 1
    assert history.runs_view.item(0, 4).text() == "done"
    assert history.runs_view.item(0, 5).text() == "2"
    run_id = first_run_id(history)
    history.select_run(run_id)
    assert history.actions_view.rowCount() == 2
    assert history.actions_view.item(0, 1).text() == "rename"
    assert history.undo_button.isEnabled() and history.export_button.isEnabled()
    assert f"Run {run_id}" in history.summary_label.text()


def test_undo_through_the_history_page_restores_the_files(
    qtbot: QtBot, window: MainWindow, dialogs: Dialogs, renamed: tuple[Path, list[str]]
) -> None:
    root, original = renamed
    assert names(root) != original
    history = window.history_page
    run_id = first_run_id(history)
    history.select_run(run_id)
    with qtbot.waitSignal(history.runUndone, timeout=20000) as undone:
        assert history.undo_selected() is not None
    assert undone.args == [run_id]
    assert names(root) == original
    assert len(dialogs.confirms) == 1 and "2 move or rename" in dialogs.confirms[0][1]
    assert dialogs.informs[-1][0].startswith("Undo run")
    assert "2 action(s) were put back" in dialogs.informs[-1][1]
    assert history.runs_view.item(0, 4).text() == "undone"
    history.select_run(run_id)
    assert not history.undo_button.isEnabled()
    assert history.undo_selected() is None


def test_declined_undo_does_nothing(
    window: MainWindow, dialogs: Dialogs, renamed: tuple[Path, list[str]]
) -> None:
    root, original = renamed
    after_apply = names(root)
    history = window.history_page
    history.select_run(first_run_id(history))
    dialogs.default = False
    assert history.undo_selected() is None
    assert names(root) == after_apply != original
    assert history.runs_view.item(0, 4).text() == "done"
    assert dialogs.informs == []


def test_a_trash_run_cannot_be_undone_from_history(
    window: MainWindow,
    open_tool: OpenTool,
    scan: PageStep,
    apply: PageStep,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "e"
    (folder / "x").mkdir(parents=True)
    page = open_tool("empty_folders", str(folder))
    scan(page)
    apply(page)
    history = window.history_page
    history.select_run(first_run_id(history))
    assert history.actions_view.rowCount() == 1
    assert not history.undo_button.isEnabled()
    assert history.undo_selected() is None
    assert history.export_button.isEnabled()


def test_export_selected_writes_a_file(
    qtbot: QtBot,
    window: MainWindow,
    dialogs: Dialogs,
    renamed: tuple[Path, list[str]],
    tmp_path: Path,
) -> None:
    history = window.history_page
    history.select_run(first_run_id(history))
    target = tmp_path / "run.csv"
    assert history.export_selected(str(target)) is not None
    qtbot.waitUntil(lambda: bool(dialogs.informs), timeout=20000)
    assert dialogs.informs[-1][0] == "Export"
    text = target.read_text(encoding="utf-8-sig")
    assert "rename" in text and "Snapchat" in text


def test_export_selected_failure_is_reported(
    qtbot: QtBot,
    window: MainWindow,
    dialogs: Dialogs,
    renamed: tuple[Path, list[str]],
    tmp_path: Path,
) -> None:
    history = window.history_page
    history.select_run(first_run_id(history))
    assert history.export_selected(str(tmp_path / "run.unknown")) is not None
    qtbot.waitUntil(lambda: bool(dialogs.informs), timeout=20000)
    assert dialogs.informs[-1][0] == "Export failed"


def test_history_reload_keeps_the_selection(
    window: MainWindow, renamed: tuple[Path, list[str]]
) -> None:
    history = window.history_page
    history.select_run(first_run_id(history))
    history.reload()
    assert history.undo_button.isEnabled()
    assert history.actions_view.rowCount() == 2


def test_undo_summary_text() -> None:
    report = UndoReport(run_id=3, reverted=1, skipped=("a", "b"), failed=("c",))
    text = undo_summary(report)
    assert "Run 3 is now: partially undone." in text
    assert "1 action(s) were put back." in text
    assert "Skipped (2):" in text and "Failed (1):" in text
    many = UndoReport(run_id=1, reverted=0, skipped=tuple(str(i) for i in range(25)), failed=())
    assert "... and 5 more" in undo_summary(many)


# ----- settings -------------------------------------------------------------------------------


@pytest.fixture
def restore_theme() -> Iterator[None]:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    palette = QPalette(app.palette())
    style = app.style().objectName()
    yield
    app.setPalette(palette)
    app.setStyle(style)
    app.setProperty("variousToolsFollowSystem", False)


def test_settings_form_shows_the_saved_settings(window: MainWindow, gui_state: AppState) -> None:
    built = window.settings_page.form.build()
    assert isinstance(built, Settings)
    assert built.model_dump() == gui_state.settings.model_dump()


@pytest.mark.usefixtures("restore_theme")
def test_save_writes_the_file_and_applies_the_theme(
    window: MainWindow, gui_state: AppState
) -> None:
    page = window.settings_page
    page.form.update_values({"theme": "dark", "duplicate_folder": "dupes", "workers": 2})
    assert page.save() is True
    assert page.status_label.text() == "Saved."
    assert gui_state.settings.theme == "dark"
    reloaded = AppState.load(gui_state.settings_path).settings
    assert (reloaded.theme, reloaded.duplicate_folder, reloaded.workers) == ("dark", "dupes", 2)
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    assert app.palette().color(QPalette.ColorRole.Window).lightness() < 100
    page.form.update_values({"theme": "light"})
    assert page.save() is True
    assert app.palette().color(QPalette.ColorRole.Window).lightness() > 150


def test_save_keeps_the_external_program_paths(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    gui_state.update_settings(external_tools={"ffmpeg": str(tmp_path / "ffmpeg.exe")})
    page = window.settings_page
    page.form.update_values({"duplicate_folder": "d2"})
    assert page.save() is True
    assert gui_state.settings.external_tools == {"ffmpeg": str(tmp_path / "ffmpeg.exe")}


def test_save_with_an_invalid_value_changes_nothing(
    window: MainWindow, gui_state: AppState
) -> None:
    before = gui_state.settings_path.read_text(encoding="utf-8")
    page = window.settings_page
    page.form.update_values({"default_output_folder": ""})
    assert page.save() is False
    assert "Not saved" in page.status_label.text()
    assert "default_output_folder" in page.form.errors()
    assert gui_state.settings_path.read_text(encoding="utf-8") == before
    assert gui_state.settings.default_output_folder == "resized"


def test_revert_shows_the_saved_settings_again(window: MainWindow, gui_state: AppState) -> None:
    page = window.settings_page
    page.form.update_values({"duplicate_folder": "changed"})
    page.revert()
    assert page.form.raw_values()["duplicate_folder"] == gui_state.settings.duplicate_folder
    assert page.status_label.text() == ""


def test_restore_defaults_asks_first_and_does_not_save(
    window: MainWindow, gui_state: AppState, dialogs: Dialogs
) -> None:
    gui_state.update_settings(duplicate_folder="mine")
    page = window.settings_page
    page.revert()
    dialogs.default = False
    page.restore_defaults()
    assert page.form.raw_values()["duplicate_folder"] == "mine"
    dialogs.default = True
    page.restore_defaults()
    assert page.form.raw_values()["duplicate_folder"] == "duplicate"
    assert gui_state.settings.duplicate_folder == "mine"  # not saved yet
    assert "Click Save" in page.status_label.text()
    page.revert()
    assert page.form.raw_values()["duplicate_folder"] == "mine"


def test_locked_setting_asks_inside_the_form(window: MainWindow) -> None:
    page = window.settings_page
    seen: list[str] = []

    def decline(spec: object, _parent: object) -> bool:
        seen.append(getattr(spec, "name", ""))
        return False

    page.form._confirm = decline  # pyright: ignore[reportPrivateUsage]
    combo = page.form.editor("delete_mode")
    combo.setCurrentText("permanent")  # type: ignore[attr-defined]
    assert seen == ["delete_mode"]
    assert combo.currentText() == "trash"  # type: ignore[attr-defined]


def test_shell_toggle_writes_the_fake_registry_only_when_changed(
    window: MainWindow, registry: shell.FakeRegistry
) -> None:
    page = window.settings_page
    page.form.update_values({"duplicate_folder": "x"})
    assert page.save() is True
    assert registry.writes == []  # the toggle did not change
    page.form.update_values({"shell_integration": True})
    assert page.save() is True
    assert shell.is_installed(registry)
    count = len(registry.writes)
    assert page.save() is True
    assert len(registry.writes) == count  # unchanged again: nothing written
    page.form.update_values({"shell_integration": False})
    assert page.save() is True
    assert not shell.is_installed(registry)


def test_shell_failure_keeps_the_setting_off_and_informs(
    qtbot: QtBot, dialogs: Dialogs, gui_state: AppState
) -> None:
    class Broken(shell.FakeRegistry):
        def delete_tree(self, key_path: str) -> None:
            raise OSError("access denied")

    page = SettingsPage(gui_state, shell_backend=Broken())
    qtbot.addWidget(page)
    dialogs.attach(page)
    page.form.update_values({"shell_integration": True})
    assert page.save() is True  # the other settings still save
    assert dialogs.informs and "access denied" in dialogs.informs[-1][1]
    assert gui_state.settings.shell_integration is False
    assert page.form.raw_values()["shell_integration"] is False


# ----- external programs panel ----------------------------------------------------------------


def make_panel(qtbot: QtBot, state: AppState) -> ExternalToolsPanel:
    panel = ExternalToolsPanel(state)
    qtbot.addWidget(panel)
    return panel


def wait_worker(qtbot: QtBot, worker: Worker | None) -> None:
    assert worker is not None
    qtbot.waitUntil(worker.isFinished, timeout=30000)


def test_panel_has_a_row_per_program(qtbot: QtBot, gui_state: AppState) -> None:
    panel = make_panel(qtbot, gui_state)
    assert list(panel.rows) == list(TOOL_SPECS)
    assert not panel.rows["ffmpeg"].reset_button.isEnabled()
    assert panel.rows["ollama"].browse_button.isHidden()


def test_set_program_with_a_working_program_saves_the_override(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path, fake_exe: Callable[..., Path]
) -> None:
    exe = fake_exe(tmp_path / "bin", "ffmpeg", "ffmpeg version 7.0")
    panel = make_panel(qtbot, gui_state)
    wait_worker(qtbot, panel.set_program("ffmpeg", str(exe)))
    qtbot.waitUntil(
        lambda: gui_state.settings.external_tools.get("ffmpeg") == str(exe), timeout=15000
    )
    assert AppState.load(gui_state.settings_path).settings.external_tools["ffmpeg"] == str(exe)
    row = panel.rows["ffmpeg"]
    qtbot.waitUntil(lambda: "found" in row.status_label.text(), timeout=30000)
    assert row.location_edit.text() == str(exe)
    assert "ffmpeg version 7.0" in row.version_label.text()
    assert row.reset_button.isEnabled()


def test_set_program_with_an_invalid_file_is_not_saved(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    junk = tmp_path / "junk.txt"
    junk.write_text("not a program")
    panel = make_panel(qtbot, gui_state)
    row = panel.rows["ffmpeg"]
    wait_worker(qtbot, panel.set_program("ffmpeg", str(junk)))
    qtbot.waitUntil(lambda: row.message_label.text().startswith("Not saved"), timeout=15000)
    wait_worker(qtbot, panel.set_program("ffmpeg", str(tmp_path / "missing.exe")))
    qtbot.waitUntil(lambda: "There is no file" in row.message_label.text(), timeout=15000)
    wait_worker(qtbot, panel.set_program("ffmpeg", ""))
    qtbot.waitUntil(lambda: "No file was chosen" in row.message_label.text(), timeout=15000)
    assert gui_state.settings.external_tools == {}


def test_reset_removes_the_override(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path, fake_exe: Callable[..., Path]
) -> None:
    exe = fake_exe(tmp_path / "bin", "ffmpeg")
    gui_state.update_settings(external_tools={"ffmpeg": str(exe)})
    panel = make_panel(qtbot, gui_state)
    assert panel.reset("ffprobe") is None  # nothing saved for it
    wait_worker(qtbot, panel.reset("ffmpeg"))
    assert gui_state.settings.external_tools == {}
    assert AppState.load(gui_state.settings_path).settings.external_tools == {}


def test_test_button_reports_the_program_and_ollama(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path, fake_exe: Callable[..., Path]
) -> None:
    exe = fake_exe(tmp_path / "bin", "ffmpeg", "ffmpeg version 7.0")
    gui_state.update_settings(external_tools={"ffmpeg": str(exe)})
    panel = make_panel(qtbot, gui_state)
    wait_worker(qtbot, panel.test("ffmpeg"))
    qtbot.waitUntil(lambda: "works" in panel.rows["ffmpeg"].message_label.text(), timeout=15000)
    wait_worker(qtbot, panel.test("ollama"))
    qtbot.waitUntil(
        lambda: "did not answer" in panel.rows["ollama"].message_label.text(), timeout=15000
    )


def test_auto_detect_fills_every_row(qtbot: QtBot, gui_state: AppState) -> None:
    panel = make_panel(qtbot, gui_state)
    wait_worker(qtbot, panel.auto_detect())
    for row in panel.rows.values():
        qtbot.waitUntil(lambda r=row: r.status_label.text() != "Checking...", timeout=30000)
        assert any(word in row.status_label.text() for word in ("found", "missing", "not working"))


def test_check_program_and_status_of(tmp_path: Path, fake_exe: Callable[..., Path]) -> None:
    exe = fake_exe(tmp_path, "ffmpeg", "ffmpeg version 1")
    good = check_program("ffmpeg", f'"{exe}"', 10)  # quotes are tolerated
    assert good.ok and good.version == "ffmpeg version 1"
    assert not check_program("ffmpeg", str(tmp_path / "nope.exe"), 10).ok
    assert not check_program("ffmpeg", "  ", 10).ok
    location = ExternalTools({}).resolve("ffprobe")
    assert status_of(location) in {"found", "missing", "not working"}


def test_locate_program_saves_only_a_working_program(
    qtbot: QtBot,
    gui_state: AppState,
    tmp_path: Path,
    fake_exe: Callable[..., Path],
    problems: list[tuple[str, str]],
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    exe = fake_exe(tmp_path / "bin", "exiftool", "12.50")
    assert locate_program(parent, gui_state, "exiftool", path=str(exe)) is True
    assert gui_state.settings.external_tools["exiftool"] == str(exe)
    assert problems == []
    junk = tmp_path / "junk.txt"
    junk.write_text("x")
    assert locate_program(parent, gui_state, "ffmpeg", path=str(junk)) is False
    assert "ffmpeg" not in gui_state.settings.external_tools
    assert problems and problems[-1][0] == "Locate ffmpeg"
    assert "Not saved" in problems[-1][1]
