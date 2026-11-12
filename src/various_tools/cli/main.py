"""Command-line entry point (`various-tools`). Tool sub-commands are added in Phase 4."""

import typer

from various_tools import __version__

app = typer.Typer(
    name="various-tools",
    help="Various Tools: file and media cleanup, from the command line.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Command group root. Keeps the app in group mode when sub-commands are added."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"various-tools {__version__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
