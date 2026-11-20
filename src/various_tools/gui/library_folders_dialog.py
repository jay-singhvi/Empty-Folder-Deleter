"""The "Library folders" dialog: which folders the Timeline and the Map look at.

Folders are saved in ``Settings.library_folders``. "Add the working folder" adds the folder(s)
of the app's working folder when the app has one (``state.workspace``, added in 0.6.0; this code
copes with it being missing).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from various_tools.gui.state import AppState
from various_tools.gui.widgets.path_picker import PathListEdit

log = logging.getLogger(__name__)


def workspace_paths(state: AppState) -> list[Path]:
    """The working folder(s) of the app as paths, or an empty list when there are none.

    ``state.workspace`` may be a path, a list of paths, a callable returning either, or an object
    with a ``paths`` (or ``path``) attribute. Anything else, or any error, means "none".
    """
    try:
        found: Any = getattr(state, "workspace", None)
        for name in ("paths", "path", "folders", "folder"):
            if found is not None and not isinstance(found, (str, Path, list, tuple)):
                inner: Any = getattr(found, name, None)
                if inner is not None:
                    found = inner
                    break
        if callable(found):
            found = found()
        if found is None:
            return []
        if isinstance(found, (str, Path)):
            return [Path(found)]
        values: Iterable[Any] = found
        return [Path(str(p)) for p in values if p]
    except Exception as exc:  # noqa: BLE001 - a half-built workspace must not break this dialog
        log.info("Could not read the working folder: %s", exc)
        return []


class LibraryFoldersDialog(QDialog):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("Library folders")
        self.resize(560, 380)
        intro = QLabel(
            "The folders whose photos and videos appear in the Timeline and on the Map. "
            "Add as many as you like. Nothing in them is changed."
        )
        intro.setWordWrap(True)
        self.folders = PathListEdit(self)
        self.folders.set_paths(str(p) for p in state.settings.library_folders)
        self.add_workspace_button = QPushButton("Add the working folder")
        self.add_workspace_button.clicked.connect(lambda _c=False: self.add_workspace())
        self.add_workspace_button.setEnabled(bool(workspace_paths(state)))
        if not self.add_workspace_button.isEnabled():
            self.add_workspace_button.setToolTip("There is no working folder set yet.")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.folders, 1)
        layout.addWidget(self.add_workspace_button)
        layout.addWidget(buttons)
        self.on_saved: Callable[[], None] | None = None

    def add_workspace(self) -> None:
        self.folders.add_paths(str(p) for p in workspace_paths(self.state))

    def chosen(self) -> list[Path]:
        return [Path(p) for p in self.folders.paths()]

    def accept(self) -> None:
        try:
            self.state.update_settings(library_folders=[str(p) for p in self.chosen()])
        except Exception as exc:  # noqa: BLE001 - a bad path must not crash the dialog
            log.warning("Could not save the library folders: %s", exc)
            self.add_workspace_button.setToolTip(f"Could not save the folders: {exc}")
            return
        super().accept()
        if self.on_saved is not None:
            self.on_saved()
