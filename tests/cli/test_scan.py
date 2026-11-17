"""``<tool> scan``: tables, JSON, saved plans, positional paths and validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from various_tools.core.plan import ActionKind, Plan

if TYPE_CHECKING:
    from tests.cli.conftest import Cli

Snapshot = Callable[[Path], list[tuple[str, bool]]]


def test_scan_prints_a_table_and_changes_nothing(
    cli: Cli, empty_tree: Path, snapshot: Snapshot
) -> None:
    before = snapshot(cli.work)
    result = cli.run("empty_folders", "scan", str(empty_tree))
    assert result.exit_code == 0, result.output
    assert "Plan for empty_folders" in result.output
    for column in ("Kind", "Source", "Destination", "Reason", "Selected"):
        assert column in result.output
    assert str(empty_tree / "a" / "b") in result.output
    assert "Thumbs.db" in result.output
    assert "4 action(s), 4 selected" in result.output
    assert "Dry run: nothing was changed" in result.output
    assert snapshot(cli.work) == before
    assert not cli.journal.exists(), "a scan needs no journal"


def test_scan_json_is_only_the_plan(cli: Cli, empty_tree: Path) -> None:
    result = cli.run("empty_folders", "scan", str(empty_tree), "--json")
    assert result.exit_code == 0, result.output
    plan = Plan.from_json(result.stdout)  # the whole of stdout parses: no decoration
    assert plan.tool == "empty_folders"
    assert plan.roots == [empty_tree]
    assert {a.kind for a in plan.actions} == {ActionKind.TRASH}
    assert any(a.src == empty_tree / "c" / "Thumbs.db" for a in plan.actions)
    assert "Dry run" not in result.stdout


def test_save_plan_writes_a_plan_file(cli: Cli, empty_tree: Path) -> None:
    target = cli.work / "plan.json"
    result = cli.run("empty_folders", "scan", str(empty_tree), "--save-plan", str(target))
    assert result.exit_code == 0, result.output
    plan = Plan.from_json(target.read_text(encoding="utf-8"))
    assert len(plan.actions) == 4
    assert json.loads(target.read_text(encoding="utf-8"))["tool"] == "empty_folders"


def test_save_plan_and_json_together(cli: Cli, empty_tree: Path) -> None:
    target = cli.work / "plan.json"
    result = cli.run("empty_folders", "scan", str(empty_tree), "--json", "--save-plan", str(target))
    assert result.exit_code == 0, result.output
    assert (
        Plan.from_json(result.stdout).actions == Plan.from_json(target.read_text("utf-8")).actions
    )


def test_roots_flag_and_positional_paths_combine(cli: Cli, empty_tree: Path) -> None:
    other = cli.work / "other"
    (other / "x").mkdir(parents=True)
    result = cli.run("empty_folders", "scan", "--roots", str(empty_tree), str(other), "--json")
    assert result.exit_code == 0, result.output
    assert set(Plan.from_json(result.stdout).roots) == {empty_tree, other}


def test_single_folder_tool_takes_one_path(cli: Cli) -> None:
    project = cli.work / "proj"
    project.mkdir()
    (project / "main.py").write_text("print('hi')\n")
    ok = cli.run("project_export", "scan", str(project), "--json")
    assert ok.exit_code == 0, ok.output
    two = cli.run("project_export", "scan", str(project), str(cli.work), "--json")
    assert two.exit_code == 2
    assert "takes one folder" in two.output


def test_validation_error_exits_2_and_names_the_flag(cli: Cli, empty_tree: Path) -> None:
    result = cli.run("empty_folders", "scan", str(empty_tree), "--min-age-days", "-5")
    assert result.exit_code == 2
    assert "--min-age-days" in result.output


def test_bad_choice_and_bad_number_exit_2(cli: Cli, empty_tree: Path) -> None:
    choice = cli.run("empty_folders", "scan", str(empty_tree), "--delete-mode", "shred")
    assert choice.exit_code == 2
    assert "--delete-mode" in choice.output
    assert "not one of" in choice.output
    number = cli.run("empty_folders", "scan", str(empty_tree), "--min-age-days", "soon")
    assert number.exit_code == 2
    assert "whole number" in number.output


def test_missing_required_option_exits_2(cli: Cli) -> None:
    result = cli.run("project_export", "scan")
    assert result.exit_code == 2
    assert "is required" in result.output
    assert "argument" in result.output


def test_unknown_flag_exits_2(cli: Cli, empty_tree: Path) -> None:
    result = cli.run("empty_folders", "scan", str(empty_tree), "--no-such-flag")
    assert result.exit_code == 2


def test_bool_flags_and_list_flags(cli: Cli) -> None:
    tree = cli.work / "t"
    (tree / "x").mkdir(parents=True)
    (tree / "x" / "scratch.tmp").write_text("junk")
    default = cli.run("empty_folders", "scan", str(tree), "--json")
    assert [a.src for a in Plan.from_json(default.stdout).actions] == []
    both = cli.run(
        "empty_folders", "scan", str(tree), "--treat-as-empty", "a.x,scratch.tmp", "--json"
    )
    assert both.exit_code == 0, both.output
    assert len(Plan.from_json(both.stdout).actions) == 2
    repeated = cli.run(
        "empty_folders",
        "scan",
        str(tree),
        "--treat-as-empty",
        "a.x",
        "--treat-as-empty",
        "scratch.tmp",
        "--no-skip-hidden-system",
        "--json",
    )
    assert len(Plan.from_json(repeated.stdout).actions) == 2
    assert json.loads(repeated.stdout)["options"]["skip_hidden_system"] is False


def test_the_removed_preset_flag_is_rejected(cli: Cli, empty_tree: Path) -> None:
    """Saved presets were removed in 0.3.0: an old '--preset NAME' is a usage error."""
    result = cli.run("empty_folders", "scan", str(empty_tree), "--preset", "nope")
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_reserved_name_collision_becomes_opt_flag(cli: Cli) -> None:
    folder = cli.work / "chat"
    folder.mkdir()
    (folder / "Snapchat-123.jpg").write_bytes(b"x")
    result = cli.run("rename", "scan", str(folder), "--opt-preset", "snapchat", "--json")
    assert result.exit_code == 0, result.output
    plan = Plan.from_json(result.stdout)
    assert json.loads(result.stdout)["options"]["preset"] == "snapchat"
    assert [a.kind for a in plan.actions] == [ActionKind.RENAME]
    # --preset stays reserved (it was the removed saved-preset flag), so it is not the field.
    clash = cli.run("rename", "scan", str(folder), "--preset", "snapchat")
    assert clash.exit_code == 2
    assert "No such option" in clash.output


def test_plan_notes_are_printed(cli: Cli) -> None:
    result = cli.run("empty_folders", "scan", str(cli.work / "missing"))
    assert result.exit_code == 0, result.output
    assert "Notes" in result.output
    assert "No actions planned" in result.output


def test_paths_with_brackets_are_shown_literally(cli: Cli) -> None:
    tree = cli.work / "odd [name]"
    (tree / "empty").mkdir(parents=True)
    result = cli.run("empty_folders", "scan", str(tree))
    assert result.exit_code == 0, result.output
    assert "odd [name]" in result.output
