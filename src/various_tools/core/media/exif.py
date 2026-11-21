"""Capture time, camera and GPS from EXIF, with an ExifTool fallback for RAW files.

Ported from the duplicate finder's ``capture_time`` and metadata-8's ``get_exif_data``,
``parse_exif_datetime``, ``is_plausible_date`` and ``get_exiftool_date_taken``.

Reading EXIF opens the file, so :func:`read_exif` checks OneDrive state first. ExifTool is
passed in as a resolved path (from ``RunContext.deps``). This module never locates it.

Every ExifTool call goes through :func:`run_exiftool`. On Windows ExifTool reads its command line
in the ANSI code page, so a file name with letters outside it (accents, Chinese, emoji) fails as a
plain argument. The arguments are passed in a UTF-8 argument file with ``-charset filename=utf8``
instead, which works for any name.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from various_tools.core.deps import run_tool
from various_tools.core.media.imaging import IMAGE_EXTS, open_image
from various_tools.core.onedrive import CloudOnlyFileError, ensure_on_device
from various_tools.core.settings import (
    DEFAULT_EXIFTOOL_READ_TIMEOUT_S,
    DEFAULT_MIN_PLAUSIBLE_YEAR,
)

log = logging.getLogger(__name__)

EXIF_IFD = 0x8769  # Exif sub-IFD: DateTimeOriginal and friends
GPS_IFD = 0x8825
TAG_DATETIME_ORIGINAL = 0x9003  # 36867
TAG_DATETIME_DIGITIZED = 0x9004  # 36868
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
DEFAULT_MIN_YEAR = DEFAULT_MIN_PLAUSIBLE_YEAR


@dataclass(frozen=True)
class ExifInfo:
    capture_time: datetime | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    width: int | None = None
    height: int | None = None
    gps: tuple[float, float] | None = None


def parse_exif_datetime(value: object) -> datetime | None:
    """Parse an EXIF ``YYYY:MM:DD HH:MM:SS`` value. Blank, zeroed or garbled text gives None."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    text = str(value).replace("\x00", "").strip()[:19]
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def is_plausible_date(
    value: object, *, min_year: int = DEFAULT_MIN_YEAR, now: datetime | None = None
) -> bool:
    """Reject reset camera clocks, zero epochs (1970, 1904) and dates in the far future."""
    current = now or datetime.now()
    return isinstance(value, datetime) and min_year <= value.year <= current.year + 1


def convert_gps(gps_info: dict[int, object]) -> tuple[float, float] | None:
    """Decimal ``(lat, lon)`` from an EXIF GPS IFD, or None when it is missing or malformed."""
    try:

        def to_degrees(value: Any) -> float:
            d, m, s = value
            return float(d) + float(m) / 60.0 + float(s) / 3600.0

        lat = to_degrees(gps_info[2])
        if gps_info.get(1) == "S":
            lat = -lat
        lon = to_degrees(gps_info[4])
        if gps_info.get(3) == "W":
            lon = -lon
        return round(lat, 6), round(lon, 6)
    except KeyError, TypeError, ValueError, ZeroDivisionError:
        return None


def read_exif(path: Path) -> ExifInfo:
    """EXIF from a Pillow-readable image. Non-image files and unreadable files give empty info.

    Raises :class:`CloudOnlyFileError` for cloud-only files. Callers report those as skipped.
    """
    if path.suffix.lower() not in IMAGE_EXTS:
        return ExifInfo()
    try:
        with open_image(path) as img:
            return _exif_from_image(img)
    except CloudOnlyFileError:
        raise
    except Exception as exc:  # malformed EXIF raises many types in Pillow; none may stop a scan
        log.warning("No readable EXIF in %s: %s", path, exc)
        return ExifInfo()


def _exif_from_image(img: Image.Image) -> ExifInfo:
    width, height = img.size
    try:
        exif = img.getexif()
    except OSError, ValueError:
        return ExifInfo(width=width, height=height)
    if not exif:
        return ExifInfo(width=width, height=height)

    sub_ifd = exif.get_ifd(EXIF_IFD)
    capture: datetime | None = None
    for tag in (TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED):
        raw_value = sub_ifd.get(tag) or exif.get(tag)
        capture = parse_exif_datetime(raw_value)
        if capture:
            break

    gps_ifd = exif.get_ifd(GPS_IFD)
    gps = convert_gps(dict(gps_ifd)) if gps_ifd else None
    make = exif.get(TAG_MAKE)
    model = exif.get(TAG_MODEL)
    return ExifInfo(
        capture_time=capture,
        camera_make=str(make).strip() if make else None,
        camera_model=str(model).strip() if model else None,
        width=width,
        height=height,
        gps=gps,
    )


