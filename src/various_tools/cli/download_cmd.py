"""``various-tools download``: have OneDrive download cloud-only files (AGENTS.md rule 3).

Scans never read a cloud-only file; they list it in the plan (``Plan.cloud_only``) and say
"skipped: cloud-only". This command is the explicit action that downloads such files:

    various-tools download [PATHS...] [--plan FILE] [--wait SECONDS] [--yes]

It always asks first. With no terminal to answer, it refuses unless ``--yes`` is given. Ctrl+C
stops after the current file. Downloading changes nothing else and is not journaled: the files
stay where they are, they just also take space on this device afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from various_tools.cli.runner import run_job
from various_tools.cli.state import EXIT_FAILED, confirm, get_state, note, out, usage_error
from various_tools.core import onedrive
from various_tools.core.context import RunContext
from various_tools.core.plan import Plan

Outcome = tuple[Path, bool, str]


def _paths_from_plan(text: str) -> list[Path]:
    try:
        plan = Plan.from_json(Path(text).read_text(encoding="utf-8"))
    except OSError as exc:
        usage_error(f"cannot read the plan file: {exc.strerror or exc}")
    except ValueError as exc:
        usage_error(f"{text} is not a usable plan: {exc}")
    return list(plan.cloud_only)


def _unique(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def download(
    ctx: typer.Context,
    paths: Annotated[
        list[str] | None,
        typer.Argument(metavar="PATHS", help="Cloud-only files to download."),
    ] = None,
    plan: Annotated[
        str | None,
        typer.Option(
            "--plan",
            metavar="FILE",
            help="Also download the files a saved plan lists as skipped (its 'cloud_only').",
        ),
    ] = None,
    wait: Annotated[
        int | None,
        typer.Option(
            "--wait",
            metavar="SECONDS",
            min=1,
            max=3600,
            help="How long to wait for each file. Default: 30 seconds.",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Do not ask for confirmation (for scripts).")
    ] = False,
) -> None:
    """Download cloud-only OneDrive files that a scan skipped. Asks first."""
    state = get_state(ctx)
    targets = [Path(p) for p in paths or []]
    if plan:
        targets += _paths_from_plan(plan)
    targets = _unique(targets)
    if not targets:
        if plan:
            note("The plan lists no cloud-only files; nothing to download.")
            return
        usage_error("Give the files to download, or --plan FILE with a saved plan.")
    console = out()
    console.print(f"{len(targets)} cloud-only file(s) to download:")
    for path in targets[:20]:
        console.print(f"  {escape(str(path))}")
    if len(targets) > 20:
        console.print(f"  … and {len(targets) - 20} more")
    if not confirm(
        f"Download {len(targets)} file(s) from OneDrive to this device? They take space here "
        "afterwards.",
        yes=yes,
    ):
        note("Nothing was downloaded.")
        raise typer.Exit(EXIT_FAILED)

    def job(run: RunContext) -> list[Outcome]:
        results: list[Outcome] = []
        for done, path in enumerate(targets):
            run.check_cancelled()
            run.report(done, len(targets), path.name)
            if wait is None:
                ok, detail = onedrive.force_download(path)
            else:
                ok, detail = onedrive.force_download(path, wait_seconds=wait)
            results.append((path, ok, detail))
        run.report(len(targets), len(targets), "done")
        return results

    outcome = run_job(state, job, label="Downloading", with_journal=False)
    results = outcome.unwrap()
    table = Table(title="Download", title_justify="left")
    table.add_column("File", overflow="fold")
    table.add_column("Result")
    for path, ok, detail in results:
        table.add_row(escape(str(path)), escape(detail if ok else f"failed: {detail}"))
    console.print(table)
    good = sum(1 for _p, ok, _d in results if ok)
    console.print(f"Downloaded {good} of {len(results)} file(s). Scan again to include them.")
    if good < len(results):
        raise typer.Exit(EXIT_FAILED)
