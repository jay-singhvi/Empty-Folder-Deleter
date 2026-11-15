"""``various-tools schedule``. The Task Scheduler client always has a fake process runner, so no
test creates, changes or deletes a real scheduled task: assertions are on the ``schtasks`` command
lines that would run."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from various_tools.cli import schedule_cmd
from various_tools.core.presets import PresetStore
from various_tools.core.schedule import TaskScheduler

if TYPE_CHECKING:
    from tests.cli.conftest import Cli

SCHTASKS = "schtasks-fake"


class Fake:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.tasks: dict[str, str] = {}  # full name -> command

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
                row[1], row[2], row[3], row[6], row[8] = f"\\{name}", "soon", "Ready", "0", command
                row[18], row[19] = "Daily", "9:00:00 AM"
                lines.append(",".join('"' + c.replace('"', '""') + '"' for c in row))
            return subprocess.CompletedProcess(args, 0, "\n".join(lines), "")
        if verb == "/Create":
            self.tasks[args[args.index("/TN") + 1]] = args[args.index("/TR") + 1]
        if verb == "/Delete":
            self.tasks.pop(args[args.index("/TN") + 1], None)
        return subprocess.CompletedProcess(args, 0, "SUCCESS", "")

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    runner = Fake()
    monkeypatch.setattr(
        schedule_cmd, "make_scheduler", lambda: TaskScheduler(runner, schtasks=SCHTASKS)
    )
    return runner


@pytest.fixture
def preset(cli: Cli) -> str:
    folder = cli.work / "tree"
    folder.mkdir()
    saved = cli.run("presets", "save", "empty_folders", "Weekly", str(folder))
    assert saved.exit_code == 0, saved.output
    return "Weekly"


def add(cli: Cli, *args: str, input: str | None = "y\n"):
    return cli.run(
        "schedule", "add", "Nightly", "--tool", "empty_folders", "--preset", "Weekly", *args,
        input=input,
    )  # fmt: skip


# ----- add -----------------------------------------------------------------------------------


def test_add_shows_what_will_run_and_creates_the_task(cli: Cli, preset: str, fake: Fake) -> None:
    result = add(cli, "--daily", "9:00")
    assert result.exit_code == 0, result.output
    assert "Create this scheduled run?" in result.output
    assert "every day at 09:00" in result.output
    assert "empty_folders apply --preset Weekly --yes" in result.output
    assert "journaled" in result.output and "undone" in result.output and "History" in result.output
    assert "Recycle Bin" in result.output

    create = next(c for c in fake.calls if c[1] == "/Create")
    assert create[0] == SCHTASKS
    assert create[create.index("/TN") + 1] == "Various Tools\\Nightly"
    command = create[create.index("/TR") + 1]
    assert command.startswith(f"{sys.executable} -m various_tools.cli.main ")
    assert command.endswith("empty_folders apply --preset Weekly --yes")
    assert create[create.index("/SC") + 1] == "DAILY"


def test_add_passes_a_non_default_config_to_the_task(cli: Cli, preset: str, fake: Fake) -> None:
    assert add(cli, "--daily", "9:00").exit_code == 0
    command = fake.tasks["Various Tools\\Nightly"]
    assert f"--config {cli.config}" in command or f'--config "{cli.config}"' in command


def test_add_weekly_and_monthly(cli: Cli, preset: str, fake: Fake) -> None:
    assert add(cli, "--weekly", "mon,thu", "6:30").exit_code == 0
    create = fake.calls[-1]
    assert create[create.index("/SC") + 1 : create.index("/SC") + 6] == [
        "WEEKLY",
        "/D",
        "MON,THU",
        "/ST",
        "06:30",
    ]
    fake.tasks.clear()
    assert add(cli, "--monthly", "15", "23:00").exit_code == 0
    create = fake.calls[-1]
    assert create[create.index("/SC") + 1 : create.index("/SC") + 6] == [
        "MONTHLY",
        "/D",
        "15",
        "/ST",
        "23:00",
    ]


def test_add_declined_creates_nothing(cli: Cli, preset: str, fake: Fake) -> None:
    result = add(cli, "--daily", "9:00", input="n\n")
    assert result.exit_code == 1
    assert "Nothing was scheduled" in result.output
    assert "/Create" not in fake.verbs()


def test_add_without_a_terminal_and_without_yes_is_refused(
    cli: Cli, preset: str, fake: Fake
) -> None:
    result = add(cli, "--daily", "9:00", input=None)
    assert result.exit_code == 1
    assert "--yes" in result.output
    assert "/Create" not in fake.verbs()


def test_add_yes_skips_the_question_but_still_shows_the_summary(
    cli: Cli, preset: str, fake: Fake
) -> None:
    result = add(cli, "--daily", "9:00", "--yes", input=None)
    assert result.exit_code == 0, result.output
    assert "empty_folders apply --preset Weekly --yes" in result.output
    assert "journaled" in result.output
    assert "/Create" in fake.verbs()


def test_add_needs_exactly_one_trigger(cli: Cli, preset: str, fake: Fake) -> None:
    assert add(cli, "--yes").exit_code == 2
    assert add(cli, "--daily", "9:00", "--weekly", "mon", "9:00", "--yes").exit_code == 2
    assert fake.calls == []


@pytest.mark.parametrize(
    "trigger", [["--daily", "25:00"], ["--weekly", "someday", "9:00"], ["--monthly", "31", "9:00"]]
)
def test_add_rejects_bad_times_and_days(
    cli: Cli, preset: str, fake: Fake, trigger: list[str]
) -> None:
    assert add(cli, *trigger, "--yes").exit_code == 2
    assert fake.calls == []


def test_add_rejects_bad_names(cli: Cli, preset: str, fake: Fake) -> None:
    for name in ("a\\b", 'a"b', "..", "a/b"):
        result = cli.run(
            "schedule", "add", name, "--tool", "empty_folders", "--preset", "Weekly",
            "--daily", "9:00", "--yes",
        )  # fmt: skip
        assert result.exit_code == 2, name
    assert fake.calls == []


def test_add_unknown_tool_or_preset(cli: Cli, preset: str, fake: Fake) -> None:
    unknown_tool = cli.run(
        "schedule", "add", "x", "--tool", "nope", "--preset", "Weekly", "--daily", "9:00", "--yes"
    )
    assert unknown_tool.exit_code == 2
    assert "Unknown tool" in unknown_tool.output
    unknown_preset = cli.run(
        "schedule", "add", "x", "--tool", "empty_folders", "--preset", "Nope",
        "--daily", "9:00", "--yes",
    )  # fmt: skip
    assert unknown_preset.exit_code == 2
    assert "Saved presets: Weekly" in unknown_preset.output
    assert fake.calls == []


def test_add_refuses_a_permanent_delete_preset(cli: Cli, preset: str, fake: Fake) -> None:
    PresetStore(cli.presets).save("empty_folders", "Weekly", {"delete_mode": "permanent"})
    result = add(cli, "--daily", "9:00", "--yes")
    assert result.exit_code == 1
    assert "permanent" in result.output and "Recycle Bin" in result.output
    assert fake.calls == []


def test_add_refuses_changed_locked_options(cli: Cli, fake: Fake) -> None:
    PresetStore(cli.presets).save("duplicates", "Loose", {"phash_max": 5})
    result = cli.run(
        "schedule", "add", "x", "--tool", "duplicates", "--preset", "Loose",
        "--daily", "9:00", "--yes",
    )  # fmt: skip
    assert result.exit_code == 1
    assert "locked" in result.output and "--phash-max" in result.output
    assert fake.calls == []


def test_add_refuses_an_invalid_preset(cli: Cli, fake: Fake) -> None:
    PresetStore(cli.presets).save("empty_folders", "Broken", {"min_age_days": -5})
    result = cli.run(
        "schedule", "add", "x", "--tool", "empty_folders", "--preset", "Broken",
        "--daily", "9:00", "--yes",
    )  # fmt: skip
    assert result.exit_code == 1
    assert "not complete enough" in result.output
    assert fake.calls == []


def test_add_refuses_a_preset_name_that_cannot_be_quoted(cli: Cli, fake: Fake) -> None:
    PresetStore(cli.presets).save("empty_folders", 'say "hi"', {})
    result = cli.run(
        "schedule", "add", "x", "--tool", "empty_folders", "--preset", 'say "hi"',
        "--daily", "9:00", "--yes",
    )  # fmt: skip
    assert result.exit_code == 1
    assert "/Create" not in fake.verbs()


def test_add_never_overwrites(cli: Cli, preset: str, fake: Fake) -> None:
    fake.tasks["Various Tools\\Nightly"] = "something"
    result = add(cli, "--daily", "9:00", "--yes")
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert "/Create" not in fake.verbs()
    assert fake.tasks["Various Tools\\Nightly"] == "something"


# ----- list, remove, run ---------------------------------------------------------------------


def test_list_empty_and_after_add(cli: Cli, preset: str) -> None:
    assert "No scheduled runs" in cli.run("schedule", "list").output
    assert add(cli, "--daily", "9:00").exit_code == 0
    listing = cli.run("schedule", "list")
    assert listing.exit_code == 0, listing.output
    assert "Nightly" in listing.output
    assert "empty_folders" in listing.output
    assert "Weekly" in listing.output
    assert "Daily" in listing.output


def test_list_ignores_other_tasks(cli: Cli, fake: Fake) -> None:
    fake.tasks["Microsoft\\Windows\\Defrag"] = "defrag.exe"
    assert "No scheduled runs" in cli.run("schedule", "list").output


def test_remove_asks_then_deletes(cli: Cli, preset: str, fake: Fake) -> None:
    assert add(cli, "--daily", "9:00").exit_code == 0
    declined = cli.run("schedule", "remove", "Nightly", input="n\n")
    assert declined.exit_code == 1
    assert "Various Tools\\Nightly" in fake.tasks

    no_input = cli.run("schedule", "remove", "Nightly", input=None)
    assert no_input.exit_code == 1
    assert "Various Tools\\Nightly" in fake.tasks

    done = cli.run("schedule", "remove", "Nightly", input="y\n")
    assert done.exit_code == 0, done.output
    assert fake.tasks == {}
    assert [SCHTASKS, "/Delete", "/TN", "Various Tools\\Nightly", "/F"] in fake.calls


def test_remove_missing_and_bad_name(cli: Cli, fake: Fake) -> None:
    assert cli.run("schedule", "remove", "Nope", "--yes").exit_code == 1
    assert cli.run("schedule", "remove", "a\\b", "--yes").exit_code == 2
    assert "/Delete" not in fake.verbs()


def test_run_now_confirms_then_starts(cli: Cli, preset: str, fake: Fake) -> None:
    assert add(cli, "--daily", "9:00").exit_code == 0
    declined = cli.run("schedule", "run", "Nightly", input="n\n")
    assert declined.exit_code == 1
    assert "/Run" not in fake.verbs()
    started = cli.run("schedule", "run", "Nightly", input="y\n")
    assert started.exit_code == 0, started.output
    assert [SCHTASKS, "/Run", "/TN", "Various Tools\\Nightly"] in fake.calls


def test_run_now_rechecks_the_preset(cli: Cli, preset: str, fake: Fake) -> None:
    assert add(cli, "--daily", "9:00").exit_code == 0
    PresetStore(cli.presets).save("empty_folders", "Weekly", {"delete_mode": "permanent"})
    result = cli.run("schedule", "run", "Nightly", "--yes")
    assert result.exit_code == 1
    assert "/Run" not in fake.verbs()


def test_run_now_refuses_a_task_that_is_not_ours(cli: Cli, fake: Fake) -> None:
    fake.tasks["Various Tools\\Hand made"] = "notepad.exe"
    result = cli.run("schedule", "run", "Hand made", "--yes")
    assert result.exit_code == 1
    assert "/Run" not in fake.verbs()


def test_run_now_missing_task(cli: Cli, fake: Fake) -> None:
    assert cli.run("schedule", "run", "Nope", "--yes").exit_code == 1


def test_a_missing_schtasks_is_reported(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_args: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("schtasks.exe")

    monkeypatch.setattr(schedule_cmd, "make_scheduler", lambda: TaskScheduler(broken))
    result = cli.run("schedule", "list")
    assert result.exit_code == 1
    assert "schtasks" in result.output


def test_help_lists_schedule(cli: Cli) -> None:
    assert "schedule" in cli.run("--help", config=False).output
