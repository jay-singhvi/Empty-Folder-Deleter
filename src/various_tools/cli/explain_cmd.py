"""``various-tools duplicates explain A B``: show every check the visual pass makes for two images.

Hand-written (not generated) because it takes two files, not options. It reads the files and
changes nothing. The thresholds default to the tuned values; the flags are for experiments.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.table import Table
from rich.text import Text

from various_tools.cli.state import EXIT_FAILED, fail, out
from various_tools.core.onedrive import CloudOnlyFileError, SyncStatus, get_sync_status
from various_tools.tools.duplicates.explain import explain
from various_tools.tools.duplicates.images import ImageRules


def _cloud_only(path: Path) -> NoReturn:
    fail(
        f"skipped: cloud-only: {path}. It was not opened or downloaded. Make it available on "
        "this device in OneDrive first (Always keep on this device), then try again.",
        EXIT_FAILED,
    )


def explain_command(
    a: Annotated[
        Path,
        typer.Argument(
            metavar="A", help="The first image.", exists=True, dir_okay=False, readable=True
        ),
    ],
    b: Annotated[
        Path,
        typer.Argument(
            metavar="B", help="The second image.", exists=True, dir_okay=False, readable=True
        ),
    ],
    phash_max: Annotated[
        int | None, typer.Option("--phash-max", min=0, help="pHash distance limit to test.")
    ] = None,
    dhash_max: Annotated[
        int | None, typer.Option("--dhash-max", min=0, help="dHash distance limit to test.")
    ] = None,
    aspect_tol: Annotated[
        float | None, typer.Option("--aspect-tol", min=0, help="Aspect-ratio tolerance to test.")
    ] = None,
    pixel_block_max: Annotated[
        float | None, typer.Option("--pixel-block-max", min=0, help="8x8 block difference limit.")
    ] = None,
) -> None:
    """Explain why two images are, or are not, treated as visual duplicates."""
    given = {
        "phash_max": phash_max,
        "dhash_max": dhash_max,
        "aspect_tol": aspect_tol,
        "pixel_block_max": pixel_block_max,
    }
    rules = replace(ImageRules(), **{k: v for k, v in given.items() if v is not None})
    # Rule 3: a cloud-only OneDrive file is never opened, so nothing is downloaded.
    for path in (a, b):
        if get_sync_status(path) is SyncStatus.CLOUD_ONLY:
            _cloud_only(path)
    try:
        report = explain(a, b, rules)
    except CloudOnlyFileError as exc:
        _cloud_only(exc.path)
    except (ValueError, OSError) as exc:
        fail(str(exc), EXIT_FAILED)
    console = out()
    console.print(Text(f"A: {report.a.path} ({report.a.w}x{report.a.h})"))
    console.print(Text(f"B: {report.b.path} ({report.b.w}x{report.b.h})"))
    table = Table(title="Checks", title_justify="left")
    table.add_column("Check")
    table.add_column("Value", overflow="fold")
    table.add_column("Rule")
    table.add_column("Result", no_wrap=True)
    for check in report.checks:
        table.add_row(
            check.name,
            Text(check.value),
            Text(check.rule),
            Text("pass" if check.ok else "FAIL", style="green" if check.ok else "red"),
        )
    console.print(table)
    if report.matched:
        console.print("[bold green]Verdict: visual duplicate[/bold green] (every check passed).")
    else:
        failed = ", ".join(c.name for c in report.checks if not c.ok)
        console.print(f"[bold]Verdict: not a visual duplicate[/bold] (failed: {failed}).")
