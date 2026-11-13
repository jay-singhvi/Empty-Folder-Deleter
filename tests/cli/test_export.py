"""``--export`` on scan and apply, and the ``export`` command. All trees are generated."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

from openpyxl import load_workbook

from various_tools.cli.toolcmds import flag_for

if TYPE_CHECKING:
    from tests.cli.conftest import Cli

RENAME_FLAGS = (
    "--match",
    "photo*.txt",
    "--template",
    "pic-{counter:03}{ext}",
    "--date-source",
    "modified",
    "--counter-scope",
    "global",
)


def _photos(cli: Cli) -> Path:
    folder = cli.work / "photos"
    folder.mkdir()
    (folder / "photo1.txt").write_text("one")
    (folder / "photo2.txt").write_text("two")
    return folder


def _csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def test_export_flags_never_clash_with_tool_options() -> None:
    assert flag_for("export") == "--opt-export"
    assert flag_for("export_format") == "--opt-export-format"


def test_scan_export_csv(cli: Cli, empty_tree: Path) -> None:
    target = cli.work / "out" / "plan.csv"
    result = cli.run("empty_folders", "scan", str(empty_tree), "--export", str(target))
    assert result.exit_code == 0, result.output
    assert "Dry run: nothing was changed" in result.output
    rows = _csv(target)
    assert rows[0][:3] == ["Selected", "Kind", "Source"]
    assert len(rows) == 5  # the header and four actions
    assert any(row[2] == str(empty_tree / "c" / "Thumbs.db") for row in rows[1:])
    assert not cli.journal.exists(), "a scan needs no journal"


def test_scan_export_fills_in_sizes(cli: Cli, empty_tree: Path) -> None:
    target = cli.work / "plan.csv"
    cli.run("empty_folders", "scan", str(empty_tree), "--export", str(target))
    sizes = {Path(row[2]).name: row[6] for row in _csv(target)[1:]}
    assert sizes["Thumbs.db"] == "4"
    assert sizes["b"] == ""  # a folder has no size


def test_scan_export_with_json_keeps_stdout_clean(cli: Cli, empty_tree: Path) -> None:
    target = cli.work / "plan.xlsx"
    result = cli.run("empty_folders", "scan", str(empty_tree), "--json", "--export", str(target))
    assert result.exit_code == 0, result.output
    assert result.stdout.lstrip().startswith("{")
    assert load_workbook(target).sheetnames == ["Actions", "Summary"]


def test_scan_export_format_overrides_the_suffix(cli: Cli, empty_tree: Path) -> None:
    target = cli.work / "plan.dat"
    result = cli.run(
        "empty_folders", "scan", str(empty_tree), "--export", str(target), "--export-format", "html"
    )
    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_scan_export_bad_suffix_fails_before_scanning(cli: Cli, empty_tree: Path) -> None:
    target = cli.work / "plan.docx"
    result = cli.run("empty_folders", "scan", str(empty_tree), "--export", str(target))
    assert result.exit_code == 2
    assert "format" in result.output
    assert "Plan for" not in result.output
    assert not target.exists()


def test_scan_export_bad_format_name(cli: Cli, empty_tree: Path) -> None:
    result = cli.run(
        "empty_folders",
        "scan",
        str(empty_tree),
        "--export",
        str(cli.work / "p.csv"),
        "--export-format",
        "pdf",
    )
    assert result.exit_code == 2
    assert "unknown export format" in result.output


def test_export_format_alone_is_a_usage_error(cli: Cli, empty_tree: Path) -> None:
    result = cli.run("empty_folders", "scan", str(empty_tree), "--export-format", "csv")
    assert result.exit_code == 2
    assert "--export" in result.output


def test_scan_export_to_an_unwritable_place(cli: Cli, empty_tree: Path) -> None:
    blocker = cli.work / "blocker"
    blocker.write_text("a file, not a folder")
    result = cli.run("empty_folders", "scan", str(empty_tree), "--export", str(blocker / "p.csv"))
    assert result.exit_code == 1
    assert "cannot write the export file" in result.output


def test_apply_export_writes_the_result(cli: Cli) -> None:
    folder = _photos(cli)
    target = cli.work / "result.csv"
    result = cli.run(
        "rename", "apply", str(folder), *RENAME_FLAGS, "--yes", "--export", str(target)
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in folder.iterdir()) == ["pic-001.txt", "pic-002.txt"]
    rows = _csv(target)
    assert rows[0][:3] == ["Seq", "Kind", "Status"]
    assert [row[2] for row in rows[1:]] == ["done", "done"]
    assert str(folder / "pic-001.txt") in {row[4] for row in rows[1:]}
    assert any(row[5] for row in rows[1:]), "the plan's reason is carried over"


def test_apply_declined_exports_nothing(cli: Cli) -> None:
    folder = _photos(cli)
    target = cli.work / "result.csv"
    result = cli.run(
        "rename", "apply", str(folder), *RENAME_FLAGS, "--export", str(target), input="n\n"
    )
    assert result.exit_code == 1
    assert not target.exists()


def test_apply_with_a_bad_export_name_changes_nothing(cli: Cli) -> None:
    folder = _photos(cli)
    result = cli.run(
        "rename", "apply", str(folder), *RENAME_FLAGS, "--yes", "--export", str(cli.work / "r.doc")
    )
    assert result.exit_code == 2
    assert sorted(p.name for p in folder.iterdir()) == ["photo1.txt", "photo2.txt"]
    assert not cli.journal.exists()


def test_export_command_from_a_saved_plan(cli: Cli, empty_tree: Path) -> None:
    plan = cli.work / "plan.json"
    scan = cli.run("empty_folders", "scan", str(empty_tree), "--save-plan", str(plan))
    assert scan.exit_code == 0, scan.output
    xlsx = cli.work / "plan.xlsx"
    result = cli.run("export", str(xlsx), "--plan", str(plan))
    assert result.exit_code == 0, result.output
    book = load_workbook(xlsx)
    assert book["Actions"].max_row == 5
    summary = {row[0].value: row[1].value for row in book["Summary"].iter_rows()}
    assert summary["Tool id"] == "empty_folders"
    assert summary["Tool"] != "empty_folders", "the tool's display name is used"

    html = cli.work / "plan.html"
    result = cli.run("export", str(html), "--plan", str(plan), "--no-thumbnails")
    assert result.exit_code == 0, result.output
    assert "Thumbs.db" in html.read_text(encoding="utf-8")

    txt = cli.work / "plan.txt"
    assert cli.run("export", str(txt), "--plan", str(plan)).exit_code == 0
    assert "DRY RUN" in txt.read_text(encoding="utf-8")


def test_export_command_from_a_run(cli: Cli) -> None:
    folder = _photos(cli)
    applied = cli.run("rename", "apply", str(folder), *RENAME_FLAGS, "--yes")
    assert applied.exit_code == 0, applied.output
    target = cli.work / "run.csv"
    result = cli.run("export", str(target), "--run", "1")
    assert result.exit_code == 0, result.output
    rows = _csv(target)
    assert rows[0][:3] == ["Seq", "Kind", "Status"]
    assert [row[2] for row in rows[1:]] == ["done", "done"]

    log = cli.work / "log.csv"
    assert cli.run("export", str(log), "--run", "1", "--format", "log").exit_code == 0
    assert _csv(log)[0][0] == "moved_file"


def test_export_run_without_a_journal(cli: Cli) -> None:
    target = cli.work / "run.csv"
    result = cli.run("export", str(target), "--run", "1")
    assert result.exit_code == 1
    assert "no history" in result.output.lower()
    assert not target.exists()


def test_export_unknown_run(cli: Cli) -> None:
    folder = _photos(cli)
    assert cli.run("rename", "apply", str(folder), *RENAME_FLAGS, "--yes").exit_code == 0
    result = cli.run("export", str(cli.work / "run.csv"), "--run", "99")
    assert result.exit_code == 1
    assert "No run with id 99" in result.output


def test_export_needs_exactly_one_source(cli: Cli, empty_tree: Path) -> None:
    target = str(cli.work / "x.csv")
    neither = cli.run("export", target)
    assert neither.exit_code == 2
    assert "exactly one" in neither.output
    plan = cli.work / "plan.json"
    assert (
        cli.run("empty_folders", "scan", str(empty_tree), "--save-plan", str(plan)).exit_code == 0
    )
    both = cli.run("export", target, "--plan", str(plan), "--run", "1")
    assert both.exit_code == 2


def test_export_plan_errors(cli: Cli) -> None:
    target = str(cli.work / "x.csv")
    missing = cli.run("export", target, "--plan", str(cli.work / "nope.json"))
    assert missing.exit_code == 2
    assert "cannot read the plan file" in missing.output
    bad = cli.work / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    broken = cli.run("export", target, "--plan", str(bad))
    assert broken.exit_code == 2
    assert "not a usable plan" in broken.output


def test_export_bad_format_is_a_usage_error(cli: Cli, empty_tree: Path) -> None:
    plan = cli.work / "plan.json"
    assert (
        cli.run("empty_folders", "scan", str(empty_tree), "--save-plan", str(plan)).exit_code == 0
    )
    result = cli.run("export", str(cli.work / "x.csv"), "--plan", str(plan), "--format", "pdf")
    assert result.exit_code == 2
    assert "unknown export format" in result.output
    suffix = cli.run("export", str(cli.work / "x.weird"), "--plan", str(plan))
    assert suffix.exit_code == 2


def test_export_is_listed_in_the_help(cli: Cli) -> None:
    result = cli.run("--help")
    assert result.exit_code == 0
    assert "export" in result.output
