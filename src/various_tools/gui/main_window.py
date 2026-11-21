"""The main window: a searchable sidebar of tools by category, the pages, and the log panel.

Tool pages are created the first time they are opened, so start-up stays quick however many
tools are registered. History and Settings are always at the bottom of the sidebar.

Closing: no worker may outlive the window. While an apply or an undo runs, closing asks "A run is
in progress. Stop it and close?". On yes every job is told to stop and the window stays open
(disabled) until the last worker has finished, then closes; on no everything keeps running.
Scans and other read-only jobs are cancelled and waited for without a question.

Shell: a menu bar (File, View, Help), a toolbar with the working-folder chip, History and
Settings, and the Home page (the first page). The working folder lives in ``AppState``; every
tool page follows it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from various_tools import __version__
from various_tools.core.media.places import PLACES_ATTRIBUTION
from various_tools.core.plan import Plan
from various_tools.gui import shell
from various_tools.gui.history_page import HistoryPage
from various_tools.gui.home_page import HomePage
from various_tools.gui.library_model import library_model
from various_tools.gui.map_page import MapPage
from various_tools.gui.recipes_page import RecipesPage
from various_tools.gui.settings_page import SettingsPage
from various_tools.gui.state import AppState
from various_tools.gui.timeline_page import TimelinePage
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.widgets.log_panel import LogBridge, LogPanel
from various_tools.gui.widgets.workspace_bar import WorkspaceBar
from various_tools.gui.workers import active_workers, cancel_all
from various_tools.tools.base import CATEGORIES

log = logging.getLogger(__name__)

APP_TITLE = "Various Tools"
HOME_ID = "__home__"
HISTORY_ID = "__history__"
RECIPES_ID = "__recipes__"
SETTINGS_ID = "__settings__"
TIMELINE_ID = "__timeline__"
MAP_ID = "__map__"
# Pages that browse photos and videos (they never change files), above "App" in the sidebar.
MEDIA_SECTION = "Photos & videos"
_PAGE_ROLE = Qt.ItemDataRole.UserRole
CLOSE_QUESTION = "A run is in progress. Stop it and close?"
CLOSE_POLL_MS = 100
MEDIA_PAGES = (
    (TIMELINE_ID, "Timeline", "Browse your photos and videos by month."),
    (MAP_ID, "Map", "See where your photos and videos were taken."),
)
# (what it does, the keys) for the Help > Keyboard shortcuts window. The last three act in the
# results list of a tool page.
SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Choose working folder", "Ctrl+O"),
    ("Settings", "Ctrl+,"),
    ("Exit", "Ctrl+Q"),
    ("Show or hide the log", "Ctrl+L"),
    ("Find a tool (search the sidebar)", "Ctrl+K"),
    ("Select every row in the results list", "Ctrl+A"),
    ("Tick or untick the selected rows", "Space"),
    ("Filter the results list", "Ctrl+F"),
)


def _default_confirm(parent: QWidget) -> Callable[[str, str], bool]:
    def confirm(title: str, text: str) -> bool:
        answer = QMessageBox.question(
            parent,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    return confirm


def _default_inform(parent: QWidget) -> Callable[[str, str], None]:
    def inform(title: str, text: str) -> None:
        QMessageBox.information(parent, title, text)

    return inform


def _default_choose_folder(parent: QWidget) -> Callable[[], str | None]:
    def choose() -> str | None:
        return QFileDialog.getExistingDirectory(parent, "Choose the working folder") or None

    return choose


def _default_open_folder(path: Path) -> None:
    if hasattr(os, "startfile"):
        os.startfile(path)  # noqa: S606 (a folder the app owns, opened in File Explorer)


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
        # Tests replace this so no message box opens.
        self.confirm: Callable[[str, str], bool] = _default_confirm(self)
        self.inform: Callable[[str, str], None] = _default_inform(self)
        self.choose_folder: Callable[[], str | None] = _default_choose_folder(self)
        self.open_folder: Callable[[Path], None] = _default_open_folder
        self.shortcuts_dialog: QDialog | None = None
        self.closing = False  # a close was confirmed; waiting for the workers to finish
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(CLOSE_POLL_MS)
        self._close_timer.timeout.connect(self._close_when_idle)

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
        self.timeline_page: TimelinePage | None = None  # created on first use
        self.map_page: MapPage | None = None
        self.recipes_page: RecipesPage | None = None  # created on first use
        self.history_page = HistoryPage(state)
        self.settings_page = SettingsPage(state, shell_backend=shell_backend)
        self.home_page = HomePage(state, media_section=MEDIA_SECTION, media_pages=MEDIA_PAGES)
        self.welcome = self.home_page  # the first page (older name)
        for page in (self.home_page, self.history_page, self.settings_page):
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

        self._build_menus()
        self._build_toolbar()
        self.home_page.openPage.connect(self.show_page)
        self.home_page.chooseFolder.connect(self.choose_workspace)
        self.home_page.openHistory.connect(lambda: self.show_page(HISTORY_ID))
        state.on_workspace_changed(self._workspace_changed)
        self.search_edit.textChanged.connect(self.filter_tools)
        self.sidebar.currentItemChanged.connect(self._item_changed)
        self.history_page.runUndone.connect(self._run_undone)
        # The log is one of the "Show all settings" extras.
        self.log_panel.setVisible(state.show_all_settings())
        self.show_all_action.setChecked(state.show_all_settings())
        self.show_log_action.setChecked(state.show_all_settings())
        state.on_show_all_changed(self._show_all_changed)
        self.statusBar().showMessage("Ready")

    # ----- menus and toolbar ------------------------------------------------------------------

    def _action(
        self, text: str, slot: Callable[[], object], shortcut: str | None = None
    ) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        # Resolved when triggered, so tests can replace the method on the window.
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        self.choose_folder_action = self._action(
            "Choose working folder…", lambda: self.choose_workspace(), "Ctrl+O"
        )
        self.add_folder_action = self._action(
            "Add a folder to the working folder…", lambda: self.add_workspace_folder()
        )
        self.recent_menu = file_menu.addMenu("Recent folders")
        self.settings_action = self._action(
            "Settings…", lambda: self.show_page(SETTINGS_ID), "Ctrl+,"
        )
        self.exit_action = self._action("Exit", lambda: self.close(), "Ctrl+Q")
        file_menu.insertAction(self.recent_menu.menuAction(), self.choose_folder_action)
        file_menu.insertAction(self.recent_menu.menuAction(), self.add_folder_action)
        file_menu.addSeparator()
        file_menu.addAction(self.settings_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        view_menu = bar.addMenu("&View")
        self.show_all_action = QAction("Show all settings", self)
        self.show_all_action.setCheckable(True)
        self.show_all_action.setToolTip("Show every setting and control on every page.")
        self.show_all_action.triggered.connect(
            lambda checked=False: self.state.set_show_all_settings(bool(checked))
        )
        self.show_log_action = QAction("Show log", self)
        self.show_log_action.setCheckable(True)
        self.show_log_action.setShortcut(QKeySequence("Ctrl+L"))
        self.show_log_action.triggered.connect(
            lambda checked=False: self.log_panel.setVisible(bool(checked))
        )
        self.find_tool_action = self._action("Find a tool", lambda: self.focus_search(), "Ctrl+K")
        self.home_action = self._action("Home", lambda: self.show_page(HOME_ID))
        self.recipes_action = self._action("Recipes", lambda: self.show_page(RECIPES_ID))
        self.recipes_action.setToolTip("Saved lists of steps you can run one after the other.")
        view_menu.addAction(self.show_all_action)
        view_menu.addAction(self.show_log_action)
        view_menu.addAction(self.find_tool_action)
        view_menu.addSeparator()
        view_menu.addAction(self.home_action)
        view_menu.addAction(self.recipes_action)

        help_menu = bar.addMenu("&Help")
        self.shortcuts_action = self._action("Keyboard shortcuts", lambda: self.show_shortcuts())
        self.open_settings_folder_action = self._action(
            "Open settings folder", lambda: self.open_settings_folder()
        )
        self.about_action = self._action("About Various Tools", lambda: self.show_about())
        help_menu.addAction(self.shortcuts_action)
        help_menu.addAction(self.open_settings_folder_action)
        help_menu.addSeparator()
        help_menu.addAction(self.about_action)
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)

    def _build_toolbar(self) -> None:
        self.toolbar = QToolBar("Main")
        self.toolbar.setMovable(False)
        self.workspace_bar = WorkspaceBar()
        self.workspace_bar.set_paths(self.state.workspace())
        self.workspace_bar.set_recent(self.state.recent_workspaces())
        self.workspace_bar.changeRequested.connect(self.choose_workspace)
        self.workspace_bar.recentChosen.connect(self.state.set_workspace)
        self.toolbar.addWidget(self.workspace_bar)
        self.toolbar.addSeparator()
        self.history_action = self._action("History", lambda: self.show_page(HISTORY_ID))
        self.history_action.setToolTip("See what was applied, and undo it.")
        self.toolbar.addAction(self.history_action)
        self.toolbar.addAction(self.settings_action)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        recent = self.state.recent_workspaces()
        self.recent_menu.setEnabled(bool(recent))
        for entry in recent:
            action = self.recent_menu.addAction("; ".join(str(p) for p in entry))
            action.triggered.connect(lambda _c=False, e=list(entry): self.state.set_workspace(e))

    # ----- the working folder -----------------------------------------------------------------

    def choose_workspace(self) -> bool:
        """Ask for a folder and make it the working folder. False when nothing was chosen."""
        chosen = self.choose_folder()
        if not chosen:
            return False
        self.state.set_workspace([Path(chosen)])
        return True

    def add_workspace_folder(self) -> bool:
        """Ask for one more folder and add it to the working folder."""
        chosen = self.choose_folder()
        if not chosen:
            return False
        self.state.set_workspace([*self.state.workspace(), Path(chosen)])
        return True

    def _workspace_changed(self, paths: list[Path]) -> None:
        self.workspace_bar.set_paths(paths)
        self.workspace_bar.set_recent(self.state.recent_workspaces())
        self.home_page.refresh()

    # ----- menu commands ----------------------------------------------------------------------

    def focus_search(self) -> None:
        self.search_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_edit.selectAll()

    def shortcuts_text(self) -> str:
        return "\n".join(f"{keys}\t{what}" for what, keys in SHORTCUTS)

    def show_shortcuts(self) -> QDialog:
        """The list of every shortcut (a window that does not block the app)."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Keyboard shortcuts")
        layout = QVBoxLayout(dialog)
        label = QLabel(
            "<table cellspacing='6'>"
            + "".join(f"<tr><td><b>{keys}</b></td><td>{what}</td></tr>" for what, keys in SHORTCUTS)
            + "</table>"
        )
        layout.addWidget(label)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.shortcuts_dialog = dialog
        dialog.show()
        return dialog

    def open_settings_folder(self) -> None:
        self.open_folder(self.state.settings_path.parent)

    def show_about(self) -> None:
        self.inform(
            f"About {APP_TITLE}",
            f"{APP_TITLE} {__version__}\n\nOne app for tidying folders and photos. Every tool "
            "previews first and changes nothing until you Apply.\n\n"
            f"{PLACES_ATTRIBUTION}",
        )

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
        self._add_section(MEDIA_SECTION, ((TIMELINE_ID, "Timeline"), (MAP_ID, "Map")))
        other = QTreeWidgetItem(["App"])
        other.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = other.font(0)
        font.setBold(True)
        other.setFont(0, font)
        self.sidebar.addTopLevelItem(other)
        for page_id, label in (
            (RECIPES_ID, "Recipes"),
            (HISTORY_ID, "History"),
            (SETTINGS_ID, "Settings"),
        ):
            item = QTreeWidgetItem([label])
            item.setData(0, _PAGE_ROLE, page_id)
            other.addChild(item)
            self._tool_items[page_id] = item
        other.setExpanded(True)

    def _add_section(self, title: str, pages: tuple[tuple[str, str], ...]) -> None:
        header = QTreeWidgetItem([title])
        header.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = header.font(0)
        font.setBold(True)
        header.setFont(0, font)
        self.sidebar.addTopLevelItem(header)
        for page_id, label in pages:
            item = QTreeWidgetItem([label])
            item.setData(0, _PAGE_ROLE, page_id)
            header.addChild(item)
            self._tool_items[page_id] = item
        header.setExpanded(True)

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
            page.runUndone.connect(self._run_undone)
            page.busyChanged.connect(self._busy_changed)
            page.scanFinished.connect(self._scan_finished)
            page.clipboardCopied.connect(self._clipboard_copied)
            self.stack.addWidget(page)
            self.tool_pages[tool_id] = page
        return page

    def recipes(self) -> RecipesPage:
        """The Recipes page, created on first use and read again each time it is shown."""
        if self.recipes_page is None:
            page = RecipesPage(self.state)
            page.runApplied.connect(lambda _run: self.history_page.reload())
            page.runUndone.connect(self._run_undone)
            page.busyChanged.connect(self._busy_changed)
            page.scanFinished.connect(self._scan_finished)
            page.clipboardCopied.connect(self._clipboard_copied)
            self.stack.addWidget(page)
            self.recipes_page = page
        else:
            self.recipes_page.refresh()
        return self.recipes_page

    def show_page(self, page_id: str) -> QWidget:
        page: QWidget
        if page_id == HOME_ID:
            self.home_page.refresh()
            page = self.home_page
        elif page_id == HISTORY_ID:
            self.history_page.reload()
            page = self.history_page
        elif page_id == SETTINGS_ID:
            page = self.settings_page
        elif page_id == RECIPES_ID:
            page = self.recipes()
        elif page_id == TIMELINE_ID:
            if self.timeline_page is None:
                self.timeline_page = TimelinePage(self.state)
                self.timeline_page.showOnMap.connect(self.show_on_map)
                self.timeline_page.openTool.connect(self.open_tool_with_folders)
                self.stack.addWidget(self.timeline_page)
            page = self.timeline_page
        elif page_id == MAP_ID:
            if self.map_page is None:
                self.map_page = MapPage(self.state)
                self.stack.addWidget(self.map_page)
            page = self.map_page
        else:
            page = self.tool_page(page_id)
        self.stack.setCurrentWidget(page)
        item = self._tool_items.get(page_id)
        if self.sidebar.currentItem() is not item:
            # A page without a sidebar row (Home) clears the highlight, so the sidebar never
            # points at a page that is not shown.
            self.sidebar.blockSignals(True)
            if item is None:
                self.sidebar.setCurrentItem(None)  # type: ignore[arg-type]
                self.sidebar.clearSelection()
            else:
                self.sidebar.setCurrentItem(item)
            self.sidebar.blockSignals(False)
        return page

    def show_on_map(self, path: object) -> None:
        """Timeline's "Show on map": open the Map page, centred on that photo or video."""
        page = self.show_page(MAP_ID)
        if isinstance(page, MapPage):
            page.focus_item(Path(str(path)))

    def open_tool_with_folders(self, tool_id: str, folders: list[object]) -> ToolPage | None:
        """Open a tool on these folders (the Timeline's "Find duplicates…" on the library)."""
        page = self.open_tool(tool_id)
        if page is not None:
            page.set_folders([str(folder) for folder in folders])
        return page

    def _clipboard_copied(self, message: str) -> None:
        self.statusBar().showMessage(message, 5000)

    def _scan_finished(self, plan: object) -> None:
        """A Duplicate Finder scan is remembered by the photo library, so the Timeline can show
        those groups; the library ignores files it does not hold."""
        if isinstance(plan, Plan) and plan.tool == "duplicates":
            library_model(self.state).update_duplicates(plan)

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

    def _show_all_changed(self, on: bool) -> None:
        """The "Show all settings" switch changed on some page: every page follows."""
        self.log_panel.setVisible(on)
        for action in (self.show_all_action, self.show_log_action):
            action.setChecked(on)
        for page in self.tool_pages.values():
            page.set_show_all(on)

    def _busy_changed(self, busy: bool) -> None:
        if not self.closing:
            self.statusBar().showMessage("Working…" if busy else "Ready")

    def _run_undone(self, run_id: int) -> None:
        """After any undo (History or a tool page), every page shows the run's new state."""
        if self.closing:
            return
        self.history_page.reload()
        for page in self.tool_pages.values():
            page.refresh_undo(run_id)

    # ----- closing ----------------------------------------------------------------------------

    def run_in_progress(self) -> bool:
        """An apply or an undo is running (a job that changes files and the journal)."""
        return any(worker.writes for worker in active_workers())

    def closeEvent(self, event: QCloseEvent) -> None:
        if not active_workers():
            self._really_close(event)
            return
        event.ignore()
        if self.closing:
            return  # already stopping; the window closes when the last worker is done
        if self.run_in_progress() and not self.confirm("Close Various Tools", CLOSE_QUESTION):
            return  # keep running
        self._stop_everything()

    def _stop_everything(self) -> None:
        self.closing = True
        for page in self.tool_pages.values():
            page.shutdown()
        self.history_page.shutdown()
        if self.recipes_page is not None:
            self.recipes_page.shutdown()
        for media_page in (self.timeline_page, self.map_page):
            if media_page is not None:
                media_page.shutdown()
        cancel_all()
        central = self.centralWidget()
        if central is not None:
            central.setEnabled(False)
        self.statusBar().showMessage(
            "Stopping: the window closes as soon as the current step has finished…"
        )
        self._close_timer.start()

    def _close_when_idle(self) -> None:
        if active_workers():
            cancel_all()  # a job started by a late signal stops too
            return
        self._close_timer.stop()
        self.close()

    def _really_close(self, event: QCloseEvent) -> None:
        self._close_timer.stop()
        logging.getLogger("various_tools").removeHandler(self.log_bridge)
        super().closeEvent(event)
