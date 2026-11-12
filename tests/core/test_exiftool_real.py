"""Real ExifTool, run on a generated JPEG. Skipped when ExifTool is not installed."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image

from various_tools.core.media import exif

TAG_DATETIME_ORIGINAL = 0x9003


def test_real_exiftool_reads_capture_time(tmp_path: Path, exiftool_path: Path) -> None:
    path = tmp_path / "exiftool-check.jpg"
    exif_obj = Image.Exif()
    exif_obj.get_ifd(exif.EXIF_IFD)[TAG_DATETIME_ORIGINAL] = "2022:08:09 07:06:05"
    Image.new("RGB", (16, 16)).save(path, format="JPEG", exif=exif_obj)

    assert exif.exiftool_capture_time(exiftool_path, path) == datetime(2022, 8, 9, 7, 6, 5)


def test_real_exiftool_returns_none_without_a_date(tmp_path: Path, exiftool_path: Path) -> None:
    path = tmp_path / "no-date.jpg"
    Image.new("RGB", (16, 16)).save(path, format="JPEG")
    assert exif.exiftool_capture_time(exiftool_path, path) is None
