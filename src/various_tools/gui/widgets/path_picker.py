"""Folder and file pickers: a text box with a Browse button, and a list of folders.

Both widgets accept folders (or, for a file picker, files) dragged in from Explorer. The drop
logic lives in ``accept_mime`` so tests can call it with a hand-built ``QMimeData`` instead of
synthesizing real drag events. Dragging over a picker never touches the disk (a network path
could block); only the drop checks that the paths are folders or files.

``changed`` is emitted when the person edits, browses, drops or removes something. Setting the
value from code (``set_text`` / ``set_paths``) never emits it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Literal

from PySide6.QtCore import QMimeData, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

PathMode = Literal["folder", "file", "save_file"]


def local_paths(mime: QMimeData) -> list[str]:
    """The local file-system paths in a drop, in order. Web links and the like are skipped."""
    if not mime.hasUrls():
        return []
    return [url.toLocalFile() for url in mime.urls() if url.isLocalFile() and url.toLocalFile()]


def drag_has_local_paths(mime: QMimeData) -> bool:
    """Whether a drag carries local paths. Looks at the URLs only, never at the disk, so a
    drag over a slow network path cannot freeze the window. The drop does the real check."""
    return bool(local_paths(mime))


def _path_key(path: str) -> str:
    """A comparison key: case-insensitive on Windows, slash-style and trailing-slash neutral."""
    return os.path.normcase(os.path.normpath(path))


class PathEdit(QWidget):
    """One folder or file: a text box plus a Browse button, with drag-and-drop."""

    changed = Signal()

    def __init__(
        self,
        mode: PathMode = "folder",
        placeholder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode: PathMode = mode
        self._silent = False
        self.line_edit = QLineEdit(self)
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setClearButtonEnabled(True)
        # The drop lands on this widget, not on the text box (which would insert the path as text).
        self.line_edit.setAcceptDrops(False)
        self.browse_button = QPushButton("Browse…", self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.line_edit, 1)
        layout.addWidget(self.browse_button)
        self.setAcceptDrops(True)
        self.setFocusProxy(self.line_edit)
        self.line_edit.textChanged.connect(self._on_text_changed)
        self.browse_button.clicked.connect(self.browse)

    def text(self) -> str:
        return self.line_edit.text()

    def set_text(self, text: str) -> None:
        """Show ``text`` without emitting ``changed``."""
        self._silent = True
        try:
            self.line_edit.setText(text)
        finally:
            self._silent = False

    def browse(self) -> None:
        """Open the matching file dialog and use the chosen path, if any."""
        current = self.text().strip()
        if self.mode == "folder":
            start = current if os.path.isdir(current) else ""
            chosen = QFileDialog.getExistingDirectory(self, "Choose a folder", start)
        else:
            start = current if os.path.isdir(os.path.dirname(current)) else ""
            if self.mode == "file":
                chosen, _ = QFileDialog.getOpenFileName(self, "Choose a file", start)
            else:
                chosen, _ = QFileDialog.getSaveFileName(self, "Save as", start)
        if chosen:
            self.line_edit.setText(os.path.normpath(chosen))

    def accept_mime(self, mime: QMimeData) -> bool:
        """Use the first dropped path that fits this picker. False if none does."""
        for path in self._matching(mime):
            self.line_edit.setText(os.path.normpath(path))
            return True
        return False

    def _matching(self, mime: QMimeData) -> list[str]:
        check = os.path.isdir if self.mode == "folder" else os.path.isfile
        return [path for path in local_paths(mime) if check(path)]

    def _on_text_changed(self, _text: str) -> None:
        if not self._silent:
            self.changed.emit()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt name)
        # No disk access here (it could block on a network path): local paths are enough.
        if drag_has_local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt name)
        if self.accept_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()


class PathListEdit(QWidget):
    """A list of folders with Add and Remove buttons, and drag-and-drop of several folders."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list_widget = QListWidget(self)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.setMinimumHeight(70)
        self.list_widget.setAcceptDrops(False)
        self.add_button = QPushButton("Add…", self)
        self.remove_button = QPushButton("Remove", self)
        buttons = QVBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(buttons)
        self.setAcceptDrops(True)
        self.add_button.clicked.connect(self.browse)
        self.remove_button.clicked.connect(self.remove_selected)

    def paths(self) -> list[str]:
        return [self.list_widget.item(row).text() for row in range(self.list_widget.count())]

    def set_paths(self, paths: Iterable[str]) -> None:
        """Replace the list (duplicates dropped) without emitting ``changed``."""
        self.list_widget.clear()
        self._append(paths)

    def add_paths(self, paths: Iterable[str]) -> None:
        """Add folders that are not already listed; emits ``changed`` if any were added."""
        if self._append(paths):
            self.changed.emit()

    def remove_selected(self) -> None:
        selected = self.list_widget.selectedItems()
        rows = sorted((self.list_widget.row(item) for item in selected), reverse=True)
        for row in rows:
            self.list_widget.takeItem(row)
        if rows:
            self.changed.emit()

    def browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Add a folder")
        if chosen:
            self.add_paths([os.path.normpath(chosen)])

    def accept_mime(self, mime: QMimeData) -> bool:
        """Add every dropped folder. False if the drop held no folder."""
        folders = self._folders(mime)
        if not folders:
            return False
        self.add_paths(os.path.normpath(folder) for folder in folders)
        return True

    @staticmethod
    def _folders(mime: QMimeData) -> list[str]:
        return [path for path in local_paths(mime) if os.path.isdir(path)]

    def _append(self, paths: Iterable[str]) -> bool:
        known = {_path_key(path) for path in self.paths()}
        added = False
        for raw in paths:
            path = str(raw).strip()
            if not path or _path_key(path) in known:
                continue
            known.add(_path_key(path))
            self.list_widget.addItem(path)
            added = True
        return added

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt name)
        # No disk access here (it could block on a network path); the drop checks the folders.
        if drag_has_local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt name)
        if self.accept_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()
