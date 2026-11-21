"""``recipe list | show | run``: dry run of the first step, apply step by step, History markers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from various_tools.core.journal import Journal
from various_tools.core.recipe import (
    Recipe,
    RecipeStep,
    parse_recipe_mark,
    recipes_path,
    save_recipes,
)

if TYPE_CHECKING:
    from tests.cli.conftest import Cli


def _save(cli: Cli, *recipes: Recipe) -> None:
    save_recipes(recipes_path(cli.config), recipes)


def _tidy() -> Recipe:
    return Recipe("Tidy", [RecipeStep("empty_files"), RecipeStep("empty_folders")])


def _tree(cli: Cli) -> Path:
    root = cli.work / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "keep").mkdir()
    (root / "keep" / "file.txt").write_text("data")
    (root / "empty.txt").write_text("")
    return root


def test_list_and_show(cli: Cli) -> None:
    empty = cli.run("recipe", "list")
    assert empty.exit_code == 0 and "No recipes yet" in empty.output
    _save(cli, _tidy(), Recipe("Broken", [RecipeStep("no_such_tool")]))
    listed = cli.run("recipe", "list")
    assert "Tidy" in listed.output and "Broken" in listed.output
    shown = cli.run("recipe", "show", "tidy")
    assert shown.exit_code == 0
    assert "1. Empty Files" in shown.output and "2. Empty Folder Deleter" in shown.output
    broken = cli.run("recipe", "show", "Broken")
    assert "needs attention" in broken.output
    unknown = cli.run("recipe", "show", "nope")
    assert unknown.exit_code == 2 and "No recipe is named" in unknown.output


def test_a_corrupt_file_is_set_aside_and_said(cli: Cli) -> None:
    path = recipes_path(cli.config)
    path.write_text("{oops", encoding="utf-8")
    result = cli.run("recipe", "list")
    assert result.exit_code == 0 and "recipes.json.bad-" in result.output
    assert not path.exists() and list(path.parent.glob("recipes.json.bad-*"))


def test_run_without_apply_scans_the_first_step_only(cli: Cli) -> None:
    _save(cli, _tidy())
    root = _tree(cli)
    before = sorted(p.name for p in root.rglob("*"))
    result = cli.run("recipe", "run", "Tidy", str(root))
    assert result.exit_code == 0, result.output
    assert "Step 1 of 2" in result.output and "Step 2" not in result.output
    assert "depend on what it changes" in result.output
    assert sorted(p.name for p in root.rglob("*")) == before
    assert not cli.journal.exists() or Journal(cli.journal).runs() == []


def test_run_with_apply_applies_each_step_and_marks_the_runs(cli: Cli) -> None:
    _save(cli, _tidy())
    root = _tree(cli)
    result = cli.run("recipe", "run", "Tidy", str(root), "--apply", "--yes")
    assert result.exit_code == 0, result.output
    assert not (root / "empty.txt").exists() and not (root / "a").exists()
    assert (root / "keep" / "file.txt").exists()
    assert "finished: 2 step(s) applied" in result.output
    with Journal(cli.journal) as journal:
        runs = journal.runs()
    marks = [parse_recipe_mark(r.options_json) for r in runs]
    assert len(runs) == 2 and all(m is not None for m in marks)
    assert len({m.run_id for m in marks if m}) == 1
    assert sorted(m.step for m in marks if m) == [1, 2]


def test_declining_a_step_stops_the_recipe(cli: Cli) -> None:
    _save(cli, _tidy())
    root = _tree(cli)
    result = cli.run("recipe", "run", "Tidy", str(root), "--apply", input="n\n")
    assert result.exit_code == 1
    assert (root / "empty.txt").exists() and (root / "a").exists()


def test_a_step_with_nothing_selected_stops_without_error(cli: Cli) -> None:
    _save(cli, _tidy())
    root = cli.work / "plain"
    root.mkdir()
    (root / "f.txt").write_text("x")
    result = cli.run("recipe", "run", "Tidy", str(root), "--apply", "--yes")
    assert result.exit_code == 0
    assert "no action is selected" in result.output


def test_a_recipe_that_needs_attention_does_not_run(cli: Cli) -> None:
    _save(cli, Recipe("Broken", [RecipeStep("empty_files"), RecipeStep("no_such_tool")]))
    root = _tree(cli)
    result = cli.run("recipe", "run", "Broken", str(root), "--apply", "--yes")
    assert result.exit_code == 1 and "needs attention" in result.output
    assert (root / "empty.txt").exists()


def test_run_needs_folders(cli: Cli) -> None:
    _save(cli, _tidy())
    result = cli.run("recipe", "run", "Tidy")
    assert result.exit_code == 2 and "folder" in result.output


def test_pacing_flags_are_checked_as_on_apply(cli: Cli) -> None:
    _save(cli, _tidy())
    root = _tree(cli)
    result = cli.run("recipe", "run", "Tidy", str(root), "--apply", "--batch-size", "2")
    assert result.exit_code == 2 and "--batch-size and --pause go together" in result.output
