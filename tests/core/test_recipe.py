"""Recipes: storage, validation, the History marker, running and undoing a recipe run."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from various_tools.core.context import make_context
from various_tools.core.journal import Journal
from various_tools.core.recipe import (
    Recipe,
    RecipeStep,
    check_recipe,
    check_step,
    load_recipes,
    parse_recipe_mark,
    recipe_run_members,
    recipes_path,
    run_recipe,
    save_recipes,
    starter_recipes,
    step_from_options,
    step_values,
    strip_recipe_mark,
    undo_recipe_run,
    workspace_field,
)
from various_tools.core.schema import describe
from various_tools.core.settings import Settings
from various_tools.tools.base import Tool, discover


@pytest.fixture(scope="module")
def tools() -> dict[str, Tool]:
    return dict(discover())


def _tree(base: Path) -> Path:
    root = base / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "keep").mkdir()
    (root / "keep" / "file.txt").write_text("data")
    (root / "empty.txt").write_text("")
    return root


def test_recipes_path_is_beside_the_settings_file(tmp_path: Path) -> None:
    assert recipes_path(tmp_path / "cfg" / "settings.toml") == tmp_path / "cfg" / "recipes.json"


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "recipes.json"
    recipes = [Recipe("One", [RecipeStep("empty_files", {"min_age_days": 3}, "first")])]
    save_recipes(path, recipes)
    assert json.loads(path.read_text(encoding="utf-8"))["format"] == 1
    loaded = load_recipes(path)
    assert loaded.notice is None
    assert loaded.recipes == recipes
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_file_is_an_empty_list(tmp_path: Path) -> None:
    loaded = load_recipes(tmp_path / "recipes.json")
    assert loaded.recipes == [] and loaded.notice is None


@pytest.mark.parametrize(
    "text",
    ["{not json", '{"format": 99, "recipes": []}', '{"format": 1, "recipes": [{"steps": []}]}'],
)
def test_a_bad_file_is_renamed_aside_and_reported(tmp_path: Path, text: str) -> None:
    path = tmp_path / "recipes.json"
    path.write_text(text, encoding="utf-8")
    loaded = load_recipes(path, now=datetime(2026, 11, 21, 10, 30, 5))
    assert loaded.recipes == []
    assert loaded.notice is not None and "recipes.json.bad-20261121-103005" in loaded.notice
    assert not path.exists()
    assert (tmp_path / "recipes.json.bad-20261121-103005").read_text(encoding="utf-8") == text


def test_step_validation(tools: dict[str, Tool]) -> None:
    assert check_step(RecipeStep("empty_files"), tools) is None
    assert "not available" in str(check_step(RecipeStep("no_such_tool"), tools))
    bad = check_step(RecipeStep("empty_files", {"min_age_days": "maybe"}), tools)
    assert bad is not None
    unknown = check_step(RecipeStep("empty_files", {"gone_option": 1}), tools)
    assert unknown is not None
    recipe = Recipe("r", [RecipeStep("empty_files"), RecipeStep("no_such_tool")])
    problems = check_recipe(recipe, tools)
    assert problems[0] is None and problems[1]


def test_starter_recipes_all_validate(tools: dict[str, Tool]) -> None:
    starters = starter_recipes()
    assert [r.name for r in starters] == [
        "Tidy a folder",
        "Prepare photos to share",
        "Fix photo names",
    ]
    for recipe in starters:
        assert check_recipe(recipe, tools) == [None] * len(recipe.steps), recipe.name


def test_workspace_folder_is_filled_at_run_time(tools: dict[str, Tool], tmp_path: Path) -> None:
    tool = tools["empty_files"]
    assert workspace_field(tool) == "roots"
    values = step_values(RecipeStep("empty_files"), tool, [tmp_path])
    assert values["roots"] == [str(tmp_path)]
    own = RecipeStep("empty_files", {"roots": ["elsewhere"]})
    assert step_values(own, tool, [tmp_path])["roots"] == ["elsewhere"]
    assert workspace_field(tools["temp_cleaner"]) is None


def test_step_from_options_drops_the_workspace_and_permanent(tools: dict[str, Tool]) -> None:
    tool = tools["empty_files"]
    values = {"roots": ["x"], "min_age_days": 3, "delete_mode": "permanent"}
    step = step_from_options(tool, values, own_folders=False)
    assert "roots" not in step.options and step.options["min_age_days"] == 3
    assert step.options.get("delete_mode") != "permanent"
    assert step_from_options(tool, values, own_folders=True).options["roots"] == ["x"]


def test_marker_parse_and_strip() -> None:
    text = json.dumps({"a": 1, "_recipe": {"name": "N", "run_id": "abc", "step": 2, "steps": 3}})
    mark = parse_recipe_mark(text)
    assert mark is not None and mark.label == "Recipe: N, step 2 of 3"
    assert json.loads(strip_recipe_mark(text)) == {"a": 1}
    assert parse_recipe_mark('{"a": 1}') is None
    assert parse_recipe_mark("not json") is None
    assert parse_recipe_mark('{"_recipe": {"name": "N"}}') is None
    assert strip_recipe_mark('{"a": 1}') == '{"a": 1}'


def _ctx(tmp_path: Path):
    return make_context(Settings(data_folder=tmp_path / "data"))


def test_run_recipe_applies_each_step_as_its_own_marked_run(
    tools: dict[str, Tool], tmp_path: Path
) -> None:
    root = _tree(tmp_path)
    recipe = Recipe("Tidy", [RecipeStep("empty_files"), RecipeStep("empty_folders")])
    ctx = _ctx(tmp_path)
    seen: list[str] = []

    def confirm(step: RecipeStep, plan: object) -> bool:
        seen.append(step.tool)
        return True

    try:
        run = run_recipe(recipe, ctx, confirm, tools=tools, folders=[root])
        assert ctx.recipe_tag is None
        assert ctx.journal is not None
        runs = ctx.journal.runs()
    finally:
        ctx.close()
    assert seen == ["empty_files", "empty_folders"]
    assert run.completed and [o.status for o in run.outcomes] == ["applied", "applied"]
    assert not (root / "empty.txt").exists() and not (root / "a").exists()
    assert (root / "keep" / "file.txt").exists()
    marks = [parse_recipe_mark(r.options_json) for r in runs]
    assert all(m is not None and m.run_id == run.recipe_run_id for m in marks)
    assert sorted(m.step for m in marks if m) == [1, 2]
    # the marker is the journal's copy only; the tool's own options still validate
    for r in runs:
        tools[r.tool].Options.model_validate_json(strip_recipe_mark(r.options_json))
    assert [r.id for r in recipe_run_members(runs, run.recipe_run_id)] == sorted(
        (r.id for r in runs), reverse=True
    )


def test_run_recipe_stops_at_a_refusal(tools: dict[str, Tool], tmp_path: Path) -> None:
    root = _tree(tmp_path)
    recipe = Recipe("Tidy", [RecipeStep("empty_files"), RecipeStep("empty_folders")])
    ctx = _ctx(tmp_path)
    try:
        run = run_recipe(recipe, ctx, lambda step, plan: False, tools=tools, folders=[root])
        assert ctx.journal is not None and ctx.journal.runs() == []
    finally:
        ctx.close()
    assert [o.status for o in run.outcomes] == ["declined"]
    assert (root / "empty.txt").exists()


def test_run_recipe_stops_when_nothing_is_selected(tools: dict[str, Tool], tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "f.txt").write_text("x")
    recipe = Recipe("Tidy", [RecipeStep("empty_files"), RecipeStep("empty_folders")])
    ctx = _ctx(tmp_path)
    try:
        run = run_recipe(recipe, ctx, lambda s, p: True, tools=tools, folders=[root])
    finally:
        ctx.close()
    assert [o.status for o in run.outcomes] == ["nothing"]


def test_a_step_needing_attention_blocks_the_whole_run(
    tools: dict[str, Tool], tmp_path: Path
) -> None:
    root = _tree(tmp_path)
    recipe = Recipe("Broken", [RecipeStep("empty_files"), RecipeStep("no_such_tool")])
    ctx = _ctx(tmp_path)
    try:
        run = run_recipe(recipe, ctx, lambda s, p: True, tools=tools, folders=[root])
    finally:
        ctx.close()
    assert [(o.index, o.status) for o in run.outcomes] == [(2, "blocked")]
    assert (root / "empty.txt").exists()  # nothing ran, not even step 1


def test_undo_recipe_run_goes_last_to_first_and_reports_each(
    tools: dict[str, Tool], tmp_path: Path
) -> None:
    root = _tree(tmp_path)
    recipe = Recipe("Tidy", [RecipeStep("empty_files"), RecipeStep("empty_folders")])
    ctx = _ctx(tmp_path)
    try:
        run = run_recipe(recipe, ctx, lambda s, p: True, tools=tools, folders=[root])
        assert ctx.journal is not None
        members = recipe_run_members(ctx.journal.runs(), run.recipe_run_id)
    finally:
        ctx.close()
    ids = [m.id for m in members]
    assert ids == sorted(ids, reverse=True)
    path = Settings(data_folder=tmp_path / "data").journal_path()
    with Journal(path) as journal:
        refused = {ids[0]: "still applying"}
        first = undo_recipe_run(journal, ids, can_undo=lambda rid: refused.get(rid))
    assert [rid for rid, _ in first] == ids
    assert first[0][1] == "still applying"
    assert not isinstance(first[1][1], str)
    with Journal(path) as journal:
        second = undo_recipe_run(journal, ids[:1])
    assert not isinstance(second[0][1], str)


def test_step_from_options_drops_locked_and_replaces_originals(tools: dict[str, Tool]) -> None:
    tool = tools["media_resize"]
    values = {"output": "overwrite", "allow_overwrite": True, "remove_raw_originals": True}
    step = step_from_options(tool, {**values, "keep_metadata": False}, own_folders=False)
    assert step.options == {"keep_metadata": False}
    dup = tools["duplicates"]
    thresholds = {s.name: 99 for s in describe(dup.Options) if s.locked}
    assert thresholds
    assert step_from_options(dup, thresholds, own_folders=False).options == {}


def test_a_hand_edited_step_with_unsafe_values_needs_attention(tools: dict[str, Tool]) -> None:
    step = RecipeStep("media_resize", {"output": "overwrite", "allow_overwrite": True})
    problem = check_step(step, tools)
    assert problem is not None and "output" in problem
    assert check_step(RecipeStep("media_resize", {"output": "new_folder"}), tools) is None
    assert check_step(RecipeStep("media_resize", {"keep_metadata": False}), tools) is None


def test_the_share_starter_has_no_step_that_touches_the_originals(
    tools: dict[str, Tool],
) -> None:
    starter = next(r for r in starter_recipes() if r.name == "Prepare photos to share")
    assert [s.tool for s in starter.steps] == ["media_resize"]
    assert starter.steps[0].options == {"keep_metadata": False}
