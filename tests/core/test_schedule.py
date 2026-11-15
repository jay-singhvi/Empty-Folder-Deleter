"""Scheduled runs (``core/schedule.py``). The process runner is always a fake: no test ever
creates, changes or deletes a real scheduled task. Tests assert on the command lines that would
run."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from various_tools.core import schedule as sched
from various_tools.core.schedule import ScheduledTask, ScheduleError, TaskScheduler


class FakeRunner:
    """Records every command line and answers like ``schtasks`` would, from a set of task names."""

    def __init__(self, tasks: set[str] | None = None, *, csv: str = "") -> None:
        self.calls: list[list[str]] = []
        self.tasks = tasks if tasks is not None else set()
        self.csv = csv
        self.fail_with: tuple[int, str] | None = None

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if self.fail_with and args[1] in {"/Create", "/Delete", "/Run"}:
            code, message = self.fail_with
            return subprocess.CompletedProcess(args, code, "", message)
        verb = args[1]
        if verb == "/Query" and "/TN" in args:
            name = args[args.index("/TN") + 1]
            return subprocess.CompletedProcess(args, 0 if name in self.tasks else 1, "", "")
        if verb == "/Query":
            return subprocess.CompletedProcess(args, 0, self.csv, "")
        if verb == "/Create":
            self.tasks.add(args[args.index("/TN") + 1])
        if verb == "/Delete":
            self.tasks.discard(args[args.index("/TN") + 1])
        return subprocess.CompletedProcess(args, 0, "SUCCESS", "")


SCHTASKS = r"C:\Windows\System32\schtasks.exe"


def make(runner: FakeRunner) -> TaskScheduler:
    return TaskScheduler(runner, schtasks=SCHTASKS)


# ----- names and triggers --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Nightly", "Photos cleanup 2", "a.b_c-d (x)"])
def test_good_names(name: str) -> None:
    assert sched.validate_name(f"  {name} ") == name
    assert sched.task_path(name) == f"Various Tools\\{name}"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "a\\b",
        "a/b",
        'a"b',
        "a'b",
        "..",
        ".hidden",
        "name.",
        "x" * 65,
        "caf\u00e9",
        "a%b",
    ],
)
def test_bad_names_are_rejected(name: str) -> None:
    with pytest.raises(ScheduleError):
        sched.validate_name(name)


def test_time_parsing() -> None:
    assert sched.parse_time("9:05") == "09:05"
    assert sched.parse_time(" 23:59 ") == "23:59"
    for bad in ("24:00", "9", "09:60", "9am", "", "09:5"):
        with pytest.raises(ScheduleError):
            sched.parse_time(bad)


def test_days_are_normalised_and_ordered() -> None:
    assert sched.parse_days("thu,mon") == ("MON", "THU")
    assert sched.parse_days("Monday, friday mon") == ("MON", "FRI")
    for bad in ("", "funday", "mon,xyz"):
        with pytest.raises(ScheduleError):
            sched.parse_days(bad)


def test_trigger_arguments() -> None:
    assert sched.daily("9:00").schtasks_args() == ["/SC", "DAILY", "/ST", "09:00"]
    assert sched.weekly("fri,mon", "06:30").schtasks_args() == [
        "/SC",
        "WEEKLY",
        "/D",
        "MON,FRI",
        "/ST",
        "06:30",
    ]
    assert sched.monthly("15", "23:15").schtasks_args() == [
        "/SC",
        "MONTHLY",
        "/D",
        "15",
        "/ST",
        "23:15",
    ]
    assert sched.daily("9:00").describe() == "every day at 09:00"
    assert sched.weekly("mon", "06:30").describe() == "every MON at 06:30"
    assert sched.monthly(1, "00:00").describe() == "on day 1 of every month at 00:00"


@pytest.mark.parametrize("day", ["0", "29", "31", "x", "", "-1"])
def test_month_day_must_be_1_to_28(day: str) -> None:
    with pytest.raises(ScheduleError):
        sched.monthly(day, "09:00")


# ----- the command a task runs ---------------------------------------------------------------


def test_source_mode_runs_the_module_with_the_current_python() -> None:
    argv = sched.build_task_argv(
        "empty_folders", "Weekly", frozen=False, executable="C:\\py\\python.exe"
    )
    assert argv == [
        "C:\\py\\python.exe",
        "-m",
        "various_tools.cli.main",
        "empty_folders",
        "apply",
        "--preset",
        "Weekly",
        "--yes",
    ]


def test_the_source_module_really_exists() -> None:
    assert importlib.util.find_spec(sched.SOURCE_MODULE) is not None


def test_frozen_mode_uses_the_console_program_beside_the_app(tmp_path: Path) -> None:
    app = tmp_path / "Various Tools"
    app.mkdir()
    (app / "various-tools.exe").write_text("x")
    gui = app / "Various Tools.exe"
    gui.write_text("x")
    argv = sched.build_task_argv("duplicates", "P", frozen=True, executable=str(gui))
    assert argv == [str(app / "various-tools.exe"), "duplicates", "apply", "--preset", "P", "--yes"]


def test_frozen_mode_without_the_console_program_is_an_error(tmp_path: Path) -> None:
    gui = tmp_path / "Various Tools.exe"
    gui.write_text("x")
    with pytest.raises(ScheduleError, match="does not exist"):
        sched.build_task_argv("duplicates", "P", frozen=True, executable=str(gui))


def test_config_path_is_passed_before_the_command() -> None:
    argv = sched.build_task_argv(
        "t", "p", config_path=Path("D:/my cfg/settings.toml"), frozen=False, executable="py"
    )
    assert argv[3:5] == ["--config", str(Path("D:/my cfg/settings.toml"))]
    assert argv[5:] == ["t", "apply", "--preset", "p", "--yes"]


@pytest.mark.parametrize("preset", ['say "hi"', "50%", "tab\there"])
def test_preset_names_that_break_a_command_line_are_refused(preset: str) -> None:
    with pytest.raises(ScheduleError):
        sched.build_task_argv("t", preset, frozen=False, executable="py")


def test_names_with_spaces_are_quoted_and_parse_back() -> None:
    argv = sched.build_task_argv(
        "empty_folders",
        "My weekly preset",
        frozen=False,
        executable=r"C:\Program Files\Py\python.exe",
    )
    line = sched.command_line(argv)
    assert line.startswith('"C:\\Program Files\\Py\\python.exe" -m various_tools.cli.main ')
    assert line.endswith('empty_folders apply --preset "My weekly preset" --yes')
    assert sched.parse_task_command(line) == ("empty_folders", "My weekly preset")


def test_parse_task_command_rejects_other_commands() -> None:
    assert sched.parse_task_command("notepad.exe") is None
    assert sched.parse_task_command("x.exe t apply --preset p") is None  # no --yes
    assert sched.parse_task_command("apply --preset p --yes") is None  # no tool
    assert sched.parse_task_command("") is None


def test_too_long_a_command_is_refused() -> None:
    with pytest.raises(ScheduleError, match="characters"):
        sched.command_line(["x" * 300])


# ----- Task Scheduler calls ------------------------------------------------------------------


def test_add_runs_create_with_exactly_these_arguments() -> None:
    runner = FakeRunner()
    argv = sched.build_task_argv("empty_folders", "My P", frozen=False, executable="py")
    make(runner).add("Nightly", sched.weekly("mon,thu", "7:30"), argv)

    assert runner.calls[0] == [SCHTASKS, "/Query", "/TN", "Various Tools\\Nightly"]
    assert runner.calls[1] == [
        SCHTASKS,
        "/Create",
        "/TN",
        "Various Tools\\Nightly",
        "/TR",
        'py -m various_tools.cli.main empty_folders apply --preset "My P" --yes',
        "/SC",
        "WEEKLY",
        "/D",
        "MON,THU",
        "/ST",
        "07:30",
        "/RL",
        "LIMITED",
    ]
    flat = [a for call in runner.calls for a in call]
    # Per-user task: no other account, no password, and an existing task is never overwritten.
    for forbidden in ("/RU", "/RP", "/F", "/IT", "/S"):
        assert forbidden not in flat


def test_add_never_overwrites_an_existing_task() -> None:
    runner = FakeRunner({"Various Tools\\Nightly"})
    with pytest.raises(ScheduleError, match="already exists"):
        make(runner).add("Nightly", sched.daily("09:00"), ["py"])
    assert all(call[1] != "/Create" for call in runner.calls)


def test_add_reports_a_refusal_from_task_scheduler() -> None:
    runner = FakeRunner()
    runner.fail_with = (1, "ERROR: Access is denied.")
    with pytest.raises(ScheduleError, match="Access is denied"):
        make(runner).add("Nightly", sched.daily("09:00"), ["py"])


def test_a_bad_name_never_reaches_the_runner() -> None:
    runner = FakeRunner()
    scheduler = make(runner)
    with pytest.raises(ScheduleError):
        scheduler.add("..\\Microsoft\\Windows\\x", sched.daily("09:00"), ["py"])
    with pytest.raises(ScheduleError):
        scheduler.remove("a/b")
    with pytest.raises(ScheduleError):
        scheduler.run_now('a"b')
    assert runner.calls == []


def test_remove_and_run_target_only_the_various_tools_folder() -> None:
    runner = FakeRunner({"Various Tools\\Nightly"})
    scheduler = make(runner)
    scheduler.run_now("Nightly")
    scheduler.remove(" Nightly ")
    assert [SCHTASKS, "/Run", "/TN", "Various Tools\\Nightly"] in runner.calls
    assert [SCHTASKS, "/Delete", "/TN", "Various Tools\\Nightly", "/F"] in runner.calls
    assert runner.tasks == set()


def test_remove_and_run_of_a_missing_task_are_errors() -> None:
    runner = FakeRunner()
    scheduler = make(runner)
    with pytest.raises(ScheduleError, match="no scheduled run"):
        scheduler.remove("Nope")
    with pytest.raises(ScheduleError, match="no scheduled run"):
        scheduler.run_now("Nope")
    assert all(call[1] == "/Query" for call in runner.calls)


def test_a_missing_schtasks_is_a_clear_error() -> None:
    def broken(_args: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("schtasks.exe")

    with pytest.raises(ScheduleError, match="could not be run"):
        TaskScheduler(broken, schtasks="schtasks.exe").list()


# ----- listing -------------------------------------------------------------------------------


def _row(name: str, command: str, **cells: str) -> str:
    row = [""] * 28
    row[0] = "PC"
    row[1] = name
    row[2] = cells.get("next", "11/16/2026 9:00:00 AM")
    row[3] = cells.get("status", "Ready")
    row[5] = cells.get("last", "N/A")
    row[6] = cells.get("result", "0")
    row[8] = command
    row[18] = cells.get("type", "Daily")
    row[19] = cells.get("start", "9:00:00 AM")
    return ",".join('"' + c.replace('"', '""') + '"' for c in row)


def test_list_returns_only_the_various_tools_folder() -> None:
    command = 'py -m various_tools.cli.main empty_folders apply --preset "My P" --yes'
    text = "\n".join(
        [
            _row("\\Microsoft\\Windows\\Defrag\\ScheduledDefrag", "defrag.exe -c"),
            _row("\\Various Tools\\Nightly", command, result="267011"),
            _row("\\Various Tools Evil\\x", command),
            _row("\\various tools\\alpha", command),
            _row("\\OtherTask", "x.exe"),
        ]
    )
    tasks = make(FakeRunner(csv=text)).list()
    assert [t.name for t in tasks] == ["alpha", "Nightly"]
    nightly = tasks[1]
    assert nightly == ScheduledTask(
        name="Nightly",
        command=command,
        next_run="11/16/2026 9:00:00 AM",
        last_run="N/A",
        last_result="267011",
        status="Ready",
        schedule="Daily 9:00:00 AM",
    )
    assert nightly.tool_and_preset == ("empty_folders", "My P")


def test_list_query_command_line_and_failure() -> None:
    runner = FakeRunner()
    make(runner).list()
    assert runner.calls == [[SCHTASKS, "/Query", "/FO", "CSV", "/V", "/NH"]]

    def failing(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "ERROR: boom")

    with pytest.raises(ScheduleError, match="boom"):
        TaskScheduler(failing, schtasks=SCHTASKS).list()


def test_short_or_empty_output_is_ignored() -> None:
    assert sched.parse_query_csv("") == []
    assert sched.parse_query_csv('"a","\\Various Tools\\x"\n') == []


def test_default_schtasks_path_comes_from_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sched.deps, "system_tool", lambda name: f"<{name}>")
    assert TaskScheduler(FakeRunner())._schtasks == "<schtasks>"  # pyright: ignore[reportPrivateUsage]
