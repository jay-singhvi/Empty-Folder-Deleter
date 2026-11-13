"""The fixed (non-generated) commands: list, history, undo and doctor."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from various_tools import __version__
from various_tools.cli.render import actions_table, doctor_table, runs_table
from various_tools.cli.state import (
    EXIT_FAILED,
    CliState,
    confirm,
    fail,
    get_state,
    note,
    out,
)
from various_tools.core.context import make_deps
from various_tools.core.deps import TOOL_SPECS, ToolLocation
from various_tools.core.journal import REVERSIBLE_KINDS, Journal, undo_run
from various_tools.tools.base import Tool


def version() -> None:
    """Print the installed version."""
    typer.echo(f"various-tools {__version__}")


def make_list_command(tools: Mapping[str, Tool]):
    def list_tools() -> None:
        """List the tools: id, name, category and what each does."""
        table = Table(title="Tools", title_justify="left")
        table.add_column("Id", no_wrap=True)
        table.add_column("Name")
        table.add_column("Category", no_wrap=True)
        table.add_column("Description", overflow="fold", ratio=3)
        for tool in tools.values():
            table.add_row(tool.id, tool.name, tool.category, Text(tool.description))
        out().print(table)
        out().print("Run 'various-tools TOOL scan --help' to see a tool's options.")

    return list_tools


def _open_journal(state: CliState) -> Journal | None:
    """The history journal, or None when there is none yet (nothing is created)."""
    path = state.settings().journal_path()
    return Journal(path) if path.is_file() else None


def history(
    ctx: typer.Context,
    run_id: Annotated[
        int | None, typer.Argument(metavar="[RUN_ID]", help="Show this run's actions.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many runs to list.")] = 20,
) -> None:
    """Show the history of applied runs, or one run's actions."""
    state = get_state(ctx)
    journal = _open_journal(state)
    if journal is None:
        out().print("No history yet: nothing has been applied.")
        return
    with journal:
        console = out()
        if run_id is None:
            runs = journal.runs(limit)
            if not runs:
                console.print("No history yet: nothing has been applied.")
                return
            counts = {run.id: len(journal.actions(run.id)) for run in runs}
            console.print(runs_table(runs, counts))
            console.print("Show a run with: various-tools history RUN_ID")
            return
        run = journal.run(run_id)
        if run is None:
            fail(f"No run with id {run_id}. List runs with: various-tools history")
        console.print(
            f"Run [bold]{run.id}[/bold]: {escape(run.tool)}, {run.status}, started {run.started}"
            + (f", finished {run.finished}" if run.finished else "")
        )
        console.print(Text(f"Options: {run.options_json}"))
        console.print(actions_table(journal.actions(run.id), f"Actions of run {run.id}"))


def undo(
    ctx: typer.Context,
    run_id: Annotated[int, typer.Argument(metavar="RUN_ID", help="The run to undo.")],
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Undo a run: move and rename actions are put back. Deleted items stay in the Recycle Bin."""
    state = get_state(ctx)
    journal = _open_journal(state)
    if journal is None:
        fail("There is no history to undo.")
    with journal:
        run = journal.run(run_id)
        if run is None:
            fail(f"No run with id {run_id}. List runs with: various-tools history")
        done = [a for a in journal.actions(run_id) if a.status == "done"]
        reversible = [a for a in done if a.kind in REVERSIBLE_KINDS]
        console = out()
        console.print(
            f"Run [bold]{run.id}[/bold] ({escape(run.tool)}, {run.status}): "
            f"{len(reversible)} action(s) can be moved back, "
            f"{len(done) - len(reversible)} cannot be undone here (restore deleted items from "
            "the Recycle Bin)."
        )
        if not reversible:
            fail("Nothing to undo in this run.")
        if not confirm(f"Undo run {run_id}?", yes=yes):
            note("Nothing was changed.")
            raise typer.Exit(EXIT_FAILED)
        report = undo_run(journal, run_id)
        console.print(f"Run {run_id} {report.status}: {report.reverted} action(s) moved back.")
        for label, items in (("Skipped", report.skipped), ("Failed", report.failed)):
            if items:
                console.print(f"[bold]{label}[/bold]")
                for item in items:
                    console.print(Text(f"  - {item}"))
        if report.failed or not report.reverted:
            raise typer.Exit(EXIT_FAILED)


def doctor(ctx: typer.Context) -> None:
    """Check the external programs (ffmpeg, ffprobe, ExifTool, Ollama) and how each was found."""
    state = get_state(ctx)
    settings = state.settings()
    deps = make_deps(settings, state.one_off)
    locations: list[ToolLocation] = []
    # Fallback warnings are logged by core/deps.py and also appear in the Problems column.
    deps_log = logging.getLogger("various_tools.core.deps")
    previous_level = deps_log.level
    deps_log.setLevel(logging.CRITICAL)
    try:
        for name in TOOL_SPECS:
            locations.append(
                deps.resolve_ollama(settings.ollama_url) if name == "ollama" else deps.resolve(name)
            )
    finally:
        deps_log.setLevel(previous_level)
    console = out()
    console.print(doctor_table(locations))
    missing = [loc.tool for loc in locations if not loc.valid]
    if missing:
        console.print(
            f"Not available: {', '.join(missing)}. The tools that need them are turned off until "
            "they are installed (see the Install column) or located with "
            "'various-tools config set external_tools.<program>.path PATH'."
        )
    else:
        console.print("Every external program was found.")
