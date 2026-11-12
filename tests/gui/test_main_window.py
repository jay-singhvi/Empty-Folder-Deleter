"""Phase 1 GUI smoke test: the placeholder window opens and shows the version."""

from pytestqt.qtbot import QtBot

from various_tools import __version__
from various_tools.gui.app import make_window


def test_placeholder_window_shows_version(qtbot: QtBot) -> None:
    window = make_window()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    assert window.windowTitle() == "Various Tools"
    assert __version__ in window.text()
