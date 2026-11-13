"""Desktop GUI entry point (``various-tools-gui``, or ``python -m various_tools.gui``).

Arguments (all optional; the Explorer right-click menu uses ``--tool`` and ``--folder``)::

    various-tools-gui [--tool ID] [--folder PATH] [--config FILE]

Unknown arguments are passed on to Qt.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from various_tools.gui.main_window import MainWindow
    from various_tools.gui.shell import RegistryBackend
    from various_tools.gui.state import AppState

APP_NAME = "Various Tools"
APP_USER_MODEL_ID = "VariousTools.Desktop"


def parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="various-tools-gui", add_help=True)
    parser.add_argument("--tool", metavar="ID", help="Open this tool's page (e.g. empty_folders).")
    parser.add_argument(
        "--folder", metavar="PATH", help="Put this folder in the tool's folder field."
    )
    parser.add_argument("--config", metavar="FILE", help="Settings file (TOML) to use.")
    return parser.parse_known_args(list(argv))


def make_window(
    state: AppState | None = None,
    *,
    tool: str | None = None,
    folder: str | None = None,
    shell_backend: RegistryBackend | None = None,
) -> MainWindow:
    """Build the main window (tests call this with a temporary AppState)."""
    from various_tools.gui.icon import app_icon
    from various_tools.gui.main_window import MainWindow
    from various_tools.gui.state import AppState

    window = MainWindow(state or AppState.load(), shell_backend=shell_backend)
    window.setWindowIcon(app_icon())
    if tool:
        window.open_tool(tool, folder)
    return window


def _set_app_user_model_id() -> None:
    """Give the process its own taskbar identity, so Windows shows our icon, not Python's."""
    if sys.platform != "win32":
        return
    import contextlib
    import ctypes

    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)


def main(argv: Sequence[str] | None = None) -> int:
    args, qt_args = parse_args(sys.argv[1:] if argv is None else argv)
    # Qt is imported here so the CLI and the core library never load it.
    from PySide6.QtWidgets import QApplication

    from various_tools.gui.icon import app_icon
    from various_tools.gui.state import AppState
    from various_tools.gui.theme import apply_theme

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _set_app_user_model_id()
    existing = QApplication.instance()
    qt_app = (
        existing if isinstance(existing, QApplication) else QApplication([sys.argv[0], *qt_args])
    )
    qt_app.setApplicationName(APP_NAME)
    qt_app.setOrganizationName(APP_NAME)
    qt_app.setWindowIcon(app_icon())

    state = AppState.load(Path(args.config) if args.config else None)
    apply_theme(qt_app, state.settings.theme)
    window = make_window(state, tool=args.tool, folder=args.folder)
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
