"""``config show | get | set | unset`` and the one-off global options."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from various_tools.core.settings import Settings, load_settings

if TYPE_CHECKING:
    from tests.cli.conftest import Cli


def _saved(cli: Cli) -> dict[str, object]:
    with cli.config.open("rb") as handle:
        return tomllib.load(handle)


def test_show_lists_every_setting_with_defaults_and_lock(cli: Cli) -> None:
    result = cli.run("config", "show")
    assert result.exit_code == 0, result.output
    for name in Settings.model_fields:
        assert name in result.output
    assert "Key" in result.output
    assert "Default" in result.output
    assert "\U0001f512" in result.output  # locked safety settings carry the lock
    assert str(cli.config) in result.output


def test_get_set_unset_a_number(cli: Cli) -> None:
    assert cli.run("config", "set", "workers", "3").exit_code == 0
    assert _saved(cli)["workers"] == 3
    got = cli.run("config", "get", "workers")
    assert got.exit_code == 0
    assert got.stdout.strip() == "3"
    assert load_settings(cli.config).workers == 3

    unset = cli.run("config", "unset", "workers")
    assert unset.exit_code == 0, unset.output
    assert "workers" not in _saved(cli)
    assert cli.run("config", "get", "workers").stdout.strip() == str(Settings().workers)
    assert "already at its default" in cli.run("config", "unset", "workers").output


def test_set_keeps_the_data_folder_and_other_values(cli: Cli) -> None:
    cli.run("config", "set", "ollama_url", "http://example.test:1234")
    saved = _saved(cli)
    assert saved["ollama_url"] == "http://example.test:1234"
    assert saved["data_folder"] == str(cli.state)


def test_set_a_list_and_a_bool(cli: Cli) -> None:
    assert cli.run("config", "set", "protect_user_profile", "yes", "--yes").exit_code == 0
    assert cli.run("config", "set", "allow_protected_paths", "false").exit_code == 0
    result = cli.run("config", "set", "protected_folders", '["C:\\\\Windows"]', "--yes")
    assert result.exit_code == 0, result.output
    assert load_settings(cli.config).protected_folders == ["C:\\Windows"]


def test_external_program_paths(cli: Cli, fake_exe: Callable[..., Path], tmp_path: Path) -> None:
    exe = fake_exe(tmp_path / "bin", "exiftool", "12.34")
    empty = cli.run("config", "get", "external_tools.exiftool.path")
    assert empty.exit_code == 1
    assert "not set" in empty.output

    for key in ("external_tools.exiftool.path", "external_tools.exiftool"):
        cli.run("config", "unset", key)
        result = cli.run("config", "set", key, str(exe))
        assert result.exit_code == 0, result.output
        assert load_settings(cli.config).external_tools == {"exiftool": str(exe)}
        got = cli.run("config", "get", "external_tools.exiftool.path")
        assert got.stdout.strip() == str(exe)

    # the table form in TOML, as the plan §3.5 describes
    assert _saved(cli)["external_tools"] == {"exiftool": str(exe)}
    unset = cli.run("config", "unset", "external_tools.exiftool.path")
    assert unset.exit_code == 0, unset.output
    assert "external_tools" not in _saved(cli)


def test_a_locked_setting_asks_first(cli: Cli) -> None:
    declined = cli.run("config", "set", "delete_mode", "permanent", input="n\n")
    assert declined.exit_code == 1
    assert "locked" in declined.output
    assert load_settings(cli.config).delete_mode == "trash"
    refused = cli.run("config", "set", "delete_mode", "permanent")  # nobody to answer
    assert refused.exit_code == 1
    assert load_settings(cli.config).delete_mode == "trash"

    assert cli.run("config", "set", "delete_mode", "permanent", input="y\n").exit_code == 0
    assert load_settings(cli.config).delete_mode == "permanent"
    # going back to the safe default needs no question
    back = cli.run("config", "unset", "delete_mode")
    assert back.exit_code == 0
    assert load_settings(cli.config).delete_mode == "trash"


def test_unknown_key_suggests_the_closest(cli: Cli) -> None:
    result = cli.run("config", "set", "wrokers", "3")
    assert result.exit_code == 2
    assert "Unknown setting 'wrokers'" in result.output
    assert "workers" in result.output
    assert cli.run("config", "get", "nope").exit_code == 2
    assert cli.run("config", "unset", "nope").exit_code == 2
    program = cli.run("config", "set", "external_tools.photoshop.path", "x")
    assert program.exit_code == 2
    assert "exiftool" in program.output


def test_invalid_values_are_rejected_and_not_saved(cli: Cli) -> None:
    before = cli.config.read_text(encoding="utf-8")
    for key, value in (
        ("workers", "0"),
        ("workers", "many"),
        ("delete_mode", "shred"),
        ("min_plausible_year", "1500"),
        ("protect_drive_roots", "maybe"),
    ):
        result = cli.run("config", "set", key, value, "--yes")
        assert result.exit_code == 2, (key, value, result.output)
        assert cli.config.read_text(encoding="utf-8") == before


def test_a_broken_settings_file_is_never_replaced(cli: Cli) -> None:
    cli.config.write_text("workers = = 3", encoding="utf-8")
    result = cli.run("config", "set", "workers", "2")
    assert result.exit_code == 2
    assert cli.config.read_text(encoding="utf-8") == "workers = = 3"


def test_one_off_globals_are_not_saved(
    cli: Cli, fake_exe: Callable[..., Path], tmp_path: Path
) -> None:
    before = cli.config.read_text(encoding="utf-8")
    bin_dir = tmp_path / "bin"
    result = cli.run(
        "--exiftool",
        str(fake_exe(bin_dir, "exiftool")),
        "--ffmpeg",
        str(fake_exe(bin_dir, "ffmpeg")),
        "--ffprobe",
        str(fake_exe(bin_dir, "ffprobe")),
        "--ollama-url",
        "http://127.0.0.1:9",
        "doctor",
    )
    assert result.exit_code == 0, result.output
    assert cli.config.read_text(encoding="utf-8") == before


def test_config_file_can_come_from_the_environment(cli: Cli) -> None:
    cli.run("config", "set", "workers", "5")
    result = cli.run("config", "get", "workers", config=False)  # env var only
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "5"
