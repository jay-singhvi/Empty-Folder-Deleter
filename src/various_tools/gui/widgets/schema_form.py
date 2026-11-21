"""A form generated from a pydantic options or settings model.

``SchemaForm(Model)`` reads the model with :func:`various_tools.core.schema.describe` and draws one
editor per field: checkbox, spin box, drop-down, text box, folder picker, folder list, reorderable
checklist or key/value table. Basic fields are always shown; advanced ones sit in a collapsible
"Advanced options" section. Fields are grouped by their ``group`` hint.

The form never validates while the person types. ``raw_values()`` returns what the editors hold,
and ``build()`` validates that through the model, shows each problem under its field and raises
``pydantic.ValidationError`` if anything is wrong.

Conventions:

* A nullable number has a "Use app setting" checkbox next to the spin box (checked means ``None``).
  A nullable choice starts with an "(app setting)" entry. A nullable text or path box that is
  empty means ``None``.
* A 🔒 locked field asks for confirmation the first time the person moves it away from its
  default (once per form). Loading values from code never asks.
* ``depends_on = "name=a|b"`` hides a field unless that other field currently reads ``a`` or ``b``.
  Hidden fields still count in ``raw_values()``.
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import Any, cast

import shiboken6
from pydantic import BaseModel, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase, QShowEvent, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.schema import FieldSpec, describe, format_value, parse_value
from various_tools.gui.widgets.path_picker import PathEdit, PathListEdit
from various_tools.gui.workers import Worker

ConfirmLocked = Callable[[FieldSpec, QWidget], bool]
"""Called when the person changes a 🔒 value. Return True to allow the change."""

LoadChoices = Callable[[str], Worker | None]
"""Given a ``choices_from`` source, return a worker (not started) whose result is a list of
strings, or None when no list can be loaded. The form starts it and fills the drop-down."""

APP_SETTING_TEXT = "(app setting)"
USE_APP_SETTING_TEXT = "Use app setting"
_INT_MAX = 2**31 - 1
_FLOAT_LIMIT = 1.0e9
_FLOAT_DECIMALS = 3
_ERROR_STYLE = "color: #c0392b;"


class _Missing:
    """Marks "no value": the key is left out so pydantic reports it as required."""

    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


# ----- small editor widgets --------------------------------------------------------------------


class _WheelGuardSpin(QSpinBox):
    """A spin box that ignores the mouse wheel unless focused, so scrolling the form is safe."""

    def __init__(self) -> None:
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setKeyboardTracking(False)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt name)
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _WheelGuardDoubleSpin(QDoubleSpinBox):
    """The decimal version of :class:`_WheelGuardSpin`."""

    def __init__(self) -> None:
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setKeyboardTracking(False)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt name)
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _WheelGuardCombo(QComboBox):
    """A drop-down that ignores the mouse wheel unless it has focus."""

    def __init__(self) -> None:
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt name)
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NullableNumber(QWidget):
    """A spin box with a "Use app setting" checkbox. Checked means the value is ``None``."""

    changed = Signal()

    def __init__(self, spin: QSpinBox | QDoubleSpinBox, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spin = spin
        self.use_default = QCheckBox(USE_APP_SETTING_TEXT, self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.spin)
        layout.addWidget(self.use_default)
        layout.addStretch(1)
        self.use_default.toggled.connect(self._on_toggle)
        self.spin.valueChanged.connect(lambda _v: self.changed.emit())
        self.use_default.setChecked(True)

    def value(self) -> int | float | None:
        return None if self.use_default.isChecked() else self.spin.value()

    def set_value(self, value: int | float | None) -> None:
        if value is None:
            self.use_default.setChecked(True)
        else:
            self.use_default.setChecked(False)
            self.spin.setValue(value)  # pyright: ignore[reportArgumentType]

    def _on_toggle(self, checked: bool) -> None:
        self.spin.setEnabled(not checked)
        self.changed.emit()


class OrderedChecklist(QWidget):
    """Checkable choices that can be reordered. The value is the checked ones, in shown order."""

    changed = Signal()

    def __init__(self, choices: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._choices = tuple(choices)
        self._silent = False
        self.list_widget = QListWidget(self)
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.up_button = QPushButton("Up", self)
        self.down_button = QPushButton("Down", self)
        buttons = QVBoxLayout()
        buttons.addWidget(self.up_button)
        buttons.addWidget(self.down_button)
        buttons.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(buttons)
        self.set_checked([])
        self.list_widget.itemChanged.connect(self._emit)
        self.list_widget.model().rowsMoved.connect(self._emit)
        self.up_button.clicked.connect(lambda: self.move_current(-1))
        self.down_button.clicked.connect(lambda: self.move_current(1))

    def checked(self) -> list[str]:
        """The checked choices, top to bottom."""
        result: list[str] = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.text())
        return result

    def order(self) -> list[str]:
        """Every choice (checked or not), top to bottom."""
        return [self.list_widget.item(row).text() for row in range(self.list_widget.count())]

    def set_checked(self, items: Iterable[str]) -> None:
        """Check ``items`` in the given order, then list the other choices unchecked.

        Does not emit ``changed``. Items that are not known choices are added too, so a bad value
        stays visible instead of being dropped without a word.
        """
        wanted = [str(item) for item in items]
        rest = [c for c in self._choices if c not in wanted]
        self._silent = True
        try:
            self.list_widget.clear()
            for text in wanted + rest:
                item = QListWidgetItem(text)
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsDragEnabled
                )
                state = Qt.CheckState.Checked if text in wanted else Qt.CheckState.Unchecked
                item.setCheckState(state)
                self.list_widget.addItem(item)
        finally:
            self._silent = False
        rows = max(1, self.list_widget.count())
        row_height = self.list_widget.sizeHintForRow(0) if self.list_widget.count() else 20
        self.list_widget.setFixedHeight(
            rows * max(row_height, 18) + 2 * self.list_widget.frameWidth() + 4
        )

    def move_current(self, delta: int) -> None:
        """Move the selected row up (-1) or down (+1)."""
        row = self.list_widget.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.list_widget.count():
            return
        self._silent = True
        try:
            item = self.list_widget.takeItem(row)
            if item is None:
                return
            self.list_widget.insertItem(target, item)
            self.list_widget.setCurrentRow(target)
        finally:
            self._silent = False
        self.changed.emit()

    def _emit(self, *_args: object) -> None:
        if not self._silent:
            self.changed.emit()


class KeyValueEdit(QWidget):
    """A small two-column table (Key, Value) with Add row and Remove row buttons."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._silent = False
        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Key", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(110)
        self.add_button = QPushButton("Add row", self)
        self.remove_button = QPushButton("Remove row", self)
        buttons = QVBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.table.itemChanged.connect(self._emit)
        self.add_button.clicked.connect(self.add_row)
        self.remove_button.clicked.connect(self.remove_selected)

    def items(self) -> dict[str, str]:
        """The rows as a dict. Rows with an empty key are ignored; a repeated key keeps the last."""
        result: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            key_item = self.table.item(row, 0)
            value_item = self.table.item(row, 1)
            key = key_item.text().strip() if key_item else ""
            if key:
                result[key] = value_item.text() if value_item else ""
        return result

    def set_items(self, items: Mapping[str, Any]) -> None:
        """Replace the rows without emitting ``changed``."""
        self._silent = True
        try:
            self.table.setRowCount(0)
            for key, value in items.items():
                self._append(str(key), format_value(value))
        finally:
            self._silent = False

    def add_row(self) -> None:
        self._append("", "")
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)
        item = self.table.item(self.table.rowCount() - 1, 0)
        if item is not None:
            self.table.editItem(item)

    def remove_selected(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        if not rows and self.table.currentRow() >= 0:
            rows = {self.table.currentRow()}
        for row in sorted(rows, reverse=True):
            self.table.removeRow(row)
        if rows:
            self.changed.emit()

    def _append(self, key: str, value: str) -> None:
        was_silent = self._silent
        self._silent = True
        try:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(key))
            self.table.setItem(row, 1, QTableWidgetItem(value))
        finally:
            self._silent = was_silent

    def _emit(self, *_args: object) -> None:
        if not self._silent:
            self.changed.emit()


