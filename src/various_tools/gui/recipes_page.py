"""The Recipes page: saved lists of steps, and running one step by step.

Left: the recipes (New, Duplicate, Rename, Delete). Right: the steps of the chosen recipe (Add,
Change, move up and down, Remove) and *Run recipe*.

Running a recipe reuses the tool page instead of copying it: each step is a real ``ToolPage``
built from the step's options, scanned at once, with its normal results list, Apply window
(batches and the OneDrive wait), cloud-only banner and safety questions. When the page's apply
has finished (or on *Skip this step*) the next step opens. *Stop* ends the recipe. Every apply is
journaled as its own run, marked with the recipe (``ToolPage.recipe_tag``), so History can show
and undo it.

Nothing here changes a file by itself. Tests replace :attr:`confirm`, :attr:`inform`,
:attr:`ask_name` and :attr:`edit_step`, and :attr:`prepare_page` to quiet each step's page.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import shiboken6
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.recipe import (
    Recipe,
    RecipeStep,
    check_step,
    new_recipe_run_id,
    recipe_tag,
    starter_recipes,
    step_title,
    uses_own_folders,
    workspace_field,
)
from various_tools.gui.recipe_store import RecipeStore
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.widgets.recipe_step_dialog import RecipeStepDialog
from various_tools.gui.wording import (
    CHOOSE_WORKSPACE_FIRST,
    NEEDS_ATTENTION,
    NEW_RECIPE_NAME,
    NO_RECIPES_TEXT,
    RECIPES_INTRO,
    RECIPES_TITLE,
    RUN_RECIPE,
    SKIP_STEP,
    STOP_RECIPE,
)

log = logging.getLogger(__name__)

_RED = QBrush(QColor("#b00020"))


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


def _default_ask_name(parent: QWidget) -> Callable[[str, str, str], str | None]:
    def ask(title: str, label: str, default: str) -> str | None:
        text, ok = QInputDialog.getText(parent, title, label, text=default)
        return str(text).strip() if ok and str(text).strip() else None

    return ask


@dataclass
class _Run:
    """The recipe run in progress."""

    recipe_name: str
    steps: list[RecipeStep]
    recipe_run_id: str = field(default_factory=new_recipe_run_id)
    index: int = 0
    applied: int = 0
    skipped: int = 0
    stopping: bool = False
    step_applied: bool = False
    page: ToolPage | None = None


class RecipesPage(QWidget):
    """Recipes list and editor, plus the step-by-step runner."""

    runApplied = Signal(int)
    runUndone = Signal(int)
    busyChanged = Signal(bool)
    runFinished = Signal(str)

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.store = RecipeStore(state.settings_path)
        self.run: _Run | None = None
        self._closing = False
        self.confirm: Callable[[str, str], bool] = _default_confirm(self)
        self.inform: Callable[[str, str], None] = _default_inform(self)
        self.ask_name: Callable[[str, str, str], str | None] = _default_ask_name(self)
        self.edit_step: Callable[[RecipeStep | None], RecipeStep | None] = self._open_step_dialog
        self.prepare_page: Callable[[ToolPage], None] = lambda _page: None

        title = QLabel(f"<h2>{RECIPES_TITLE}</h2>")
        intro = QLabel(RECIPES_INTRO)
        intro.setWordWrap(True)
        self.notice_label = QLabel()
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)

        # ----- left: the recipes ----------------------------------------------------------
        self.recipe_list = QListWidget()
        self.new_button = QPushButton("New")
        self.duplicate_button = QPushButton("Duplicate")
        self.rename_button = QPushButton("Rename")
        self.delete_button = QPushButton("Delete")
        left_buttons = QHBoxLayout()
        for button in (self.new_button, self.duplicate_button, self.rename_button):
            left_buttons.addWidget(button)
        left_buttons.addWidget(self.delete_button)
        self.starter_box = QWidget()
        starter_layout = QVBoxLayout(self.starter_box)
        starter_layout.setContentsMargins(0, 0, 0, 0)
        starter_layout.addWidget(QLabel(NO_RECIPES_TEXT))
        self.starter_buttons: list[QPushButton] = []
        for recipe in starter_recipes():
            names = ", then ".join(self._tool_name(s.tool) for s in recipe.steps)
            button = QPushButton(f"{recipe.name} ({names})")
            button.setToolTip("Add this recipe to your list. You can change it afterwards.")
            button.clicked.connect(lambda _c=False, r=recipe: self.add_starter(r))
            starter_layout.addWidget(button)
            self.starter_buttons.append(button)
        self.left = QWidget()
        left_layout = QVBoxLayout(self.left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.recipe_list, 1)
        left_layout.addWidget(self.starter_box)
        left_layout.addLayout(left_buttons)

        # ----- right: the steps (editor) ----------------------------------------------------
        self.steps_title = QLabel()
        self.steps_list = QListWidget()
        self.steps_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.add_step_button = QPushButton("Add step…")
        self.change_step_button = QPushButton("Change step…")
        self.up_button = QPushButton("Move up")
        self.down_button = QPushButton("Move down")
        self.remove_step_button = QPushButton("Remove step")
        self.run_button = QPushButton(RUN_RECIPE)
        self.run_button.setToolTip(
            "Go through the steps one by one on the working folder. Each step shows its list "
            "first; nothing changes until you apply it."
        )
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        step_buttons = QHBoxLayout()
        for button in (
            self.add_step_button,
            self.change_step_button,
            self.up_button,
            self.down_button,
            self.remove_step_button,
        ):
            step_buttons.addWidget(button)
        step_buttons.addStretch(1)
        step_buttons.addWidget(self.run_button)
        self.editor = QWidget()
        editor_layout = QVBoxLayout(self.editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addWidget(self.steps_title)
        editor_layout.addWidget(self.steps_list, 1)
        editor_layout.addLayout(step_buttons)
        editor_layout.addWidget(self.status_label)

        # ----- right: the runner --------------------------------------------------------------
        self.step_label = QLabel()
        font = self.step_label.font()
        font.setBold(True)
        self.step_label.setFont(font)
        self.skip_button = QPushButton(SKIP_STEP)
        self.skip_button.setToolTip("Leave this step out and go to the next one.")
        self.stop_button = QPushButton(STOP_RECIPE)
        self.stop_button.setToolTip("End the recipe here. Steps already applied stay applied.")
        runner_head = QHBoxLayout()
        runner_head.addWidget(self.step_label, 1)
        runner_head.addWidget(self.skip_button)
        runner_head.addWidget(self.stop_button)
        self.runner = QWidget()
        self._runner_layout = QVBoxLayout(self.runner)
        self._runner_layout.setContentsMargins(0, 0, 0, 0)
        self._runner_layout.addLayout(runner_head)

        self.right = QStackedWidget()
        self.right.addWidget(self.editor)
        self.right.addWidget(self.runner)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.left)
        splitter.addWidget(self.right)
        splitter.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addWidget(self.notice_label)
        layout.addWidget(splitter, 1)

        self.recipe_list.currentRowChanged.connect(lambda _r: self._recipe_chosen())
        self.steps_list.currentRowChanged.connect(lambda _r: self._update_buttons())
        self.new_button.clicked.connect(lambda _c=False: self.new_recipe())
        self.duplicate_button.clicked.connect(lambda _c=False: self.duplicate_recipe())
        self.rename_button.clicked.connect(lambda _c=False: self.rename_recipe())
        self.delete_button.clicked.connect(lambda _c=False: self.delete_recipe())
        self.add_step_button.clicked.connect(lambda _c=False: self.add_step())
        self.change_step_button.clicked.connect(lambda _c=False: self.change_step())
        self.up_button.clicked.connect(lambda _c=False: self.move_step(-1))
        self.down_button.clicked.connect(lambda _c=False: self.move_step(1))
        self.remove_step_button.clicked.connect(lambda _c=False: self.remove_step())
        self.run_button.clicked.connect(lambda _c=False: self.run_recipe())
        self.skip_button.clicked.connect(lambda _c=False: self.skip_step())
        self.stop_button.clicked.connect(lambda _c=False: self.stop())
        self._fill_list()

    # ----- helpers ----------------------------------------------------------------------------

    def _tool_name(self, tool_id: str) -> str:
        tool = self.state.tools.get(tool_id)
        return tool.name if tool is not None else tool_id

    @property
    def running(self) -> bool:
        return self.run is not None

    @property
    def runner_page(self) -> ToolPage | None:
        return self.run.page if self.run is not None else None

    def current_recipe(self) -> Recipe | None:
        row = self.recipe_list.currentRow()
        return self.store.recipes[row] if 0 <= row < len(self.store.recipes) else None

    def current_step_index(self) -> int:
        row = self.steps_list.currentRow()
        recipe = self.current_recipe()
        return row if recipe is not None and 0 <= row < len(recipe.steps) else -1

    def select_recipe(self, name: str) -> bool:
        for row, recipe in enumerate(self.store.recipes):
            if recipe.name == name:
                self.recipe_list.setCurrentRow(row)
                return True
        return False

    def refresh(self) -> None:
        """Read the recipes again (a tool page may have added a step). Not during a run."""
        if self.running:
            return
        keep = self.current_recipe()
        self.store.reload()
        self._fill_list(keep.name if keep is not None else None)

    # ----- the list ---------------------------------------------------------------------------

    def _fill_list(self, select: str | None = None) -> None:
        self.recipe_list.blockSignals(True)
        self.recipe_list.clear()
        for recipe in self.store.recipes:
            self.recipe_list.addItem(recipe.name)
        row = next((i for i, r in enumerate(self.store.recipes) if r.name == select), 0)
        if self.store.recipes:
            self.recipe_list.setCurrentRow(row)
        self.recipe_list.blockSignals(False)
        self.notice_label.setText(self.store.notice or "")
        self.notice_label.setVisible(bool(self.store.notice))
        self.starter_box.setVisible(not self.store.recipes)
        self._recipe_chosen()

    def _recipe_chosen(self) -> None:
        recipe = self.current_recipe()
        self.steps_list.clear()
        self.steps_title.setText(
            f"<b>{recipe.name}</b>: the steps run in this order" if recipe else ""
        )
        if recipe is not None:
            for number, step in enumerate(recipe.steps, start=1):
                problem = check_step(step, self.state.tools)
                tool = self.state.tools.get(step.tool)
                text = f"{number}. {step_title(step, self.state.tools)}"
                if tool is not None and uses_own_folders(step, tool):
                    text += "  (own folders)"
                if problem:
                    text += f"  ({NEEDS_ATTENTION})"
                item = QListWidgetItem(text)
                if problem:
                    item.setForeground(_RED)
                    item.setToolTip(problem)
                self.steps_list.addItem(item)
        self._update_buttons()

    def _update_buttons(self) -> None:
        recipe = self.current_recipe()
        busy = self.running
        has = recipe is not None and not busy
        step = self.current_step_index()
        count = len(recipe.steps) if recipe is not None else 0
        self.new_button.setEnabled(not busy)
        for button in (self.duplicate_button, self.rename_button, self.delete_button):
            button.setEnabled(has)
        self.add_step_button.setEnabled(has)
        self.change_step_button.setEnabled(has and step >= 0)
        self.remove_step_button.setEnabled(has and step >= 0)
        self.up_button.setEnabled(has and step > 0)
        self.down_button.setEnabled(has and 0 <= step < count - 1)
        self.run_button.setEnabled(has and count > 0)
        self.recipe_list.setEnabled(not busy)
        for button in self.starter_buttons:
            button.setEnabled(not busy)

    def _changed(self, select: str | None = None, step: int | None = None) -> None:
        self.store.save()
        self._fill_list(select)
        if step is not None and 0 <= step < self.steps_list.count():
            self.steps_list.setCurrentRow(step)

    # ----- recipes ----------------------------------------------------------------------------

    def _ask_free_name(self, title: str, default: str, current: Recipe | None) -> str | None:
        name = self.ask_name(title, "Name of the recipe:", default)
        if not name:
            return None
        other = self.store.get(name)
        if other is not None and other is not current:
            self.inform(title, f"There is already a recipe named {name!r}. Choose another name.")
            return None
        return name

    def new_recipe(self) -> Recipe | None:
        name = self._ask_free_name("New recipe", NEW_RECIPE_NAME, None)
        if name is None:
            return None
        recipe = self.store.create(name)
        if recipe is not None:
            self._fill_list(recipe.name)
        return recipe

    def add_starter(self, starter: Recipe) -> Recipe | None:
        recipe = self.store.create(starter.name, list(starter.steps))
        if recipe is not None:
            self._fill_list(recipe.name)
        return recipe

    def duplicate_recipe(self) -> Recipe | None:
        recipe = self.current_recipe()
        if recipe is None or self.running:
            return None
        clone = self.store.duplicate(recipe)
        self._fill_list(clone.name)
        return clone

    def rename_recipe(self) -> bool:
        recipe = self.current_recipe()
        if recipe is None or self.running:
            return False
        name = self._ask_free_name("Rename recipe", recipe.name, recipe)
        if name is None or not self.store.rename(recipe, name):
            return False
        self._fill_list(name)
        return True

    def delete_recipe(self) -> bool:
        recipe = self.current_recipe()
        if recipe is None or self.running:
            return False
        if not self.confirm(
            "Delete recipe",
            f"Delete the recipe {recipe.name!r} and its {len(recipe.steps)} step(s)? "
            "Files are not touched.",
        ):
            return False
        self.store.delete(recipe)
        self._fill_list()
        return True

    # ----- steps ------------------------------------------------------------------------------

    def _open_step_dialog(self, step: RecipeStep | None) -> RecipeStep | None:
        workspace = self.state.workspace()
        dialog = RecipeStepDialog(
            self.state.tools, workspace[0] if workspace else Path.home(), step, self
        )
        if dialog.exec() != RecipeStepDialog.DialogCode.Accepted:
            return None
        return dialog.result_step

    def add_step(self) -> bool:
        recipe = self.current_recipe()
        if recipe is None or self.running:
            return False
        step = self.edit_step(None)
        if step is None:
            return False
        recipe.steps.append(step)
        self._changed(recipe.name, len(recipe.steps) - 1)
        return True

    def change_step(self) -> bool:
        recipe, index = self.current_recipe(), self.current_step_index()
        if recipe is None or index < 0 or self.running:
            return False
        step = self.edit_step(recipe.steps[index])
        if step is None:
            return False
        recipe.steps[index] = step
        self._changed(recipe.name, index)
        return True

    def move_step(self, delta: int) -> bool:
        recipe, index = self.current_recipe(), self.current_step_index()
        target = index + delta
        if recipe is None or index < 0 or not 0 <= target < len(recipe.steps) or self.running:
            return False
        recipe.steps[index], recipe.steps[target] = recipe.steps[target], recipe.steps[index]
        self._changed(recipe.name, target)
        return True

    def remove_step(self) -> bool:
        recipe, index = self.current_recipe(), self.current_step_index()
        if recipe is None or index < 0 or self.running:
            return False
        del recipe.steps[index]
        self._changed(recipe.name, min(index, len(recipe.steps) - 1))
        return True

    # ----- running ----------------------------------------------------------------------------

    def run_recipe(self) -> bool:
        """Start the chosen recipe at its first step. False when it cannot start."""
        recipe = self.current_recipe()
        if recipe is None or self.running or self._closing or not recipe.steps:
            return False
        blocked = [
            f"Step {n}: {problem}"
            for n, step in enumerate(recipe.steps, start=1)
            if (problem := check_step(step, self.state.tools))
        ]
        if blocked:
            self.inform(
                "This recipe needs attention",
                "Fix these steps first (Change step…, or remove them):\n\n" + "\n".join(blocked),
            )
            return False
        if not self.state.workspace() and any(self._needs_workspace(s) for s in recipe.steps):
            self.inform(RUN_RECIPE, CHOOSE_WORKSPACE_FIRST)
            return False
        self.run = _Run(
            recipe.name, [RecipeStep(s.tool, dict(s.options), s.label) for s in recipe.steps]
        )
        self.right.setCurrentWidget(self.runner)
        self.status_label.setText("")
        self._update_buttons()
        self.busyChanged.emit(True)
        self._show_step()
        return True

    def _needs_workspace(self, step: RecipeStep) -> bool:
        tool = self.state.tools.get(step.tool)
        return (
            tool is not None
            and workspace_field(tool) is not None
            and not uses_own_folders(step, tool)
        )

    def _show_step(self) -> None:
        run = self.run
        if run is None:
            return
        step = run.steps[run.index]
        tool = self.state.tools[step.tool]
        own = uses_own_folders(step, tool)
        page = ToolPage(self.state, tool, preset=(step.options, own))
        page.recipe_tag = recipe_tag(
            run.recipe_name, run.recipe_run_id, run.index + 1, len(run.steps)
        )
        page.runApplied.connect(lambda run_id, p=page: self._page_applied(p, run_id))
        page.runUndone.connect(self.runUndone)
        page.busyChanged.connect(lambda busy, p=page: self._page_busy(p, busy))
        run.page = page
        run.step_applied = False
        self._runner_layout.addWidget(page, 1)
        self.step_label.setText(
            f"{run.recipe_name}: step {run.index + 1} of {len(run.steps)}: "
            f"{step_title(step, self.state.tools)}"
        )
        self.prepare_page(page)
        self._update_run_buttons()
        page.scan()

    def _page_applied(self, page: ToolPage, run_id: int) -> None:
        run = self.run
        if run is None or run.page is not page:
            return
        run.step_applied = True
        run.applied += 1
        self.runApplied.emit(run_id)

    def _page_busy(self, page: ToolPage, busy: bool) -> None:
        run = self.run
        if run is None or run.page is not page or not shiboken6.isValid(self):
            return
        self._update_run_buttons()
        if busy or self._closing:
            return
        # The worker is done (after its message box, if any). Go on only after an apply or a stop.
        if run.stopping:
            QTimer.singleShot(0, lambda: self._end("Stopped."))
        elif run.step_applied:
            QTimer.singleShot(0, self._advance)

    def _update_run_buttons(self) -> None:
        run = self.run
        page = run.page if run is not None else None
        idle = page is not None and not page.busy
        self.skip_button.setEnabled(run is not None and idle and not run.stopping)
        self.stop_button.setEnabled(run is not None and not run.stopping)

    def _advance(self) -> None:
        run = self.run
        if run is None or self._closing:
            return
        self._drop_page(run.page)
        run.page = None
        run.index += 1
        if run.index >= len(run.steps):
            self._end("Finished.")
        else:
            self._show_step()

    def _drop_page(self, page: ToolPage | None) -> None:
        if page is not None and shiboken6.isValid(page):
            page.setParent(None)
            page.deleteLater()

    def skip_step(self) -> bool:
        """Leave this step out and open the next one."""
        run = self.run
        if run is None or run.page is None or run.page.busy or run.stopping:
            return False
        run.skipped += 1
        self._advance()
        return True

    def stop(self) -> bool:
        """End the recipe. A running scan or apply is cancelled first (what it already did
        stays done and journaled)."""
        run = self.run
        if run is None or run.stopping:
            return False
        page = run.page
        if page is not None and page.busy:
            run.stopping = True
            page.cancel()
            self._update_run_buttons()
            return True
        self._end("Stopped.")
        return True

    def _end(self, how: str) -> None:
        run = self.run
        if run is None:
            return
        self._drop_page(run.page)
        self.run = None
        summary = (
            f"{how} {run.recipe_name}: {run.applied} step(s) applied, "
            f"{run.skipped} skipped, of {len(run.steps)}."
        )
        self.status_label.setText(summary)
        self.right.setCurrentWidget(self.editor)
        self._recipe_chosen()
        self.busyChanged.emit(False)
        self.runFinished.emit(summary)

    def shutdown(self) -> None:
        """The window is closing: cancel the step's job and start nothing new."""
        self._closing = True
        run = self.run
        if run is not None and run.page is not None and shiboken6.isValid(run.page):
            run.page.shutdown()
