"""Safety review fixes on the CLI side.

* ``schedule add`` refuses presets that replace or remove originals (a field marked
  ``replaces_originals`` set away from its default), and the task it creates passes the hidden
  ``--scheduled`` marker. Under ``--scheduled``, ``apply`` re-checks the same rules at run time
  and refuses any permanent delete, whatever ``--yes`` or ``--allow-locked`` say.
* The apply confirmation says how many cloud-only files the ticked rows download.
* ``download`` refuses folders.

The Task Scheduler client has a fake runner (no real task); ``force_download`` is a fake; every
tree lives under ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from various_tools.cli import schedule_cmd
from various_tools.core import onedrive as onedrive_module
from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.core.presets import PresetStore
from various_tools.core.schedule import TaskScheduler
from various_tools.core.schema import describe
from various_tools.tools.base import discover

if TYPE_CHECKING:
    from conftest import Cli

SCHTASKS = "schtasks-fake"


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.tasks: dict[str, str] = {}

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        verb = args[1]
        if verb == "/Query" and "/TN" in args:
            name = args[args.index("/TN") + 1]
            return subprocess.CompletedProcess(args, 0 if name in self.tasks else 1, "", "")
        if verb == "/Query":
            lines = []
            for name, command in self.tasks.items():
                row = [""] * 28
                row[1], row[8] = f"\\{name}", command
                lines.append(",".join('"' + c.replace('"', '""') + '"' for c in row))
            return subprocess.CompletedProcess(args, 0, "\n".join(lines), "")
        if verb == "/Create":
            self.tasks[args[args.index("/TN") + 1]] = args[args.index("/TR") + 1]
        return subprocess.CompletedProcess(args, 0, "SUCCESS", "")

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setattr(
        schedule_cmd, "make_scheduler", lambda: TaskScheduler(runner, schtasks=SCHTASKS)
    )
    return runner


def _schedule(cli: Cli, tool: str, preset: str) -> Any:
    return cli.run(
        "schedule", "add", "Nightly", "--tool", tool, "--preset", preset, "--daily", "9:00",
        "--yes",
    )  # fmt: skip


# ----- item 1: which fields replace or remove originals ------------------------------------------

MARKED = {
    "media_resize": {"output", "remove_raw_originals"},
    "metadata_strip": {"in_place"},
    "media_convert": {"trash_originals"},
    "organize_by_date": {"also_resize", "cloud_only"},
}


def test_exactly_the_fields_that_replace_originals_are_marked() -> None:
    tools = discover()
    for tool_id, tool in tools.items():
        marked = {s.name for s in describe(tool.Options) if s.replaces_originals}
        assert marked == MARKED.get(tool_id, set()), tool_id


@pytest.mark.parametrize(
    ("tool", "values", "flag"),
    [
        ("media_resize", {"output": "overwrite", "allow_overwrite": True}, "--output"),
        (
            "media_resize",
            {"convert_raw_to_jpeg": True, "remove_raw_originals": True},
            "--remove-raw-originals",
        ),
        ("metadata_strip", {"in_place": True}, "--in-place"),
        ("media_convert", {"trash_originals": True}, "--trash-originals"),
        ("organize_by_date", {"also_resize": True}, "--also-resize"),
    ],
)
def test_schedule_add_refuses_presets_that_replace_originals(
    cli: Cli, fake: FakeRunner, tool: str, values: dict[str, Any], flag: str
) -> None:
    PresetStore(cli.presets).save(tool, "Risky", values)
    result = _schedule(cli, tool, "Risky")
    assert result.exit_code == 1, result.output
    assert "original" in result.output and flag in result.output
    assert "/Create" not in fake.verbs()


def test_schedule_add_allows_a_safe_media_preset(cli: Cli, fake: FakeRunner) -> None:
    PresetStore(cli.presets).save(
        "media_resize", "Safe", {"roots": [str(cli.work)], "convert_raw_to_jpeg": True}
    )
    result = _schedule(cli, "media_resize", "Safe")
    assert result.exit_code == 0, result.output
    command = fake.tasks["Various Tools\\Nightly"]
    assert command.endswith("media_resize apply --preset Safe --scheduled")


def test_schedule_run_refuses_a_task_without_the_scheduled_marker(
    cli: Cli, fake: FakeRunner
) -> None:
    PresetStore(cli.presets).save("empty_folders", "Weekly", {"roots": [str(cli.work)]})
    fake.tasks["Various Tools\\Old"] = (
        "py -m various_tools.cli.main empty_folders apply --preset Weekly --yes"
    )
    result = cli.run("schedule", "run", "Old", "--yes")
    assert result.exit_code == 1
    assert "/Run" not in fake.verbs()
    assert "schedule remove" in result.output


# ----- item 1: apply --scheduled re-checks at run time -----------------------------------------


def test_scheduled_apply_refuses_a_preset_changed_to_replace_originals(cli: Cli) -> None:
    folder = cli.work / "pics"
    folder.mkdir()
    PresetStore(cli.presets).save(
        "media_convert", "Nightly", {"roots": [str(folder)], "trash_originals": True}
    )
    result = cli.run("media_convert", "apply", "--preset", "Nightly", "--yes", "--scheduled")
    assert result.exit_code == 1
    assert "--trash-originals" in result.output
    assert not cli.journal.exists()  # nothing was applied, so nothing was journaled


def test_scheduled_apply_refuses_permanent_deletes_even_with_allow_locked(
    cli: Cli, empty_tree: Path
) -> None:
    cli.write_settings('delete_mode = "permanent"\n')
    PresetStore(cli.presets).save(
        "empty_folders", "Nightly", {"roots": [str(empty_tree)], "delete_mode": "permanent"}
    )
    before = sorted(p.name for p in empty_tree.rglob("*"))
    result = cli.run(
        "empty_folders", "apply", "--preset", "Nightly", "--yes", "--allow-locked", "--scheduled"
    )
    assert result.exit_code == 1
    assert "permanent" in result.output.casefold()
    assert sorted(p.name for p in empty_tree.rglob("*")) == before
    assert not cli.journal.exists()


def test_scheduled_apply_needs_a_preset(cli: Cli, empty_tree: Path) -> None:
    result = cli.run("empty_folders", "apply", str(empty_tree), "--yes", "--scheduled")
    assert result.exit_code == 2
    assert "--preset" in result.output


def test_scheduled_apply_of_a_safe_preset_runs(cli: Cli, empty_tree: Path) -> None:
    PresetStore(cli.presets).save("empty_folders", "Nightly", {"roots": [str(empty_tree)]})
    result = cli.run("empty_folders", "apply", "--preset", "Nightly", "--yes", "--scheduled")
    assert result.exit_code == 0, result.output
    assert cli.journal.exists()


def test_the_scheduled_flag_is_hidden_from_help(cli: Cli) -> None:
    result = cli.run("empty_folders", "apply", "--help")
    assert "--scheduled" not in result.output


# ----- item 6a: download wording in the apply confirmation ---------------------------------------


def test_apply_confirmation_says_how_many_files_will_be_downloaded(cli: Cli) -> None:
    plan = Plan(
        tool="organize_by_date",
        roots=[cli.work],
        actions=[
            PlannedAction(ActionKind.CONVERT, cli.work / "a.heic", None, "cloud-only: download"),
            PlannedAction(ActionKind.CONVERT, cli.work / "b.heic", None, "cloud-only: download"),
        ],
    )
    saved = cli.work / "plan.json"
    saved.write_text(plan.to_json(), encoding="utf-8")
    result = cli.run("organize_by_date", "apply", "--plan", str(saved), input="n\n")
    assert result.exit_code == 1
    assert "download 2 cloud-only file(s) from OneDrive" in result.output
    assert "Nothing was changed" in result.output


# ----- item 6b: download refuses folders ---------------------------------------------------------


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def fake_download(path: Path, wait_seconds: int | None = None) -> tuple[bool, str]:
        calls.append(path)
        return True, "downloaded"

    monkeypatch.setattr(onedrive_module, "force_download", fake_download)
    return calls


def test_download_refuses_a_folder(cli: Cli, downloads: list[Path]) -> None:
    folder = cli.work / "Pictures"
    folder.mkdir()
    result = cli.run("download", str(folder), str(cli.work / "a.jpg"), "--yes")
    assert result.exit_code == 2
    assert "folder" in result.output and "plan" in result.output
    assert downloads == []


def test_download_from_a_plan_refuses_a_listed_folder(cli: Cli, downloads: list[Path]) -> None:
    folder = cli.work / "Pictures"
    folder.mkdir()
    plan = Plan(tool="t", roots=[cli.work], cloud_only=[folder])
    saved = cli.work / "plan.json"
    saved.write_text(plan.to_json(), encoding="utf-8")
    result = cli.run("download", "--plan", str(saved), "--yes")
    assert result.exit_code == 2
    assert downloads == []