class PairsEdit(QWidget):
    """A list of text items, each two cells joined by a separator (``widget = "pairs"``).

    Used for the sort-by-type rules (``*.pdf`` | ``Documents/PDF`` is ``*.pdf=Documents/PDF``).
    An item is split at the first separator. A row whose second cell is empty gives the first
    cell alone, and a row with both cells empty is ignored. Rows keep their order, which matters
    (the first matching rule wins), so they can be moved up and down.
    """

    changed = Signal()

    def __init__(
        self, headers: Sequence[str], separator: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.separator = separator
        self._silent = False
        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(list(headers))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setMinimumHeight(160)
        self.add_button = QPushButton("Add", self)
        self.remove_button = QPushButton("Remove", self)
        self.up_button = QPushButton("Move up", self)
        self.down_button = QPushButton("Move down", self)
        buttons = QVBoxLayout()
        for button in (self.add_button, self.remove_button, self.up_button, self.down_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.table.itemChanged.connect(self._emit)
        self.add_button.clicked.connect(lambda _c=False: self.add_row())
        self.remove_button.clicked.connect(lambda _c=False: self.remove_current())
        self.up_button.clicked.connect(lambda _c=False: self.move_current(-1))
        self.down_button.clicked.connect(lambda _c=False: self.move_current(1))

    def rows(self) -> list[tuple[str, str]]:
        """Every row as ``(first, second)`` text, stripped, blank rows included."""
        result: list[tuple[str, str]] = []
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, column) for column in (0, 1)]
            first, second = (c.text().strip() if c is not None else "" for c in cells)
            result.append((first, second))
        return result

    def items(self) -> list[str]:
        """The rows joined with the separator, top to bottom; blank rows are left out."""
        result: list[str] = []
        for first, second in self.rows():
            if not first and not second:
                continue
            result.append(f"{first}{self.separator}{second}" if second else first)
        return result

    def set_items(self, items: Iterable[Any]) -> None:
        """Replace the rows without emitting ``changed``."""
        self._silent = True
        try:
            self.table.setRowCount(0)
            for item in items:
                text = format_value(item)
                first, sep, second = text.partition(self.separator)
                self._append(first.strip(), second.strip() if sep else "")
        finally:
            self._silent = False

    def add_row(self) -> None:
        self._append("", "")
        row = self.table.rowCount() - 1
        self.table.setCurrentCell(row, 0)
        item = self.table.item(row, 0)
        if item is not None and self.table.isVisible():
            self.table.editItem(item)

    def remove_current(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self.table.removeRow(row)
        self.changed.emit()

    def move_current(self, delta: int) -> None:
        """Move the current row up (-1) or down (+1)."""
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.table.rowCount():
            return
        rows = self.rows()
        rows[row], rows[target] = rows[target], rows[row]
        column = max(self.table.currentColumn(), 0)
        self._silent = True
        try:
            for index in (row, target):
                for col, text in enumerate(rows[index]):
                    cell = self.table.item(index, col)
                    if cell is None:
                        self.table.setItem(index, col, QTableWidgetItem(text))
                    else:
                        cell.setText(text)
        finally:
            self._silent = False
        self.table.setCurrentCell(target, column)
        self.changed.emit()

    def _append(self, first: str, second: str) -> None:
        was_silent = self._silent
        self._silent = True
        try:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(first))
            self.table.setItem(row, 1, QTableWidgetItem(second))
        finally:
            self._silent = was_silent

    def _emit(self, *_args: object) -> None:
        if not self._silent:
            self.changed.emit()


