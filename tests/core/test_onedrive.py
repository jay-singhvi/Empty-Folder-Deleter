"""OneDrive placeholder rules (AGENTS.md rule 3). Status comes from attributes, not file reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from various_tools.core import onedrive
from various_tools.core.onedrive import (
    CloudOnlyFileError,
    SyncStatus,
    ensure_on_device,
    get_sync_status,
    is_on_device,
    is_regular_file_no_hydrate,
    status_from_attributes,
)

OFFLINE = 0x1000
RECALL_ON_OPEN = 0x40000
PINNED = 0x80000
RECALL_ON_DATA_ACCESS = 0x400000
DIRECTORY = 0x10


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        (0x20, SyncStatus.LOCAL),
        (OFFLINE, SyncStatus.CLOUD_ONLY),
        (RECALL_ON_OPEN, SyncStatus.CLOUD_ONLY),
        (RECALL_ON_DATA_ACCESS, SyncStatus.CLOUD_ONLY),
        (OFFLINE | PINNED, SyncStatus.CLOUD_ONLY),
        (PINNED, SyncStatus.PINNED),
    ],
)
def test_status_from_attributes(attributes: int, expected: SyncStatus) -> None:
    assert status_from_attributes(attributes) is expected


def test_only_local_and_pinned_are_on_device() -> None:
    assert is_on_device(SyncStatus.LOCAL)
    assert is_on_device(SyncStatus.PINNED)
    assert not is_on_device(SyncStatus.CLOUD_ONLY)
    assert not is_on_device(SyncStatus.UNKNOWN)


def test_status_labels_match_the_gui_wording() -> None:
    assert SyncStatus.CLOUD_ONLY.label == "Cloud-only (not downloaded)"
    assert SyncStatus.PINNED.label == "Always available (pinned)"


def test_real_local_file_is_local_and_regular(tmp_path: Path) -> None:
    target = tmp_path / "local.txt"
    target.write_bytes(b"hi")
    assert get_sync_status(target) is SyncStatus.LOCAL
    ensure_on_device(target)  # must not raise
    assert is_regular_file_no_hydrate(target)
    assert not is_regular_file_no_hydrate(tmp_path)  # a folder is not a regular file


def test_missing_path_is_unknown_and_not_a_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.jpg"
    assert get_sync_status(missing) is SyncStatus.UNKNOWN
    assert not is_regular_file_no_hydrate(missing)
    with pytest.raises(CloudOnlyFileError):
        ensure_on_device(missing)


def test_cloud_only_file_is_refused_without_being_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cloud.jpg"
    target.write_bytes(b"not really downloaded")
    monkeypatch.setattr(onedrive, "get_attributes", lambda _path: OFFLINE)

    opened: list[Path] = []
    real_open = Path.open

    def spy_open(self: Path, *args: object, **kwargs: object) -> object:
        opened.append(self)
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", spy_open)
    with pytest.raises(CloudOnlyFileError) as info:
        ensure_on_device(target)
    assert info.value.path == target
    assert "skipped: cloud-only" in str(info.value)
    assert opened == []


def test_force_download_reports_missing_file(tmp_path: Path) -> None:
    ok, detail = onedrive.force_download(tmp_path / "gone.jpg", wait_seconds=1)
    assert ok is False
    assert "no longer exists" in detail


def test_unpin_file_is_a_no_op_outside_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onedrive.sys, "platform", "linux")
    assert onedrive.unpin_file(tmp_path / "x") is False
