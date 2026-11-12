"""long_path(): the \\\\?\\ prefix is added only on Windows and only past MAX_PATH."""

from __future__ import annotations

import os
import types

import pytest

from various_tools.core import fs


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch only the module's view of sys, so the rest of the test run is untouched.
    monkeypatch.setattr(fs, "sys", types.SimpleNamespace(platform="win32"))


def test_short_paths_are_unchanged(windows: None) -> None:
    short = r"C:\Users\someone\Pictures\photo.jpg"
    assert fs.long_path(short) == short


def test_long_local_paths_get_the_extended_prefix(windows: None) -> None:
    long_local = "C:\\" + "a" * 260 + "\\photo.jpg"
    assert fs.long_path(long_local) == "\\\\?\\" + long_local


def test_long_unc_paths_get_the_unc_prefix(windows: None) -> None:
    unc = "\\\\server\\share\\" + "b" * 260
    assert fs.long_path(unc) == "\\\\?\\UNC\\server\\share\\" + "b" * 260


def test_already_prefixed_paths_are_not_prefixed_twice(windows: None) -> None:
    prefixed = "\\\\?\\C:\\" + "c" * 260
    assert fs.long_path(prefixed) == prefixed


def test_non_windows_paths_are_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "sys", types.SimpleNamespace(platform="linux"))
    long_path_text = "/tmp/" + "d" * 300
    # No prefix is added off Windows. The only change is the usual abspath normalisation.
    assert fs.long_path(long_path_text) == os.path.abspath(long_path_text)
    assert not fs.long_path(long_path_text).startswith("\\\\?\\")