class DynamicChoiceEdit(QWidget):
    """An editable drop-down whose suggestions are only known at run time (``choices_from``).

    The value is the text in the box, so a value that is not in the list stays valid. The list is
    filled by the form through :meth:`set_choices` (from a worker); an empty list leaves a plain
    editable box. ``refreshRequested`` asks the form to load the list again.
    """

    changed = Signal()
    refreshRequested = Signal()

    def __init__(self, can_refresh: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.loading = False
        self.combo = _WheelGuardCombo()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.setMinimumContentsLength(18)
        self.refresh_button = QToolButton(self)
        self.refresh_button.setText("Refresh")
        self.refresh_button.setToolTip("Ask again for the list of choices.")
        self.refresh_button.setVisible(can_refresh)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.combo, 1)
        layout.addWidget(self.refresh_button)
        self.combo.editTextChanged.connect(lambda _t: self.changed.emit())
        self.refresh_button.clicked.connect(lambda _c=False: self.refreshRequested.emit())

    def text(self) -> str:
        return self.combo.currentText()

    def set_text(self, text: str) -> None:
        self.combo.setEditText(text)

    def choices(self) -> list[str]:
        return [self.combo.itemText(i) for i in range(self.combo.count())]

    def set_loading(self, loading: bool) -> None:
        self.loading = loading
        self.refresh_button.setEnabled(not loading)
        self.refresh_button.setText("Loading…" if loading else "Refresh")

    def set_choices(self, items: Iterable[str]) -> None:
        """Replace the suggestions and keep the text the box holds. Emits nothing."""
        current = self.combo.currentText()
        self.combo.blockSignals(True)
        try:
            self.combo.clear()
            self.combo.addItems([str(item) for item in items])
            self.combo.setEditText(current)
        finally:
            self.combo.blockSignals(False)


# ----- the form --------------------------------------------------------------------------------


class _Field:
    """One field's widgets and how to read and write its value."""

    def __init__(
        self,
        spec: FieldSpec,
        label: QLabel,
        editor: QWidget,
        holder: QWidget,
        error_label: QLabel,
        getter: Callable[[], Any],
        setter: Callable[[Any], None],
    ) -> None:
        self.spec = spec
        self.label = label
        self.editor = editor
        self.holder = holder
        self.error_label = error_label
        self.get = getter
        self.set = setter
        self.layout: QFormLayout | None = None
        self.depends: tuple[str, frozenset[str]] | None = _parse_depends(spec.depends_on)


class _Group:
    """A group box with its rows, so the whole box can be hidden when every row is hidden."""

    def __init__(self, box: QGroupBox, layout: QFormLayout) -> None:
        self.box = box
        self.layout = layout
        self.names: list[str] = []


def _parse_depends(text: str | None) -> tuple[str, frozenset[str]] | None:
    if not text or "=" not in text:
        return None
    name, _, values = text.partition("=")
    wanted = frozenset(v.strip().casefold() for v in values.split("|"))
    return name.strip(), wanted


