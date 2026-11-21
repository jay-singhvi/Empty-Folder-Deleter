"""The window for adding or changing one step of a recipe: pick a tool, then its options.

The options form is the same ``SchemaForm`` a tool page uses. The working folder is not part of a
step (it is filled in when the recipe runs), so the tool's folder field stays hidden unless "Use
other folders" is ticked.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.recipe import (
    RecipeStep,
    not_remembered_fields,
    step_from_options,
    uses_own_folders,
    workspace_field,
)
from various_tools.core.schema import describe
from various_tools.gui.widgets.schema_form import SchemaForm
from various_tools.gui.wording import USE_OTHER_FOLDERS
from various_tools.tools.base import Tool


class RecipeStepDialog(QDialog):
    """Add a step (``step`` is None) or change one. ``result_step()`` holds the answer."""

    def __init__(
        self,
        tools: Mapping[str, Tool],
        placeholder_folder: Path,
        step: RecipeStep | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a step" if step is None else "Change this step")
        self.resize(640, 560)
        self._tools = tools
        self._placeholder = str(placeholder_folder)
        self._form: SchemaForm | None = None
        self._tool: Tool | None = None
        self.result_step: RecipeStep | None = None

        self.tool_combo = QComboBox()
        for tool in sorted(tools.values(), key=lambda t: t.name.casefold()):
            self.tool_combo.addItem(tool.name, tool.id)
        self.tool_combo.setEnabled(step is None)  # a step keeps its tool; add a new one instead
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.other_folders_check = QCheckBox(USE_OTHER_FOLDERS)
        self.other_folders_check.setToolTip(
            "Look in folders of your own for this step instead of the working folder."
        )
        self.label_edit = QLineEdit(step.label if step is not None else "")
        self.label_edit.setPlaceholderText("A note for this step (optional)")
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Tool"))
        layout.addWidget(self.tool_combo)
        layout.addWidget(self.description)
        layout.addWidget(self.other_folders_check)
        layout.addWidget(self._scroll, 1)
        layout.addWidget(self.label_edit)
        layout.addWidget(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        if step is not None:
            index = self.tool_combo.findData(step.tool)
            if index >= 0:
                self.tool_combo.setCurrentIndex(index)
        self.tool_combo.currentIndexChanged.connect(lambda _i: self._show_tool(None))
        self.other_folders_check.toggled.connect(self._other_folders_toggled)
        self._show_tool(step)

    @property
    def form(self) -> SchemaForm | None:
        return self._form

    @property
    def tool(self) -> Tool | None:
        return self._tool

    def _show_tool(self, step: RecipeStep | None) -> None:
        tool = self._tools.get(str(self.tool_combo.currentData()))
        self._tool = tool
        if tool is None:
            return
        self.description.setText(tool.description)
        name = workspace_field(tool)
        own = step is not None and uses_own_folders(step, tool)
        blocked = not_remembered_fields(tool)
        values: dict[str, Any] = (
            {k: v for k, v in step.options.items() if k not in blocked} if step is not None else {}
        )
        form = SchemaForm(tool.Options, values)
        self._form = form
        self._scroll.setWidget(form)
        self.other_folders_check.blockSignals(True)
        self.other_folders_check.setChecked(own)
        self.other_folders_check.blockSignals(False)
        self.other_folders_check.setVisible(name is not None)
        if name is not None:
            form.hide_field(name, not own)

    def _other_folders_toggled(self, on: bool) -> None:
        tool, form = self._tool, self._form
        name = workspace_field(tool) if tool is not None else None
        if form is not None and name is not None:
            form.hide_field(name, not on)

    def _placeholder_value(self, tool: Tool, name: str) -> Any:
        is_list = any(s.name == name and s.kind == "list" for s in describe(tool.Options))
        return [self._placeholder] if is_list else self._placeholder

    def accept(self) -> None:
        tool, form = self._tool, self._form
        if tool is None or form is None:
            return
        name = workspace_field(tool)
        own = bool(name) and self.other_folders_check.isChecked()
        if name is not None and not own:
            # Hidden and not saved; it only lets the options validate as a whole.
            form.set_field_value(name, self._placeholder_value(tool, name))
        try:
            options = form.build()
        except ValidationError:
            return  # the form shows what is wrong; the window stays open
        step = step_from_options(tool, options.model_dump(mode="json"), own_folders=own)
        step.label = self.label_edit.text().strip()
        self.result_step = step
        super().accept()
