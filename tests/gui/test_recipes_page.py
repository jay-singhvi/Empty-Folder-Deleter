"""The Recipes page: editing, the step dialog, running step by step, "Save as recipe step",
History labels and "Undo this recipe run"."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pytestqt.qtbot import QtBot

from various_tools.core.journal import Journal
from various_tools.core.recipe import (
    Recipe,
    RecipeStep,
    parse_recipe_mark,
    recipes_path,
    save_recipes,
)
from various_tools.gui.main_window import RECIPES_ID, MainWindow
from various_tools.gui.recipes_page import RecipesPage
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.widgets.recipe_step_dialog import RecipeStepDialog

if TYPE_CHECKING:
    from conftest import Dialogs


def _tidy() -> Recipe:
    return Recipe("Tidy", [RecipeStep("empty_files"), RecipeStep("empty_folders")])


def _tree(base: Path) -> Path:
    root = base / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "keep").mkdir()
    (root / "keep" / "file.txt").write_text("data")
    (root / "empty.txt").write_text("")
    return root


@pytest.fixture
def make_page(qtbot: QtBot, gui_state: AppState, dialogs: Dialogs) -> Callable[..., RecipesPage]:
    def _make(*recipes: Recipe) -> RecipesPage:
        if recipes:
            save_recipes(recipes_path(gui_state.settings_path), recipes)
        page = RecipesPage(gui_state)
        qtbot.addWidget(page)
        dialogs.attach(page)
        page.ask_name = lambda _t, _l, default: default
        page.prepare_page = dialogs.attach
        return page

    return _make


def _saved(state: AppState) -> list[Recipe]:
    from various_tools.core.recipe import load_recipes

    return load_recipes(recipes_path(state.settings_path)).recipes


# ----- editing ---------------------------------------------------------------------------------


def test_empty_page_offers_the_starters(make_page: Callable[..., RecipesPage]) -> None:
    page = make_page()
    assert not page.starter_box.isHidden()
    assert len(page.starter_buttons) == 3
    page.starter_buttons[0].click()
    assert [r.name for r in page.store.recipes] == ["Tidy a folder"]
    assert page.starter_box.isHidden()
    assert page.steps_list.count() == 2
    assert [r.name for r in _saved(page.state)] == ["Tidy a folder"]


def test_new_duplicate_rename_delete(
    make_page: Callable[..., RecipesPage], dialogs: Dialogs
) -> None:
    page = make_page()
    page.ask_name = lambda _t, _l, _d: "First"
    assert page.new_recipe() is not None
    assert page.new_recipe() is None  # a name is used once, whatever the letter case
    page.ask_name = lambda _t, _l, _d: "first"
    assert page.new_recipe() is None
    assert dialogs.informs and "already a recipe" in dialogs.informs[-1][1]
    clone = page.duplicate_recipe()
    assert clone is not None and clone.name == "First (copy)"
    page.ask_name = lambda _t, _l, _d: "Second"
    assert page.rename_recipe()
    assert [r.name for r in _saved(page.state)] == ["First", "Second"]
    dialogs.default = False
    assert not page.delete_recipe()  # the question said no
    assert len(page.store.recipes) == 2
    dialogs.default = True
    assert page.delete_recipe()
    assert [r.name for r in _saved(page.state)] == ["First"]
    assert "Delete" in dialogs.confirms[-1][0]


def test_steps_add_change_move_remove(make_page: Callable[..., RecipesPage]) -> None:
    page = make_page(Recipe("R", []))
    answers = [RecipeStep("empty_files"), RecipeStep("empty_folders")]
    page.edit_step = lambda _step: answers.pop(0)
    assert page.add_step() and page.add_step()
    assert [s.tool for s in page.store.recipes[0].steps] == ["empty_files", "empty_folders"]
    page.steps_list.setCurrentRow(1)
    assert page.move_step(-1)
    assert [s.tool for s in page.store.recipes[0].steps] == ["empty_folders", "empty_files"]
    page.edit_step = lambda step: RecipeStep(step.tool if step else "x", {"min_age_days": 3})
    assert page.change_step()
    assert page.store.recipes[0].steps[0].options == {"min_age_days": 3}
    assert page.remove_step()
    assert [s.tool for s in _saved(page.state)[0].steps] == ["empty_files"]
    page.edit_step = lambda _s: None
    assert not page.add_step()  # cancelled


def test_a_broken_step_is_marked_and_blocks_the_run(
    make_page: Callable[..., RecipesPage], dialogs: Dialogs, gui_state: AppState, tmp_path: Path
) -> None:
    gui_state.set_workspace([_tree(tmp_path)])
    page = make_page(Recipe("Broken", [RecipeStep("empty_files"), RecipeStep("gone_tool")]))
    texts = [page.steps_list.item(i).text() for i in range(page.steps_list.count())]  # type: ignore[union-attr]
    assert "needs attention" not in texts[0] and "needs attention" in texts[1]
    assert not page.run_recipe()
    assert page.run is None
    assert "gone_tool" in dialogs.informs[-1][1]


def test_a_corrupt_recipes_file_is_set_aside_and_shown(qtbot: QtBot, gui_state: AppState) -> None:
    path = recipes_path(gui_state.settings_path)
    path.write_text("{oops", encoding="utf-8")
    page = RecipesPage(gui_state)
    qtbot.addWidget(page)
    assert not page.notice_label.isHidden() and "recipes.json.bad-" in page.notice_label.text()
    assert not path.exists() and list(path.parent.glob("recipes.json.bad-*"))


# ----- the step dialog -------------------------------------------------------------------------


def test_step_dialog_builds_a_step_without_the_workspace(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    dialog = RecipeStepDialog(gui_state.tools, tmp_path)
    qtbot.addWidget(dialog)
    dialog.tool_combo.setCurrentIndex(dialog.tool_combo.findData("empty_files"))
    assert dialog.form is not None
    dialog.form.set_field_value("min_age_days", 3)
    dialog.label_edit.setText(" first ")
    dialog.accept()
    step = dialog.result_step
    assert step is not None and step.tool == "empty_files" and step.label == "first"
    assert "roots" not in step.options


def test_step_dialog_keeps_own_folders_and_edits_a_step(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    own = RecipeStep("empty_files", {"roots": [str(tmp_path / "x")]})
    dialog = RecipeStepDialog(gui_state.tools, tmp_path, own)
    qtbot.addWidget(dialog)
    assert dialog.other_folders_check.isChecked() and not dialog.tool_combo.isEnabled()
    dialog.accept()
    assert dialog.result_step is not None
    assert dialog.result_step.options["roots"] == [str(tmp_path / "x")]
    dialog.other_folders_check.setChecked(False)
    dialog.accept()
    assert dialog.result_step is not None and "roots" not in dialog.result_step.options


# ----- running ---------------------------------------------------------------------------------


def _wait_scanned(qtbot: QtBot, page: RecipesPage) -> ToolPage:
    runner = page.runner_page
    assert runner is not None
    qtbot.waitUntil(lambda: runner.worker is None and runner.plan is not None, timeout=60000)
    return runner


def test_run_goes_through_the_steps_and_marks_the_runs(
    qtbot: QtBot,
    make_page: Callable[..., RecipesPage],
    gui_state: AppState,
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)
    gui_state.set_workspace([root])
    page = make_page(_tidy())
    applied: list[int] = []
    page.runApplied.connect(applied.append)
    finished: list[str] = []
    page.runFinished.connect(finished.append)
    assert page.run_recipe()
    first = _wait_scanned(qtbot, page)
    assert "step 1 of 2" in page.step_label.text()
    assert first.tool.id == "empty_files" and first.recipe_tag is not None
    assert first.apply() is not None
    qtbot.waitUntil(
        lambda: page.runner_page is not None and page.runner_page.tool.id == "empty_folders",
        timeout=60000,
    )
    second = _wait_scanned(qtbot, page)
    assert "step 2 of 2" in page.step_label.text()
    assert not (root / "empty.txt").exists() and (root / "a").exists()
    assert second.apply() is not None
    qtbot.waitUntil(lambda: page.run is None, timeout=60000)
    assert not (root / "a").exists() and (root / "keep" / "file.txt").exists()
    assert len(applied) == 2 and finished and "2 step(s) applied" in finished[0]
    assert page.right.currentWidget() is page.editor
    with Journal(gui_state.settings.journal_path()) as journal:
        runs = journal.runs()
    marks = [parse_recipe_mark(r.options_json) for r in runs]
    assert all(m is not None for m in marks) and len({m.run_id for m in marks if m}) == 1
    assert sorted(m.step for m in marks if m) == [1, 2]
    # the tool pages' remembered options were not touched by the recipe
    assert gui_state.last_options("empty_files") == {}


def test_skip_and_stop(
    qtbot: QtBot,
    make_page: Callable[..., RecipesPage],
    gui_state: AppState,
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)
    gui_state.set_workspace([root])
    page = make_page(_tidy())
    assert page.run_recipe()
    _wait_scanned(qtbot, page)
    assert page.skip_step()
    qtbot.waitUntil(
        lambda: page.runner_page is not None and page.runner_page.tool.id == "empty_folders"
    )
    _wait_scanned(qtbot, page)
    assert (root / "empty.txt").exists()  # skipped, so untouched
    assert page.stop()
    assert page.run is None
    assert (
        "1 skipped" in page.status_label.text() and "0 step(s) applied" in page.status_label.text()
    )
    assert (root / "a").exists()


def test_declining_the_apply_window_keeps_the_step_open(
    qtbot: QtBot,
    make_page: Callable[..., RecipesPage],
    gui_state: AppState,
    tmp_path: Path,
    dialogs: Dialogs,
) -> None:
    root = _tree(tmp_path)
    gui_state.set_workspace([root])
    page = make_page(_tidy())
    assert page.run_recipe()
    runner = _wait_scanned(qtbot, page)
    dialogs.default = False
    assert runner.apply() is None
    assert page.runner_page is runner and (root / "empty.txt").exists()
    assert page.stop()


def test_run_needs_a_working_folder(
    make_page: Callable[..., RecipesPage], dialogs: Dialogs
) -> None:
    page = make_page(_tidy())
    assert not page.run_recipe()
    assert dialogs.informs and page.run is None


def test_stop_cancels_a_running_step(
    qtbot: QtBot,
    make_page: Callable[..., RecipesPage],
    gui_state: AppState,
    tmp_path: Path,
) -> None:
    gui_state.set_workspace([_tree(tmp_path)])
    page = make_page(_tidy())
    assert page.run_recipe()
    page.stop()  # the first scan may still be running
    qtbot.waitUntil(lambda: page.run is None, timeout=60000)
    assert "Stopped." in page.status_label.text()


# ----- Save as recipe step -----------------------------------------------------------------------


def test_save_as_recipe_step(
    window: MainWindow, gui_state: AppState, tmp_path: Path, dialogs: Dialogs
) -> None:
    gui_state.set_workspace([_tree(tmp_path)])
    page = window.open_tool("empty_files")
    assert page is not None
    dialogs.attach(page)
    page.form.set_field_value("min_age_days", 3)
    chosen: list[list[str]] = []

    def choose(names: list[str]) -> str | None:
        chosen.append(names)
        return "My recipe"

    page.choose_recipe = choose
    assert page.save_as_recipe_step()
    assert page.save_as_recipe_step()
    assert chosen == [[], ["My recipe"]]
    saved = _saved(gui_state)
    assert [r.name for r in saved] == ["My recipe"] and len(saved[0].steps) == 2
    step = saved[0].steps[0]
    assert step.tool == "empty_files" and step.options.get("min_age_days") == 3
    assert "roots" not in step.options  # the working folder is filled in at run time
    page.choose_recipe = lambda _names: None
    assert not page.save_as_recipe_step()
    assert len(_saved(gui_state)[0].steps) == 2


# ----- the window ------------------------------------------------------------------------------


def test_window_has_the_recipes_page(window: MainWindow) -> None:
    assert RECIPES_ID in window._tool_items  # pyright: ignore[reportPrivateUsage]
    assert window._tool_items[RECIPES_ID].text(0) == "Recipes"  # pyright: ignore[reportPrivateUsage]
    window.recipes_action.trigger()
    assert isinstance(window.stack.currentWidget(), RecipesPage)
    assert window.recipes_page is window.stack.currentWidget()


# ----- History ---------------------------------------------------------------------------------


def _run_whole_recipe(qtbot: QtBot, page: RecipesPage, state: AppState, root: Path) -> None:
    state.set_workspace([root])
    assert page.run_recipe()
    for tool_id in ("empty_files", "empty_folders"):
        qtbot.waitUntil(
            lambda t=tool_id: page.runner_page is not None and page.runner_page.tool.id == t,
            timeout=60000,
        )
        runner = _wait_scanned(qtbot, page)
        assert runner.apply() is not None
    qtbot.waitUntil(lambda: page.run is None, timeout=60000)


def test_history_labels_and_undoes_a_recipe_run(
    qtbot: QtBot,
    window: MainWindow,
    gui_state: AppState,
    tmp_path: Path,
    dialogs: Dialogs,
) -> None:
    root = _tree(tmp_path)
    save_recipes(recipes_path(gui_state.settings_path), [_tidy()])
    recipes = window.recipes()
    dialogs.attach(recipes)
    recipes.prepare_page = dialogs.attach
    _run_whole_recipe(qtbot, recipes, gui_state, root)
    assert not (root / "empty.txt").exists() and not (root / "a").exists()

    history = window.history_page
    history.reload()
    qtbot.waitUntil(lambda: not history.loading, timeout=15000)
    labels = [history.runs_view.item(r, 1).text() for r in range(history.runs_view.rowCount())]  # type: ignore[union-attr]
    assert any("Recipe: Tidy, step 1 of 2" in t for t in labels)
    assert any("Recipe: Tidy, step 2 of 2" in t for t in labels)
    history.runs_view.selectRow(0)
    qtbot.waitUntil(lambda: not history.loading and history.undo_info() is not None, timeout=15000)
    assert not history.undo_recipe_button.isHidden() and history.undo_recipe_button.isEnabled()

    worker = history.undo_recipe_run()
    assert worker is not None
    qtbot.waitUntil(lambda: not history._busy, timeout=60000)  # pyright: ignore[reportPrivateUsage]
    question = dialogs.confirms[-1]
    assert "Tidy" in question[1] and "last to the first" in question[1]
    summary = dialogs.informs[-1][1]
    assert summary.count("Run ") >= 2 and "Tidy" in summary
    assert dialogs.confirms[-1][0] == "Undo this recipe run"
    # Recycle Bin items cannot be restored in tests (no bin), but both steps were tried,
    # last step first, and each is reported.
    assert summary.index("Run 2") < summary.index("Run 1")


def test_undo_recipe_run_finds_steps_beyond_the_loaded_page(
    qtbot: QtBot,
    window: MainWindow,
    gui_state: AppState,
    tmp_path: Path,
    dialogs: Dialogs,
) -> None:
    root = _tree(tmp_path)
    save_recipes(recipes_path(gui_state.settings_path), [_tidy()])
    recipes = window.recipes()
    dialogs.attach(recipes)
    recipes.prepare_page = dialogs.attach
    _run_whole_recipe(qtbot, recipes, gui_state, root)
    history = window.history_page
    history.reload()
    qtbot.waitUntil(lambda: not history.loading, timeout=15000)
    history.runs_view.selectRow(0)
    qtbot.waitUntil(lambda: not history.loading and history.undo_info() is not None, timeout=15000)
    history._runs = history._runs[:1]  # pyright: ignore[reportPrivateUsage]  # only the newest step is loaded
    dialogs.default = False  # look at the question only
    assert history.undo_recipe_run() is None
    question = dialogs.confirms[-1][1]
    assert "step 1: " in question and "step 2: " in question


def test_history_refuses_a_recipe_run_that_is_still_applying(
    qtbot: QtBot,
    window: MainWindow,
    gui_state: AppState,
    tmp_path: Path,
    dialogs: Dialogs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _tree(tmp_path)
    save_recipes(recipes_path(gui_state.settings_path), [_tidy()])
    recipes = window.recipes()
    dialogs.attach(recipes)
    recipes.prepare_page = dialogs.attach
    _run_whole_recipe(qtbot, recipes, gui_state, root)
    history = window.history_page
    history.reload()
    qtbot.waitUntil(lambda: not history.loading, timeout=15000)
    history.runs_view.selectRow(0)
    qtbot.waitUntil(lambda: not history.loading, timeout=15000)
    from various_tools.gui import history_page as history_module

    monkeypatch.setattr(history_module, "applying_run_ids", lambda: {history._runs[0].id})  # pyright: ignore[reportPrivateUsage]
    before = len(dialogs.confirms)
    assert history.undo_recipe_run() is None
    assert len(dialogs.confirms) == before  # refused before asking
    assert "still" in dialogs.informs[-1][1]
    assert not (root / "a").exists()


def test_a_plain_run_has_no_recipe_button(
    qtbot: QtBot, window: MainWindow, gui_state: AppState, tmp_path: Path, dialogs: Dialogs
) -> None:
    root = _tree(tmp_path)
    gui_state.set_workspace([root])
    page = window.open_tool("empty_files")
    assert page is not None
    dialogs.attach(page)
    page.scan()
    qtbot.waitUntil(lambda: page.worker is None and page.plan is not None, timeout=60000)
    assert page.apply() is not None
    qtbot.waitUntil(lambda: page.worker is None, timeout=60000)
    history = window.history_page
    history.reload()
    qtbot.waitUntil(lambda: not history.loading, timeout=15000)
    history.runs_view.selectRow(0)
    qtbot.waitUntil(lambda: not history.loading, timeout=15000)
    assert history.undo_recipe_button.isHidden()
    assert "Recipe" not in history.runs_view.item(0, 1).text()  # type: ignore[union-attr]


def test_a_step_whose_tool_is_missing_cannot_be_changed(
    make_page: Callable[..., RecipesPage], dialogs: Dialogs, gui_state: AppState
) -> None:
    page = make_page(Recipe("R", [RecipeStep("gone_tool", {"x": 1})]))
    asked: list[object] = []
    page.edit_step = lambda step: asked.append(step)
    page.steps_list.setCurrentRow(0)
    assert not page.change_step()
    assert not asked and "not available" in dialogs.informs[-1][1]
    assert page.store.recipes[0].steps[0].tool == "gone_tool"
    dialog = RecipeStepDialog(gui_state.tools, Path.home(), RecipeStep("gone_tool"))
    dialog.accept()
    assert dialog.result_step is None


def test_a_step_saved_during_a_run_is_not_overwritten(
    qtbot: QtBot,
    make_page: Callable[..., RecipesPage],
    gui_state: AppState,
    tmp_path: Path,
) -> None:
    from various_tools.gui.recipe_store import RecipeStore

    gui_state.set_workspace([_tree(tmp_path)])
    page = make_page(_tidy())
    assert page.run_recipe()
    _wait_scanned(qtbot, page)
    other = RecipeStore(gui_state.settings_path)  # a tool page saved a step meanwhile
    other.add_step("Tidy", RecipeStep("temp_cleaner"))
    assert page.stop()
    assert [s.tool for s in page.store.recipes[0].steps] == [
        "empty_files",
        "empty_folders",
        "temp_cleaner",
    ]
    page.steps_list.setCurrentRow(0)
    assert page.move_step(1)
    assert len(_saved(gui_state)[0].steps) == 3


def test_the_runner_relays_the_steps_scan_and_clipboard(
    qtbot: QtBot,
    make_page: Callable[..., RecipesPage],
    gui_state: AppState,
    tmp_path: Path,
) -> None:
    gui_state.set_workspace([_tree(tmp_path)])
    page = make_page(_tidy())
    plans: list[object] = []
    copied: list[str] = []
    page.scanFinished.connect(plans.append)
    page.clipboardCopied.connect(copied.append)
    assert page.run_recipe()
    runner = _wait_scanned(qtbot, page)
    assert len(plans) == 1
    runner.clipboardCopied.emit("Copied.")
    assert copied == ["Copied."]
    assert page.stop()


def test_a_step_that_did_not_finish_cleanly_stops_the_recipe(
    qtbot: QtBot,
    make_page: Callable[..., RecipesPage],
    gui_state: AppState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from various_tools.core.plan import ActionOutcome, RunResult

    root = _tree(tmp_path)
    gui_state.set_workspace([root])
    page = make_page(_tidy())
    finished: list[str] = []
    page.runFinished.connect(finished.append)
    assert page.run_recipe()
    first = _wait_scanned(qtbot, page)
    failed = RunResult(1, "partial", [ActionOutcome(1, "trash", "failed", "x", None, "locked")])
    monkeypatch.setattr(type(first.tool), "apply", lambda _self, _plan, _ctx: failed)
    assert first.apply() is not None
    qtbot.waitUntil(lambda: page.run is None, timeout=60000)
    assert finished and "Stopped at step 1: 1 action(s) failed" in finished[0]
    assert "0 step(s) applied" in finished[0]
    assert page.run is None  # step 2 was never opened
