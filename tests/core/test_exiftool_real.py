"""Real ExifTool, run on a generated JPEG. Skipped when ExifTool is not installed."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from various_tools.core.media import exif

TAG_DATETIME_ORIGINAL = 0x9003


def test_real_exiftool_reads_capture_time(tmp_path: Path, exiftool_path: Path) -> None:
    path = tmp_path / "exiftool-check.jpg"
    exif_obj = Image.Exif()
    exif_obj.get_ifd(exif.EXIF_IFD)[TAG_DATETIME_ORIGINAL] = "2022:08:09 07:06:05"
    Image.new("RGB", (16, 16)).save(path, format="JPEG", exif=exif_obj)

    assert exif.exiftool_capture_time(exiftool_path, path) == datetime(2022, 8, 9, 7, 6, 5)


NON_ASCII_NAME = "café 写真 🙂.jpg"


def test_real_exiftool_reads_a_non_ascii_file_name(tmp_path: Path, exiftool_path: Path) -> None:
    """Plain command-line arguments fail on such names on Windows; the argument file works."""
    path = tmp_path / "ünïcode 文件夹" / NON_ASCII_NAME
    path.parent.mkdir()
    exif_obj = Image.Exif()
    exif_obj.get_ifd(exif.EXIF_IFD)[TAG_DATETIME_ORIGINAL] = "2021:03:04 05:06:07"
    Image.new("RGB", (16, 16)).save(path, format="JPEG", exif=exif_obj)

    assert exif.exiftool_capture_time(exiftool_path, path) == datetime(2021, 3, 4, 5, 6, 7)


def test_run_exiftool_removes_its_argument_file(
    tmp_path: Path, exiftool_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arg_dir = tmp_path / "args"
    arg_dir.mkdir()
    monkeypatch.setattr(exif.tempfile, "tempdir", str(arg_dir))
    path = tmp_path / NON_ASCII_NAME
    Image.new("RGB", (16, 16)).save(path, format="JPEG")

    result = exif.run_exiftool(exiftool_path, ["-s3", "-ImageWidth", str(path)], 30)

    assert result.returncode == 0
    assert result.stdout.strip() == "16"
    assert list(arg_dir.iterdir()) == []


def test_real_exiftool_returns_none_without_a_date(tmp_path: Path, exiftool_path: Path) -> None:
    path = tmp_path / "no-date.jpg"
    Image.new("RGB", (16, 16)).save(path, format="JPEG")
    assert exif.exiftool_capture_time(exiftool_path, path) is None
