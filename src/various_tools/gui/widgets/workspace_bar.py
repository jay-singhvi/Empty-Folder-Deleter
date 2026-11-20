"""The working-folder chip in the toolbar: the folder(s), a Change button and a recent list."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QWidget

from various_tools.gui.wording import NO_WORKSPACE, WORKSPACE_TITLE


def paths_text(paths: Sequence[Path | str]) -> str:
    return "; ".join(str(p) for p in paths)


class WorkspaceBar(QWidget):
    """Shows the working folder. ``changeRequested`` asks for a new one; ``recentChosen(paths)``
    reports a pick from the recent list."""

    changeRequested = Signal()
    recentChosen = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title_label = QLabel(f"{WORKSPACE_TITLE}:")
        self.path_label = QLabel(NO_WORKSPACE)
        font = self.path_label.font()
        font.setBold(True)
        self.path_label.setFont(font)
        self.change_button = QPushButton("Change…")
        self.change_button.setToolTip("Choose another working folder (Ctrl+O).")
        self.recent_button = QToolButton()
        self.recent_button.setText("▾")
        self.recent_button.setToolTip("Recent working folders")
        self.recent_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.recent_menu = QMenu(self.recent_button)
        self.recent_button.setMenu(self.recent_menu)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        for widget in (self.title_label, self.path_label, self.change_button, self.recent_button):
            layout.addWidget(widget)
        self.change_button.clicked.connect(lambda _c=False: self.changeRequested.emit())
        self.set_recent([])

    def set_paths(self, paths: Sequence[Path | str]) -> None:
        text = paths_text(paths) or NO_WORKSPACE
        self.path_label.setText(text)
        self.path_label.setToolTip(text)

    def set_recent(self, recent: Sequence[Sequence[Path]]) -> None:
        self.recent_menu.clear()
        self.recent_button.setEnabled(bool(recent))
        for entry in recent:
            action = self.recent_menu.addAction(paths_text(entry))
            action.triggered.connect(lambda _c=False, e=list(entry): self.recentChosen.emit(e))
