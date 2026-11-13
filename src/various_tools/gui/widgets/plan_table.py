"""The plan table: every planned action with a check box, grouped, sortable and filterable.

Duplicate plans group their actions under a "Keeps <name>" row. Other plans with a ``group_id``
show the id; plans without groups are a flat list. Toggling a check box sets
``PlannedAction.selected`` (a per-tool hook may do more, see ``SELECTION_HOOKS``). The table
never touches the disk: sizes are computed by the caller in a worker and passed in.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.gui.plan_edit import human_size

ALL_KINDS = "All kinds"
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


class Node:
    """One row: a group (``action`` is None) or one planned action."""

    __slots__ = ("action", "children", "index", "label", "parent", "row")

    def __init__(
        self,
        parent: Node | None,
        action: PlannedAction | None = None,
        index: int = -1,
        label: str = "",
    ) -> None:
        self.parent = parent
        self.action = action
        self.index = index  # position in plan.actions
        self.label = label  # a group's id
        self.children: list[Node] = []
        self.row = 0


def _name(path: Path | None) -> str:
    if path is None:
        return ""
    return path.name or str(path)


def _dash(text: str) -> str:
    return text or EM_DASH


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
        self._columns = ["Kind", "Source", "Destination", "Reason", "Size"]

    # ----- loading ------------------------------------------------------------------

    def load(self, plan: Plan | None, tool_id: str, sizes: Sequence[int | None] | None) -> None:
        self.beginResetModel()
        self._plan = plan
        self._tool_id = tool_id
        actions = plan.actions if plan is not None else []
        self._sizes = list(sizes)[: len(actions)] if sizes is not None else []
        self._sizes += [None] * (len(actions) - len(self._sizes))
        self._rename_layout = bool(actions) and all(a.kind is ActionKind.RENAME for a in actions)
        if self._rename_layout:
            self._columns = ["Kind", "Folder", "Old name", "New name", "Reason", "Size"]
        else:
            self._columns = ["Kind", "Source", "Destination", "Reason", "Size"]
        self.top = []
        self._by_index = []
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
        self.endResetModel()

    @property
    def plan(self) -> Plan | None:
        return self._plan

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

    def group_label(self, node: Node) -> str:
        plan_tool = self._plan.tool if self._plan is not None else ""
        if self._tool_id == "duplicates" or plan_tool == "duplicates":
            return f"Keeps {_name(Path(node.label))}"
        return node.label

    # ----- sizes and totals -------------------------------------------------------------

    def size_of(self, node: Node) -> int | None:
        if node.action is not None:
            return self._sizes[node.index]
        values = [s for s in (self._sizes[c.index] for c in node.children) if s is not None]
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

    # ----- check states -----------------------------------------------------------------

    def _group_state(self, node: Node) -> Qt.CheckState:
        selected = sum(1 for c in node.children if c.action is not None and c.action.selected)
        if selected == 0:
            return Qt.CheckState.Unchecked
        if selected == len(node.children):
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked

    def set_node_checked(self, node: Node, checked: bool) -> None:
        """Check or uncheck one row (a group sets all of its actions), as a user click does."""
        plan = self._plan
        if plan is None:
            return
        targets = [node] if node.action is not None else list(node.children)
        hook = SELECTION_HOOKS.get(self._tool_id)
        for target in targets:
            action = target.action
            if action is None:
                continue
            if hook is not None:
                hook(plan, action, checked)
            else:
                action.selected = checked
        self.refresh_checks()
        self.checksChanged.emit()

    def refresh_checks(self) -> None:
        """Tell the views that every check state may have changed."""
        if not self.top:
            return
        roles = [Qt.ItemDataRole.CheckStateRole]
        self.dataChanged.emit(self.index(0, 0), self.index(len(self.top) - 1, 0), roles)
        for group in self.top:
            if group.action is None and group.children:
                parent = self.index(group.row, 0)
                last = len(group.children) - 1
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
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def _cells(self, node: Node) -> list[str]:
        """Display text for the columns between Kind and Size."""
        action = node.action
        if action is None:
            count = f"{len(node.children)} items"
            if self._rename_layout:
                return ["", "", count, ""]
            return ["", count, ""]
        if self._rename_layout:
            folder = str(action.src.parent) if action.src is not None else ""
            return [
                _dash(folder),
                _dash(_name(action.src)),
                _dash(_name(action.dst)),
                action.reason,
            ]
        return [
            _dash(str(action.src) if action.src is not None else ""),
            _dash(str(action.dst) if action.dst is not None else ""),
            action.reason,
        ]

    def _tooltip(self, node: Node, column: int) -> str | None:
        action = node.action
        if action is None:
            return node.label if column == 0 else None
        name = self._columns[column]
        if name == "Folder":
            return str(action.src.parent) if action.src is not None else None
        if name in ("Source", "Old name"):
            return str(action.src) if action.src is not None else None
        if name in ("Destination", "New name"):
            return str(action.dst) if action.dst is not None else None
        if name == "Reason":
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
            if action is None:
                return self._group_state(node)
            return Qt.CheckState.Checked if action.selected else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(node, column)
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return str(action.kind) if action is not None else self.group_label(node)
            if column == last:
                return human_size(self.size_of(node))
            return self._cells(node)[column - 1]
        if role == SORT_ROLE:
            if column == last:
                size = self.size_of(node)
                return -1 if size is None else size
            if column == 0:
                return str(action.kind) if action is not None else self.group_label(node)
            return self._cells(node)[column - 1]
        return None

    def setData(self, index: AnyIndex, value: Any, role: int = 2) -> bool:  # type: ignore[override]
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != 0:
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
        self.setSourceModel(model)
        self.setSortRole(SORT_ROLE)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setRecursiveFilteringEnabled(True)

    def set_filters(self, text: str, kind: str) -> None:
        # begin/endFilterChange replace the deprecated invalidateFilter() (Qt 6.10+).
        self.beginFilterChange()
        self._text = text.strip().casefold()
        self._kind = kind
        self.endFilterChange()

    def filterAcceptsRow(self, source_row: int, source_parent: AnyIndex) -> bool:
        node = self._model.node_at(self._model.index(source_row, 0, source_parent))
        if node is None:
            return False
        action = node.action
        if action is None:
            # A group shows only when one of its children does (recursive filtering).
            return not self._text and not self._kind
        if self._kind and str(action.kind) != self._kind:
            return False
        if not self._text:
            return True
        group = self._model.group_label(node.parent) if node.parent is not None else ""
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

    MAX_AUTO_EXPAND_GROUPS = 400

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = PlanModel(self)
        self._proxy = _PlanProxy(self._model, self)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by path or reason")
        self.filter_edit.setClearButtonEnabled(True)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem(ALL_KINDS)
        self.totals_label = QLabel("No plan")

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

        top = QHBoxLayout()
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.kind_combo)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.totals_label)

        self.filter_edit.textChanged.connect(self._filters_changed)
        self.kind_combo.currentIndexChanged.connect(self._filters_changed)
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
        return node.label if node.action is None else node.action.group_id

    def set_action_checked(self, index: int, checked: bool) -> None:
        node = self._model.node_for_action(index)
        if node is not None:
            self._model.set_node_checked(node, checked)

    def totals_text(self) -> str:
        return self._model.totals_text()

    # ----- internals ----------------------------------------------------------------------

    def _current_node(self) -> Node | None:
        return self._model.node_at(self._proxy.mapToSource(self.view.currentIndex()))

    def _expanded_groups(self) -> tuple[set[str], set[str]]:
        """Group ids that are expanded, and every group id currently in the model."""
        expanded: set[str] = set()
        known: set[str] = set()
        for group in self._model.top:
            if group.action is not None:
                continue
            known.add(group.label)
            proxy_index = self._proxy.mapFromSource(self._model.index_of(group))
            if proxy_index.isValid() and self.view.isExpanded(proxy_index):
                expanded.add(group.label)
        return expanded, known

    def _load(
        self,
        plan: Plan | None,
        tool_id: str,
        sizes: Sequence[int | None] | None,
        *,
        keep: bool,
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
            self._model.load(plan, tool_id, sizes)
            self._sync_kinds()
            self._proxy.set_filters(self.filter_edit.text(), self._kind_filter())
            self._restore_view(keep, expanded, known, current_action)
            if not keep:
                self._set_column_widths()
        finally:
            self.view.setUpdatesEnabled(True)
        self.totals_label.setText(self._model.totals_text())
        if not keep:
            self.currentActionChanged.emit(None)

    def _restore_view(
        self,
        keep: bool,
        expanded: set[str],
        known: set[str],
        current_action: PlannedAction | None,
    ) -> None:
        groups = [n for n in self._model.top if n.action is None]
        auto_open = len(groups) <= self.MAX_AUTO_EXPAND_GROUPS
        for group in groups:
            proxy_index = self._proxy.mapFromSource(self._model.index_of(group))
            if not proxy_index.isValid():
                continue
            if keep:
                # Open groups stay open. A group id we have not seen (a keeper that just
                # changed) opens too.
                self.view.setExpanded(
                    proxy_index, group.label in expanded or group.label not in known
                )
            elif auto_open:
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

    def _sync_kinds(self) -> None:
        previous = self.kind_combo.currentText()
        self.kind_combo.blockSignals(True)
        self.kind_combo.clear()
        self.kind_combo.addItem(ALL_KINDS)
        self.kind_combo.addItems(self._model.kinds())
        self.kind_combo.setCurrentIndex(max(self.kind_combo.findText(previous), 0))
        self.kind_combo.blockSignals(False)

    def _kind_filter(self) -> str:
        return "" if self.kind_combo.currentIndex() <= 0 else self.kind_combo.currentText()

    def _filters_changed(self, *_: object) -> None:
        kind = self._kind_filter()
        self._proxy.set_filters(self.filter_edit.text(), kind)
        if (self.filter_edit.text().strip() or kind) and (
            self._proxy.rowCount() <= self.MAX_AUTO_EXPAND_GROUPS
        ):
            self.view.expandAll()

    def _on_checks_changed(self) -> None:
        self.totals_label.setText(self._model.totals_text())
        self.selectionChanged.emit()

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        node = self._model.node_at(self._proxy.mapToSource(current))
        self.currentActionChanged.emit(node.action if node is not None else None)