def _tooltip(spec: FieldSpec) -> str:
    """Help text for a field: description, allowed range and default, as small rich text."""
    lines: list[str] = []
    if spec.locked and "locked" not in spec.description.casefold():
        lines.append(
            "🔒 Locked default: a tuned or safety value. You are asked before changing it."
        )
    if spec.description:
        lines.append(spec.description)
    if spec.bounds_text:
        lines.append(f"Allowed: {spec.bounds_text}")
    if not spec.required:
        default = format_value(spec.default)
        if spec.default is None:
            default = "app setting" if spec.nullable else "none"
        elif default == "":
            default = "(empty)" if spec.kind in ("str", "path") else "(none)"
        lines.append(f"Default: {default}")
    return "<br>".join(html.escape(line, quote=False) for line in lines)


def _is_folder_field(spec: FieldSpec) -> bool:
    if spec.widget in ("folder", "folders"):
        return True
    if spec.widget == "file":
        return False
    return spec.kind == "path" or (spec.kind == "list" and spec.item_kind == "path")


def folder_field_names(model: type[BaseModel]) -> list[str]:
    """Names of a model's folder fields (single or list), in declaration order."""
    return [s.name for s in describe(model) if _is_folder_field(s)]


def _is_path_list(spec: FieldSpec) -> bool:
    return spec.kind == "list" and (spec.widget == "folders" or spec.item_kind == "path")


def _is_choice_list(spec: FieldSpec) -> bool:
    return spec.kind == "list" and spec.item_kind == "choice" and bool(spec.choices)


def _make_spin(spec: FieldSpec) -> QSpinBox | QDoubleSpinBox:
    """A spin box whose range comes from the field's bounds."""
    if spec.kind == "int":
        low = -_INT_MAX
        high = _INT_MAX
        if spec.ge is not None:
            low = math.ceil(spec.ge)
        elif spec.gt is not None:
            low = math.floor(spec.gt) + 1
        if spec.le is not None:
            high = math.floor(spec.le)
        elif spec.lt is not None:
            high = math.ceil(spec.lt) - 1
        spin = _WheelGuardSpin()
        spin.setRange(max(low, -_INT_MAX), min(high, _INT_MAX))
        return spin
    epsilon = 10.0**-_FLOAT_DECIMALS
    flow = -_FLOAT_LIMIT
    fhigh = _FLOAT_LIMIT
    if spec.ge is not None:
        flow = spec.ge
    elif spec.gt is not None:
        flow = spec.gt + epsilon
    if spec.le is not None:
        fhigh = spec.le
    elif spec.lt is not None:
        fhigh = spec.lt - epsilon
    dspin = _WheelGuardDoubleSpin()
    dspin.setDecimals(_FLOAT_DECIMALS)
    dspin.setRange(flow, fhigh)
    span = fhigh - flow
    dspin.setSingleStep(0.01 if span <= 10 else 0.1 if span <= 1000 else 1.0)
    return dspin


def _set_spin(spin: QSpinBox | QDoubleSpinBox, spec: FieldSpec, value: Any) -> None:
    """Show ``value`` (``None`` or junk becomes the lowest allowed value)."""
    try:
        number = float(value)
    except TypeError, ValueError:
        number = float(spin.minimum())
    if isinstance(spin, QSpinBox):
        spin.setValue(round(number))
    else:
        spin.setValue(number)


