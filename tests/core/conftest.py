"""Helpers for the core tests: a hand-written Recycle Bin under tmp_path (never the real one)."""

from __future__ import annotations

import struct
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

_EPOCH_AS_FILETIME = 116444736000000000

BinWriter = Callable[..., Path]


def _filetime(when: datetime) -> int:
    return int(when.timestamp() * 10_000_000) + _EPOCH_AS_FILETIME


def index_v2(original: Path | str, deleted: datetime, size: int) -> bytes:
    """A Windows 10+ ``$I`` file: version, size, FILETIME, path length, path."""
    text = str(original) + "\x00"
    return struct.pack("<qqqi", 2, size, _filetime(deleted), len(text)) + text.encode("utf-16-le")


def index_v1(original: Path | str, deleted: datetime, size: int) -> bytes:
    """A Vista to 8.1 ``$I`` file: version, size, FILETIME, then a fixed 260-character path."""
    raw = (str(original) + "\x00").encode("utf-16-le").ljust(520, b"\x00")
    return struct.pack("<qqq", 1, size, _filetime(deleted)) + raw


def put_in_bin(
    bin_folder: Path,
    key: str,
    original: Path,
    deleted: datetime,
    content: bytes | None = b"data",
    *,
    version: int = 2,
) -> Path:
    """Write a ``$I``/``$R`` pair as Windows would. ``content=None`` makes ``$R`` a folder."""
    bin_folder.mkdir(parents=True, exist_ok=True)
    item = bin_folder / f"$R{key}{original.suffix}"
    if content is None:
        item.mkdir()
        size = 0
    else:
        item.write_bytes(content)
        size = len(content)
    builder = index_v2 if version == 2 else index_v1
    (bin_folder / f"$I{key}{original.suffix}").write_bytes(builder(original, deleted, size))
    return item


# The real Recycle Bin is kept out by the global autouse fixture in tests/conftest.py (fake
# send2trash, no bin found by undo, every path recyclable). Do not shadow it here.


@pytest.fixture
def bin_writer() -> BinWriter:
    """``put_in_bin(bin_folder, key, original, deleted, content=b"data", version=2)``."""
    return put_in_bin


@pytest.fixture
def index_builders() -> dict[int, Callable[[Path | str, datetime, int], bytes]]:
    return {1: index_v1, 2: index_v2}
