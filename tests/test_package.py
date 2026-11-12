"""Smoke tests for the Phase 1 skeleton: version, CLI and GUI entry points."""

from importlib.metadata import version

from typer.testing import CliRunner

import various_tools
from various_tools.cli.main import app


def test_version_comes_from_package_metadata() -> None:
    assert various_tools.__version__ == version("various-tools")
    assert various_tools.__version__ != "0.0.0+unknown"


def test_cli_version_command() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"various-tools {various_tools.__version__}"


def test_cli_help_lists_version_command() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.output
