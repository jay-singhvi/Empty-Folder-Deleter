"""Main window shell: log panel, busy status and close handling."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pytestqt.qtbot import QtBot

from various_tools import __version__
from various_tools.gui.main_window import MainWindow

if TYPE_CHECKING:
    from conftest import Dialogs


def test_make_window_returns_a_main_window(window: MainWindow) -> None:
    assert isinstance(window, MainWindow)
    assert window.windowTitle() == f"Various Tools {__version__}"
    assert window.statusBar().currentMessage() == "Ready"


def test_log_records_reach_the_log_panel(qtbot: QtBot, window: MainWindow) -> None:
    logging.getLogger("various_tools.test").info("hello from a test")
    qtbot.waitUntil(lambda: "hello from a test" in window.log_panel.text(), timeout=5000)
    window.log_panel.set_expanded(True)
    window.log_panel.clear_button.click()
    assert window.log_panel.text() == ""


def test_closing_removes_the_log_handler(window: MainWindow) -> None:
    logger = logging.getLogger("various_tools")
    assert window.log_bridge in logger.handlers
    window.close()
    assert window.log_bridge not in logger.handlers


def test_status_bar_shows_busy_while_a_page_works(
    window: MainWindow, tmp_path: Path, dialogs: Dialogs
) -> None:
    page = window.open_tool("empty_folders", str(tmp_path))
    assert page is not None
    dialogs.attach(page)
    page.busyChanged.emit(True)
    assert "Working" in window.statusBar().currentMessage()
    page.busyChanged.emit(False)
    assert window.statusBar().currentMessage() == "Ready"


def test_timeline_show_on_map_opens_the_map_centred_on_the_item(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.gui.main_window import TIMELINE_ID
    from various_tools.gui.map_page import MapPage
    from various_tools.gui.timeline_page import TimelinePage

    focused: list[Path] = []
    monkeypatch.setattr(MapPage, "focus_item", lambda _self, path: focused.append(path) or True)
    timeline = window.show_page(TIMELINE_ID)
    assert isinstance(timeline, TimelinePage)
    photo = tmp_path / "a.jpg"
    timeline.showOnMap.emit(photo)
    assert window.map_page is not None
    assert window.stack.currentWidget() is window.map_page
    assert focused == [photo]


def test_home_clears_the_sidebar_highlight(window: MainWindow) -> None:
    from various_tools.gui.main_window import HISTORY_ID, HOME_ID

    window.show_page(HISTORY_ID)
    assert window.sidebar.currentItem() is not None
    window.show_page(HOME_ID)
    assert window.stack.currentWidget() is window.home_page
    assert window.sidebar.currentItem() is None
    assert window.sidebar.selectedItems() == []


def test_find_duplicates_opens_the_duplicate_finder_on_the_library_folders(
    window: MainWindow, tmp_path: Path
) -> None:
    from various_tools.gui.main_window import TIMELINE_ID
    from various_tools.gui.timeline_page import TimelinePage

    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    timeline = window.show_page(TIMELINE_ID)
    assert isinstance(timeline, TimelinePage)
    timeline.openTool.emit("duplicates", [one, two])
    page = window.tool_pages["duplicates"]
    assert window.stack.currentWidget() is page
    assert page.other_folders_check.isChecked()
    options = page.form.build()
    assert [Path(p) for p in options.roots] == [one, two]  # type: ignore[attr-defined]


def test_a_duplicates_scan_is_handed_to_the_photo_library(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.core.plan import Plan
    from various_tools.gui.library_model import LibraryModel

    seen: list[str] = []
    monkeypatch.setattr(
        LibraryModel, "update_duplicates", lambda _self, plan: seen.append(plan.tool) or 0
    )
    window.tool_page("duplicates").scanFinished.emit(Plan(tool="duplicates", roots=[]))
    window.tool_page("empty_files").scanFinished.emit(Plan(tool="empty_files", roots=[]))
    assert seen == ["duplicates"]


def test_a_recipe_step_scan_and_copy_reach_the_window(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.core.plan import Plan
    from various_tools.gui.library_model import LibraryModel

    seen: list[str] = []
    monkeypatch.setattr(
        LibraryModel, "update_duplicates", lambda _self, plan: seen.append(plan.tool) or 0
    )
    recipes = window.recipes()
    recipes.scanFinished.emit(Plan(tool="duplicates", roots=[]))
    assert seen == ["duplicates"]
    recipes.clipboardCopied.emit("Copied 3 paths.")
    assert "Copied 3 paths." in window.statusBar().currentMessage()


def test_about_credits_the_place_names(window: MainWindow) -> None:
    from various_tools.core.media.places import PLACES_ATTRIBUTION

    shown: list[str] = []
    window.inform = lambda _title, text: shown.append(text)
    window.show_about()
    assert PLACES_ATTRIBUTION in shown[0]
