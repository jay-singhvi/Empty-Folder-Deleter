"""Rename: options that used to be constants change what the tool does (nothing is constant)."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.settings import Settings
from various_tools.tools.rename import TOOL, RenameOptions
from various_tools.tools.rename import tool as rename_tool
from various_tools.tools.rename.naming import (
    FILENAME_DATE_PATTERN,
    date_from_filename,
    filename_pattern_problem,
)


def _ctx(tmp_path: Path, **settings: Any) -> RunContext:
    values: dict[str, Any] = {"onedrive_root": tmp_path, **settings}
    return RunContext(
        settings=Settings.model_validate(values),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
    )


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


def _names(plan: Any) -> list[str]:
    return [a.dst.name for a in plan.actions if a.dst and a.selected]


def _by_filename(root: Path, **kwargs: Any) -> RenameOptions:
    return RenameOptions(
        roots=[root],
        match="*.jpg",
        template="P-{date:%Y%m%d}-{counter:03}{ext}",
        date_source="filename",
        **kwargs,
    )


def test_min_plausible_year_comes_from_the_setting_and_the_option_overrides_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pics"
    _file(root / "IMG_19950101.jpg")
    assert _names(TOOL.scan(_by_filename(root), _ctx(tmp_path))) == ["P-19950101-001.jpg"]
    strict = _ctx(tmp_path, min_plausible_year=2000)
    assert _names(TOOL.scan(_by_filename(root), strict)) == []
    assert _names(TOOL.scan(_by_filename(root, min_plausible_year=1990), strict)) == [
        "P-19950101-001.jpg"
    ]
    assert _names(TOOL.scan(_by_filename(root, min_plausible_year=2000), _ctx(tmp_path))) == []


def test_default_filename_pattern_is_unchanged() -> None:
    assert RenameOptions().filename_date_pattern == FILENAME_DATE_PATTERN
    assert date_from_filename("a_20240101_123456.jpg", 1990, FILENAME_DATE_PATTERN) == (
        datetime(2024, 1, 1, 12, 34, 56),
        True,
    )


def test_custom_filename_pattern_finds_dates_in_another_layout(tmp_path: Path) -> None:
    root = tmp_path / "pics"
    _file(root / "photo 2024-03-09.jpg")
    default = TOOL.scan(_by_filename(root), _ctx(tmp_path))
    assert _names(default) == []
    assert any("no date in the file name" in note for note in default.notes)
    custom = _by_filename(root, filename_date_pattern=r"(\d{4})-(\d{2})-(\d{2})")
    assert _names(TOOL.scan(custom, _ctx(tmp_path))) == ["P-20240309-001.jpg"]


def test_custom_filename_pattern_with_a_time() -> None:
    pattern = r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})"
    assert date_from_filename("x20240102T030405.jpg", 1990, pattern) == (
        datetime(2024, 1, 2, 3, 4, 5),
        True,
    )


@pytest.mark.parametrize("bad", ["(unclosed", r"(\d{4})(\d{2})", r"\d{8}", r"(\d)(\d)(\d)(\d)"])
def test_filename_pattern_must_compile_and_have_the_right_groups(tmp_path: Path, bad: str) -> None:
    assert filename_pattern_problem(bad) is not None
    root = tmp_path / "pics"
    _file(root / "IMG_20240101.jpg")
    plan = TOOL.scan(_by_filename(root, filename_date_pattern=bad), _ctx(tmp_path))
    assert plan.actions == []
    assert any("filename date pattern" in note for note in plan.notes)


def test_a_pattern_that_matches_non_numbers_skips_the_file(tmp_path: Path) -> None:
    root = tmp_path / "pics"
    _file(root / "abc.jpg")
    opts = _by_filename(root, filename_date_pattern=r"(a)(b)(c)")
    assert _names(TOOL.scan(opts, _ctx(tmp_path))) == []


def test_capture_passes_the_settings_timeouts_and_the_resolved_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pics"
    _file(root / "a.jpg")
    seen: dict[str, Any] = {}

    def fake_capture(path: Path, **kwargs: Any) -> datetime | None:
        seen.update(kwargs)
        return datetime(2020, 1, 1, 10, 0, 0)

    monkeypatch.setattr(rename_tool, "capture_time", fake_capture)
    opts = RenameOptions(
        roots=[root],
        match="*.jpg",
        template="P-{date:%Y%m%d}{ext}",
        date_source="capture",
        min_plausible_year=2001,
    )
    plan = TOOL.scan(opts, _ctx(tmp_path, exiftool_read_timeout_s=17, min_plausible_year=1995))
    assert _names(plan) == ["P-20200101.jpg"]
    assert seen["exiftool_timeout"] == 17
    assert seen["min_year"] == 2001


def test_ffprobe_timeout_comes_from_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "clips"
    _file(root / "v.mp4")
    seen: dict[str, Any] = {}

    def fake_creation(ffprobe: Path, path: Path, *, timeout: float) -> datetime:
        seen["timeout"] = timeout
        return datetime(2020, 1, 1, 10, 0, 0)

    ctx = _ctx(tmp_path, ffprobe_timeout_s=21)
    monkeypatch.setattr(ctx.deps, "ffprobe", lambda: tmp_path / "ffprobe.exe")
    monkeypatch.setattr(ctx.deps, "exiftool", lambda: None)
    monkeypatch.setattr(rename_tool.ffmpeg, "creation_time", fake_creation)
    opts = RenameOptions(
        roots=[root], match="*.mp4", template="V-{date:%Y%m%d}{ext}", date_source="capture"
    )
    assert _names(TOOL.scan(opts, ctx)) == ["V-20200101.mp4"]
    assert seen["timeout"] == 21


def test_protected_folders_come_from_the_settings(tmp_path: Path) -> None:
    guarded = tmp_path / "Guarded"
    _file(guarded / "IMG_20240101.jpg")
    opts = _by_filename(guarded)
    plan = TOOL.scan(opts, _ctx(tmp_path, protected_folders=[str(guarded)]))
    assert plan.actions == []
    assert any("rotected" in note for note in plan.notes)
    assert os.path.exists(guarded / "IMG_20240101.jpg")
    other = _ctx(tmp_path, protected_folders=[str(tmp_path / "Elsewhere")])
    assert _names(TOOL.scan(opts, other)) == ["P-20240101-001.jpg"]
