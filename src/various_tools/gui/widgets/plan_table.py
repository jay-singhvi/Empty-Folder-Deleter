"""The plan table: every planned action with a check box, grouped, sortable and filterable.

Two views, switched by the person for any plan (the tool page picks the first one from the tool's
optional ``plan_view`` attribute):

* **Grouped**: duplicate plans group their actions under a "Keeps <full path>" row, with a
  "Kept copy" column on each duplicate, so two kept files with the same name can be told
  apart. Other plans with a ``group_id`` show the id; plans without groups are a flat list.
* **Folder tree**: the folders under each plan root, with every action at its path. A folder row's
  tri-state check box covers everything inside it (plan 3.1).

Toggling a check box sets
``PlannedAction.selected`` (a per-tool hook may do more, see ``SELECTION_HOOKS``). The table
never touches the disk: sizes are computed by the caller in a worker and passed in.

With a text or kind filter active, ticking a group or folder row changes only the rows below it
that the filter shows, never the hidden ones; the row's tri-state still counts every row below
it, shown or not. :meth:`PlanTable.set_shown_checked` ("Select all shown") works the same way.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.gui import reveal
from various_tools.gui.plan_edit import human_size
from various_tools.gui.wording import kind_label

ALL_KINDS = "All kinds"
SHOW_ALL = "all"
SHOW_TICKED = "ticked"
SHOW_UNTICKED = "unticked"
SHOW_LABELS: dict[str, str] = {
    SHOW_ALL: "All",
    SHOW_TICKED: "Ticked",
    SHOW_UNTICKED: "Not ticked",
}
DEFAULT_TICK_FIRST = 100
EM_DASH = "—"
SORT_ROLE = int(Qt.ItemDataRole.UserRole)

SelectionHook = Callable[[Plan, PlannedAction, bool], None]
AnyIndex = QModelIndex | QPersistentModelIndex


def _empty_folders_hook(plan: Plan, action: PlannedAction, checked: bool) -> None:
    """Empty folders: unchecking a folder also unchecks its parents and their junk files."""
    if action.src is None:
        action.selected = checked
        return
    from various_tools.tools.empty_folders import set_folder_selected

    set_folder_selected(plan, action.src, checked)


# The only per-tool code in the table. A tool id maps to what a check-box click should do.
SELECTION_HOOKS: dict[str, SelectionHook] = {"empty_folders": _empty_folders_hook}


# Column titles, in plain words (exports and the CLI keep their own names).
KIND = "What happens"
FILE = "File"
GOES_TO = "Goes to"
KEPT = "Kept copy"
WHY = "Why"
SIZE = "Size"

GROUPED = "grouped"
FOLDER_TREE = "tree"
VIEW_LABELS: dict[str, str] = {GROUPED: "Grouped", FOLDER_TREE: "Folder tree"}


class Node:
    """One row: a group or folder (``action`` is None) or one planned action.

    In the folder tree an action whose path is a folder can have children too, and ``path`` is
    the folder or file the row stands for. ``key`` identifies a row with children across
    reloads, so expanded rows stay expanded.
    """

    __slots__ = ("action", "children", "index", "key", "label", "parent", "path", "row")

    def __init__(
        self,
        parent: Node | None,
        action: PlannedAction | None = None,
        index: int = -1,
        label: str = "",
        path: Path | None = None,
    ) -> None:
        self.parent = parent
        self.action = action
        self.index = index  # position in plan.actions
        self.label = label  # a group's id, or a tree row's display name
        self.path = path  # folder tree only
        self.key = f"g:{label}" if path is None else f"p:{_path_key(path)}"
        self.children: list[Node] = []
        self.row = 0


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path))).casefold()


def _action_path(action: PlannedAction) -> Path | None:
    return action.src if action.src is not None else action.dst


def _walk(nodes: Sequence[Node]) -> list[Node]:
    """Every node in ``nodes`` and below them, parents before children."""
    result: list[Node] = []
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        result.append(node)
        stack.extend(reversed(node.children))
    return result


def _subtree_actions(node: Node) -> list[Node]:
    """The action rows at and below ``node``."""
    return [n for n in _walk([node]) if n.action is not None]


def _name(path: Path | None) -> str:
    if path is None:
        return ""
    return path.name or str(path)


def _dash(text: str) -> str:
    return text or EM_DASH


def _is_under(path: Path, root: Path) -> bool:
    """``path`` is ``root`` or inside it (compared as Windows compares names)."""
    key, base = _path_key(path), _path_key(root)
    return key == base or key.startswith(base.rstrip("\\/") + os.sep)


class PlanModel(QAbstractItemModel):
    """The tree model over a plan. Rebuilt in one go with a model reset, never row by row."""

    checksChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._plan: Plan | None = None
        self._tool_id = ""
        self._sizes: list[int | None] = []
        self.top: list[Node] = []
        self._by_index: list[Node] = []
        self._rename_layout = False
        self._editable = True
        self._mode = GROUPED
        self._columns = [KIND, FILE, GOES_TO, WHY, SIZE]
        self._shown: Callable[[Node], bool] | None = None

    # ----- loading ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    def load(
        self,
        plan: Plan | None,
        tool_id: str,
        sizes: Sequence[int | None] | None,
        mode: str | None = None,
    ) -> None:
        self.beginResetModel()
        self._plan = plan
        self._tool_id = tool_id
        if mode is not None:
            self._mode = mode
        actions = plan.actions if plan is not None else []
        self._sizes = list(sizes)[: len(actions)] if sizes is not None else []
        self._sizes += [None] * (len(actions) - len(self._sizes))
        self._rename_layout = (
            self._mode == GROUPED
            and bool(actions)
            and all(a.kind is ActionKind.RENAME for a in actions)
        )
        if self._mode == FOLDER_TREE:
            self._columns = ["Name", KIND, GOES_TO, WHY, SIZE]
        elif self._rename_layout:
            self._columns = [KIND, "Folder", "Old name", "New name", WHY, SIZE]
        elif self.is_duplicates():
            self._columns = [KIND, FILE, GOES_TO, KEPT, WHY, SIZE]
        else:
            self._columns = [KIND, FILE, GOES_TO, WHY, SIZE]
        self.top = []
        self._by_index = []
        if self._mode == FOLDER_TREE and plan is not None:
            self._build_tree(plan)
        else:
            self._build_groups(actions)
        self.endResetModel()

    def _build_groups(self, actions: Sequence[PlannedAction]) -> None:
        groups: dict[str, Node] = {}
        for i, action in enumerate(actions):
            if action.group_id is None:
                node = Node(None, action, i)
                node.row = len(self.top)
                self.top.append(node)
            else:
                group = groups.get(action.group_id)
                if group is None:
                    group = Node(None, None, -1, action.group_id)
                    group.row = len(self.top)
                    self.top.append(group)
                    groups[action.group_id] = group
                node = Node(group, action, i)
                node.row = len(group.children)
                group.children.append(node)
            self._by_index.append(node)

    def _build_tree(self, plan: Plan) -> None:
        """Folders under each plan root as rows, actions at their paths (plan 3.1).

        An action whose path is a folder of other actions holds them as children. An action
        with no path, or outside every root, is a row at the top level.
        """
        roots = list(plan.roots)
        root_keys = {_path_key(r): r for r in roots}
        holders: dict[str, Node] = {}  # path key -> the row that holds that folder's children
        nodes: list[Node] = []
        for i, action in enumerate(plan.actions):
            path = _action_path(action)
            node = Node(None, action, i, _name(path), path)
            nodes.append(node)
            if path is not None:
                holders.setdefault(_path_key(path), node)
        self._by_index = nodes
        top_roots: dict[str, Node] = {}
        loose: list[Node] = []

        def inside_root(path: Path) -> bool:
            return any(_is_under(path, root) for root in roots)

        def holder(folder: Path) -> Node | None:
            key = _path_key(folder)
            found = holders.get(key)
            if found is not None:
                return found
            if key in root_keys:
                node = Node(None, None, -1, str(root_keys[key]), root_keys[key])
                top_roots[key] = node
            elif inside_root(folder) and folder.parent != folder:
                node = Node(None, None, -1, _name(folder), folder)
                parent = holder(folder.parent)
                if parent is None:
                    return None
                node.parent = parent
                parent.children.append(node)
            else:
                return None
            holders[key] = node
            return node

        for node in nodes:
            path = node.path
            parent: Node | None = None
            if path is not None and inside_root(path) and _path_key(path) not in root_keys:
                parent = holder(path.parent)
            if parent is None or parent is node:
                loose.append(node)
                continue
            node.parent = parent
            parent.children.append(node)

        ordered_roots = [top_roots[k] for k in (_path_key(r) for r in roots) if k in top_roots]
        self.top = ordered_roots + loose
        for row, node in enumerate(self.top):
            node.row = row
            node.parent = None
        for node in _walk(self.top):
            node.children.sort(key=lambda n: (not n.children, n.label.casefold(), n.index))
            for row, child in enumerate(node.children):
                child.row = row

    @property
    def plan(self) -> Plan | None:
        return self._plan

    @property
    def editable(self) -> bool:
        return self._editable

    def set_editable(self, editable: bool) -> None:
        """Allow or forbid check-box changes (forbidden while an apply runs)."""
        if editable == self._editable:
            return
        self._editable = editable
        if self.top:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.top) - 1, 0))

    @property
    def tool_id(self) -> str:
        return self._tool_id

    def node_at(self, index: AnyIndex) -> Node | None:
        if not index.isValid():
            return None
        node = index.internalPointer()
        return node if isinstance(node, Node) else None

    def node_for_action(self, action_index: int) -> Node | None:
        if 0 <= action_index < len(self._by_index):
            return self._by_index[action_index]
        return None

    def index_of(self, node: Node) -> QModelIndex:
        return self.createIndex(node.row, 0, node)

    def kinds(self) -> list[str]:
        seen: dict[str, None] = {}
        for node in self._by_index:
            if node.action is not None:
                seen.setdefault(str(node.action.kind), None)
        return list(seen)

    def is_duplicates(self) -> bool:
        """The plan comes from the duplicate finder (its group ids are the kept files)."""
        plan_tool = self._plan.tool if self._plan is not None else ""
        return self._tool_id == "duplicates" or plan_tool == "duplicates"

    def group_label(self, node: Node) -> str:
        if node.path is not None:
            return node.label
        if self.is_duplicates():
            return f"Keeps {node.label}"
        return node.label

    def all_nodes(self) -> list[Node]:
        return _walk(self.top)

    # ----- sizes and totals -------------------------------------------------------------

    def size_of(self, node: Node) -> int | None:
        if node.action is not None and not node.children:
            return self._sizes[node.index]
        values = [
            s for s in (self._sizes[n.index] for n in _subtree_actions(node)) if s is not None
        ]
        return sum(values) if values else None

    def sizes(self) -> list[int | None]:
        return list(self._sizes)

    def totals_text(self) -> str:
        if self._plan is None:
            return "No plan"
        actions = self._plan.actions
        selected = [i for i, a in enumerate(actions) if a.selected]
        text = f"{len(actions)} actions, {len(selected)} selected"
        if any(s is not None for s in self._sizes):
            known = sum(s for s in (self._sizes[i] for i in selected) if s is not None)
            text += f", {human_size(known)} selected"
        return text

    def status_text(self) -> str:
        """ "2,113 found · 150 ticked (4.4 GB)": what the list holds and what is ticked."""
        if self._plan is None:
            return "No plan"
        actions = self._plan.actions
        ticked = [i for i, a in enumerate(actions) if a.selected]
        text = f"{len(actions):,} found · {len(ticked):,} ticked"
        if any(s is not None for s in self._sizes):
            known = sum(s for s in (self._sizes[i] for i in ticked) if s is not None)
            text += f" ({human_size(known)})"
        return text

    # ----- check states -----------------------------------------------------------------

    def _group_state(self, node: Node) -> Qt.CheckState:
        """A row with children: checked, unchecked or partly, from every action below it."""
        actions = [n.action for n in _subtree_actions(node) if n.action is not None]
        selected = sum(1 for a in actions if a.selected)
        if selected == 0:
            return Qt.CheckState.Unchecked
        if selected == len(actions):
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked

    def set_shown_filter(self, shown: Callable[[Node], bool] | None) -> None:
        """``shown(node)`` says whether the view's filter shows a row; None means all rows.

        Ticking a group or folder row changes only the rows below it that are shown, so a
        filter never lets a click change rows the person cannot see.
        """
        self._shown = shown

    def set_node_checked(self, node: Node, checked: bool) -> None:
        """Check or uncheck one row, as a user click does. A group or folder row sets every
        action below it that the filter shows (and its own action, in the folder tree). Each
        action goes through the tool's selection hook, the same in both views.

        Does nothing while the table is not editable.
        """
        shown = self._shown
        targets = [n for n in _subtree_actions(node) if n is node or shown is None or shown(n)]
        self.set_actions_checked(targets, checked)

    def _apply_checked(self, targets: Iterable[Node], checked: bool) -> None:
        plan = self._plan
        if plan is None:
            return
        hook = SELECTION_HOOKS.get(self._tool_id)
        for target in targets:
            action = target.action
            if action is None:
                continue
            if hook is not None:
                hook(plan, action, checked)
            else:
                action.selected = checked

    def set_actions_checked(self, targets: Iterable[Node], checked: bool) -> None:
        """Check or uncheck these action rows, each through the tool's selection hook.

        The views are told once, after the last row, never per row.
        """
        if self._plan is None or not self._editable:
            return
        self._apply_checked(targets, checked)
        self.refresh_checks()
        self.checksChanged.emit()

    def tick_only(self, universe: Iterable[Node], ticked: Iterable[Node]) -> None:
        """Uncheck every row of ``universe``, then check ``ticked``; one update at the end."""
        if self._plan is None or not self._editable:
            return
        self._apply_checked(universe, False)
        self._apply_checked(ticked, True)
        self.refresh_checks()
        self.checksChanged.emit()

    def refresh_checks(self) -> None:
        """Tell the views that every check state may have changed."""
        if not self.top:
            return
        roles = [Qt.ItemDataRole.CheckStateRole]
        self.dataChanged.emit(self.index(0, 0), self.index(len(self.top) - 1, 0), roles)
        for owner in _walk(self.top):
            if owner.children:
                parent = self.index_of(owner)
                last = len(owner.children) - 1
                self.dataChanged.emit(self.index(0, 0, parent), self.index(last, 0, parent), roles)

    # ----- QAbstractItemModel -------------------------------------------------------------

    def index(self, row: int, column: int, parent: AnyIndex | None = None) -> QModelIndex:
        if row < 0 or column < 0 or column >= len(self._columns):
            return QModelIndex()
        if parent is None or not parent.isValid():
            if row >= len(self.top):
                return QModelIndex()
            return self.createIndex(row, column, self.top[row])
        owner = self.node_at(parent)
        if owner is None or row >= len(owner.children):
            return QModelIndex()
        return self.createIndex(row, column, owner.children[row])

    def parent(self, child: AnyIndex | None = None) -> QModelIndex:  # type: ignore[override]
        node = self.node_at(child) if child is not None else None
        if node is None or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.parent.row, 0, node.parent)

    def rowCount(self, parent: AnyIndex | None = None) -> int:
        if parent is None or not parent.isValid():
            return len(self.top)
        if parent.column() != 0:
            return 0
        node = self.node_at(parent)
        return len(node.children) if node is not None else 0

    def columnCount(self, parent: AnyIndex | None = None) -> int:
        return len(self._columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self._columns)
        ):
            return self._columns[section]
        return None

    def flags(self, index: AnyIndex) -> Qt.ItemFlag:  # type: ignore[override]
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0 and self._editable:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def _cells(self, node: Node) -> list[str]:
        """Display text for the columns between the first one and Size."""
        action = node.action
        if self._mode == FOLDER_TREE:
            if action is None:
                return ["", f"{len(_subtree_actions(node))} items", ""]
            return [
                str(action.kind),
                _dash(str(action.dst) if action.dst is not None else ""),
                action.reason,
            ]
        if action is None:
            count = f"{len(node.children)} items"
            if self._rename_layout:
                return ["", "", count, ""]
            if KEPT in self._columns:
                return ["", count, "", ""]
            return ["", count, ""]
        if self._rename_layout:
            folder = str(action.src.parent) if action.src is not None else ""
            return [
                _dash(folder),
                _dash(_name(action.src)),
                _dash(_name(action.dst)),
                action.reason,
            ]
        if KEPT in self._columns:
            return [
                _dash(str(action.src) if action.src is not None else ""),
                _dash(str(action.dst) if action.dst is not None else ""),
                _dash(action.group_id or ""),
                action.reason,
            ]
        return [
            _dash(str(action.src) if action.src is not None else ""),
            _dash(str(action.dst) if action.dst is not None else ""),
            action.reason,
        ]

    def _tooltip(self, node: Node, column: int) -> str | None:
        action = node.action
        name = self._columns[column]
        if name == "Name":
            return str(node.path) if node.path is not None else None
        if action is None:
            return node.label if column == 0 else None
        if name == "Folder":
            return str(action.src.parent) if action.src is not None else None
        if name == KEPT:
            return action.group_id
        if name in (FILE, "Old name"):
            return str(action.src) if action.src is not None else None
        if name in (GOES_TO, "New name"):
            return str(action.dst) if action.dst is not None else None
        if name == WHY:
            return action.reason or None
        return None

    def data(self, index: AnyIndex, role: int = 0) -> Any:
        node = self.node_at(index)
        if node is None:
            return None
        column = index.column()
        action = node.action
        last = len(self._columns) - 1
        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            if action is None or node.children:
                return self._group_state(node)
            return Qt.CheckState.Checked if action.selected else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(node, column)
        if role in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
            if column == last:
                size = self.size_of(node)
                if role == SORT_ROLE:
                    return -1 if size is None else size
                return human_size(size)
            if column == 0:
                if self._mode == FOLDER_TREE or action is None:
                    return self.group_label(node)
                return str(action.kind) if role == SORT_ROLE else kind_label(action.kind)
            text = self._cells(node)[column - 1]
            if role == Qt.ItemDataRole.DisplayRole and self._mode == FOLDER_TREE and column == 1:
                return kind_label(text) if action is not None else text
            return text
        return None

    def setData(self, index: AnyIndex, value: Any, role: int = 2) -> bool:  # type: ignore[override]
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != 0 or not self._editable:
            return False
        node = self.node_at(index)
        if node is None:
            return False
        state = getattr(value, "value", value)
        self.set_node_checked(node, int(state) == Qt.CheckState.Checked.value)
        return True


class _PlanProxy(QSortFilterProxyModel):
    """Sorts by ``SORT_ROLE`` and filters on text and kind, keeping groups with a match."""

    def __init__(self, model: PlanModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._model = model
        self._text = ""
        self._kind = ""
        self._tick = SHOW_ALL
        self.setSourceModel(model)
        self.setSortRole(SORT_ROLE)
        # A check-box change must not re-run the filter over every row (100,000 rows take
        # seconds); the table re-filters itself when the Show filter needs it.
        self.setDynamicSortFilter(False)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setRecursiveFilteringEnabled(True)

    def set_filters(self, text: str, kind: str, tick: str = SHOW_ALL) -> None:
        # begin/endFilterChange replace the deprecated invalidateFilter() (Qt 6.10+).
        self.beginFilterChange()
        self._text = text.strip().casefold()
        self._kind = kind
        self._tick = tick
        self.endFilterChange()

    @property
    def active(self) -> bool:
        """True while a text, kind or ticked filter hides rows (or could)."""
        return bool(self._text or self._kind or self._tick != SHOW_ALL)

    def shows(self, node: Node) -> bool:
        """The row is shown by the current filter (its parents too)."""
        if not self.active:
            return True
        return self.mapFromSource(self._model.index_of(node)).isValid()

    def filterAcceptsRow(self, source_row: int, source_parent: AnyIndex) -> bool:
        node = self._model.node_at(self._model.index(source_row, 0, source_parent))
        if node is None:
            return False
        action = node.action
        if action is None:
            # A group shows only when one of its children does (recursive filtering).
            return not self.active
        if self._kind and str(action.kind) != self._kind:
            return False
        if self._tick == SHOW_TICKED and not action.selected:
            return False
        if self._tick == SHOW_UNTICKED and action.selected:
            return False
        if not self._text:
            return True
        parent = node.parent
        group = (
            self._model.group_label(parent) if parent is not None and parent.path is None else ""
        )
        haystack = " ".join(
            (str(action.src or ""), str(action.dst or ""), action.reason, group)
        ).casefold()
        return self._text in haystack

    def lessThan(self, source_left: AnyIndex, source_right: AnyIndex) -> bool:
        a, b = source_left.data(SORT_ROLE), source_right.data(SORT_ROLE)
        if isinstance(a, str) and isinstance(b, str):
            return os.path.normcase(a).casefold() < os.path.normcase(b).casefold()
        return bool(a < b)


class PlanTable(QWidget):
    """Filter box, kind filter, the tree of actions and a totals line."""

    selectionChanged = Signal()
    currentActionChanged = Signal(object)
    filterChanged = Signal(bool)  # True while a filter hides rows

    MAX_AUTO_EXPAND_GROUPS = 400

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = PlanModel(self)
        self._proxy = _PlanProxy(self._model, self)
        self._model.set_shown_filter(self._proxy.shows)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by path or reason")
        self.filter_edit.setClearButtonEnabled(True)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem(ALL_KINDS)
        self.view_combo = QComboBox()
        for mode, label in VIEW_LABELS.items():
            self.view_combo.addItem(label, mode)
        self.view_combo.setToolTip(
            "Grouped: one row per group. Folder tree: the folders the actions are in, with "
            "check boxes that cover everything inside a folder."
        )
        self.totals_label = QLabel("No plan")
        # The selection row is always visible (not only with "Show all settings").
        self.tick_all_button = QPushButton("Tick all")
        self.untick_all_button = QPushButton("Untick all")
        self.tick_first_label = QLabel("Tick only the first")
        self.tick_first_spin = QSpinBox()
        self.tick_first_spin.setRange(1, 10_000_000)
        self.tick_first_spin.setValue(DEFAULT_TICK_FIRST)
        self.tick_first_spin.setGroupSeparatorShown(True)
        self.tick_first_button = QPushButton("▸")
        self.tick_first_button.setToolTip(
            "Untick every row that is shown, then tick the first rows in the order of the list."
        )
        self.tick_first_button.setAccessibleName("Tick only the first rows")
        self.show_label = QLabel("Show:")
        self.show_combo = QComboBox()
        for mode, label in SHOW_LABELS.items():
            self.show_combo.addItem(label, mode)
        self.show_combo.setToolTip("Show every row, only the ticked rows or only the others.")

        self.view = QTreeView()
        self.view.setModel(self._proxy)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setUniformRowHeights(True)
        self.view.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.view.header().setStretchLastSection(False)
        self.view.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._show_context_menu)
        # Hooks that tests replace, so nothing opens Explorer, an app or a message box.
        self.reveal: Callable[[Path], bool] = reveal.show_in_explorer
        self.open_file: Callable[[Path], bool] = reveal.open_path
        self.confirm: Callable[[str, str], bool] = self._default_confirm
        self.show_properties: Callable[[Path], bool] = reveal.show_properties

        top = QHBoxLayout()
        self._toolbar = top
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.kind_combo)
        self.view_label = QLabel("View:")
        top.addWidget(self.view_label)
        top.addWidget(self.view_combo)
        # Shown only with "Show all settings" on (the filter box is always shown).
        self._full_only: list[QWidget] = [self.kind_combo, self.view_label, self.view_combo]
        self._full_controls = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        picking = QHBoxLayout()
        self._selection_row = picking
        for widget in (self.tick_all_button, self.untick_all_button):
            picking.addWidget(widget)
        picking.addSpacing(12)
        for widget in (self.tick_first_label, self.tick_first_spin, self.tick_first_button):
            picking.addWidget(widget)
        picking.addStretch(1)
        picking.addWidget(self.show_label)
        picking.addWidget(self.show_combo)
        layout.addLayout(picking)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.totals_label)

        self.filter_edit.textChanged.connect(self._filters_changed)
        self.show_combo.currentIndexChanged.connect(self._filters_changed)
        self.tick_first_button.clicked.connect(lambda _c=False: self.tick_only_first())
        self.find_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self.find_shortcut.activated.connect(self.focus_filter)
        self.view.installEventFilter(self)
        self.kind_combo.currentIndexChanged.connect(self._filters_changed)
        self.view_combo.currentIndexChanged.connect(self._view_chosen)
        self._model.checksChanged.connect(self._on_checks_changed)
        selection = self.view.selectionModel()
        selection.currentChanged.connect(self._on_current_changed)
        self.view.sortByColumn(-1, Qt.SortOrder.AscendingOrder)

    # ----- public API ---------------------------------------------------------------------

    def set_plan(
        self,
        plan: Plan | None,
        tool_id: str = "",
        sizes: Sequence[int | None] | None = None,
    ) -> None:
        """Show ``plan``. ``sizes[i]`` is the size in bytes of ``plan.actions[i]``, or None."""
        self._load(plan, tool_id, sizes, keep=False)

    def plan(self) -> Plan | None:
        return self._model.plan

    def refresh(self, sizes: Sequence[int | None] | None = None) -> None:
        """Re-read the plan after an outside change, keeping expansion, sort and filters.

        ``sizes`` replaces the known sizes (for example after a keeper change swapped a source).
        """
        known = list(sizes) if sizes is not None else self._model.sizes()
        self._load(self._model.plan, self._model.tool_id, known, keep=True)

    def sizes(self) -> list[int | None]:
        """The size in bytes of each plan action, as given to :meth:`set_plan`."""
        return self._model.sizes()

    def current_group_id(self) -> str | None:
        node = self._current_node()
        if node is None:
            return None
        if node.action is None:
            return None if node.path is not None else node.label  # a folder is no group
        return node.action.group_id

    def view_mode(self) -> str:
        """``GROUPED`` or ``FOLDER_TREE``."""
        return self._model.mode

    def add_toolbar_widget(self, widget: QWidget, *, full_only: bool = False) -> None:
        """Put ``widget`` at the end of the toolbar above the table (filter, kind, view).

        ``full_only``: shown only while :meth:`set_full_controls` is on.
        """
        self._toolbar.addWidget(widget)
        if full_only:
            self._full_only.append(widget)
            widget.setVisible(self._full_controls)

    def set_full_controls(self, full: bool) -> None:
        """Show the kind filter, the view switch and the other "full" toolbar widgets, or hide
        them (the "Show all settings" switch). The text filter is always shown."""
        self._full_controls = full
        for widget in self._full_only:
            widget.setVisible(full)
        if not full and self.kind_combo.currentIndex() > 0:
            # A filter nobody can see must not hide rows that Apply still acts on.
            self.kind_combo.setCurrentIndex(0)

    def set_view_mode(self, mode: str) -> None:
        """Show the plan grouped or as a folder tree. The plan and its checks stay as they are."""
        if mode not in VIEW_LABELS:
            raise ValueError(f"unknown plan view {mode!r}")
        index = self.view_combo.findData(mode)
        if self.view_combo.currentIndex() != index:
            self.view_combo.blockSignals(True)
            self.view_combo.setCurrentIndex(index)
            self.view_combo.blockSignals(False)
        if mode == self._model.mode:
            return
        self._load(
            self._model.plan, self._model.tool_id, self._model.sizes(), keep=False, mode=mode
        )

    def select_actions(self, indices: Iterable[int]) -> int:
        """Select the rows of these plan actions (opening their folders) and scroll to the
        first. Rows hidden by the filter are skipped. Returns how many rows were selected."""
        selection = self.view.selectionModel()
        selection.clearSelection()
        first: QModelIndex | None = None
        count = 0
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for action_index in indices:
            node = self._model.node_for_action(action_index)
            if node is None:
                continue
            proxy_index = self._proxy.mapFromSource(self._model.index_of(node))
            if not proxy_index.isValid():
                continue
            parent = proxy_index.parent()
            while parent.isValid():
                self.view.setExpanded(parent, True)
                parent = parent.parent()
            selection.select(proxy_index, flags)
            count += 1
            if first is None:
                first = proxy_index
        if first is not None:
            selection.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.view.scrollTo(first)
        return count

    def selected_action_indices(self) -> list[int]:
        """The plan positions of the action rows that are selected (highlighted) in the view."""
        result: list[int] = []
        for proxy_index in self.view.selectionModel().selectedRows(0):
            node = self._model.node_at(self._proxy.mapToSource(proxy_index))
            if node is not None and node.action is not None:
                result.append(node.index)
        return result

    def set_action_checked(self, index: int, checked: bool) -> None:
        node = self._model.node_for_action(index)
        if node is not None:
            self._model.set_node_checked(node, checked)

    def filter_active(self) -> bool:
        """True while the text or kind filter is set, so some rows may be hidden."""
        return self._proxy.active

    def shown_action_indices(self) -> list[int]:
        """The plan positions of the action rows the filter shows (all of them without one)."""
        return [
            node.index
            for node in self._model.all_nodes()
            if node.action is not None and self._proxy.shows(node)
        ]

    def set_shown_checked(self, checked: bool) -> int:
        """Check or uncheck every action row the filter shows, each through the tool's
        selection hook. Returns how many actions of the plan changed."""
        plan = self._model.plan
        if plan is None or not self._model.editable:
            return 0
        before = [a.selected for a in plan.actions]
        nodes = [
            n for n in (self._model.node_for_action(i) for i in self.shown_action_indices()) if n
        ]
        self._model.set_actions_checked(nodes, checked)
        return sum(1 for was, a in zip(before, plan.actions, strict=True) if was != a.selected)

    def editable(self) -> bool:
        return self._model.editable

    def set_selection_enabled(self, enabled: bool) -> None:
        """Enable or disable the "Tick only the first" controls (the page owns the other two
        buttons)."""
        self.tick_first_spin.setEnabled(enabled)
        self.tick_first_button.setEnabled(enabled)

    def focus_filter(self) -> None:
        """Ctrl+F: put the cursor in the filter box."""
        self.filter_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.filter_edit.selectAll()

    def _view_action_nodes(self) -> list[Node]:
        """The action rows the filter shows, in the order the list shows them (sort included)."""
        proxy, model = self._proxy, self._model
        if not proxy.active and self.view.header().sortIndicatorSection() < 0:
            return [n for n in model.all_nodes() if n.action is not None]  # model order
        found: list[Node] = []

        def visit(parent: QModelIndex) -> None:
            for row in range(proxy.rowCount(parent)):
                index = proxy.index(row, 0, parent)
                node = model.node_at(proxy.mapToSource(index))
                if node is not None and node.action is not None:
                    found.append(node)
                if proxy.hasChildren(index):
                    visit(index)

        visit(QModelIndex())
        return found

    def tick_only_first(self, count: int | None = None) -> int:
        """Untick every shown row, then tick the first ``count`` shown rows in the order of the
        list (the spin box value when ``count`` is None). Returns how many rows are ticked."""
        if self._model.plan is None or not self._model.editable:
            return 0
        wanted = self.tick_first_spin.value() if count is None else count
        nodes = self._view_action_nodes()
        first = nodes[: max(wanted, 0)]
        self._model.tick_only(nodes, first)
        return len(first)

    def highlight_all_shown(self) -> int:
        """Ctrl+A: highlight every row the filter shows, also inside folded groups."""
        proxy = self._proxy
        selection = QItemSelection()
        last = proxy.columnCount() - 1
        count = 0

        def visit(parent: QModelIndex) -> None:
            nonlocal count
            rows = proxy.rowCount(parent)
            if not rows:
                return
            selection.select(proxy.index(0, 0, parent), proxy.index(rows - 1, last, parent))
            count += rows
            for row in range(rows):
                index = proxy.index(row, 0, parent)
                if proxy.hasChildren(index):
                    visit(index)

        visit(QModelIndex())
        self.view.selectionModel().select(
            selection, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        return count

    def _highlighted_nodes(self) -> list[Node]:
        """The action rows that are highlighted. A highlighted group or folder row stands for
        the shown rows below it. Each action once."""
        seen: set[int] = set()
        result: list[Node] = []
        proxy, model = self._proxy, self._model
        # Walk the selected ranges (selectedRows() is far too slow for 100,000 rows).
        blocks = self.view.selectionModel().selection()
        for k in range(blocks.count()):
            block = blocks.at(k)
            parent = block.parent()
            for row in range(block.top(), block.bottom() + 1):
                node = model.node_at(proxy.mapToSource(proxy.index(row, 0, parent)))
                if node is None:
                    continue
                if node.action is None:
                    targets = [t for t in _subtree_actions(node) if proxy.shows(t)]
                else:
                    targets = [node]
                for target in targets:
                    if target.index not in seen:
                        seen.add(target.index)
                        result.append(target)
        return result

    def highlighted_action_indices(self) -> list[int]:
        """Plan positions of the highlighted actions (see :meth:`_highlighted_nodes`)."""
        return [n.index for n in self._highlighted_nodes()]

    def tick_highlighted(self, checked: bool) -> int:
        nodes = self._highlighted_nodes()
        self._model.set_actions_checked(nodes, checked)
        return len(nodes)

    def tick_only_highlighted(self) -> int:
        """Untick every shown row, then tick the highlighted ones."""
        nodes = self._highlighted_nodes()
        self._model.tick_only(self._view_action_nodes(), nodes)
        return len(nodes)

    def toggle_highlighted(self) -> int:
        """Space: tick or untick the highlighted rows, all to the opposite of the first one."""
        nodes = self._highlighted_nodes()
        if not nodes:
            return 0
        if len(nodes) > 1:
            position = {n.index: i for i, n in enumerate(self._view_action_nodes())}
            nodes.sort(key=lambda n: position.get(n.index, len(position)))
        first = nodes[0].action
        assert first is not None
        self._model.set_actions_checked(nodes, not first.selected)
        return len(nodes)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.view and event.type() == QEvent.Type.KeyPress:
            assert isinstance(event, QKeyEvent)
            mods = event.modifiers()
            if event.key() == Qt.Key.Key_A and mods == Qt.KeyboardModifier.ControlModifier:
                self.highlight_all_shown()
                return True
            space = event.key() == Qt.Key.Key_Space and mods == Qt.KeyboardModifier.NoModifier
            if space and self.view.selectionModel().hasSelection():
                if self._model.editable:
                    self.toggle_highlighted()
                return True
        return super().eventFilter(watched, event)

    def set_editable(self, editable: bool) -> None:
        """Allow or forbid changing the check boxes (the tool page forbids it while busy)."""
        self._model.set_editable(editable)

    def totals_text(self) -> str:
        return self._model.totals_text()

    def status_text(self) -> str:
        """The line under the list, for example "2,113 found · 150 ticked (4.4 GB)"."""
        return self._model.status_text()

    # ----- right-click menu ------------------------------------------------------------------

    def context_menu_at(self, pos: QPoint) -> QMenu | None:
        """The right-click menu for the row at ``pos`` (viewport coordinates), not shown yet.

        It acts on the highlighted rows (the row under the cursor is highlighted first when it
        is not part of the highlight): Tick, Untick, Tick only these. With one row: one "show"
        entry per place the row is about (for a duplicate: this copy and the kept copy), then
        Open, Properties and Copy path for the row's own file; entries for a path that no
        longer exists are disabled. With several rows only Copy paths is available. None when
        there is no row at ``pos``.
        """
        proxy_index = self.view.indexAt(pos)
        node = self._model.node_at(self._proxy.mapToSource(proxy_index))
        if node is None:
            return None
        selection = self.view.selectionModel()
        if not selection.isRowSelected(proxy_index.row(), proxy_index.parent()):
            selection.select(
                proxy_index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        rows = self._highlighted_nodes()
        menu = QMenu(self.view)
        editable = self._model.editable
        for label, handler in (
            ("Tick", lambda: self.tick_highlighted(True)),
            ("Untick", lambda: self.tick_highlighted(False)),
            ("Tick only these", self.tick_only_highlighted),
        ):
            entry = menu.addAction(label)
            entry.setEnabled(editable and bool(rows))
            entry.triggered.connect(lambda _c=False, h=handler: h())
        menu.addSeparator()
        menu.setToolTipsVisible(True)
        if len(rows) > 1:
            for label in ("Open", reveal.SHOW, "Properties"):
                entry = menu.addAction(label)
                entry.setEnabled(False)
                entry.setToolTip("Highlight a single row to use this.")
            paths = [p for p in (_action_path(n.action) for n in rows if n.action) if p]
            copy_entry = menu.addAction("Copy paths")
            copy_entry.triggered.connect(
                lambda _c=False, ps=paths: self._copy_text("\n".join(str(p) for p in ps))
            )
            return menu
        targets = reveal.reveal_targets(node.action, node.path, self._model.is_duplicates())
        if not targets:
            return menu if editable else None
        for target in targets:
            entry = menu.addAction(target.label)
            entry.setEnabled(target.exists)
            entry.setToolTip(str(target.path))
            entry.triggered.connect(lambda _c=False, p=target.path: self.reveal(p))
        own = targets[0]
        open_entry = menu.addAction("Open")
        open_entry.setEnabled(own.exists and not own.is_destination)
        open_entry.triggered.connect(lambda _c=False, p=own.path: self._open(p))
        properties = menu.addAction("Properties")
        properties.setEnabled(own.exists)
        properties.triggered.connect(lambda _c=False, p=own.path: self.show_properties(p))
        copy_entry = menu.addAction("Copy path")
        copy_entry.triggered.connect(lambda _c=False, p=own.own_path: self._copy_path(p))
        return menu

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = self.context_menu_at(pos)
        if menu is not None:
            menu.exec(self.view.viewport().mapToGlobal(pos))
            menu.deleteLater()

    def _open(self, path: Path) -> bool:
        """Open ``path`` in its default app; a cloud-only file is downloaded only if asked."""
        if reveal.needs_download(path) and not self.confirm(
            "Download from OneDrive?",
            f"{path.name} is only in OneDrive, not on this device. Opening it downloads it "
            "to this device.\n\nDownload and open it?",
        ):
            return False
        return self.open_file(path)

    @staticmethod
    def _copy_text(text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    @classmethod
    def _copy_path(cls, path: Path) -> None:
        cls._copy_text(str(path))

    def _default_confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ----- internals ----------------------------------------------------------------------

    def _current_node(self) -> Node | None:
        return self._model.node_at(self._proxy.mapToSource(self.view.currentIndex()))

    def _parents(self) -> list[Node]:
        """Every row that has children (groups, folders, folder actions)."""
        return [n for n in self._model.all_nodes() if n.children]

    def _expanded_groups(self) -> tuple[set[str], set[str]]:
        """Keys of the rows that are expanded, and of every row with children in the model."""
        expanded: set[str] = set()
        known: set[str] = set()
        for group in self._parents():
            known.add(group.key)
            proxy_index = self._proxy.mapFromSource(self._model.index_of(group))
            if proxy_index.isValid() and self.view.isExpanded(proxy_index):
                expanded.add(group.key)
        return expanded, known

    def _view_chosen(self, index: int) -> None:
        mode = self.view_combo.itemData(index)
        if isinstance(mode, str):
            self.set_view_mode(mode)

    def _load(
        self,
        plan: Plan | None,
        tool_id: str,
        sizes: Sequence[int | None] | None,
        *,
        keep: bool,
        mode: str | None = None,
    ) -> None:
        expanded: set[str] = set()
        known: set[str] = set()
        current_action: PlannedAction | None = None
        if keep:
            expanded, known = self._expanded_groups()
            node = self._current_node()
            current_action = node.action if node is not None else None
        self.view.setUpdatesEnabled(False)
        try:
            self._model.load(plan, tool_id, sizes, mode)
            self._sync_kinds()
            self._proxy.set_filters(self.filter_edit.text(), self._kind_filter(), self._tick_mode())
            self._restore_view(keep, expanded, known, current_action)
            if not keep:
                self._set_column_widths()
        finally:
            self.view.setUpdatesEnabled(True)
        self._update_status()
        if not keep:
            self.currentActionChanged.emit(None)

    def _restore_view(
        self,
        keep: bool,
        expanded: set[str],
        known: set[str],
        current_action: PlannedAction | None,
    ) -> None:
        groups = self._parents()
        auto_open = len(groups) <= self.MAX_AUTO_EXPAND_GROUPS
        for group in groups:
            proxy_index = self._proxy.mapFromSource(self._model.index_of(group))
            if not proxy_index.isValid():
                continue
            if keep:
                # Open rows stay open. A row we have not seen (a keeper that just changed)
                # opens too.
                self.view.setExpanded(proxy_index, group.key in expanded or group.key not in known)
            elif auto_open or (group.parent is None and self._model.mode == FOLDER_TREE):
                self.view.setExpanded(proxy_index, True)
        plan = self._model.plan
        if current_action is not None and plan is not None:
            for i, action in enumerate(plan.actions):
                if action is current_action:
                    node = self._model.node_for_action(i)
                    if node is not None:
                        index = self._proxy.mapFromSource(self._model.index_of(node))
                        if index.isValid():
                            self.view.setCurrentIndex(index)
                    break

    def _set_column_widths(self) -> None:
        last = self._model.columnCount() - 1
        for column in range(self._model.columnCount()):
            self.view.setColumnWidth(column, 300)
        self.view.setColumnWidth(0, 190)
        self.view.setColumnWidth(last, 90)
        self.view.setColumnWidth(last - 1, 280)
        if self._model.mode == FOLDER_TREE:
            self.view.setColumnWidth(0, 340)
            self.view.setColumnWidth(1, 150)

    def _sync_kinds(self) -> None:
        previous = self.kind_combo.currentText()
        self.kind_combo.blockSignals(True)
        self.kind_combo.clear()
        self.kind_combo.addItem(ALL_KINDS)
        for kind in self._model.kinds():
            self.kind_combo.addItem(kind_label(kind), kind)
        self.kind_combo.setCurrentIndex(max(self.kind_combo.findText(previous), 0))
        self.kind_combo.blockSignals(False)

    def _kind_filter(self) -> str:
        if self.kind_combo.currentIndex() <= 0:
            return ""
        return str(self.kind_combo.currentData())

    def _tick_mode(self) -> str:
        mode = self.show_combo.currentData()
        return mode if isinstance(mode, str) else SHOW_ALL

    def _filters_changed(self, *_: object) -> None:
        kind = self._kind_filter()
        self._proxy.set_filters(self.filter_edit.text(), kind, self._tick_mode())
        if self._proxy.active and self._proxy.rowCount() <= self.MAX_AUTO_EXPAND_GROUPS:
            self.view.expandAll()
        self._update_status()
        self.filterChanged.emit(self._proxy.active)

    def _update_status(self) -> None:
        text = self._model.status_text()
        if self._proxy.active and self._model.plan is not None:
            text += f" · {len(self.shown_action_indices()):,} shown"
        self.totals_label.setText(text)

    def _on_checks_changed(self) -> None:
        if self._tick_mode() != SHOW_ALL:
            # Rows moved in or out of the Ticked / Not ticked view.
            self._proxy.set_filters(self.filter_edit.text(), self._kind_filter(), self._tick_mode())
        self._update_status()
        self.selectionChanged.emit()

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        node = self._model.node_at(self._proxy.mapToSource(current))
        self.currentActionChanged.emit(node.action if node is not None else None)
