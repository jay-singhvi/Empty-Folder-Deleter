"""``various-tools schedule``: run a saved preset on a schedule through Windows Task Scheduler.

``schedule add NAME --tool T --preset P (--daily HH:MM | --weekly DAYS HH:MM | --monthly DAY
HH:MM)`` creates a per-user task that runs ``various-tools T apply --preset P --scheduled``.
A schedule is a standing "apply", so adding one is guarded (safety rule 1): it refuses presets that
delete permanently, change a 🔒 locked option, or replace, remove or download originals (a field
marked ``replaces_originals`` away from its default), shows exactly what will run and how often,
says that every run is journaled and can be undone, and asks first. ``--yes`` answers that one
question only.

The hidden ``--scheduled`` marker (in place of ``--yes``, which it implies) makes each run check
the preset again with the same rules, and
refuse any permanent delete, so a preset or setting changed after ``schedule add`` cannot make an
unattended run do what ``schedule add`` would have refused. ``schedule run`` refuses a task made
without the marker (by an older version).

The Task Scheduler calls live in ``core/schedule.py``; tests replace :func:`make_scheduler` with a
fake runner, so no real task is ever created.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from various_tools.cli.state import (
    EXIT_FAILED,
    CliState,
    confirm,
    err,
    fail,
    get_state,
    note,
    out,
    usage_error,
)
from various_tools.cli.toolcmds import SCHEDULED_FLAG, unattended_refusal
from various_tools.core import schedule as sched
from various_tools.core.schedule import ScheduleError, TaskScheduler, Trigger
from various_tools.core.schema import describe
from various_tools.core.settings import default_settings_path
from various_tools.tools.base import Tool


def make_scheduler() -> TaskScheduler:
    """The Task Scheduler client. Tests replace this with one that has a fake process runner."""
    return TaskScheduler()


def _check_name(name: str) -> str:
    try:
        return sched.validate_name(name)
    except ScheduleError as exc:
        usage_error(str(exc))


def _check_preset(tools: Mapping[str, Tool], state: CliState, tool_id: str, preset: str) -> Tool:
    """Refuse (exit 1, or 2 for unknown names) unless the preset is safe to run unattended."""
    tool = tools.get(tool_id)
    if tool is None:
        usage_error(f"Unknown tool {tool_id!r}. Tools: {', '.join(tools)}.")
    store = state.presets()
    try:
        values = store.get(tool_id, preset)
    except KeyError:
        known = store.names(tool_id)
        listing = f" Saved presets: {', '.join(known)}." if known else " No presets are saved."
        usage_error(f"No preset {preset!r} for {tool_id}.{listing}")
    try:
        opts = tool.Options.model_validate(values)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'options'}: {e['msg']}" for e in exc.errors()
        )
        fail(f"the preset {preset!r} is not complete enough to run unattended: {problems}")
    refusal = unattended_refusal(opts, describe(tool.Options))
    if refusal is not None:
        fail(f"the preset {preset!r} {refusal}")
    return tool


def _config_for_task(state: CliState) -> Path | None:
    """The settings file to pass to the task, when it is not the default one."""
    chosen = Path(os.path.abspath(state.config_path))
    default = Path(os.path.abspath(default_settings_path()))
    return None if os.path.normcase(chosen) == os.path.normcase(default) else chosen


def _build_trigger(
    daily: str | None, weekly: tuple[str, str] | None, monthly: tuple[str, str] | None
) -> Trigger:
    given = [x for x in (daily, weekly, monthly) if x is not None]
    if len(given) != 1:
        usage_error("Give exactly one of --daily, --weekly or --monthly.")
    try:
        if daily is not None:
            return sched.daily(daily)
        if weekly is not None:
            return sched.weekly(weekly[0], weekly[1])
        assert monthly is not None
        return sched.monthly(monthly[0], monthly[1])
    except ScheduleError as exc:
        usage_error(str(exc))


def _journal_note() -> str:
    return (
        "Every run is journaled and can be undone from History "
        "(various-tools history, various-tools undo RUN_ID, or the History page). "
        "Deleted items go to the Recycle Bin."
    )


def build_schedule_app(tools: Mapping[str, Tool]) -> typer.Typer:
    app = typer.Typer(
        name="schedule",
        help=escape(
            "Run a saved preset on a schedule through Windows Task Scheduler: "
            "schedule add | list | remove | run."
        ),
        no_args_is_help=True,
        add_completion=False,
    )

    @app.command("add")
    def add(
        ctx: typer.Context,
        name: Annotated[str, typer.Argument(metavar="NAME", help="A name for this scheduled run.")],
        tool: Annotated[str, typer.Option("--tool", metavar="TOOL", help="The tool id.")],
        preset: Annotated[
            str, typer.Option("--preset", metavar="NAME", help="The saved preset to apply.")
        ],
        daily: Annotated[
            str | None, typer.Option("--daily", metavar="HH:MM", help="Run every day at this time.")
        ] = None,
        weekly: Annotated[
            tuple[str, str] | None,
            typer.Option(
                "--weekly",
                metavar="DAYS HH:MM",
                help="Run on these weekdays (for example MON,THU) at this time.",
            ),
        ] = None,
        monthly: Annotated[
            tuple[str, str] | None,
            typer.Option(
                "--monthly",
                metavar="DAY HH:MM",
                help="Run on this day of the month (1 to 28) at this time.",
            ),
        ] = None,
        yes: Annotated[
            bool, typer.Option("--yes", help="Do not ask for confirmation (for scripts).")
        ] = False,
    ) -> None:
        """Create a scheduled run of ``TOOL apply --preset PRESET``. Asks first."""
        state = get_state(ctx)
        clean = _check_name(name)
        trigger = _build_trigger(daily, weekly, monthly)
        _check_preset(tools, state, tool, preset)
        try:
            # ``--scheduled`` takes the place of ``--yes`` (it answers the one question too, and
            # keeps the command within Task Scheduler's length limit). It makes 'apply' re-check
            # the preset at run time and refuse permanent deletes, whatever the preset or the
            # settings have become since.
            argv = sched.build_task_argv(tool, preset, config_path=_config_for_task(state))
            argv[-1:] = [SCHEDULED_FLAG]
            command = sched.command_line(argv)
            scheduler = make_scheduler()
            if scheduler.exists(clean):
                raise ScheduleError(
                    f"a scheduled run named {clean!r} already exists. "
                    f"Remove it first with: various-tools schedule remove {clean!r}"
                )
        except ScheduleError as exc:
            fail(str(exc))

        console = out()
        console.print(f"[bold]Scheduled run to create:[/bold] {escape(clean)}")
        console.print(f"  Tool:    {escape(tool)}")
        console.print(f"  Preset:  {escape(preset)}")
        console.print(f"  When:    {escape(trigger.describe())}, only while you are logged on")
        console.print(f"  Runs:    {escape(command)}", soft_wrap=True)
        console.print(
            "This is a standing apply: each run changes files without asking, using the saved "
            "preset as it is at that time."
        )
        console.print(escape(_journal_note()))
        if not confirm("Create this scheduled run?", yes=yes):
            note("Nothing was scheduled.")
            raise typer.Exit(EXIT_FAILED)
        try:
            scheduler.add(clean, trigger, argv)
        except ScheduleError as exc:
            fail(str(exc))
        console.print(f"Scheduled {escape(repr(clean))}: {escape(trigger.describe())}.")
        console.print("Check it with: various-tools schedule list")

    @app.command("list")
    def list_runs() -> None:
        """List the scheduled runs made with this program (the 'Various Tools' task folder)."""
        try:
            found = make_scheduler().list()
        except ScheduleError as exc:
            fail(str(exc))
        console = out()
        if not found:
            console.print(
                "No scheduled runs. Create one with: various-tools schedule add NAME --tool TOOL "
                "--preset PRESET --daily 09:00"
            )
            return
        table = Table(title="Scheduled runs", title_justify="left")
        for column in ("Name", "Tool", "Preset", "Schedule", "Next run", "Last run", "Last result"):
            table.add_column(column, overflow="fold")
        for task in found:
            parsed = task.tool_and_preset
            tool_id, preset = parsed if parsed else ("?", task.command)
            table.add_row(
                Text(task.name),
                Text(tool_id),
                Text(preset),
                Text(task.schedule),
                Text(task.next_run),
                Text(task.last_run),
                Text(task.last_result),
            )
        console.print(table)

    @app.command("remove")
    def remove(
        name: Annotated[str, typer.Argument(metavar="NAME", help="The scheduled run to remove.")],
        yes: Annotated[
            bool, typer.Option("--yes", help="Do not ask for confirmation (for scripts).")
        ] = False,
    ) -> None:
        """Remove a scheduled run. Past runs stay in History."""
        clean = _check_name(name)
        scheduler = make_scheduler()
        try:
            if not scheduler.exists(clean):
                fail(f"There is no scheduled run named {clean!r}.")
        except ScheduleError as exc:
            fail(str(exc))
        if not confirm(f"Remove the scheduled run {clean!r}?", yes=yes):
            note("Nothing was removed.")
            raise typer.Exit(EXIT_FAILED)
        try:
            scheduler.remove(clean)
        except ScheduleError as exc:
            fail(str(exc))
        out().print(f"Removed the scheduled run {escape(repr(clean))}.")

    @app.command("run")
    def run_now(
        ctx: typer.Context,
        name: Annotated[str, typer.Argument(metavar="NAME", help="The scheduled run to start.")],
        yes: Annotated[
            bool, typer.Option("--yes", help="Do not ask for confirmation (for scripts).")
        ] = False,
    ) -> None:
        """Start a scheduled run once, now. It applies the preset and is journaled."""
        clean = _check_name(name)
        scheduler = make_scheduler()
        try:
            task = next(
                (t for t in scheduler.list() if t.name.casefold() == clean.casefold()), None
            )
        except ScheduleError as exc:
            fail(str(exc))
        if task is None:
            fail(f"There is no scheduled run named {clean!r}.")
        parsed = task.tool_and_preset
        if parsed is None:
            fail(
                f"The task {clean!r} does not look like one made by 'schedule add' "
                f"(command: {task.command}), so it is not started from here."
            )
        if SCHEDULED_FLAG not in task.command.split():
            fail(
                f"The task {clean!r} was made by an older version, without the checks a scheduled "
                "run now does at run time, so it is not started. Make it again with: "
                f"various-tools schedule remove {clean!r}, then schedule add."
            )
        # The preset may have changed since the task was made: check it again.
        _check_preset(tools, get_state(ctx), parsed[0], parsed[1])
        err().print(
            f"This applies the preset {escape(repr(parsed[1]))} with {escape(parsed[0])} now. "
            + escape(_journal_note())
        )
        if not confirm(f"Run {clean!r} now?", yes=yes):
            note("Nothing was started.")
            raise typer.Exit(EXIT_FAILED)
        try:
            scheduler.run_now(clean)
        except ScheduleError as exc:
            fail(str(exc))
        out().print(
            f"Started {escape(repr(clean))}. It runs in the background; see "
            "'various-tools history' for the result."
        )

    return app
