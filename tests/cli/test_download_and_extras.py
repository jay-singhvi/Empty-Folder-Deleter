"""``various-tools download`` (cloud-only files, AGENTS.md rule 3) and generated tool extras.

``force_download`` is replaced by a fake in every test, so nothing ever talks to OneDrive. The
extras are tested on a fake tool mounted under the real root callback, so no registry change is
needed and nothing outside ``tmp_path`` is touched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
import typer
from typer.testing import CliRunner

from various_tools.cli import main as cli_main
from various_tools.cli.toolcmds import add_extra_commands, build_tool_app
from various_tools.core import onedrive as onedrive_module
from various_tools.core.context import RunContext
from various_tools.core.plan import Plan, RunResult
from various_tools.tools.base import Category, ToolOptions

if TYPE_CHECKING:
    from conftest import Cli


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int | None]]:
    calls: list[tuple[Path, int | None]] = []

    def fake(path: Path, wait_seconds: int | None = None) -> tuple[bool, str]:
        calls.append((path, wait_seconds))
        if path.name.startswith("bad"):
            return False, "attrib did not finish download within 30s"
        return True, "downloaded"

    monkeypatch.setattr(onedrive_module, "force_download", fake)
    return calls


# ----- download --------------------------------------------------------------------------------


def test_download_refuses_without_a_terminal_and_without_yes(
    cli: Cli, downloads: list[tuple[Path, int | None]]
) -> None:
    result = cli.run("download", str(cli.work / "a.jpg"))
    assert result.exit_code == 1
    assert "not interactive" in result.output
    assert downloads == []


def test_download_declined_downloads_nothing(
    cli: Cli, downloads: list[tuple[Path, int | None]]
) -> None:
    result = cli.run("download", str(cli.work / "a.jpg"), input="n\n")
    assert result.exit_code == 1
    assert "Nothing was downloaded" in result.output
    assert downloads == []


def test_download_with_yes_downloads_each_file_once(
    cli: Cli, downloads: list[tuple[Path, int | None]]
) -> None:
    a, b = cli.work / "a.jpg", cli.work / "b.jpg"
    result = cli.run("download", str(a), str(b), str(a), "--yes")
    assert result.exit_code == 0, result.output
    assert downloads == [(a, None), (b, None)]  # the function's default wait
    assert "Downloaded 2 of 2 file(s)" in result.output


def test_download_from_a_saved_plan_with_a_wait_and_a_failure(
    cli: Cli, downloads: list[tuple[Path, int | None]]
) -> None:
    plan = Plan(tool="organize_by_date", roots=[cli.work], cloud_only=[cli.work / "x.heic"])
    plan.cloud_only.append(cli.work / "bad.mov")
    saved = cli.work / "plan.json"
    saved.write_text(plan.to_json(), encoding="utf-8")
    result = cli.run("download", "--plan", str(saved), "--wait", "5", input="y\n")
    assert result.exit_code == 1  # one file failed
    assert downloads == [(cli.work / "x.heic", 5), (cli.work / "bad.mov", 5)]
    assert "Downloaded 1 of 2 file(s)" in result.output
    assert "failed: attrib did not finish" in result.output


def test_download_needs_files_or_a_plan(cli: Cli, downloads: list[tuple[Path, int | None]]) -> None:
    result = cli.run("download", "--yes")
    assert result.exit_code == 2
    empty = cli.work / "empty.json"
    empty.write_text(Plan(tool="t", roots=[]).to_json(), encoding="utf-8")
    result = cli.run("download", "--plan", str(empty), "--yes")
    assert result.exit_code == 0
    assert "nothing to download" in result.output
    assert downloads == []


# ----- tool extras --------------------------------------------------------------------------------


class NoOptions(ToolOptions):
    pass


@dataclass(frozen=True)
class FakeExtra:
    id: str
    label: str
    description: str
    path_kind: Literal["save_file", "open_file"]
    file_filter: str
    run: Callable[[Path, RunContext], str]


class ExtraTool:
    id = "fake_extras"
    name = "Fake extras"
    description = "A fake tool with extras."
    category: Category = "Organize"
    Options = NoOptions

    def __init__(self, calls: list[tuple[str, Path]]) -> None:
        def export(path: Path, ctx: RunContext) -> str:
            assert isinstance(ctx, RunContext)
            calls.append(("export", path))
            return f"Exported 3 rows to {path.name}"

        def import_(path: Path, ctx: RunContext) -> str:
            calls.append(("import", path))
            raise ValueError(f"{path.name} is not a catalog workbook")

        self.extras = (
            FakeExtra(
                "catalog-export", "Export catalog", "Write it.", "save_file", "*.csv", export
            ),
            FakeExtra(
                "catalog-import", "Import catalog", "Read it.", "open_file", "*.xlsx", import_
            ),
            FakeExtra("scan", "Bad", "Clashes with scan.", "open_file", "*", export),
        )

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        return Plan(tool=self.id, roots=[])

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        raise AssertionError("not used")


def mounted(tool: ExtraTool) -> typer.Typer:
    root = typer.Typer(add_completion=False)
    root.callback()(cli_main._root)  # pyright: ignore[reportPrivateUsage]
    root.add_typer(build_tool_app(tool), name=tool.id)  # type: ignore[arg-type]
    return root


def test_extras_become_subcommands_and_run_with_a_context(cli: Cli) -> None:
    calls: list[tuple[str, Path]] = []
    app = mounted(ExtraTool(calls))
    runner = CliRunner()
    target = cli.work / "catalog.csv"
    result = runner.invoke(
        app, ["--config", str(cli.config), "fake_extras", "catalog-export", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert calls == [("export", target)]
    assert "Exported 3 rows to catalog.csv" in result.output


def test_an_extra_value_error_is_a_plain_failure(cli: Cli) -> None:
    calls: list[tuple[str, Path]] = []
    app = mounted(ExtraTool(calls))
    result = CliRunner().invoke(
        app,
        ["--config", str(cli.config), "fake_extras", "catalog-import", str(cli.work / "x.xlsx")],
    )
    assert result.exit_code == 1
    assert "x.xlsx is not a catalog workbook" in result.output
    assert "Traceback" not in result.output


def test_an_extra_cannot_replace_scan_and_help_lists_the_extras(cli: Cli) -> None:
    calls: list[tuple[str, Path]] = []
    sub = typer.Typer()
    assert add_extra_commands(sub, ExtraTool(calls)) == ["catalog-export", "catalog-import"]  # type: ignore[arg-type]
    app = mounted(ExtraTool(calls))
    result = CliRunner().invoke(app, ["--config", str(cli.config), "fake_extras", "--help"])
    assert result.exit_code == 0
    assert "catalog-export" in result.output and "catalog-import" in result.output


def test_tools_without_extras_get_no_extra_commands() -> None:
    from various_tools.tools.empty_folders import TOOL as EMPTY_FOLDERS

    assert add_extra_commands(typer.Typer(), EMPTY_FOLDERS) == []  # type: ignore[arg-type]
