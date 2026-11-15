"""``various-tools export`` and the helpers behind ``<tool> scan|apply --export``.

``export OUTPUT --plan FILE`` writes a saved plan, ``export OUTPUT --run ID`` writes a journaled
run. The format comes from the file suffix, or from ``--format``. The same writers back the
``--export`` flag of every generated ``scan`` and ``apply`` command (see ``core/report.py``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer

from various_tools.cli.state import CliState, fail, get_state, note, usage_error
from various_tools.core import report
from various_tools.core.journal import Journal
from various_tools.core.plan import Plan, RunResult
from various_tools.tools.base import Tool


def check_export_target(target: str | None, fmt: str | None, *, force: bool = False) -> None:
    """Fail early, before any scan or apply runs: exit 2 on a bad ``--export`` name or format,
    exit 1 when the file already exists and ``--force`` was not given (nothing is overwritten
    without asking for it)."""
    if target is None:
        if fmt:
            usage_error("--export-format needs --export FILE.")
        return
    try:
        report.format_for(Path(target), fmt)
    except ValueError as exc:
        usage_error(str(exc))
    if not force and os.path.lexists(target):
        fail(f"{target} already exists. Add --force to overwrite it, or choose another name.")


def _write_failed(exc: OSError) -> None:
    fail(f"cannot write the export file: {exc.strerror or exc}")


def export_plan_file(plan: Plan, target: str, fmt: str | None, tool_name: str | None) -> None:
    """Write ``plan`` for ``scan --export``. Sizes are read from file metadata only."""
    try:
        written = report.export_plan(
            plan,
            Path(target),
            fmt,
            sizes=report.action_sizes(plan),
            tool_name=tool_name,
        )
    except OSError as exc:
        _write_failed(exc)
    note(f"Exported the plan to {written}")


def export_result_file(
    plan: Plan, result: RunResult, target: str, fmt: str | None, tool_name: str | None
) -> None:
    """Write the outcome of an apply for ``apply --export``."""
    try:
        written = report.export_result(plan, result, Path(target), fmt, tool_name=tool_name)
    except OSError as exc:
        _write_failed(exc)
    note(f"Exported the result of run {result.run_id} to {written}")


def _open_journal(state: CliState) -> Journal | None:
    path = state.settings().journal_path()
    return Journal(path) if path.is_file() else None


def make_export_command(tools: Mapping[str, Tool]):
    def export(
        ctx: typer.Context,
        output: Annotated[
            str,
            typer.Argument(metavar="OUTPUT", help="The file to write (.csv, .xlsx, .html, .txt)."),
        ],
        plan_file: Annotated[
            str | None,
            typer.Option(
                "--plan", metavar="FILE", help="Export a plan saved by 'scan --save-plan'."
            ),
        ] = None,
        run_id: Annotated[
            int | None,
            typer.Option("--run", metavar="RUN_ID", help="Export this run from the history."),
        ] = None,
        fmt: Annotated[
            str | None,
            typer.Option(
                "--format",
                metavar="FMT",
                help=f"One of: {', '.join(report.FORMATS)}. Default: from the file suffix. "
                "'log' is the old duplicate-finder CSV log, 'txt' the old review list.",
            ),
        ] = None,
        no_thumbnails: Annotated[
            bool,
            typer.Option("--no-thumbnails", help="Leave the previews out of the HTML report."),
        ] = False,
        force: Annotated[
            bool, typer.Option("--force", help="Overwrite OUTPUT if it already exists.")
        ] = False,
    ) -> None:
        """Export a saved plan or a run from the history to CSV, Excel, HTML or text."""
        if (plan_file is None) == (run_id is None):
            usage_error("Give exactly one of --plan FILE or --run RUN_ID.")
        check_export_target(output, fmt, force=force)
        target = Path(output)
        try:
            if plan_file is not None:
                written = _export_plan(plan_file, target, fmt, tools, thumbnails=not no_thumbnails)
            else:
                assert run_id is not None
                written = _export_run(get_state(ctx), run_id, target, fmt, tools)
        except OSError as exc:
            _write_failed(exc)
        note(f"Exported to {written}")

    return export


def _export_plan(
    plan_file: str, target: Path, fmt: str | None, tools: Mapping[str, Tool], *, thumbnails: bool
) -> Path:
    try:
        plan = Plan.from_json(Path(plan_file).read_text(encoding="utf-8"))
    except OSError as exc:
        usage_error(f"cannot read the plan file: {exc.strerror or exc}")
    except ValueError as exc:
        usage_error(f"{plan_file} is not a usable plan: {exc}")
    tool = tools.get(plan.tool)
    return report.export_plan(
        plan,
        target,
        fmt,
        sizes=report.action_sizes(plan),
        tool_name=tool.name if tool else None,
        thumbnails=thumbnails,
    )


def _export_run(
    state: CliState, run_id: int, target: Path, fmt: str | None, tools: Mapping[str, Tool]
) -> Path:
    journal = _open_journal(state)
    if journal is None:
        fail("There is no history to export from.")
    with journal:
        run = journal.run(run_id)
        if run is None:
            fail(f"No run with id {run_id}. List runs with: various-tools history")
        tool = tools.get(run.tool)
        return report.export_run(
            run, journal.actions(run_id), target, fmt, tool_name=tool.name if tool else None
        )
