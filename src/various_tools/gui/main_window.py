"""The main window: a searchable sidebar of tools by category, the pages, and the log panel.

Tool pages are created the first time they are opened, so start-up stays quick however many
tools are registered. History and Settings are always at the bottom of the sidebar.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from various_tools import __version__
from various_tools.gui import shell
from various_tools.gui.history_page import HistoryPage
from various_tools.gui.settings_page import SettingsPage
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.widgets.log_panel import LogBridge, LogPanel
from various_tools.tools.base import CATEGORIES

log = logging.getLogger(__name__)

APP_TITLE = "Various Tools"
HISTORY_ID = "__history__"
SETTINGS_ID = "__settings__"
_PAGE_ROLE = Qt.ItemDataRole.UserRole


class MainWindow(QMainWindow):
    def __init__(
        self,
        state: AppState,
        *,
        shell_backend: shell.RegistryBackend | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.setWindowTitle(f"{APP_TITLE} {__version__}")
        self.resize(1200, 820)

        # ----- sidebar ---------------------------------------------------------------------
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search tools")
        self.search_edit.setClearButtonEnabled(True)
        self.sidebar = QTreeWidget()
        self.sidebar.setHeaderHidden(True)
        self.sidebar.setRootIsDecorated(False)
        self._tool_items: dict[str, QTreeWidgetItem] = {}
        self._build_sidebar()
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(6, 6, 0, 6)
        side_layout.addWidget(self.search_edit)
        side_layout.addWidget(self.sidebar, 1)
        side.setMinimumWidth(200)
        side.setMaximumWidth(320)

        # ----- pages -----------------------------------------------------------------------
        self.stack = QStackedWidget()
        self.tool_pages: dict[str, ToolPage] = {}
        self.history_page = HistoryPage(state)
        self.settings_page = SettingsPage(state, shell_backend=shell_backend)
        self.welcome = QLabel(
            f"<h2>{APP_TITLE}</h2><p>Pick a tool on the left. Every tool previews first: "
            "Scan changes nothing, and only Apply does, after asking you.</p>"
        )
        self.welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.welcome.setWordWrap(True)
        for page in (self.welcome, self.history_page, self.settings_page):
            self.stack.addWidget(page)

        self.log_panel = LogPanel()
        self.log_bridge = LogBridge()
        self.log_bridge.emitter.record.connect(self.log_panel.append)
        logging.getLogger("various_tools").addHandler(self.log_bridge)
        logging.getLogger("various_tools").setLevel(logging.INFO)

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.stack)
        right.addWidget(self.log_panel)
        right.setStretchFactor(0, 5)
        right.setStretchFactor(1, 1)
        right.setCollapsible(0, False)
        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(side)
        main.addWidget(right)
        main.setStretchFactor(1, 1)
        self.setCentralWidget(main)

        self.search_edit.textChanged.connect(self.filter_tools)
        self.sidebar.currentItemChanged.connect(self._item_changed)
        self.history_page.runUndone.connect(lambda _run: None)
        self.statusBar().showMessage("Ready")

    # ----- sidebar ----------------------------------------------------------------------------

    def _build_sidebar(self) -> None:
        by_category: dict[str, list[str]] = {}
        for tool_id, tool in self.state.tools.items():
            by_category.setdefault(tool.category, []).append(tool_id)
        order = [c for c in CATEGORIES if c in by_category]
        order += sorted(c for c in by_category if c not in CATEGORIES)
        for category in order:
            header = QTreeWidgetItem([category])
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = header.font(0)
            font.setBold(True)
            header.setFont(0, font)
            self.sidebar.addTopLevelItem(header)
            tools = sorted(by_category[category], key=lambda t: self.state.tools[t].name.casefold())
            for tool_id in tools:
                tool = self.state.tools[tool_id]
                item = QTreeWidgetItem([tool.name])
                item.setData(0, _PAGE_ROLE, tool_id)
                item.setToolTip(0, tool.description)
                header.addChild(item)
                self._tool_items[tool_id] = item
            header.setExpanded(True)
        other = QTreeWidgetItem(["App"])
        other.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = other.font(0)
        font.setBold(True)
        other.setFont(0, font)
        self.sidebar.addTopLevelItem(other)
        for page_id, label in ((HISTORY_ID, "History"), (SETTINGS_ID, "Settings")):
            item = QTreeWidgetItem([label])
            item.setData(0, _PAGE_ROLE, page_id)
            other.addChild(item)
            self._tool_items[page_id] = item
        other.setExpanded(True)

    def filter_tools(self, text: str) -> None:
        """Hide tools whose name, id or description does not contain ``text``."""
        needle = text.strip().casefold()
        for index in range(self.sidebar.topLevelItemCount()):
            header = self.sidebar.topLevelItem(index)
            if header is None:
                continue
            any_visible = False
            for child_index in range(header.childCount()):
                child = header.child(child_index)
                if child is None:
                    continue
                page_id = str(child.data(0, _PAGE_ROLE))
                tool = self.state.tools.get(page_id)
                haystack = child.text(0)
                if tool is not None:
                    haystack += f" {tool.id} {tool.description}"
                visible = not needle or needle in haystack.casefold()
                child.setHidden(not visible)
                any_visible = any_visible or visible
            header.setHidden(not any_visible)

    def visible_tool_ids(self) -> list[str]:
        return [
            t
            for t, item in self._tool_items.items()
            if t in self.state.tools and not item.isHidden()
        ]

    def _item_changed(self, current: QTreeWidgetItem | None, _previous: object) -> None:
        if current is None:
            return
        page_id = current.data(0, _PAGE_ROLE)
        if page_id:
            self.show_page(str(page_id))

    # ----- pages ------------------------------------------------------------------------------

    def tool_page(self, tool_id: str) -> ToolPage:
        """The page for a tool, created on first use."""
        page = self.tool_pages.get(tool_id)
        if page is None:
            page = ToolPage(self.state, self.state.tools[tool_id])
            page.runApplied.connect(lambda _run: self.history_page.reload())
            page.busyChanged.connect(self._busy_changed)
            self.stack.addWidget(page)
            self.tool_pages[tool_id] = page
        return page

    def show_page(self, page_id: str) -> QWidget:
        page: QWidget
        if page_id == HISTORY_ID:
            self.history_page.reload()
            page = self.history_page
        elif page_id == SETTINGS_ID:
            page = self.settings_page
        else:
            page = self.tool_page(page_id)
        self.stack.setCurrentWidget(page)
        item = self._tool_items.get(page_id)
        if item is not None and self.sidebar.currentItem() is not item:
            self.sidebar.blockSignals(True)
            self.sidebar.setCurrentItem(item)
            self.sidebar.blockSignals(False)
        return page

    def open_tool(self, tool_id: str, folder: str | None = None) -> ToolPage | None:
        """Show a tool's page and optionally put ``folder`` in its first folder field."""
        if tool_id not in self.state.tools:
            self.statusBar().showMessage(f"Unknown tool: {tool_id}")
            log.warning("Unknown tool requested: %s", tool_id)
            return None
        page = self.tool_page(tool_id)
        self.show_page(tool_id)
        if folder:
            page.set_folder(folder)
        return page

    def _busy_changed(self, busy: bool) -> None:
        self.statusBar().showMessage("Working…" if busy else "Ready")

    def closeEvent(self, event: QCloseEvent) -> None:
        for page in self.tool_pages.values():
            page.shutdown()
        logging.getLogger("various_tools").removeHandler(self.log_bridge)
        super().closeEvent(event)
