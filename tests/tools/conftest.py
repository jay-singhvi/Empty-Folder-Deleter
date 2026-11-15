"""Shared fixtures for the tool tests.

``cloud_placeholder`` fakes what Windows reports for a OneDrive file or folder that is on the
device: the reparse-point attribute together with a cloud-files reparse tag. A real junction or
symlink also carries the reparse bit, so a tool that tests the bit alone cannot tell them apart.
Nothing is created on disk; the fake only changes what ``os.stat``, ``os.lstat`` and
``os.scandir`` report for the given names.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any, Self

import pytest

from various_tools.core import fs

PlaceholderFaker = Callable[..., None]

CLOUD_TAG = 0x9000701A  # IO_REPARSE_TAG_CLOUD_7, one of the tags OneDrive uses


class _FakeStat:
    """A stat result that reports the reparse bit and a cloud tag; everything else is real."""

    def __init__(self, real: os.stat_result) -> None:
        self._real = real
        self.st_file_attributes: int = (
            getattr(real, "st_file_attributes", 0) | fs.FILE_ATTRIBUTE_REPARSE_POINT
        )
        self.st_reparse_tag: int = CLOUD_TAG

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _FakeEntry:
    def __init__(self, real: os.DirEntry[str]) -> None:
        self._real = real

    def stat(self, *, follow_symlinks: bool = True) -> Any:
        return _FakeStat(self._real.stat(follow_symlinks=follow_symlinks))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _FakeScan:
    def __init__(self, real: Any, names: set[str]) -> None:
        self._real = real
        self._names = names

    def __enter__(self) -> Self:
        self._real.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.__exit__(*exc)

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        entry = next(self._real)
        return _FakeEntry(entry) if entry.name in self._names else entry

    def close(self) -> None:
        self._real.close()


@pytest.fixture
def cloud_placeholder(monkeypatch: pytest.MonkeyPatch) -> PlaceholderFaker:
    """Call ``cloud_placeholder("a.txt", "Folder")``: those names now look like OneDrive
    placeholders that are on the device (reparse bit plus a cloud tag, no recall attributes)."""
    names: set[str] = set()
    real_scandir = os.scandir
    real_lstat = os.lstat
    real_stat = os.stat

    def scandir(path: Any = ".") -> Any:
        return _FakeScan(real_scandir(path), names)

    def lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_lstat(path, *args, **kwargs)
        return _FakeStat(result) if os.path.basename(os.fspath(path)) in names else result

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_stat(path, *args, **kwargs)
        if isinstance(path, int):
            return result
        return _FakeStat(result) if os.path.basename(os.fspath(path)) in names else result

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "stat", stat)

    def register(*placeholder_names: str) -> None:
        names.update(placeholder_names)

    return register
