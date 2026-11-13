"""``various-tools presets``: list, save and delete saved option presets.

``presets save TOOL NAME [options]`` is generated per tool in ``toolcmds.py``; this module adds
the app, ``list`` and ``delete``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from various_tools.cli.state import EXIT_FAILED, fail, get_state, out, usage_error
from various_tools.cli.toolcmds import build_preset_save_app
from various_tools.tools.base import Tool


def build_presets_app(tools: Mapping[str, Tool]) -> typer.Typer:
    app = typer.Typer(
        name="presets",
        help=escape(
            "Saved option presets: presets list | save TOOL NAME [options] | delete TOOL NAME."
        ),
        no_args_is_help=True,
        add_completion=False,
    )

    def check_tool(tool_id: str) -> None:
        if tool_id not in tools:
            usage_error(f"Unknown tool {tool_id!r}. Tools: {', '.join(tools)}.")

    @app.command("list")
    def list_presets(
        ctx: typer.Context,
        tool: Annotated[
            str | None, typer.Argument(metavar="[TOOL]", help="Only this tool's presets.")
        ] = None,
    ) -> None:
        """List saved presets, optionally for one tool."""
        if tool is not None:
            check_tool(tool)
        store = get_state(ctx).presets()
        everything = store.all()
        shown = {t: names for t, names in everything.items() if tool is None or t == tool}
        console = out()
        if not shown:
            console.print(
                "No presets are saved. Create one with: various-tools presets save TOOL NAME"
            )
            return
        table = Table(title="Presets", title_justify="left")
        table.add_column("Tool", no_wrap=True)
        table.add_column("Preset", overflow="fold")
        table.add_column("Options", overflow="fold", ratio=3)
        for tool_id, names in shown.items():
            for name in names:
                options = store.get(tool_id, name)
                summary = ", ".join(f"{k}={v}" for k, v in options.items()) or "(all defaults)"
                table.add_row(tool_id, Text(name), Text(summary))
        console.print(table)

    @app.command("delete")
    def delete_preset(
        ctx: typer.Context,
        tool: Annotated[str, typer.Argument(metavar="TOOL", help="The tool id.")],
        name: Annotated[str, typer.Argument(metavar="NAME", help="The preset's name.")],
    ) -> None:
        """Delete a saved preset."""
        check_tool(tool)
        if get_state(ctx).presets().delete(tool, name):
            out().print(f"Deleted preset {escape(repr(name.strip()))} for {tool}.")
            return
        fail(f"No preset {name.strip()!r} for {tool}.", EXIT_FAILED)

    app.add_typer(build_preset_save_app(tools), name="save")
    return app
