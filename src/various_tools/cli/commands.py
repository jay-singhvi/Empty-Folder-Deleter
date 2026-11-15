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
from various_tools.core.journal import UNDOABLE_KINDS, Journal, undo_run
from various_tools.gui.undo_rules import in_progress_message, run_in_progress, stale_after
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
            counts = {run.id: journal.action_count(run.id) for run in runs}  # no rows loaded
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
    """Undo a run: moves and renames are put back, Recycle Bin items are restored while they are
    still there, and unchanged written files are recycled. Permanent deletes cannot be undone."""
    state = get_state(ctx)
    journal = _open_journal(state)
    if journal is None:
        fail("There is no history to undo.")
    with journal:
        run = journal.run(run_id)
        if run is None:
            fail(f"No run with id {run_id}. List runs with: various-tools history")
        actions = journal.actions(run_id)
        limit = stale_after(state.settings().transcode_timeout_s)
        times = [t for t in (run.started, *(a.ts for a in actions)) if t]
        last_write = max(times) if times else None
        if run_in_progress(run.status, run.finished, last_write, limit=limit):
            fail(in_progress_message(run_id, limit))
        if not run.finished or run.status == "running":
            note(
                f"Run {run_id} never finished, and nothing was written to its journal for "
                f"{limit.total_seconds() / 3600:g} hours, so it is treated as stopped."
            )
        done = [a for a in actions if a.status == "done"]
        reversible = [a for a in done if a.kind in UNDOABLE_KINDS]
        console = out()
        console.print(
            f"Run [bold]{run.id}[/bold] ({escape(run.tool)}, {run.status}): "
            f"{len(reversible)} action(s) can be undone, "
            f"{len(done) - len(reversible)} cannot (permanent deletes). Items in the Recycle Bin "
            "are restored only while they are still there."
        )
        if not reversible or run.status == "undone":
            fail("Nothing to undo in this run.")
        if not confirm(f"Undo run {run_id}?", yes=yes):
            note("Nothing was changed.")
            raise typer.Exit(EXIT_FAILED)
        report = undo_run(journal, run_id, protection=state.settings().protected_paths())
        console.print(f"Run {run_id} {report.status}: {report.reverted} action(s) undone.")
        for label, items in (("Skipped", report.skipped), ("Failed", report.failed)):
            if items:
                console.print(f"[bold]{label}[/bold]")
                for item in items:
                    console.print(Text(f"  - {item}"))
        if report.failed or not report.reverted:
            raise typer.Exit(EXIT_FAILED)


_PROGRAMS_PANEL = "External programs"


def doctor(
    ctx: typer.Context,
    exiftool: Annotated[
        str | None,
        typer.Option(
            "--exiftool",
            metavar="PATH",
            help="Check this ExifTool instead (not saved).",
            rich_help_panel=_PROGRAMS_PANEL,
        ),
    ] = None,
    ffmpeg: Annotated[
        str | None,
        typer.Option(
            "--ffmpeg",
            metavar="PATH",
            help="Check this ffmpeg instead (not saved).",
            rich_help_panel=_PROGRAMS_PANEL,
        ),
    ] = None,
    ffprobe: Annotated[
        str | None,
        typer.Option(
            "--ffprobe",
            metavar="PATH",
            help="Check this ffprobe instead (not saved).",
            rich_help_panel=_PROGRAMS_PANEL,
        ),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option(
            "--ollama-url",
            metavar="URL",
            help="Check this Ollama address instead (not saved).",
            rich_help_panel=_PROGRAMS_PANEL,
        ),
    ] = None,
) -> None:
    """Check the external programs (ffmpeg, ffprobe, ExifTool, Ollama) and how each was found."""
    state = get_state(ctx).with_programs(
        exiftool=exiftool, ffmpeg=ffmpeg, ffprobe=ffprobe, ollama_url=ollama_url
    )
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
