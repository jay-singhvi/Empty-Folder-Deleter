"""A collapsible log panel fed by Python logging, from any thread.

:class:`LogBridge` is a ``logging.Handler`` that forwards each record through a Qt signal, so a
record logged on a worker thread is appended on the UI thread. :class:`LogPanel` shows them.
"""

from __future__ import annotations

import contextlib
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

MAX_LINES = 5000
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _Emitter(QObject):
    record = Signal(str, int)


class LogBridge(logging.Handler):
    """Forwards formatted log records to the UI thread through a queued signal."""

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.emitter = _Emitter()
        self.setFormatter(logging.Formatter(LOG_FORMAT, "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        # RuntimeError: the emitter was deleted while the app shut down.
        with contextlib.suppress(RuntimeError):
            self.emitter.record.emit(self.format(record), record.levelno)


class LogPanel(QWidget):
    """A header button that folds the log away, a read-only text view and a Clear button."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.toggle = QToolButton()
        self.toggle.setText("Log")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setToolButtonStyle(self.toggle.toolButtonStyle().ToolButtonTextBesideIcon)
        self.toggle.setArrowType(self.toggle.arrowType().RightArrow)
        self.toggle.setToolTip("Show or hide the log of what the app is doing.")
        self.clear_button = QPushButton("Clear")
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setMinimumHeight(120)

        header = QHBoxLayout()
        header.addWidget(self.toggle)
        header.addStretch(1)
        header.addWidget(self.clear_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.view)

        self.toggle.toggled.connect(self.set_expanded)
        self.clear_button.clicked.connect(self.view.clear)
        self.set_expanded(False)

    def set_expanded(self, expanded: bool) -> None:
        self.toggle.setChecked(expanded)
        arrow = self.toggle.arrowType()
        self.toggle.setArrowType(arrow.DownArrow if expanded else arrow.RightArrow)
        self.view.setVisible(expanded)
        self.clear_button.setVisible(expanded)

    def append(self, text: str, level: int = logging.INFO) -> None:
        self.view.appendPlainText(text)
        if level >= logging.ERROR and not self.toggle.isChecked():
            self.set_expanded(True)

    def text(self) -> str:
        return self.view.toPlainText()
