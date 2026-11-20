"""The Home page: the working folder, the tools by category, and the last runs.

Plain words only. A card opens a tool (or the Timeline and Map pages); the runs come from the
history journal (read-only here).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from various_tools.gui.state import AppState
from various_tools.gui.widgets.workspace_bar import paths_text
from various_tools.gui.wording import NO_WORKSPACE
from various_tools.tools.base import CATEGORIES

log = logging.getLogger(__name__)

RECENT_RUNS = 5
# (page id, title, one line) for pages that are not tools; they sit under ``media_section``.
PageCard = tuple[str, str, str]


def run_line(state: AppState, tool: str, started: str, status: str) -> str:
    """One finished run in plain words, for example ``Empty Folders · done · 2026-11-20 10:00``."""
    known = state.tools.get(tool)
    name = known.name if known is not None else tool
    return f"{name}  ·  {status}  ·  {started[:16].replace('T', ' ')}"


class HomePage(QWidget):
    """Emits ``openPage(id)`` (a tool id or a media page id), ``chooseFolder`` and
    ``openHistory``."""

    openPage = Signal(str)
    chooseFolder = Signal()
    openHistory = Signal()

    def __init__(
        self,
        state: AppState,
        *,
        media_section: str = "Photos & videos",
        media_pages: Sequence[PageCard] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.tool_cards: dict[str, QToolButton] = {}
        self.run_labels: list[QLabel] = []

        self.workspace_label = QLabel()
        self.workspace_label.setWordWrap(True)
        self.workspace_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.choose_button = QPushButton("Choose working folder…")
        self.choose_button.setToolTip("The folder every tool looks in by default (Ctrl+O).")
        self.prompt_label = QLabel(
            "Start by choosing a working folder. Every tool looks there unless you pick other "
            "folders for it. Every tool previews first: looking changes nothing, and only "
            "Apply does, after asking you."
        )
        self.prompt_label.setWordWrap(True)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(QLabel("<h2>Various Tools</h2>"))
        layout.addWidget(self.prompt_label)
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(card)
        card_layout.addWidget(QLabel("<b>Working folder</b>"))
        card_layout.addWidget(self.workspace_label)
        card_layout.addWidget(self.choose_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)

        by_category: dict[str, list[PageCard]] = {}
        for tool_id, tool in state.tools.items():
            by_category.setdefault(tool.category, []).append((tool_id, tool.name, tool.description))
        order = [c for c in CATEGORIES if c in by_category]
        order += sorted(c for c in by_category if c not in CATEGORIES)
        if media_pages:
            by_category.setdefault(media_section, [])
            if media_section not in order:
                order.append(media_section)
            by_category[media_section] += list(media_pages)
        for category in order:
            layout.addWidget(QLabel(f"<h3>{category}</h3>"))
            for page_id, name, description in sorted(
                by_category[category], key=lambda c: c[1].casefold()
            ):
                button = QToolButton()
                button.setText(f"{name}\n{description}")
                button.setToolTip(description)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
                button.setAutoRaise(True)
                button.setStyleSheet("QToolButton { text-align: left; padding: 6px; }")
                button.clicked.connect(lambda _c=False, p=page_id: self.openPage.emit(p))
                self.tool_cards[page_id] = button
                layout.addWidget(button)

        layout.addWidget(QLabel("<h3>Last runs</h3>"))
        self.runs_box = QVBoxLayout()
        layout.addLayout(self.runs_box)
        self.history_button = QPushButton("Open History")
        layout.addWidget(self.history_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.choose_button.clicked.connect(lambda _c=False: self.chooseFolder.emit())
        self.history_button.clicked.connect(lambda _c=False: self.openHistory.emit())
        self.refresh()

    def refresh(self) -> None:
        """Re-read the working folder and the last runs."""
        paths = self.state.workspace()
        self.workspace_label.setText(paths_text(paths) if paths else NO_WORKSPACE.capitalize())
        self.prompt_label.setVisible(not paths)
        self.choose_button.setText("Change…" if paths else "Choose working folder…")
        for label in self.run_labels:
            self.runs_box.removeWidget(label)
            label.deleteLater()
        self.run_labels = []
        for text in self._recent_run_lines() or ["Nothing has been applied yet."]:
            label = QLabel(text)
            self.runs_box.addWidget(label)
            self.run_labels.append(label)

    def _recent_run_lines(self) -> list[str]:
        journal = self.state.open_journal()
        if journal is None:
            return []
        try:
            return [
                run_line(self.state, r.tool, r.started, r.status) for r in journal.runs(RECENT_RUNS)
            ]
        except Exception:  # an unreadable journal must not break the home page
            log.exception("Could not read the last runs")
            return []
        finally:
            journal.close()
