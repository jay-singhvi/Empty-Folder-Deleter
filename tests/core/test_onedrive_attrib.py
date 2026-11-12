"""Pin and download paths of onedrive.py, run against a fake ``attrib`` so no file changes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from various_tools.core import onedrive
from various_tools.core.onedrive import SyncStatus

FakeExe = Callable[..., Path]


def _failing_attrib(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "attrib.bat"
    target.write_text("@echo off\r\necho access denied 1>&2\r\nexit /b 1\r\n", encoding="ascii")
    return target


@pytest.fixture
def skip_on_non_windows() -> None:
    import sys

    if sys.platform != "win32":
        pytest.skip("attrib is a Windows tool")


def test_unpin_reports_attrib_success(
    tmp_path: Path, fake_exe: FakeExe, monkeypatch: pytest.MonkeyPatch, skip_on_non_windows: None
) -> None:
    tool = fake_exe(tmp_path / "sys", "attrib", output="ok")
    monkeypatch.setattr(onedrive, "_attrib_command", lambda: str(tool))
    assert onedrive.unpin_file(tmp_path / "file.jpg") is True


def test_unpin_reports_attrib_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip_on_non_windows: None
) -> None:
    tool = _failing_attrib(tmp_path / "sys")
    monkeypatch.setattr(onedrive, "_attrib_command", lambda: str(tool))
    assert onedrive.unpin_file(tmp_path / "file.jpg") is False


def test_force_download_succeeds_as_soon_as_the_file_is_local(
    tmp_path: Path, fake_exe: FakeExe, monkeypatch: pytest.MonkeyPatch, skip_on_non_windows: None
) -> None:
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"data")
    tool = fake_exe(tmp_path / "sys", "attrib", output="ok")
    monkeypatch.setattr(onedrive, "_attrib_command", lambda: str(tool))
    monkeypatch.setattr(onedrive, "get_sync_status", lambda _p: SyncStatus.LOCAL)
    assert onedrive.force_download(target, wait_seconds=1) == (True, "downloaded")


def test_force_download_reads_the_bytes_when_attrib_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip_on_non_windows: None
) -> None:
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"data")
    monkeypatch.setattr(onedrive, "_attrib_command", lambda: str(_failing_attrib(tmp_path / "s")))
    # The attrib failure is noted, but reading the file still downloads it.
    assert onedrive.force_download(target, wait_seconds=1) == (True, "downloaded")


def test_force_download_reports_both_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip_on_non_windows: None
) -> None:
    folder_not_file = tmp_path / "not-a-file"
    folder_not_file.mkdir()  # exists, so it passes the first check, but cannot be read as bytes
    monkeypatch.setattr(onedrive, "_attrib_command", lambda: str(_failing_attrib(tmp_path / "s")))
    ok, detail = onedrive.force_download(folder_not_file, wait_seconds=1)
    assert ok is False
    assert "attrib failed" in detail
    assert "read failed" in detail


def test_force_download_gives_up_waiting_but_still_tries_a_read(
    tmp_path: Path, fake_exe: FakeExe, monkeypatch: pytest.MonkeyPatch, skip_on_non_windows: None
) -> None:
    target = tmp_path / "slow.jpg"
    target.write_bytes(b"data")
    tool = fake_exe(tmp_path / "sys", "attrib", output="ok")
    monkeypatch.setattr(onedrive, "_attrib_command", lambda: str(tool))
    monkeypatch.setattr(onedrive, "get_sync_status", lambda _p: SyncStatus.CLOUD_ONLY)
    monkeypatch.setattr(onedrive.time, "sleep", lambda _s: None)
    ok, detail = onedrive.force_download(target, wait_seconds=0)
    assert ok is True  # the read after the wait succeeded
    assert detail == "downloaded"
