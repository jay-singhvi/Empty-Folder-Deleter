"""``<tool> apply``: confirmation, plans, permanent delete, history and undo.

Deletes in these tests use ``delete_mode = "permanent"`` on a temporary tree, so nothing lands in
the real Recycle Bin. Moves and renames use the rename tool.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from various_tools.core.journal import Journal

if TYPE_CHECKING:
    from tests.cli.conftest import Cli

Snapshot = Callable[[Path], list[tuple[str, bool]]]
RENAME_FLAGS = (
    "--match",
    "photo*.txt",
    "--template",
    "pic-{counter:03}{ext}",
    "--date-source",
    "modified",
    "--counter-scope",
    "global",
)


def _photos(cli: Cli) -> Path:
    folder = cli.work / "photos"
    folder.mkdir()
    (folder / "photo1.txt").write_text("one")
    (folder / "photo2.txt").write_text("two")
    return folder


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def _save_empty_plan(cli: Cli, tree: Path, *extra: str) -> Path:
    plan = cli.work / "plan.json"
    scan = cli.run(
        "empty_folders", "scan", str(tree), "--delete-mode", "permanent", "--save-plan", str(plan)
    )
    assert scan.exit_code == 0, scan.output
    return plan


def test_apply_declined_changes_nothing(cli: Cli, snapshot: Snapshot) -> None:
    folder = _photos(cli)
    before = snapshot(cli.work)
    result = cli.run("rename", "apply", str(folder), *RENAME_FLAGS, input="n\n")
    assert result.exit_code == 1
    assert "Apply 2 action(s)" in result.output
    assert "Nothing was changed" in result.output
    assert snapshot(cli.work) == before
    assert not cli.journal.exists()


def test_apply_without_input_refuses(cli: Cli, snapshot: Snapshot) -> None:
    folder = _photos(cli)
    before = snapshot(cli.work)
    result = cli.run("rename", "apply", str(folder), *RENAME_FLAGS)  # no --yes, empty stdin
    assert result.exit_code == 1
    assert "--yes" in result.output
    assert snapshot(cli.work) == before


def test_apply_confirmed_with_yes_flag(cli: Cli) -> None:
    folder = _photos(cli)
    result = cli.run("rename", "apply", str(folder), *RENAME_FLAGS, "--yes")
    assert result.exit_code == 0, result.output
    assert _names(folder) == ["pic-001.txt", "pic-002.txt"]
    assert "Run 1 done: 2 done, 0 failed, 0 skipped" in result.output
    assert "various-tools undo 1" in result.output
    assert cli.journal.is_file()


def test_apply_confirmed_by_typing_yes(cli: Cli) -> None:
    folder = _photos(cli)
    result = cli.run("rename", "apply", str(folder), *RENAME_FLAGS, input="y\n")
    assert result.exit_code == 0, result.output
    assert _names(folder) == ["pic-001.txt", "pic-002.txt"]


def test_apply_a_saved_plan(cli: Cli) -> None:
    folder = _photos(cli)
    plan = cli.work / "rename-plan.json"
    scan = cli.run("rename", "scan", str(folder), *RENAME_FLAGS, "--save-plan", str(plan))
    assert scan.exit_code == 0, scan.output
    assert _names(folder) == ["photo1.txt", "photo2.txt"], "scan changes nothing"
    result = cli.run("rename", "apply", "--plan", str(plan), "--yes")
    assert result.exit_code == 0, result.output
    assert _names(folder) == ["pic-001.txt", "pic-002.txt"]


def test_apply_honours_unselected_actions_in_a_plan(cli: Cli) -> None:
    folder = _photos(cli)
    plan = cli.work / "rename-plan.json"
    cli.run("rename", "scan", str(folder), *RENAME_FLAGS, "--save-plan", str(plan))
    data = json.loads(plan.read_text(encoding="utf-8"))
    data["actions"][0]["selected"] = False  # a person edited the plan
    plan.write_text(json.dumps(data), encoding="utf-8")
    result = cli.run("rename", "apply", "--plan", str(plan), "--yes")
    assert result.exit_code == 0, result.output
    assert _names(folder) == ["photo1.txt", "pic-002.txt"]


def _unselected_plan(cli: Cli, folder: Path) -> Path:
    plan = cli.work / "rename-plan.json"
    cli.run("rename", "scan", str(folder), *RENAME_FLAGS, "--save-plan", str(plan))
    data = json.loads(plan.read_text(encoding="utf-8"))
    for action in data["actions"]:
        action["selected"] = False  # like a tool that starts its plan unselected
    plan.write_text(json.dumps(data), encoding="utf-8")
    return plan


def test_fully_unselected_plan_suggests_select_all(cli: Cli) -> None:
    folder = _photos(cli)
    result = cli.run("rename", "apply", "--plan", str(_unselected_plan(cli, folder)), "--yes")
    assert result.exit_code == 0, result.output
    assert "--select-all" in result.output
    assert _names(folder) == ["photo1.txt", "photo2.txt"]


def test_select_all_applies_every_action_after_asking(cli: Cli) -> None:
    folder = _photos(cli)
    plan = _unselected_plan(cli, folder)
    declined = cli.run("rename", "apply", "--plan", str(plan), "--select-all", input="n\n")
    assert declined.exit_code == 1
    assert "Apply 2 action(s)" in declined.output
    assert _names(folder) == ["photo1.txt", "photo2.txt"]
    result = cli.run("rename", "apply", "--plan", str(plan), "--select-all", "--yes")
    assert result.exit_code == 0, result.output
    assert _names(folder) == ["pic-001.txt", "pic-002.txt"]


def test_plan_for_another_tool_is_rejected(cli: Cli, empty_tree: Path, snapshot: Snapshot) -> None:
    plan = cli.work / "plan.json"
    cli.run("empty_folders", "scan", str(empty_tree), "--save-plan", str(plan))
    before = snapshot(cli.work)
    result = cli.run("rename", "apply", "--plan", str(plan), "--yes")
    assert result.exit_code == 2
    assert "plan for 'empty_folders'" in result.output
    assert snapshot(cli.work) == before


def test_plan_cannot_be_mixed_with_options(cli: Cli, empty_tree: Path) -> None:
    plan = cli.work / "plan.json"
    cli.run("empty_folders", "scan", str(empty_tree), "--save-plan", str(plan))
    result = cli.run("empty_folders", "apply", "--plan", str(plan), str(empty_tree), "--yes")
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_unreadable_plan_exits_2(cli: Cli) -> None:
    missing = cli.run("rename", "apply", "--plan", str(cli.work / "nope.json"), "--yes")
    assert missing.exit_code == 2
    bad = cli.work / "bad.json"
    bad.write_text("this is not json", encoding="utf-8")
    broken = cli.run("rename", "apply", "--plan", str(bad), "--yes")
    assert broken.exit_code == 2
    assert "not a usable plan" in broken.output


def test_apply_with_nothing_selected(cli: Cli, tmp_path: Path) -> None:
    empty = cli.work / "nothing"
    empty.mkdir()
    result = cli.run("empty_folders", "apply", str(empty), "--yes")
    assert result.exit_code == 0, result.output
    assert "Nothing to apply" in result.output
    assert not cli.journal.exists()


def test_permanent_delete_needs_a_second_confirmation(
    cli: Cli, empty_tree: Path, snapshot: Snapshot
) -> None:
    cli.write_settings('delete_mode = "permanent"\n')
    plan = _save_empty_plan(cli, empty_tree)
    before = snapshot(cli.work)

    declined = cli.run("empty_folders", "apply", "--plan", str(plan), input="y\nn\n")
    assert declined.exit_code == 1
    assert "PERMANENT DELETE" in declined.output
    assert snapshot(cli.work) == before

    only_first = cli.run("empty_folders", "apply", "--plan", str(plan), input="y\n")
    assert only_first.exit_code == 1, "the second question gets no answer: refused"
    assert snapshot(cli.work) == before

    confirmed = cli.run("empty_folders", "apply", "--plan", str(plan), input="y\ny\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert not (empty_tree / "a").exists()
    assert not (empty_tree / "c").exists()
    assert (empty_tree / "keep" / "file.txt").is_file()
    assert "Recycle Bin" in confirmed.output


def test_yes_skips_both_confirmations(cli: Cli, empty_tree: Path) -> None:
    cli.write_settings('delete_mode = "permanent"\n')
    plan = _save_empty_plan(cli, empty_tree)
    result = cli.run("empty_folders", "apply", "--plan", str(plan), "--yes")
    assert result.exit_code == 0, result.output
    assert not (empty_tree / "a").exists()


def test_permanent_delete_is_skipped_when_the_setting_is_off(
    cli: Cli, empty_tree: Path, snapshot: Snapshot
) -> None:
    plan = _save_empty_plan(cli, empty_tree)  # settings keep delete_mode = trash
    before = snapshot(cli.work)
    result = cli.run("empty_folders", "apply", "--plan", str(plan), "--yes")
    assert result.exit_code == 1, "nothing was done"
    assert "delete_mode" in result.output
    assert "skipped" in result.output.lower()
    assert snapshot(cli.work) == before


def test_history_lists_runs_and_shows_one(cli: Cli) -> None:
    folder = _photos(cli)
    assert "No history yet" in cli.run("history").output
    cli.run("rename", "apply", str(folder), *RENAME_FLAGS, "--yes")
    listing = cli.run("history")
    assert listing.exit_code == 0, listing.output
    assert "rename" in listing.output
    assert "done" in listing.output
    one = cli.run("history", "1")
    assert one.exit_code == 0, one.output
    assert "photo1.txt" in one.output
    assert "pic-001.txt" in one.output
    limited = cli.run("history", "--limit", "1")
    assert limited.exit_code == 0
    missing = cli.run("history", "99")
    assert missing.exit_code == 1
    assert "No run with id 99" in missing.output


def test_undo_restores_the_files(cli: Cli) -> None:
    folder = _photos(cli)
    cli.run("rename", "apply", str(folder), *RENAME_FLAGS, "--yes")
    assert _names(folder) == ["pic-001.txt", "pic-002.txt"]

    declined = cli.run("undo", "1", input="n\n")
    assert declined.exit_code == 1
    assert _names(folder) == ["pic-001.txt", "pic-002.txt"]

    result = cli.run("undo", "1", "--yes")
    assert result.exit_code == 0, result.output
    assert "undone" in result.output
    assert _names(folder) == ["photo1.txt", "photo2.txt"]
    with Journal(cli.journal) as journal:
        run = journal.run(1)
        assert run is not None
        assert run.status == "undone"

    again = cli.run("undo", "1", "--yes")
    assert again.exit_code == 1
    assert "Nothing to undo" in again.output


def test_undo_unknown_run_and_no_history(cli: Cli) -> None:
    none = cli.run("undo", "1", "--yes")
    assert none.exit_code == 1
    assert "no history" in none.output.lower()
    folder = _photos(cli)
    cli.run("rename", "apply", str(folder), *RENAME_FLAGS, "--yes")
    missing = cli.run("undo", "7", "--yes")
    assert missing.exit_code == 1
    assert "No run with id 7" in missing.output
    bad = cli.run("undo", "seven")
    assert bad.exit_code == 2


def test_undo_with_nothing_reversible(cli: Cli, empty_tree: Path) -> None:
    cli.write_settings('delete_mode = "permanent"\n')
    plan = _save_empty_plan(cli, empty_tree)
    cli.run("empty_folders", "apply", "--plan", str(plan), "--yes")
    result = cli.run("undo", "1", "--yes")
    assert result.exit_code == 1
    assert "Nothing to undo" in result.output
    assert "Recycle Bin" in result.output


def test_a_broken_settings_file_is_a_usage_error(cli: Cli) -> None:
    cli.config.write_text("this = is = not toml", encoding="utf-8")
    result = cli.run("history")
    assert result.exit_code == 2
    assert "settings" in result.output.lower()
    assert cli.config.read_text(encoding="utf-8") == "this = is = not toml"
