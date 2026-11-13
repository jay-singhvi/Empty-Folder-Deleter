"""A failing metadata reader falls through to the next date source (legacy: catches everything)."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from various_tools.core.onedrive import CloudOnlyFileError
from various_tools.tools.organize_by_date import dates

NOW = datetime(2026, 1, 1)
MODIFIED = datetime(2024, 6, 1, 12, 0, 0)


def _read(
    path: Path,
    priority: tuple[str, ...],
    *,
    ffprobe: Path | None = None,
    exiftool: Path | None = None,
) -> dates.Reading:
    return dates.read_capture_date(
        path,
        modified=MODIFIED,
        priority=priority,
        min_year=1990,
        now=NOW,
        ffprobe=ffprobe,
        exiftool=exiftool,
    )


def _timeout(*_args: object, **_kwargs: object) -> object:
    raise subprocess.TimeoutExpired(cmd="tool", timeout=1)


def _garbage(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["t"], returncode=0, stdout="}{ garbage", stderr="")


def test_ffprobe_timeout_falls_through_to_the_modified_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr("various_tools.core.media.ffmpeg.run_tool", _timeout)
    reading = _read(clip, ("video_tag", "modified"), ffprobe=tmp_path / "ffprobe.exe")
    assert reading.date == MODIFIED
    assert reading.label == dates.LABELS["modified"]


def test_garbage_ffprobe_output_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr("various_tools.core.media.ffmpeg.run_tool", _garbage)
    reading = _read(clip, ("video_tag", "modified"), ffprobe=tmp_path / "ffprobe.exe")
    assert reading.date == MODIFIED


def test_exiftool_timeout_falls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "IMG.cr3"
    raw.write_bytes(b"x")
    monkeypatch.setattr("various_tools.core.media.exif.run_tool", _timeout)
    reading = _read(raw, ("exiftool", "modified"), exiftool=tmp_path / "exiftool.exe")
    assert reading.date == MODIFIED


def test_a_reader_that_raises_anything_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "IMG.cr3"
    raw.write_bytes(b"x")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("surprise")

    monkeypatch.setattr(dates, "exiftool_capture_time", boom)
    monkeypatch.setattr(dates, "creation_time", boom)
    assert _read(raw, ("exiftool", "modified"), exiftool=raw).date == MODIFIED
    assert _read(clip, ("video_tag", "modified"), ffprobe=clip).date == MODIFIED


def test_broken_exif_reader_falls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    photo = tmp_path / "p.jpg"
    Image.new("RGB", (8, 8)).save(photo)

    def boom(_path: Path) -> object:
        raise KeyError("odd tag")

    monkeypatch.setattr(dates, "read_exif", boom)
    reading = _read(photo, ("exif", "modified"))
    assert reading.date == MODIFIED
    assert reading.exif is None


def test_cloud_only_error_is_not_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    def cloud(*_args: object, **_kwargs: object) -> object:
        raise CloudOnlyFileError(clip)

    monkeypatch.setattr(dates, "creation_time", cloud)
    with pytest.raises(CloudOnlyFileError):
        _read(clip, ("video_tag", "modified"), ffprobe=clip)
