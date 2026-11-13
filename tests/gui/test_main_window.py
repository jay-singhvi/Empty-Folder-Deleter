"""Main window shell: log panel, busy status and close handling."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

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