def run_exiftool(
    exiftool: Path, args: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ExifTool with ``args`` (written one per line, UTF-8) so any Windows file name works.

    The argument file lives in the system temp folder, never in a folder the user chose, and is
    removed afterwards. Can raise ``subprocess.TimeoutExpired`` or ``OSError``, like ``run_tool``.
    """
    fd, name = tempfile.mkstemp(prefix="various-tools-exiftool-", suffix=".args")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(args) + "\n")
        return run_tool([str(exiftool), "-charset", "filename=utf8", "-@", name], timeout=timeout)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def exiftool_capture_time(
    exiftool: Path, path: Path, timeout: float = DEFAULT_EXIFTOOL_READ_TIMEOUT_S
) -> datetime | None:
    """Capture time from ExifTool, for files Pillow cannot open (CR3). None when it has none."""
    try:
        result = run_exiftool(
            exiftool,
            ["-s3", "-d", "%Y:%m:%d %H:%M:%S", "-DateTimeOriginal", "-CreateDate", str(path)],
            timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("ExifTool timed out on %s", path)
        return None
    except (OSError, ValueError) as exc:
        log.warning("ExifTool failed on %s: %s", path, exc)
        return None
    if result.returncode != 0:
        log.warning("ExifTool failed on %s: %s", path, (result.stderr or "").strip()[:200])
        return None
    for line in (result.stdout or "").splitlines():
        parsed = parse_exif_datetime(line)
        if parsed:
            return parsed
    return None


@dataclass(frozen=True)
class RawInfo:
    """What ExifTool reports for one RAW file: capture time, GPS position and pixel size."""

    capture_time: datetime | None = None
    gps: tuple[float, float] | None = None
    width: int | None = None
    height: int | None = None


EXIFTOOL_BATCH_SIZE = 50


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _positive_int(*values: object) -> int | None:
    for value in values:
        number = _number(value)
        if number is not None and number >= 1:
            return int(number)
    return None


def _raw_info_from_record(record: dict[str, Any]) -> RawInfo:
    capture: datetime | None = None
    for tag in ("DateTimeOriginal", "CreateDate"):  # same order as exiftool_capture_time
        capture = parse_exif_datetime(record.get(tag))
        if capture:
            break
    lat, lon = _number(record.get("GPSLatitude")), _number(record.get("GPSLongitude"))
    gps = None
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        gps = (round(lat, 6), round(lon, 6))
    return RawInfo(
        capture_time=capture,
        gps=gps,
        width=_positive_int(record.get("ExifImageWidth"), record.get("ImageWidth")),
        height=_positive_int(record.get("ExifImageHeight"), record.get("ImageHeight")),
    )


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def exiftool_raw_info(
    exiftool: Path,
    paths: Sequence[Path],
    timeout: float = DEFAULT_EXIFTOOL_READ_TIMEOUT_S,
    *,
    batch_size: int = EXIFTOOL_BATCH_SIZE,
) -> dict[Path, RawInfo]:
    """Date, GPS and size of RAW files, one ExifTool process per chunk of ``batch_size`` files.

    Files ExifTool could not read are missing from the result. The caller checks OneDrive
    state first (this opens the files). A chunk that fails or times out is logged and skipped.
    """
    found: dict[Path, RawInfo] = {}
    for start in range(0, len(paths), max(1, batch_size)):
        chunk = list(paths[start : start + max(1, batch_size)])
        args = [
            "-json",
            "-n",
            "-DateTimeOriginal",
            "-CreateDate",
            "-GPSLatitude",
            "-GPSLongitude",
            "-ExifImageWidth",
            "-ExifImageHeight",
            "-ImageWidth",
            "-ImageHeight",
            *(str(p) for p in chunk),
        ]
        try:
            result = run_exiftool(exiftool, args, timeout)
        except subprocess.TimeoutExpired:
            log.warning("ExifTool timed out on a batch of %d files", len(chunk))
            continue
        except (OSError, ValueError) as exc:
            log.warning("ExifTool failed on a batch of %d files: %s", len(chunk), exc)
            continue
        try:
            records = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            log.warning("ExifTool gave unreadable output for a batch of %d files", len(chunk))
            continue
        if not isinstance(records, list):
            continue
        for record in records:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(record, dict):
                continue
            rec: dict[str, Any] = record  # pyright: ignore[reportUnknownVariableType]
            source = str(rec.get("SourceFile", ""))
            match = next((p for p in chunk if _same_file(str(p), source)), None)
            if match is not None:
                found[match] = _raw_info_from_record(rec)
    return found


def capture_time(
    path: Path,
    *,
    exiftool: Path | None = None,
    min_year: int = DEFAULT_MIN_YEAR,
    now: datetime | None = None,
    exiftool_timeout: float = DEFAULT_EXIFTOOL_READ_TIMEOUT_S,
) -> datetime | None:
    """The plausible capture time of a photo: EXIF first, then ExifTool when it is available.

    Returns None when neither gives a plausible date. The caller decides what that means.
    """
    ensure_on_device(path)  # ExifTool reads the file too, so cloud-only files stay untouched
    info = read_exif(path)
    if is_plausible_date(info.capture_time, min_year=min_year, now=now):
        return info.capture_time
    if exiftool is not None:
        fallback = exiftool_capture_time(exiftool, path, exiftool_timeout)
        if is_plausible_date(fallback, min_year=min_year, now=now):
            return fallback
    return None


EXIFTOOL_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"


def photo_date_args(when: datetime) -> list[str]:
    """ExifTool arguments that set a photo's taken date (and its created and edited dates)."""
    text = when.strftime(EXIFTOOL_DATE_FORMAT)
    return [f"-DateTimeOriginal={text}", f"-CreateDate={text}", f"-ModifyDate={text}"]


def video_date_args(when: datetime) -> list[str]:
    """ExifTool arguments that set the QuickTime dates of a video (local time, stored as UTC)."""
    text = when.strftime(EXIFTOOL_DATE_FORMAT)
    return [
        "-api",
        "QuickTimeUTC=1",
        f"-QuickTime:CreateDate={text}",
        f"-QuickTime:ModifyDate={text}",
        f"-QuickTime:MediaCreateDate={text}",
        f"-QuickTime:MediaModifyDate={text}",
        f"-QuickTime:TrackCreateDate={text}",
        f"-QuickTime:TrackModifyDate={text}",
    ]
