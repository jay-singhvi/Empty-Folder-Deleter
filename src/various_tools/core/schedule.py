"""Scheduled runs: run a saved preset on a schedule through Windows Task Scheduler.

A scheduled task runs ``various-tools <tool> apply --preset <P> --yes``, so every run goes through
the same preview-free apply path as the command line and is journaled (and can be undone from
History) like any other apply.

Everything here is plain logic plus one injected process runner: the functions build the
``schtasks`` command lines and parse its output, and a :class:`TaskScheduler` runs them. Tests
pass a fake runner and look at the command lines; nothing in this module needs a real scheduler.

Tasks are per-user: no ``/RU`` or password is given, so Windows runs them as the current user,
only while that user is logged on, and no administrator rights are needed. They all live in the
``Various Tools\\`` folder of the Task Scheduler library, and this module only ever touches tasks
in that folder.

``schtasks.exe`` is found through :func:`various_tools.core.deps.system_tool` (rule: only
``core/deps.py`` names executables).
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from various_tools.core import deps

TASK_FOLDER = "Various Tools"
MAX_NAME_LENGTH = 64
# Task Scheduler refuses an action command longer than this.
MAX_COMMAND_LENGTH = 261
# From source the CLI is started as ``python -m various_tools.cli.main`` (main.py has a
# ``__main__`` guard). A packaged build starts the console program that sits beside the app.
SOURCE_MODULE = "various_tools.cli.main"
FROZEN_CLI_NAME = "various-tools.exe"

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_FULL_DAYS = {
    "MONDAY": "MON",
    "TUESDAY": "TUE",
    "WEDNESDAY": "WED",
    "THURSDAY": "THU",
    "FRIDAY": "FRI",
    "SATURDAY": "SAT",
    "SUNDAY": "SUN",
}
# Day of the month 1 to 28 only: a later day does not exist in every month, and Task Scheduler
# would quietly skip those months.
MAX_MONTH_DAY = 28

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]*$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class ScheduleError(Exception):
    """A schedule request was invalid, or Task Scheduler refused or could not be reached."""


# ----- validation and triggers ---------------------------------------------------------------


def validate_name(name: str) -> str:
    """The cleaned task name. Letters, digits, spaces and ``. _ ( ) -`` only (ASCII).

    No path separators or quotes are accepted, so a name can never leave the ``Various Tools``
    folder or break the command line.
    """
    cleaned = name.strip()
    if not cleaned:
        raise ScheduleError("a scheduled run needs a name")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ScheduleError(f"the name is too long (at most {MAX_NAME_LENGTH} characters)")
    if not _NAME_RE.fullmatch(cleaned) or cleaned.endswith("."):
        raise ScheduleError(
            "the name may only use letters, digits, spaces and . _ ( ) - "
            "(no slashes or quotes), must start with a letter or digit and must not end in a dot"
        )
    return cleaned


def task_path(name: str) -> str:
    """Full task name in the Task Scheduler library: ``Various Tools\\<name>``."""
    return f"{TASK_FOLDER}\\{validate_name(name)}"


def parse_time(text: str) -> str:
    """``HH:MM`` (24 hour) as zero-padded text."""
    match = _TIME_RE.fullmatch(text.strip())
    if match is None:
        raise ScheduleError(f"{text!r} is not a time. Use 24-hour HH:MM, for example 09:30.")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def parse_days(text: str) -> tuple[str, ...]:
    """Weekdays such as ``mon,thu`` or ``Monday, Friday`` as ``("MON", "THU")``, in week order."""
    found: set[str] = set()
    for raw in re.split(r"[,\s]+", text.strip()):
        if not raw:
            continue
        key = raw.upper()
        day = key if key in WEEKDAYS else _FULL_DAYS.get(key)
        if day is None:
            raise ScheduleError(f"{raw!r} is not a weekday. Use {', '.join(WEEKDAYS)}.")
        found.add(day)
    if not found:
        raise ScheduleError(f"give at least one weekday ({', '.join(WEEKDAYS)})")
    return tuple(d for d in WEEKDAYS if d in found)


@dataclass(frozen=True)
class Trigger:
    """When a task runs: every day, on some weekdays, or on one day of the month."""

    kind: str  # "daily", "weekly" or "monthly"
    time: str  # "HH:MM"
    days: tuple[str, ...] = ()  # weekly
    day_of_month: int | None = None  # monthly

    def describe(self) -> str:
        if self.kind == "daily":
            return f"every day at {self.time}"
        if self.kind == "weekly":
            return f"every {', '.join(self.days)} at {self.time}"
        return f"on day {self.day_of_month} of every month at {self.time}"

    def schtasks_args(self) -> list[str]:
        """The ``/SC``, ``/D`` and ``/ST`` arguments of ``schtasks /Create``."""
        if self.kind == "daily":
            return ["/SC", "DAILY", "/ST", self.time]
        if self.kind == "weekly":
            return ["/SC", "WEEKLY", "/D", ",".join(self.days), "/ST", self.time]
        return ["/SC", "MONTHLY", "/D", str(self.day_of_month), "/ST", self.time]


def daily(time: str) -> Trigger:
    return Trigger("daily", parse_time(time))


def weekly(days: str, time: str) -> Trigger:
    return Trigger("weekly", parse_time(time), days=parse_days(days))


def monthly(day: str | int, time: str) -> Trigger:
    try:
        number = int(str(day).strip())
    except ValueError:
        number = 0
    if not 1 <= number <= MAX_MONTH_DAY:
        raise ScheduleError(f"the day of the month must be 1 to {MAX_MONTH_DAY}, not {day!r}")
    return Trigger("monthly", parse_time(time), day_of_month=number)


# ----- the command a task runs ---------------------------------------------------------------


def cli_program(
    *, frozen: bool | None = None, executable: str | None = None
) -> tuple[list[str], str | None]:
    """The program part of the task command, and a problem (if any) that makes it unusable.

    Packaged build: ``various-tools.exe`` next to the running program. From source: the current
    Python running ``-m various_tools.cli.main``.
    """
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = executable or sys.executable
    if is_frozen:
        cli = Path(exe).with_name(FROZEN_CLI_NAME)
        problem = None if cli.is_file() else f"the command-line program {cli} does not exist"
        return [str(cli)], problem
    return [exe, "-m", SOURCE_MODULE], None


def build_task_argv(
    tool_id: str,
    preset: str,
    *,
    config_path: Path | None = None,
    frozen: bool | None = None,
    executable: str | None = None,
) -> list[str]:
    """The argument list a task runs: the CLI, then ``TOOL apply --preset P --yes``.

    ``config_path`` adds ``--config FILE`` so the task reads the same settings and presets as the
    command that created it (give it only when that is not the default settings file).
    """
    if any(ch in preset for ch in '"%') or any(ord(ch) < 32 for ch in preset):
        raise ScheduleError(
            "a preset used by a scheduled run cannot contain quotes, % or control characters; "
            "save it under another name"
        )
    program, problem = cli_program(frozen=frozen, executable=executable)
    if problem:
        raise ScheduleError(problem)
    argv = list(program)
    if config_path is not None:
        argv += ["--config", str(config_path)]
    argv += [tool_id, "apply", "--preset", preset, "--yes"]
    return argv


def command_line(argv: Sequence[str]) -> str:
    """The argument list as one Windows command line (quoted where it needs quotes)."""
    text = subprocess.list2cmdline(list(argv))
    if len(text) > MAX_COMMAND_LENGTH:
        raise ScheduleError(
            f"the task command is {len(text)} characters long, and Task Scheduler allows "
            f"{MAX_COMMAND_LENGTH}. Use a shorter preset name or install location."
        )
    return text


def parse_task_command(text: str) -> tuple[str, str] | None:
    """``(tool id, preset)`` from a command made by :func:`build_task_argv`, else ``None``."""
    tokens = [quoted or bare for quoted, bare in _TOKEN_RE.findall(text)]
    try:
        at = tokens.index("apply")
        if tokens[tokens.index("--preset", at) + 2] != "--yes" or at == 0:
            return None
        return tokens[at - 1], tokens[tokens.index("--preset", at) + 1]
    except ValueError, IndexError:
        return None


# ----- Task Scheduler ------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduledTask:
    """One task of the ``Various Tools`` folder, as ``schtasks /Query`` reports it."""

    name: str  # without the folder
    command: str
    next_run: str
    last_run: str
    last_result: str
    status: str
    schedule: str

    @property
    def tool_and_preset(self) -> tuple[str, str] | None:
        return parse_task_command(self.command)


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return deps.run_tool(args, timeout=60)


# /Query /FO CSV /V /NH columns (positions are the same in every language; the headers are not).
_COL_NAME, _COL_NEXT, _COL_STATUS, _COL_LAST, _COL_RESULT, _COL_COMMAND = 1, 2, 3, 5, 6, 8
_COL_SCHEDULE_TYPE, _COL_START_TIME = 18, 19


def parse_query_csv(text: str) -> list[ScheduledTask]:
    """Tasks of the ``Various Tools`` folder from ``schtasks /Query /FO CSV /V /NH`` output."""
    prefix = f"\\{TASK_FOLDER}\\".casefold()
    tasks: list[ScheduledTask] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) <= _COL_COMMAND or not row[_COL_NAME].casefold().startswith(prefix):
            continue

        def cell(index: int, row: list[str] = row) -> str:
            return row[index].strip() if index < len(row) else ""

        schedule = " ".join(p for p in (cell(_COL_SCHEDULE_TYPE), cell(_COL_START_TIME)) if p)
        tasks.append(
            ScheduledTask(
                name=row[_COL_NAME][len(prefix) :],
                command=cell(_COL_COMMAND),
                next_run=cell(_COL_NEXT),
                last_run=cell(_COL_LAST),
                last_result=cell(_COL_RESULT),
                status=cell(_COL_STATUS),
                schedule=schedule,
            )
        )
    return sorted(tasks, key=lambda t: t.name.casefold())


class TaskScheduler:
    """Add, list, remove and run tasks in the ``Various Tools`` folder through ``schtasks``."""

    def __init__(self, runner: Runner | None = None, schtasks: str | None = None) -> None:
        self._runner: Runner = runner or _default_runner
        self._schtasks = schtasks or deps.system_tool("schtasks")

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner([self._schtasks, *args])
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScheduleError(
                f"Windows Task Scheduler (schtasks) could not be run: {exc}"
            ) from None

    @staticmethod
    def _problem(done: subprocess.CompletedProcess[str]) -> str:
        message = (done.stderr or done.stdout or "").strip()
        return message or f"schtasks exited with code {done.returncode}"

    def create_args(self, name: str, trigger: Trigger, command: str) -> list[str]:
        """The ``schtasks /Create`` arguments (no ``/F``: an existing task is never replaced)."""
        return [
            "/Create",
            "/TN",
            task_path(name),
            "/TR",
            command,
            *trigger.schtasks_args(),
            "/RL",
            "LIMITED",
        ]

    def exists(self, name: str) -> bool:
        return self._run("/Query", "/TN", task_path(name)).returncode == 0

    def add(self, name: str, trigger: Trigger, argv: Sequence[str]) -> None:
        """Create the task. Raises :class:`ScheduleError` when the name is taken or it fails."""
        command = command_line(argv)
        if self.exists(name):
            raise ScheduleError(
                f"a scheduled run named {name.strip()!r} already exists. Remove it first with: "
                f"various-tools schedule remove {name.strip()!r}"
            )
        done = self._run(*self.create_args(name, trigger, command))
        if done.returncode != 0:
            raise ScheduleError(f"Task Scheduler refused the task: {self._problem(done)}")

    def list(self) -> list[ScheduledTask]:
        done = self._run("/Query", "/FO", "CSV", "/V", "/NH")
        if done.returncode != 0:
            raise ScheduleError(f"could not list the tasks: {self._problem(done)}")
        return parse_query_csv(done.stdout)

    def remove(self, name: str) -> None:
        if not self.exists(name):
            raise ScheduleError(f"there is no scheduled run named {name.strip()!r}")
        done = self._run("/Delete", "/TN", task_path(name), "/F")
        if done.returncode != 0:
            raise ScheduleError(f"could not remove the task: {self._problem(done)}")

    def run_now(self, name: str) -> None:
        """Start the task once now (it runs in the background; its result is journaled)."""
        if not self.exists(name):
            raise ScheduleError(f"there is no scheduled run named {name.strip()!r}")
        done = self._run("/Run", "/TN", task_path(name))
        if done.returncode != 0:
            raise ScheduleError(f"could not start the task: {self._problem(done)}")
