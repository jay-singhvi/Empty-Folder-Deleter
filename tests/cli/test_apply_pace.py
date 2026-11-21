"""``apply --batch-size N --pause DURATION``: parsing, errors and a paced apply."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from various_tools.cli.toolcmds import parse_duration
from various_tools.core import plan as plan_module

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


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90", 90), ("30s", 30), ("5m", 300), ("1h", 3600), ("1.5m", 90), (" 2 M ", 120), ("0", 0)],
)
def test_parse_duration(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "m", "-5", "5x", "five", "1d", "5 minutes"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(text)


def _photos(cli: Cli) -> Path:
    folder = cli.work / "photos"
    folder.mkdir()
    for name in ("photo1.txt", "photo2.txt", "photo3.txt"):
        (folder / name).write_text(name)
    return folder


def test_a_paced_apply_runs_in_batches(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(plan_module, "_sleep", waits.append)
    folder = _photos(cli)
    result = cli.run(
        "rename", "apply", str(folder), *RENAME_FLAGS, "--yes", "--batch-size", "1", "--pause", "1s"
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in folder.iterdir()) == ["pic-001.txt", "pic-002.txt", "pic-003.txt"]
    assert sum(waits) == pytest.approx(2.0)  # between the three batches, not after the last


def test_batch_size_without_pause_is_a_usage_error(cli: Cli) -> None:
    folder = _photos(cli)
    result = cli.run("rename", "apply", str(folder), *RENAME_FLAGS, "--yes", "--batch-size", "2")
    assert result.exit_code == 2
    assert "--batch-size and --pause go together" in result.output
    assert not (folder / "pic-001.txt").exists()


def test_pause_without_batch_size_is_a_usage_error(cli: Cli) -> None:
    folder = _photos(cli)
    result = cli.run("rename", "apply", str(folder), *RENAME_FLAGS, "--yes", "--pause", "5m")
    assert result.exit_code == 2
    assert "go together" in result.output


def test_a_bad_pause_or_batch_size_is_a_usage_error(cli: Cli) -> None:
    folder = _photos(cli)
    bad = cli.run(
        "rename", "apply", str(folder), *RENAME_FLAGS, "--yes", "--batch-size", "2", "--pause", "x"
    )
    assert bad.exit_code == 2 and "--pause" in bad.output
    zero = cli.run(
        "rename", "apply", str(folder), *RENAME_FLAGS, "--yes", "--batch-size", "0", "--pause", "1"
    )
    assert zero.exit_code == 2 and "at least 1" in zero.output


def _apply_args(folder: Path, *extra: str) -> tuple[str, ...]:
    return ("rename", "apply", str(folder), *RENAME_FLAGS, "--yes", *extra)


def test_wait_for_upload_needs_a_batch_size(cli: Cli) -> None:
    folder = _photos(cli)
    result = cli.run(*_apply_args(folder, "--wait-for-upload"))
    assert result.exit_code == 2
    assert "--wait-for-upload needs --batch-size" in result.output
    assert not (folder / "pic-001.txt").exists()


def test_upload_timeout_needs_wait_for_upload(cli: Cli) -> None:
    folder = _photos(cli)
    result = cli.run(
        *_apply_args(folder, "--batch-size", "1", "--pause", "0", "--upload-timeout", "5m")
    )
    assert result.exit_code == 2
    assert "--upload-timeout needs --wait-for-upload" in result.output


def test_a_bad_upload_timeout_is_a_usage_error(cli: Cli) -> None:
    folder = _photos(cli)
    for bad in ("x", "0"):
        result = cli.run(
            *_apply_args(
                folder,
                *("--batch-size", "1", "--pause", "0", "--wait-for-upload"),
                *("--upload-timeout", bad),
            )
        )
        assert result.exit_code == 2, bad
        assert "--upload-timeout" in result.output


def test_wait_for_upload_reaches_the_run_and_a_timeout_is_reported(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.core import onedrive

    monkeypatch.setattr(plan_module, "_sleep", lambda _s: None)
    monkeypatch.setattr(onedrive, "upload_pending", lambda _p: True)
    folder = _photos(cli)
    result = cli.run(
        *_apply_args(
            folder,
            *("--batch-size", "2", "--pause", "0", "--wait-for-upload"),
            *("--upload-timeout", "1s"),
        )
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in folder.iterdir()) == ["pic-001.txt", "pic-002.txt", "pic-003.txt"]
    assert "OneDrive had not finished uploading 2 of 2 files" in result.output.replace("\n", " ")


def test_wait_for_upload_with_default_timeout_is_thirty_minutes(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.cli import toolcmds

    pace = toolcmds._pace_from_flags(5, "1m", True, None)  # pyright: ignore[reportPrivateUsage]
    assert pace is not None and pace.wait_for_upload and pace.upload_timeout_s == 1800
    plain = toolcmds._pace_from_flags(5, "1m", False, None)  # pyright: ignore[reportPrivateUsage]
    assert plain is not None and not plain.wait_for_upload
