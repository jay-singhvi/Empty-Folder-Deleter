"""Desktop GUI entry point (`various-tools-gui`). The real main window arrives in Phase 5."""

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QLabel


def make_window() -> QLabel:
    # Imported here so the CLI and the core library never load Qt.
    from PySide6.QtWidgets import QLabel

    from various_tools import __version__

    label = QLabel(f"Various Tools {__version__}\nThe tools arrive in later phases.")
    label.setWindowTitle("Various Tools")
    label.resize(420, 120)
    return label


def main() -> int:
    from PySide6.QtWidgets import QApplication

    qt_app = QApplication.instance() or QApplication(sys.argv)
    window = make_window()
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
