"""Safety review fixes on the CLI side.

* The apply confirmation says how many cloud-only files the ticked rows download.
* ``download`` refuses folders.

``force_download`` is a fake; every tree lives under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from various_tools.core import onedrive as onedrive_module
from various_tools.core.plan import ActionKind, Plan, PlannedAction

if TYPE_CHECKING:
    from conftest import Cli


# ----- item 6a: download wording in the apply confirmation ---------------------------------------


def test_apply_confirmation_says_how_many_files_will_be_downloaded(cli: Cli) -> None:
    plan = Plan(
        tool="organize_by_date",
        roots=[cli.work],
        actions=[
            PlannedAction(ActionKind.CONVERT, cli.work / "a.heic", None, "cloud-only: download"),
            PlannedAction(ActionKind.CONVERT, cli.work / "b.heic", None, "cloud-only: download"),
        ],
    )
    saved = cli.work / "plan.json"
    saved.write_text(plan.to_json(), encoding="utf-8")
    result = cli.run("organize_by_date", "apply", "--plan", str(saved), input="n\n")
    assert result.exit_code == 1
    assert "download 2 cloud-only file(s) from OneDrive" in result.output
    assert "Nothing was changed" in result.output


# ----- item 6b: download refuses folders ---------------------------------------------------------


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def fake_download(path: Path, wait_seconds: int | None = None) -> tuple[bool, str]:
        calls.append(path)
        return True, "downloaded"

    monkeypatch.setattr(onedrive_module, "force_download", fake_download)
    return calls


def test_download_refuses_a_folder(cli: Cli, downloads: list[Path]) -> None:
    folder = cli.work / "Pictures"
    folder.mkdir()
    result = cli.run("download", str(folder), str(cli.work / "a.jpg"), "--yes")
    assert result.exit_code == 2
    assert "folder" in result.output and "plan" in result.output
    assert downloads == []


def test_download_from_a_plan_refuses_a_listed_folder(cli: Cli, downloads: list[Path]) -> None:
    folder = cli.work / "Pictures"
    folder.mkdir()
    plan = Plan(tool="t", roots=[cli.work], cloud_only=[folder])
    saved = cli.work / "plan.json"
    saved.write_text(plan.to_json(), encoding="utf-8")
    result = cli.run("download", "--plan", str(saved), "--yes")
    assert result.exit_code == 2
    assert downloads == []
