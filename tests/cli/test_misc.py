"""list, version, doctor, presets, duplicates explain and the generated --help."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from various_tools.cli import toolcmds
from various_tools.core.presets import PresetStore
from various_tools.core.schema import describe
from various_tools.tools.base import discover

if TYPE_CHECKING:
    from tests.cli.conftest import Cli

TOOLS = discover()


def test_list_shows_every_tool(cli: Cli) -> None:
    result = cli.run("list")
    assert result.exit_code == 0, result.output
    for tool in TOOLS.values():
        assert tool.id in result.output
        assert tool.name in result.output
        assert tool.category in result.output


def test_version(cli: Cli) -> None:
    result = cli.run("version")
    assert result.exit_code == 0
    assert result.stdout.startswith("various-tools ")


def test_root_help_lists_commands_and_tools(cli: Cli) -> None:
    result = cli.run("--help", config=False)
    assert result.exit_code == 0, result.output
    for word in (
        "list",
        "history",
        "undo",
        "doctor",
        "config",
        "presets",
        "--exiftool",
        "--ffmpeg",
    ):
        assert word in result.output
    for tool_id in TOOLS:
        assert tool_id in result.output


def _fast_globals(fake_exe: Callable[..., Path], folder: Path) -> list[str]:
    """One-off flags that point every program at a fake, so doctor never runs a real one."""
    flags = ["--ollama-url", "http://127.0.0.1:9"]
    for program in ("ffmpeg", "ffprobe"):
        flags += [f"--{program}", str(fake_exe(folder, program, f"{program} 1.0"))]
    return flags


def test_doctor_with_a_one_off_exiftool(
    cli: Cli, fake_exe: Callable[..., Path], tmp_path: Path
) -> None:
    exe = fake_exe(tmp_path / "bin", "exiftool", "12.34")
    result = cli.run(*_fast_globals(fake_exe, tmp_path / "bin"), "--exiftool", str(exe), "doctor")
    assert result.exit_code == 0, result.output
    for program in ("ffmpeg", "ffprobe", "exiftool", "ollama"):
        assert program in result.output
    exif_line = next(line for line in result.output.splitlines() if "exiftool" in line)
    assert "found" in exif_line
    assert "12.34" in exif_line
    assert "override" in exif_line
    assert "Status" in result.output
    assert "Found by" in result.output


def test_doctor_reports_a_program_that_no_longer_works(
    cli: Cli, fake_exe: Callable[..., Path], tmp_path: Path
) -> None:
    ghost = tmp_path / "ghost" / "exiftool.exe"
    flags = _fast_globals(fake_exe, tmp_path / "bin")
    result = cli.run(*flags, "--exiftool", str(ghost), "doctor")
    assert result.exit_code == 0, result.output
    exif_row = next(line for line in result.output.splitlines() if "exiftool" in line)
    assert "no longer works" in exif_row  # whether or not another copy was found
    ollama_row = next(line for line in result.output.splitlines() if "ollama" in line)
    assert "invalid" in ollama_row or "missing" in ollama_row


def test_doctor_uses_saved_paths(cli: Cli, fake_exe: Callable[..., Path], tmp_path: Path) -> None:
    exe = fake_exe(tmp_path / "saved", "ffprobe", "ffprobe version 9.9")
    cli.run("config", "set", "external_tools.ffprobe.path", str(exe))
    flags = ["--ollama-url", "http://127.0.0.1:9"]
    for program in ("ffmpeg", "exiftool"):
        flags += [f"--{program}", str(fake_exe(tmp_path / "bin", program, f"{program} 1.0"))]
    result = cli.run(*flags, "doctor")
    row = next(line for line in result.output.splitlines() if "ffprobe" in line)
    assert "found" in row
    assert "9.9" in row


def test_presets_list_save_delete(cli: Cli) -> None:
    assert "No presets are saved" in cli.run("presets", "list").output

    saved = cli.run(
        "presets", "save", "empty_folders", "  Junk cleaner ", "--treat-as-empty", "x.tmp,y.tmp"
    )
    assert saved.exit_code == 0, saved.output
    assert "Junk cleaner" in saved.output
    store = PresetStore(cli.presets)
    assert store.names("empty_folders") == ["Junk cleaner"]
    assert store.get("empty_folders", "Junk cleaner") == {"treat_as_empty": ["x.tmp", "y.tmp"]}

    listing = cli.run("presets", "list")
    assert "Junk cleaner" in listing.output
    assert "x.tmp" in listing.output
    assert "Junk cleaner" in cli.run("presets", "list", "empty_folders").output
    assert "No presets are saved" in cli.run("presets", "list", "rename").output

    deleted = cli.run("presets", "delete", "empty_folders", "Junk cleaner")
    assert deleted.exit_code == 0, deleted.output
    assert store.names("empty_folders") == []
    again = cli.run("presets", "delete", "empty_folders", "Junk cleaner")
    assert again.exit_code == 1
    assert "No preset" in again.output


def test_presets_save_validates_and_checks_the_tool(cli: Cli) -> None:
    bad = cli.run("presets", "save", "empty_folders", "bad", "--min-age-days", "-1")
    assert bad.exit_code == 2
    assert not cli.presets.exists()
    assert cli.run("presets", "list", "no_such_tool").exit_code == 2
    assert cli.run("presets", "delete", "no_such_tool", "x").exit_code == 2
    assert cli.run("presets", "save", "empty_folders", "  ").exit_code == 2


def test_presets_save_can_start_from_another_preset(cli: Cli) -> None:
    cli.run("presets", "save", "empty_folders", "base", "--min-age-days", "7")
    derived = cli.run(
        "presets", "save", "empty_folders", "derived", "--preset", "base", "--no-skip-hidden-system"
    )
    assert derived.exit_code == 0, derived.output
    assert PresetStore(cli.presets).get("empty_folders", "derived") == {
        "min_age_days": 7,
        "skip_hidden_system": False,
    }


def test_presets_save_may_leave_a_required_option_open(cli: Cli) -> None:
    result = cli.run("presets", "save", "project_export", "md export", "--output-format", "md")
    assert result.exit_code == 0, result.output
    assert "required" in result.output
    assert PresetStore(cli.presets).get("project_export", "md export") == {"output_format": "md"}
    project = cli.work / "proj"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n")
    used = cli.run("project_export", "scan", str(project), "--preset", "md export", "--json")
    assert used.exit_code == 0, used.output
    assert json.loads(used.stdout)["options"]["output_format"] == "md"


def test_presets_save_includes_given_folders(cli: Cli) -> None:
    folder = str(cli.work / "photos")
    result = cli.run("presets", "save", "empty_folders", "photos", folder)
    assert result.exit_code == 0, result.output
    assert PresetStore(cli.presets).get("empty_folders", "photos") == {"roots": [folder]}


def test_explain_two_generated_images(cli: Cli, make_image: Callable[..., Path]) -> None:
    first = make_image("first.png", seed=1)
    same = make_image("same.png", seed=1)
    other = make_image("other.png", seed=2)

    match = cli.run("duplicates", "explain", str(first), str(same))
    assert match.exit_code == 0, match.output
    assert "pHash distance" in match.output
    assert "aspect ratio diff" in match.output
    assert "Verdict: visual duplicate" in match.output

    different = cli.run("duplicates", "explain", str(first), str(other))
    assert different.exit_code == 0, different.output
    assert "Verdict: not a visual duplicate" in different.output
    assert "FAIL" in different.output

    # thresholds can be loosened for an experiment; the checks table shows the new rule
    loose = cli.run("duplicates", "explain", str(first), str(other), "--phash-max", "64")
    assert "<= 64" in loose.output


def test_explain_rejects_missing_and_unreadable_files(
    cli: Cli, make_image: Callable[..., Path]
) -> None:
    good = make_image("good.png")
    assert cli.run("duplicates", "explain", str(good), str(cli.work / "none.png")).exit_code == 2
    text = cli.work / "not-an-image.png"
    text.write_text("plain text", encoding="utf-8")
    result = cli.run("duplicates", "explain", str(good), str(text))
    assert result.exit_code == 1
    assert "error:" in result.output


@pytest.mark.parametrize("tool_id", sorted(TOOLS))
def test_help_shows_titles_descriptions_and_markers(cli: Cli, tool_id: str) -> None:
    tool = TOOLS[tool_id]
    for command in ("scan", "apply"):
        result = cli.run(tool_id, command, "--help")
        assert result.exit_code == 0, result.output
        text = " ".join(result.output.split())
        for flag in toolcmds.make_flags(describe(tool.Options)):
            assert flag.flag in text
        for spec in describe(tool.Options):
            assert spec.title.rstrip(".") in text
            if spec.description:
                assert " ".join(spec.description.split())[:30] in text
            if spec.locked and "\U0001f512" not in spec.description:
                assert "\U0001f512" in text
        assert "--preset" in text
    assert "--json" in " ".join(cli.run(tool_id, "scan", "--help").output.split())
    assert "--yes" in " ".join(cli.run(tool_id, "apply", "--help").output.split())


def test_help_groups_advanced_options_in_their_own_panel(cli: Cli) -> None:
    result = cli.run("empty_folders", "scan", "--help")
    assert "Advanced" in result.output
    advanced = [s for s in describe(TOOLS["empty_folders"].Options) if s.advanced]
    assert advanced
    after = result.output.split("Advanced", 1)[1]
    assert toolcmds.flag_for(advanced[0].name) in after


def test_help_marks_defaults_and_bounds(cli: Cli) -> None:
    text = " ".join(cli.run("empty_folders", "scan", "--help").output.split())
    assert "Allowed: 0 to" in text  # min_age_days has bounds
    assert "Default: 0" in text


def test_reserved_names_get_an_opt_prefix() -> None:
    for name in ("preset", "json", "save_plan", "plan", "yes", "config", "ollama_url", "help"):
        assert toolcmds.flag_for(name) == f"--opt-{name.replace('_', '-')}"
    assert toolcmds.flag_for("max_width") == "--max-width"