class SchemaForm(QWidget):
    """Edit the fields of a pydantic model. See the module docstring for the rules."""

    changed = Signal()

    def __init__(
        self,
        model: type[BaseModel],
        values: Mapping[str, Any] | BaseModel | None = None,
        *,
        exclude: Collection[str] = (),
        confirm_locked: ConfirmLocked | None = None,
        load_choices: LoadChoices | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.model = model
        self._load_choices = load_choices
        self._dynamic: dict[str, tuple[str, DynamicChoiceEdit]] = {}
        self._choices_requested = False
        self._exclude = frozenset(exclude)
        all_specs = describe(model)
        self.specs: list[FieldSpec] = [s for s in all_specs if s.name not in self._exclude]
        self._excluded_specs = [s for s in all_specs if s.name in self._exclude]
        self._extra: dict[str, Any] = {}
        self._confirm = confirm_locked or _default_confirm_locked
        self._fields: dict[str, _Field] = {}
        self._groups: list[_Group] = []
        self._last: dict[str, Any] = {}
        self._confirmed: set[str] = set()
        self._parse_errors: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._loading = 0
        self._hidden: set[str] = set()
        self._advanced_button: QToolButton | None = None
        self._advanced_body: QWidget | None = None

        self._banner = QLabel(self)
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(_ERROR_STYLE)
        self._banner.setVisible(False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._banner)
        basic = [s for s in self.specs if not s.advanced]
        advanced = [s for s in self.specs if s.advanced]
        self._add_part(outer, basic)
        if advanced:
            self._add_advanced(outer, advanced)
        outer.addStretch(1)

        if values is None:
            self.reset()
        else:
            self.set_values(values)

    # ----- reading and writing ---------------------------------------------------------------

    def raw_values(self) -> dict[str, Any]:
        """Every field as a Python value pydantic accepts. Nothing is validated here.

        Excluded fields pass through unchanged. A path box left empty is left out (so a required
        one is reported as missing). A list box that cannot be parsed contributes its raw text,
        and the problem is reported by ``build()``.
        """
        result: dict[str, Any] = dict(self._extra)
        self._parse_errors = {}
        for name, field in self._fields.items():
            value = self._read(field)
            if isinstance(value, _ParseFailure):
                self._parse_errors[name] = value.message
                result[name] = value.text
            elif not isinstance(value, _Missing):
                result[name] = value
        return result

    def set_values(self, values: Mapping[str, Any] | BaseModel) -> None:
        """Show ``values``; fields not given go back to their defaults. Never asks to confirm."""
        self._load(_as_mapping(values), partial=False)

    def update_values(self, values: Mapping[str, Any]) -> None:
        """Change only the given fields. Never asks to confirm."""
        self._load(values, partial=True)

    def reset(self) -> None:
        """Put every field back to its default."""
        self._load({}, partial=False)

    def build(self) -> BaseModel:
        """Validate the current values into a model. On problems, show them and re-raise."""
        raw = self.raw_values()
        self._clear_errors()
        try:
            instance = self.model.model_validate(raw)
        except ValidationError as exc:
            errors = self._collect(exc)
            errors.update(self._parse_errors)  # the specific parse message beats pydantic's
            self._show_errors(errors)
            raise
        if self._parse_errors:
            details = [
                InitErrorDetails(
                    type=PydanticCustomError("unparsable", "{message}", {"message": message}),
                    loc=(name,),
                    input=raw.get(name),
                )
                for name, message in self._parse_errors.items()
            ]
            exc = ValidationError.from_exception_data(self.model.__name__, details)
            self._show_errors(dict(self._parse_errors))
            raise exc
        return instance

    def errors(self) -> dict[str, str]:
        """Messages from the last ``build()``: field name (``""`` for model-level) to text."""
        return dict(self._errors)

    def editor(self, name: str) -> QWidget:
        """The editor widget of a field (the container for a nullable number)."""
        return self._fields[name].editor

    def folder_fields(self) -> list[str]:
        """Names of the folder fields (single or list), in declaration order."""
        return [s.name for s in self.specs if _is_folder_field(s)]

    def set_first_folder(self, path: str) -> bool:
        """Put ``path`` in the first folder field (a list becomes ``[path]``).

        Emits ``changed`` once. Returns False if the form has no folder field.
        """
        names = self.folder_fields()
        if not names:
            return False
        field = self._fields[names[0]]
        value: Any = [path] if field.spec.kind == "list" else path
        with self._loading_block():
            field.set(value)
            self._last[field.spec.name] = self._snapshot(field)
        self._apply_visibility()
        self.changed.emit()
        return True

    def hide_field(self, name: str, hidden: bool = True) -> None:
        """Hide (or show again) one row. A hidden field still holds and returns its value."""
        if hidden:
            self._hidden.add(name)
        else:
            self._hidden.discard(name)
        self._apply_visibility()

    def set_field_value(self, name: str, value: Any) -> None:
        """Show ``value`` in one field without emitting ``changed`` or asking to confirm."""
        self._load({name: value}, partial=True)

    def field_spec(self, name: str) -> FieldSpec:
        return self._fields[name].spec

    def set_advanced_visible(self, visible: bool) -> None:
        """Expand or collapse the "Advanced options" section."""
        if self._advanced_button is not None:
            self._advanced_button.setChecked(visible)

    # ----- building the widgets --------------------------------------------------------------

    def _add_part(self, outer: QVBoxLayout, specs: list[FieldSpec]) -> None:
        by_group: dict[str, list[FieldSpec]] = {}
        for spec in specs:
            by_group.setdefault(spec.group, []).append(spec)
        for title, members in by_group.items():
            box = QGroupBox(title)
            form = QFormLayout(box)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            group = _Group(box, form)
            for spec in members:
                self._add_field(form, spec)
                group.names.append(spec.name)
            self._groups.append(group)
            outer.addWidget(box)

    def _add_advanced(self, outer: QVBoxLayout, specs: list[FieldSpec]) -> None:
        button = QToolButton(self)
        button.setText("More settings for this tool")
        button.setCheckable(True)
        button.setChecked(False)
        button.setAutoRaise(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setArrowType(Qt.ArrowType.RightArrow)
        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body.setVisible(False)
        self._add_part(body_layout, specs)

        def toggle(checked: bool) -> None:
            button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
            body.setVisible(checked)

        button.toggled.connect(toggle)
        self._advanced_button = button
        self._advanced_body = body
        outer.addWidget(button)
        outer.addWidget(body)

    def _add_field(self, form: QFormLayout, spec: FieldSpec) -> None:
        name = spec.name

        def notify() -> None:
            self._on_edited(name)

        editor, getter, setter = self._make_editor(spec, notify)
        tip = _tooltip(spec)
        title = f"🔒 {spec.title}" if spec.locked else spec.title
        label = QLabel(title)
        label.setWordWrap(True)
        label.setToolTip(tip)
        label.setBuddy(editor)
        editor.setToolTip(tip)
        error_label = QLabel()
        error_label.setWordWrap(True)
        error_label.setStyleSheet(_ERROR_STYLE)
        error_label.setVisible(False)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addWidget(editor)
        column.addWidget(error_label)
        form.addRow(label, holder)
        field = _Field(spec, label, editor, holder, error_label, getter, setter)
        field.layout = form
        self._fields[name] = field

    def _make_editor(
        self, spec: FieldSpec, notify: Callable[[], None]
    ) -> tuple[QWidget, Callable[[], Any], Callable[[Any], None]]:
        """The editor widget for ``spec`` with its getter and setter (and the change wiring)."""
        nullable_hint = "Empty: use the app setting" if spec.nullable else ""
        if spec.kind == "bool":
            check = QCheckBox()
            check.toggled.connect(lambda _c: notify())
            return check, check.isChecked, lambda v: check.setChecked(bool(v))

        if spec.kind in ("int", "float"):
            spin = _make_spin(spec)
            if not spec.nullable:
                spin.valueChanged.connect(lambda _v: notify())
                return spin, spin.value, lambda v: _set_spin(spin, spec, v)
            nullable = NullableNumber(spin)
            nullable.changed.connect(notify)

            def set_nullable(value: Any) -> None:
                if value is None:
                    nullable.set_value(None)
                else:
                    _set_spin(spin, spec, value)
                    nullable.use_default.setChecked(False)

            return nullable, nullable.value, set_nullable

        if spec.kind == "choice":
            return self._choice_editor(spec, notify)

        if spec.kind == "str" and spec.choices_from:
            return self._dynamic_editor(spec, notify)

        if spec.kind == "list" and spec.widget == "pairs":
            headers = spec.pair_headers or ("Item", "Value")
            pairs = PairsEdit(headers, spec.pair_separator or "=")
            pairs.changed.connect(notify)
            return (
                pairs,
                pairs.items,
                lambda v: pairs.set_items([] if v is None else _as_list(v)),
            )

        if spec.kind == "path" and not spec.item_kind:
            mode = "file" if spec.widget == "file" else "folder"
            placeholder = nullable_hint or (
                "Choose a file…" if mode == "file" else "Choose a folder…"
            )
            picker = PathEdit(mode, placeholder)
            picker.changed.connect(notify)
            return (
                picker,
                lambda: self._read_path(spec, picker.text()),
                lambda v: picker.set_text("" if v is None else str(v)),
            )

        if _is_path_list(spec):
            paths = PathListEdit()
            paths.changed.connect(notify)
            return (
                paths,
                paths.paths,
                lambda v: paths.set_paths([] if v is None else [str(p) for p in _as_list(v)]),
            )

        if _is_choice_list(spec):
            checklist = OrderedChecklist(spec.choices)
            checklist.changed.connect(notify)
            return (
                checklist,
                lambda: self._read_choice_list(spec, checklist),
                lambda v: checklist.set_checked(
                    [] if v is None else [format_value(item) for item in _as_list(v)]
                ),
            )

        if spec.kind == "dict":
            table = KeyValueEdit()
            table.changed.connect(notify)
            return (
                table,
                table.items,
                lambda v: table.set_items({} if v is None else cast("Mapping[str, Any]", v)),
            )

        # Text: plain strings, comma-separated lists and anything unrecognised.
        line = QLineEdit()
        if spec.widget == "template":
            line.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        if spec.kind == "str":
            line.setPlaceholderText(nullable_hint)
            getter: Callable[[], Any] = lambda: self._read_str(spec, line.text())  # noqa: E731
        else:
            line.setPlaceholderText(nullable_hint or "Comma-separated")
            getter = lambda: self._read_parsed(spec, line.text())  # noqa: E731

        def set_text(value: Any) -> None:
            if isinstance(value, str) and spec.kind != "str":
                line.setText(value)  # raw text put back after a failed parse
            elif spec.kind == "list" and value is not None:
                line.setText(list_text(_as_list(value)))
            else:
                line.setText(format_value(value))

        if spec.locked:
            line.editingFinished.connect(notify)  # one prompt per edit, not one per keystroke
        else:
            line.textChanged.connect(lambda _t: notify())
        return line, getter, set_text

    def _choice_editor(
        self, spec: FieldSpec, notify: Callable[[], None]
    ) -> tuple[QWidget, Callable[[], Any], Callable[[Any], None]]:
        combo = _WheelGuardCombo()
        if spec.nullable:
            combo.addItem(APP_SETTING_TEXT)
        for text in spec.choices:
            combo.addItem(text)
        combo.currentIndexChanged.connect(lambda _i: notify())

        def get() -> Any:
            if spec.nullable and combo.currentIndex() == 0:
                return None
            text = combo.currentText()
            try:
                return parse_value(spec, text)
            except ValueError:
                return text

        def set_choice(value: Any) -> None:
            if value is None:
                if spec.nullable:
                    combo.setCurrentIndex(0)
                return
            text = format_value(value)
            index = combo.findText(text)
            if index < 0:  # keep an unknown value visible so build() can complain about it
                combo.addItem(text)
                index = combo.count() - 1
            combo.setCurrentIndex(index)

        return combo, get, set_choice

    def _dynamic_editor(
        self, spec: FieldSpec, notify: Callable[[], None]
    ) -> tuple[QWidget, Callable[[], Any], Callable[[Any], None]]:
        """An editable drop-down for a ``choices_from`` text field (filled when shown)."""
        edit = DynamicChoiceEdit(can_refresh=self._load_choices is not None)
        line = edit.combo.lineEdit()
        if spec.nullable and line is not None:
            line.setPlaceholderText("Empty: use the app setting")
        edit.changed.connect(notify)
        name = spec.name
        source = spec.choices_from or ""
        edit.refreshRequested.connect(lambda: self.refresh_choices(name))
        self._dynamic[name] = (source, edit)
        return (
            edit,
            lambda: self._read_str(spec, edit.text()),
            lambda v: edit.set_text("" if v is None else format_value(v)),
        )

    # ----- run-time choices -------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        if not self._choices_requested and self._dynamic:
            self._choices_requested = True
            self.refresh_choices()

    def refresh_choices(self, name: str | None = None) -> list[Worker]:
        """Load the suggestions of one ``choices_from`` field (or all of them) in workers."""
        started: list[Worker] = []
        if self._load_choices is None:
            return started
        names = [name] if name is not None else list(self._dynamic)
        for field_name in names:
            entry = self._dynamic.get(field_name)
            if entry is None or entry[1].loading:
                continue
            source, edit = entry
            worker = self._load_choices(source)
            if worker is None:
                continue
            edit.set_loading(True)
            worker.succeeded.connect(lambda items, n=field_name: self._choices_ready(n, items))
            worker.finished.connect(lambda n=field_name: self._choices_done(n))
            worker.start()
            started.append(worker)
        return started

    def _choices_ready(self, name: str, items: object) -> None:
        if not shiboken6.isValid(self) or not isinstance(items, list):
            return
        entry = self._dynamic.get(name)
        if entry is not None and shiboken6.isValid(entry[1]):
            entry[1].set_choices(str(item) for item in cast("list[Any]", items))

    def _choices_done(self, name: str) -> None:
        if not shiboken6.isValid(self):
            return
        entry = self._dynamic.get(name)
        if entry is not None and shiboken6.isValid(entry[1]):
            entry[1].set_loading(False)

    # ----- reading helpers -------------------------------------------------------------------

    @staticmethod
    def _read_path(spec: FieldSpec, text: str) -> Any:
        wanted = text.strip()
        if not wanted:
            return None if spec.nullable else _MISSING
        try:
            value = parse_value(spec, wanted)
        except ValueError:
            return wanted
        return wanted if value is None else value

    @staticmethod
    def _read_str(spec: FieldSpec, text: str) -> Any:
        if text == "" and spec.nullable:
            return None
        return text

    @staticmethod
    def _read_parsed(spec: FieldSpec, text: str) -> Any:
        try:
            return parse_value(spec, text)
        except ValueError as exc:
            return _ParseFailure(text, str(exc).partition(": ")[2] or str(exc))

    @staticmethod
    def _read_choice_list(spec: FieldSpec, checklist: OrderedChecklist) -> Any:
        try:
            return parse_value(spec, ", ".join(checklist.checked()))
        except ValueError as exc:
            return _ParseFailure(", ".join(checklist.checked()), str(exc))

    def _read(self, field: _Field) -> Any:
        return field.get()

    def _snapshot(self, field: _Field) -> Any:
        """The field's current value in a form that can be compared and put back."""
        value = self._read(field)
        return value.text if isinstance(value, _ParseFailure) else value

    # ----- loading ---------------------------------------------------------------------------

    def _loading_block(self) -> _Loading:
        return _Loading(self)

    def _load(self, values: Mapping[str, Any], *, partial: bool) -> None:
        with self._loading_block():
            for spec in self.specs:
                field = self._fields[spec.name]
                if spec.name in values:
                    value = values[spec.name]
                elif partial:
                    continue
                else:
                    value = spec.default
                field.set(value)
                self._last[spec.name] = self._snapshot(field)
            for spec in self._excluded_specs:
                if spec.name in values:
                    self._extra[spec.name] = values[spec.name]
                elif not partial and not spec.required:
                    self._extra[spec.name] = spec.default
                elif not partial:
                    self._extra.pop(spec.name, None)
        self._apply_visibility()

    # ----- change handling -------------------------------------------------------------------

    def _on_edited(self, name: str) -> None:
        """The person changed a field: check the lock, re-evaluate visibility, tell the caller."""
        if self._loading:
            return
        field = self._fields[name]
        new = self._snapshot(field)
        old = self._last.get(name)
        if new == old:
            return
        spec = field.spec
        if (
            spec.locked
            and name not in self._confirmed
            and format_value(new) != format_value(spec.default)
        ):
            if not self._confirm(spec, self):
                with self._loading_block():
                    field.set(old)
                return
            self._confirmed.add(name)
        self._last[name] = new
        self._apply_visibility()
        self.changed.emit()

    def _current(self, name: str) -> Any:
        field = self._fields.get(name)
        if field is not None:
            return self._snapshot(field)
        return self._extra.get(name)

    def _apply_visibility(self) -> None:
        for field in self._fields.values():
            if field.layout is None:
                continue
            if field.spec.name in self._hidden:
                field.layout.setRowVisible(field.holder, False)
                continue
            if field.depends is None:
                field.layout.setRowVisible(field.holder, True)
                continue
            other, wanted = field.depends
            current = self._current(other)
            if isinstance(current, _Missing):
                current = None
            field.layout.setRowVisible(field.holder, format_value(current).casefold() in wanted)
        for group in self._groups:
            visible = any(not self._fields[n].holder.isHidden() for n in group.names)
            group.box.setVisible(visible)

    # ----- errors ----------------------------------------------------------------------------

    @staticmethod
    def _collect(exc: ValidationError) -> dict[str, str]:
        messages: dict[str, list[str]] = {}
        for item in exc.errors():
            loc = item["loc"]
            name = str(loc[0]) if loc else ""
            message = str(item["msg"]).removeprefix("Value error, ")
            if item["type"] == "missing":
                message = "This is required."
            elif len(loc) > 1:
                message = f"{'.'.join(str(part) for part in loc[1:])}: {message}"
            messages.setdefault(name, []).append(message)
        return {name: "; ".join(parts) for name, parts in messages.items()}

    def _clear_errors(self) -> None:
        self._errors = {}
        self._banner.setText("")
        self._banner.setVisible(False)
        for field in self._fields.values():
            field.error_label.setText("")
            field.error_label.setVisible(False)

    def _show_errors(self, errors: dict[str, str]) -> None:
        self._errors = dict(errors)
        banner: list[str] = []
        for name, message in errors.items():
            field = self._fields.get(name)
            if field is None:
                banner.append(message if not name else f"{name}: {message}")
                continue
            field.error_label.setText(message)
            field.error_label.setVisible(True)
            if field.spec.advanced and self._advanced_button is not None:
                self._advanced_button.setChecked(True)
        if banner:
            self._banner.setText("\n".join(banner))
            self._banner.setVisible(True)


class _ParseFailure:
    """Text from a box that could not be turned into a value, with the reason."""

    def __init__(self, text: str, message: str) -> None:
        self.text = text
        self.message = message


class _Loading:
    """Context manager that makes the form ignore editor signals while values are set in code."""

    def __init__(self, form: SchemaForm) -> None:
        self._form = form

    def __enter__(self) -> None:
        self._form._loading += 1  # pyright: ignore[reportPrivateUsage]

    def __exit__(self, *_exc: object) -> None:
        self._form._loading -= 1  # pyright: ignore[reportPrivateUsage]


def _as_mapping(values: Mapping[str, Any] | BaseModel) -> Mapping[str, Any]:
    if isinstance(values, BaseModel):
        return values.model_dump()
    return values


def list_text(items: Sequence[Any]) -> str:
    """List items as text for the comma-separated box, which ``parse_value`` reads back.

    Plain items are joined with ", ". When an item would not survive that (it holds a comma, has
    spaces at either end or is empty, or the text would look like JSON), the list is written as
    a JSON list instead, so ``["a, b", "c"]`` stays two items.
    """
    texts = [format_value(item) for item in items]
    awkward = any("," in t or t != t.strip() or not t for t in texts)
    if awkward or (texts and texts[0].startswith("[")):
        return json.dumps(texts, ensure_ascii=False)
    return ", ".join(texts)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list | tuple | set | frozenset):
        return list(cast("Iterable[Any]", value))
    return [value]


def _default_confirm_locked(spec: FieldSpec, parent: QWidget) -> bool:
    """Ask before a 🔒 value is changed. The default answer is No."""
    detail = spec.description.replace("🔒 Locked default.", "").strip()
    text = (
        f"“{spec.title}” is a locked value: it was tuned by hand or protects your files, "
        "so changing it can give worse results or put data at risk."
    )
    if detail:
        text += f"\n\n{detail}"
    text += "\n\nChange it anyway?"
    answer = QMessageBox.warning(
        parent,
        "Change a locked value?",
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes
