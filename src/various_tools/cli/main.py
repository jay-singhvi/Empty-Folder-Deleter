"""Command-line entry point (``various-tools``).

The tool commands are generated from each tool's options model (``toolcmds.py``), so adding a tool
adds its commands. Everything else is a fixed command: ``list``, ``history``, ``undo``, ``doctor``,
``config``, ``presets``, ``version`` and ``duplicates explain``.

Global options go before the command: ``various-tools --config FILE --exiftool PATH ... <command>``.
They apply to that run only, and nothing given on the command line is saved to the settings.

Exit codes: 0 done, 1 failure, refusal, partial apply or declined confirmation, 2 usage or
validation error, 130 cancelled with Ctrl+C.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from various_tools.cli.commands import doctor, history, make_list_command, undo, version
from various_tools.cli.config_cmd import config_app
from various_tools.cli.explain_cmd import explain_command
from various_tools.cli.presets_cmd import build_presets_app
from various_tools.cli.state import CONFIG_ENV_VAR, CliState
from various_tools.cli.toolcmds import build_tool_app
from various_tools.core.settings import default_settings_path
from various_tools.tools.base import discover


def _root(
    ctx: typer.Context,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            metavar="FILE",
            envvar=CONFIG_ENV_VAR,
            help="Settings file (TOML) to use instead of the default one. "
            f"Also read from the {CONFIG_ENV_VAR} environment variable.",
            rich_help_panel="Global options",
        ),
    ] = None,
    exiftool: Annotated[
        str | None,
        typer.Option(
            "--exiftool",
            metavar="PATH",
            help="Use this ExifTool for this run only (not saved).",
            rich_help_panel="Global options",
        ),
    ] = None,
    ffmpeg: Annotated[
        str | None,
        typer.Option(
            "--ffmpeg",
            metavar="PATH",
            help="Use this ffmpeg for this run only (not saved).",
            rich_help_panel="Global options",
        ),
    ] = None,
    ffprobe: Annotated[
        str | None,
        typer.Option(
            "--ffprobe",
            metavar="PATH",
            help="Use this ffprobe for this run only (not saved).",
            rich_help_panel="Global options",
        ),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option(
            "--ollama-url",
            metavar="URL",
            help="Use this Ollama address for this run only (not saved).",
            rich_help_panel="Global options",
        ),
    ] = None,
) -> None:
    """Command group root. Collects the global options; loads nothing yet."""
    ctx.obj = CliState(
        config_path=Path(config) if config else default_settings_path(),
        one_off={"exiftool": exiftool, "ffmpeg": ffmpeg, "ffprobe": ffprobe, "ollama": ollama_url},
        ollama_url=ollama_url,
    )


def build_app() -> typer.Typer:
    tools = discover()
    app = typer.Typer(
        name="various-tools",
        help=(
            "Various Tools: clean up, deduplicate and organize files and media.\n\n"
            "Every tool previews first: 'TOOL scan' changes nothing, and only 'TOOL apply' does. "
            "Global options (--config, --exiftool, ...) go before the command."
        ),
        no_args_is_help=True,
        add_completion=False,
        pretty_exceptions_show_locals=False,  # a crash must not print local variables
    )
    app.callback()(_root)
    app.command("list")(make_list_command(tools))
    app.command("history")(history)
    app.command("undo")(undo)
    app.command("doctor")(doctor)
    app.add_typer(config_app, name="config")
    app.add_typer(build_presets_app(tools), name="presets")
    app.command("version")(version)
    for tool in tools.values():
        tool_app = build_tool_app(tool)
        if tool.id == "duplicates":
            tool_app.command("explain")(explain_command)
        app.add_typer(tool_app, name=tool.id, help=tool.description, rich_help_panel=tool.category)
    return app


app = build_app()


def _use_robust_streams() -> None:
    """Never crash on a character the console encoding lacks (for example when piped)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _use_robust_streams()
    app()


if __name__ == "__main__":
    main()
