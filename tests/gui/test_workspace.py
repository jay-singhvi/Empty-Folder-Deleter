"""The working folder, the app shell (menus, toolbar, Home) and the folded options and notes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from various_tools.gui.main_window import HISTORY_ID, SETTINGS_ID, MainWindow
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage, group_notes, workspace_field_name

if TYPE_CHECKING:
    from conftest import Dialogs


def _tree(root: Path, name: str) -> Path:
    """A folder with one empty sub-folder, so the Empty Folders scan has something to find."""
    folder = root / name
    (folder / "empty").mkdir(parents=True)
    return folder


# ----- state ---------------------------------------------------------------------------------


def test_workspace_is_empty_at_first(gui_state: AppState) -> None:
    assert gui_state.workspace() == []
    assert gui_state.recent_workspaces() == []


def test_workspace_persists_and_notifies(
    make_state: Callable[..., AppState], tmp_path: Path
) -> None:
    state = make_state()
    seen: list[list[Path]] = []
    state.on_workspace_changed(seen.append)
    state.set_workspace([tmp_path / "a", tmp_path / "b"])
    state.set_workspace([tmp_path / "a", tmp_path / "b"])  # unchanged: no second call
    assert seen == [[tmp_path / "a", tmp_path / "b"]]
    again = AppState.load(state.settings_path)
    assert again.workspace() == [tmp_path / "a", tmp_path / "b"]


def test_workspace_persists_even_when_remembering_options_is_off(
    make_state: Callable[..., AppState], tmp_path: Path
) -> None:
    state = make_state(remember_last_folders=False)
    state.set_workspace([tmp_path / "x"])
    assert AppState.load(state.settings_path).workspace() == [tmp_path / "x"]


def test_recent_workspaces_are_newest_first_without_duplicates_and_capped(
    gui_state: AppState, tmp_path: Path
) -> None:
    for index in range(10):
        gui_state.set_workspace([tmp_path / str(index)])
    gui_state.set_workspace([tmp_path / "5"])
    recent = gui_state.recent_workspaces()
    assert len(recent) == 8
    assert recent[0] == [tmp_path / "5"]
    assert [tmp_path / "5"] not in recent[1:]


# ----- tool pages ----------------------------------------------------------------------------


def test_a_tool_page_follows_the_working_folder(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    first = _tree(tmp_path, "first")
    gui_state.set_workspace([first])
    page = window.tool_page("empty_folders")
    assert [str(p) for p in page.form.raw_values()["roots"]] == [str(first)]
    assert str(first) in page.workspace_label.text()
    assert not page.form.editor("roots").isVisibleTo(page)
    second = _tree(tmp_path, "second")
    gui_state.set_workspace([second])
    assert [str(p) for p in page.form.raw_values()["roots"]] == [str(second)]
    assert str(second) in page.workspace_label.text()


def test_other_folders_box_reveals_the_field_and_is_remembered(
    window: MainWindow, gui_state: AppState, tmp_path: Path, scan: Callable[[ToolPage], None]
) -> None:
    work = _tree(tmp_path, "work")
    other = _tree(tmp_path, "other")
    gui_state.set_workspace([work])
    page = window.tool_page("empty_folders")
    page.other_folders_check.setChecked(True)
    assert page.form.editor("roots").isVisibleTo(page)
    page.form.set_values({"roots": [str(other)]})
    scan(page)
    gui_state.set_workspace([tmp_path])  # the page does not follow while the box is ticked
    assert [str(p) for p in page.form.raw_values()["roots"]] == [str(other)]
    fresh = ToolPage(gui_state, gui_state.tools["empty_folders"])
    assert fresh.other_folders_check.isChecked()
    assert [str(p) for p in fresh.form.raw_values()["roots"]] == [str(other)]
    fresh.deleteLater()


def test_remembered_folders_do_not_override_the_working_folder(
    window: MainWindow, gui_state: AppState, tmp_path: Path, scan: Callable[[ToolPage], None]
) -> None:
    old = _tree(tmp_path, "old")
    new = _tree(tmp_path, "new")
    gui_state.set_workspace([old])
    page = window.tool_page("empty_folders")
    scan(page)
    gui_state.set_workspace([new])
    fresh = ToolPage(gui_state, gui_state.tools["empty_folders"])
    assert not fresh.other_folders_check.isChecked()
    assert [str(p) for p in fresh.form.raw_values()["roots"]] == [str(new)]
    fresh.deleteLater()


def test_a_page_with_a_list_keeps_it_and_says_to_look_again(
    window: MainWindow, gui_state: AppState, tmp_path: Path, scan: Callable[[ToolPage], None]
) -> None:
    first = _tree(tmp_path, "first")
    second = _tree(tmp_path, "second")
    gui_state.set_workspace([first])
    page = window.tool_page("empty_folders")
    scan(page)
    shown = page.plan
    assert shown is not None
    assert page.workspace_banner.isHidden()
    gui_state.set_workspace([second])
    assert not page.workspace_banner.isHidden()
    assert "Look again" in page.workspace_banner.text()
    assert page.plan is shown  # the shown plan is untouched; Apply stays tied to it
    scan(page)
    assert page.workspace_banner.isHidden()
    assert page.plan is not shown


def test_an_idle_page_shows_no_banner(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    gui_state.set_workspace([_tree(tmp_path, "a")])
    page = window.tool_page("empty_folders")
    gui_state.set_workspace([_tree(tmp_path, "b")])
    assert page.workspace_banner.isHidden()


def test_scan_without_a_working_folder_explains(window: MainWindow) -> None:
    page = window.tool_page("empty_folders")
    assert page.scan() is None
    assert "Choose a working folder first" in page.status_label.text()


def test_temp_cleaner_opts_out_of_the_working_folder(window: MainWindow) -> None:
    page = window.tool_page("temp_cleaner")
    assert page.state.tools["temp_cleaner"].workspace_field is None  # type: ignore[attr-defined]
    assert workspace_field_name(page.tool) is None
    assert page.workspace_row.isHidden()
    assert not page.uses_workspace


def test_open_tool_with_a_folder_sets_the_override_for_that_page(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    gui_state.set_workspace([_tree(tmp_path, "work")])
    target = _tree(tmp_path, "target")
    page = window.open_tool("empty_folders", str(target))
    assert page is not None
    assert page.other_folders_check.isChecked()
    assert [str(p) for p in page.form.raw_values()["roots"]] == [str(target)]


def test_workspace_field_name_rules(gui_state: AppState) -> None:
    assert workspace_field_name(gui_state.tools["empty_folders"]) == "roots"
    assert workspace_field_name(gui_state.tools["temp_cleaner"]) is None


# ----- options fold ----------------------------------------------------------------------------


def test_options_fold_into_a_summary_after_a_scan_and_unfold(
    window: MainWindow, gui_state: AppState, tmp_path: Path, scan: Callable[[ToolPage], None]
) -> None:
    gui_state.set_workspace([_tree(tmp_path, "w")])
    page = window.tool_page("empty_folders")
    assert not page.options_collapsed
    scan(page)
    assert page.options_collapsed
    assert page.options_summary_row.isHidden() is False
    assert page._form_scroll.isHidden()  # pyright: ignore[reportPrivateUsage]
    assert "working folder" in page.options_summary.text()
    page.change_settings_button.click()
    assert not page.options_collapsed
    assert not page._form_scroll.isHidden()  # pyright: ignore[reportPrivateUsage]
    page.change_settings_button.click()
    assert page.options_collapsed


def test_the_summary_lists_changed_options_by_their_titles(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    gui_state.set_workspace([_tree(tmp_path, "w")])
    page = window.tool_page("empty_folders")
    title = next(
        s.title
        for s in page.form.specs
        if s.kind == "bool" and s.name not in page.form.folder_fields()
    )
    name = next(s.name for s in page.form.specs if s.title == title)
    current = page.form.raw_values()[name]
    page.form.update_values({name: not current})
    assert f"{title}: " in page.options_summary_text()
    assert name not in page.options_summary_text() or name == title.lower()


# ----- notes -----------------------------------------------------------------------------------


def test_ten_thousand_skipped_notes_become_one_count_with_lazy_details(
    window: MainWindow,
) -> None:
    page = window.tool_page("empty_folders")
    notes = [
        f"skipped: already smaller than the target resolution: C:\\p\\{i}.jpg" for i in range(10000)
    ]
    notes.append("Hidden folder kept: C:\\h")
    page._show_notes(notes)  # pyright: ignore[reportPrivateUsage]
    text = page.notes_view.toPlainText()
    assert text.splitlines()[0] == "10,000 skipped: already smaller than the target resolution"
    assert len(text.splitlines()) == 2
    assert "C:\\p\\5.jpg" not in text
    assert page.notes_details.toPlainText() == ""  # not built yet
    page.notes_toggle.setChecked(True)
    assert len(page.notes_details.toPlainText().splitlines()) == 10001
    assert not page.notes_details.isHidden()
    page.notes_toggle.setChecked(False)
    assert page.notes_details.isHidden()


def test_few_notes_are_shown_as_they_are() -> None:
    notes = ["skipped: cloud-only: a", "skipped: cloud-only: b", "something else"]
    assert group_notes(notes) == (notes, False)


def test_group_notes_counts_by_reason_biggest_first() -> None:
    notes = (
        [f"skipped: reason one: p{i}" for i in range(3)]
        + [f"skipped: reason two: p{i}" for i in range(5)]
        + ["skipped: rare: p"]
    )
    lines, grouped = group_notes(notes)
    assert grouped
    assert lines == ["5 skipped: reason two", "3 skipped: reason one", "skipped: rare: p"]


# ----- menus, toolbar, Home ---------------------------------------------------------------------


def test_menu_actions_exist_with_their_shortcuts(window: MainWindow) -> None:
    keys = {
        window.choose_folder_action: "Ctrl+O",
        window.settings_action: "Ctrl+,",
        window.exit_action: "Ctrl+Q",
        window.show_log_action: "Ctrl+L",
        window.find_tool_action: "Ctrl+K",
    }
    for action, shortcut in keys.items():
        assert action.shortcut().toString() == shortcut
    titles = [a.text() for a in window.menuBar().actions()]
    assert titles == ["&File", "&View", "&Help"]


def test_choose_working_folder_sets_it(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    window.choose_folder = lambda: str(tmp_path)
    window.choose_folder_action.trigger()
    assert gui_state.workspace() == [tmp_path]
    assert str(tmp_path) in window.workspace_bar.path_label.text()
    assert window.home_page.workspace_label.text() == str(tmp_path)
    window.choose_folder = lambda: None  # cancelled: nothing changes
    window.choose_folder_action.trigger()
    assert gui_state.workspace() == [tmp_path]


def test_add_a_folder_to_the_working_folder(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    gui_state.set_workspace([tmp_path / "a"])
    window.choose_folder = lambda: str(tmp_path / "b")
    window.add_folder_action.trigger()
    assert gui_state.workspace() == [tmp_path / "a", tmp_path / "b"]


def test_recent_folders_menu_and_chip_choose_a_recent_one(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    gui_state.set_workspace([tmp_path / "a"])
    gui_state.set_workspace([tmp_path / "b"])
    window.recent_menu.aboutToShow.emit()
    actions = window.recent_menu.actions()
    assert [a.text() for a in actions] == [str(tmp_path / "b"), str(tmp_path / "a")]
    actions[1].trigger()
    assert gui_state.workspace() == [tmp_path / "a"]
    window.workspace_bar.recent_menu.actions()[0].trigger()
    assert gui_state.workspace() == [tmp_path / "a"]


def test_settings_history_and_home_open_their_pages(window: MainWindow) -> None:
    window.settings_action.trigger()
    assert window.stack.currentWidget() is window.settings_page
    window.history_action.trigger()
    assert window.stack.currentWidget() is window.history_page
    window.home_action.trigger()
    assert window.stack.currentWidget() is window.home_page
    assert window.show_page(SETTINGS_ID) is window.settings_page
    assert window.show_page(HISTORY_ID) is window.history_page


def test_exit_goes_through_the_close_logic(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    monkeypatch.setattr(window, "close", lambda: closed.append(True) or True)
    window.exit_action.trigger()
    assert closed == [True]


def test_exit_closes_the_window_and_removes_the_log_handler(window: MainWindow) -> None:
    window.show()
    window.exit_action.trigger()
    assert not window.isVisible()


def test_show_all_settings_action_is_synced_both_ways(
    window: MainWindow, gui_state: AppState
) -> None:
    window.show_all_action.trigger()
    assert gui_state.show_all_settings() is True
    assert window.log_panel.isVisibleTo(window)
    gui_state.set_show_all_settings(False)
    assert not window.show_all_action.isChecked()


def test_show_log_toggles_the_log(window: MainWindow) -> None:
    assert not window.log_panel.isVisibleTo(window)
    window.show_log_action.trigger()
    assert window.log_panel.isVisibleTo(window)
    window.show_log_action.trigger()
    assert not window.log_panel.isVisibleTo(window)


def test_find_a_tool_focuses_the_search_box(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[bool] = []
    monkeypatch.setattr(window, "focus_search", lambda: asked.append(True))
    window.find_tool_action.trigger()
    assert asked == [True]


def test_keyboard_shortcuts_window_lists_every_shortcut(window: MainWindow, qtbot: QtBot) -> None:
    window.shortcuts_action.trigger()
    dialog = window.shortcuts_dialog
    assert dialog is not None
    qtbot.addWidget(dialog)
    text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    for keys in ("Ctrl+O", "Ctrl+,", "Ctrl+Q", "Ctrl+L", "Ctrl+K", "Ctrl+A", "Space", "Ctrl+F"):
        assert keys in text


def test_open_settings_folder_and_about(
    window: MainWindow, gui_state: AppState, dialogs: Dialogs
) -> None:
    opened: list[Path] = []
    window.open_folder = opened.append
    window.open_settings_folder_action.trigger()
    assert opened == [gui_state.settings_path.parent]
    window.about_action.trigger()
    assert dialogs.informs
    assert "Various Tools" in dialogs.informs[-1][0]


def test_home_page_cards_open_tools_and_media_pages(window: MainWindow) -> None:
    home = window.home_page
    assert window.welcome is home
    assert window.stack.currentWidget() is home
    assert set(window.state.tools) <= set(home.tool_cards)
    home.tool_cards["empty_folders"].click()
    assert window.stack.currentWidget() is window.tool_pages["empty_folders"]
    window.home_action.trigger()
    home.tool_cards["__timeline__"].click()
    assert window.timeline_page is not None
    assert window.stack.currentWidget() is window.timeline_page


def test_home_page_prompts_for_a_folder_and_shows_it_once_chosen(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    home = window.home_page
    assert not home.prompt_label.isHidden()
    asked: list[bool] = []
    window.choose_folder = lambda: asked.append(True) or None
    home.choose_button.click()
    assert asked == [True]
    gui_state.set_workspace([tmp_path])
    assert home.prompt_label.isHidden()
    assert home.workspace_label.text() == str(tmp_path)


def test_home_page_lists_the_last_runs_and_links_to_history(
    window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    from various_tools.core.journal import Journal

    gui_state.settings.journal_path().parent.mkdir(parents=True, exist_ok=True)
    with Journal(gui_state.settings.journal_path()) as journal:
        for _ in range(7):
            run = journal.start_run("empty_folders", "{}")
            journal.finish_run(run, "done")
    window.home_page.refresh()
    texts = [label.text() for label in window.home_page.run_labels]
    assert len(texts) == 5
    name = gui_state.tools["empty_folders"].name
    assert all(name in t and "done" in t for t in texts), texts
    window.home_page.history_button.click()
    assert window.stack.currentWidget() is window.history_page


def test_a_scan_running_when_the_working_folder_changes_gets_the_banner(
    qtbot: QtBot, window: MainWindow, gui_state: AppState, tmp_path: Path
) -> None:
    first = _tree(tmp_path, "first")
    second = _tree(tmp_path, "second")
    gui_state.set_workspace([first])
    page = window.tool_page("empty_folders")
    worker = page.scan()
    assert worker is not None
    gui_state.set_workspace([second])  # while the scan of the first folder is running
    qtbot.waitUntil(lambda: page.plan is not None, timeout=30000)
    assert not page.workspace_banner.isHidden()
    qtbot.waitUntil(lambda: page.worker is None, timeout=30000)
