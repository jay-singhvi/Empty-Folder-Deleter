"""Restoring from the Recycle Bin (plan §3.3). Only a fake bin folder under tmp_path is used."""

from __future__ import annotations

import struct
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from various_tools.core import recycle
from various_tools.core.recycle import current_user_bin as real_current_user_bin

BinWriter = Callable[..., Path]
Builders = dict[int, Callable[[Path | str, datetime, int], bytes]]

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
AFTER = NOW - timedelta(minutes=2)
BEFORE = NOW + timedelta(minutes=2)


def _restore(
    original: Path, bin_folder: Path | None, size: int | None = None
) -> recycle.RestoreResult:
    return recycle.restore(
        original,
        deleted_after=AFTER,
        deleted_before=BEFORE,
        size=size,
        locate_bin=lambda _p: bin_folder,
    )


@pytest.mark.parametrize("version", [1, 2])
def test_index_files_of_both_versions_are_read(version: int, index_builders: Builders) -> None:
    entry = recycle.parse_index(index_builders[version](r"C:\Users\me\photo.jpg", NOW, 1234))
    assert entry is not None
    assert entry.original == r"C:\Users\me\photo.jpg"
    assert entry.size == 1234
    assert abs((entry.deleted - NOW).total_seconds()) < 0.001


@pytest.mark.parametrize("data", [b"", b"\x03" + b"\x00" * 40, struct.pack("<qqqi", 2, 1, 1, 99)])
def test_damaged_or_unknown_index_files_are_ignored(data: bytes) -> None:
    assert recycle.parse_index(data) is None


def test_the_matching_item_is_moved_back_and_its_index_removed(
    tmp_path: Path, bin_writer: BinWriter
) -> None:
    bin_folder = tmp_path / "bin"
    original = tmp_path / "work" / "sub" / "photo.jpg"
    bin_writer(bin_folder, "ABC123", original, NOW, b"pixels")
    result = _restore(original, bin_folder)
    assert result.restored, result.detail
    assert original.read_bytes() == b"pixels"  # the missing parent folders were made again
    assert list(bin_folder.iterdir()) == []


def test_the_newest_matching_entry_wins_and_case_does_not_matter(
    tmp_path: Path, bin_writer: BinWriter
) -> None:
    bin_folder = tmp_path / "bin"
    original = tmp_path / "photo.jpg"
    bin_writer(bin_folder, "OLD001", Path(str(original).upper()), NOW, b"old")
    bin_writer(bin_folder, "NEW001", original, NOW + timedelta(seconds=30), b"new")
    result = _restore(original, bin_folder)
    assert result.restored
    assert original.read_bytes() == b"new"
    assert sorted(p.name for p in bin_folder.iterdir()) == ["$IOLD001.JPG", "$ROLD001.JPG"]


def test_an_entry_outside_the_time_window_is_not_used(
    tmp_path: Path, bin_writer: BinWriter
) -> None:
    bin_folder = tmp_path / "bin"
    original = tmp_path / "photo.jpg"
    bin_writer(bin_folder, "WEEKAGO", original, NOW - timedelta(days=7))
    bin_writer(bin_folder, "LATER01", original, NOW + timedelta(hours=1))
    result = _restore(original, bin_folder)
    assert not result.restored
    assert "no matching" in result.detail
    assert not original.exists()


def test_the_size_must_match_when_it_is_known(tmp_path: Path, bin_writer: BinWriter) -> None:
    bin_folder = tmp_path / "bin"
    original = tmp_path / "photo.jpg"
    bin_writer(bin_folder, "ABC", original, NOW, b"12345")
    assert not _restore(original, bin_folder, size=99).restored
    assert _restore(original, bin_folder, size=5).restored


def test_an_occupied_original_path_is_never_replaced(tmp_path: Path, bin_writer: BinWriter) -> None:
    bin_folder = tmp_path / "bin"
    original = tmp_path / "photo.jpg"
    bin_writer(bin_folder, "ABC", original, NOW, b"from bin")
    original.write_bytes(b"new file")
    result = _restore(original, bin_folder)
    assert not result.restored
    assert "occupied" in result.detail
    assert original.read_bytes() == b"new file"
    assert len(list(bin_folder.iterdir())) == 2


def test_no_bin_on_that_drive_means_not_possible(tmp_path: Path) -> None:
    result = _restore(tmp_path / "x.txt", None)
    assert not result.restored
    assert "Recycle Bin" in result.detail


def test_a_folder_item_is_restored_too(tmp_path: Path, bin_writer: BinWriter) -> None:
    bin_folder = tmp_path / "bin"
    original = tmp_path / "work" / "empty"
    (tmp_path / "work").mkdir()
    bin_writer(bin_folder, "DIR001", original, NOW, None)
    assert _restore(original, bin_folder).restored
    assert original.is_dir()


def test_an_item_missing_its_r_file_is_not_possible(tmp_path: Path, bin_writer: BinWriter) -> None:
    bin_folder = tmp_path / "bin"
    original = tmp_path / "photo.jpg"
    item = bin_writer(bin_folder, "ABC", original, NOW)
    item.unlink()
    assert not _restore(original, bin_folder).restored
    assert (bin_folder / "$IABC.jpg").exists()


def test_the_current_users_bin_is_not_offered_for_network_paths() -> None:
    # The real lookup (the core conftest replaces it in every other test): no network drives.
    assert real_current_user_bin(Path(r"\\server\share\folder\file.txt")) is None
