"""Capture time, camera and GPS from EXIF, with an ExifTool fallback for RAW files.

Ported from the duplicate finder's ``capture_time`` and metadata-8's ``get_exif_data``,
``parse_exif_datetime``, ``is_plausible_date`` and ``get_exiftool_date_taken``.

Reading EXIF opens the file, so :func:`read_exif` checks OneDrive state first. ExifTool is
passed in as a resolved path (from ``RunContext.deps``). This module never locates it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from various_tools.core.deps import run_tool
from various_tools.core.media.imaging import IMAGE_EXTS, open_image
from various_tools.core.onedrive import CloudOnlyFileError, ensure_on_device

log = logging.getLogger(__name__)

EXIF_IFD = 0x8769  # Exif sub-IFD: DateTimeOriginal and friends
GPS_IFD = 0x8825
TAG_DATETIME_ORIGINAL = 0x9003  # 36867
TAG_DATETIME_DIGITIZED = 0x9004  # 36868
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
DEFAULT_MIN_YEAR = 1990


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
    except (OSError, ValueError) as exc:
        log.debug("No readable EXIF in %s: %s", path, exc)
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


def exiftool_capture_time(exiftool: Path, path: Path, timeout: float = 30) -> datetime | None:
    """Capture time from ExifTool, for files Pillow cannot open (CR3). None when it has none."""
    try:
        result = run_tool(
            [
                str(exiftool),
                "-s3",
                "-d",
                "%Y:%m:%d %H:%M:%S",
                "-DateTimeOriginal",
                "-CreateDate",
                str(path),
            ],
            timeout=timeout,
        )
    except (OSError, ValueError) as exc:
        log.debug("ExifTool failed on %s: %s", path, exc)
        return None
    for line in result.stdout.splitlines():
        parsed = parse_exif_datetime(line)
        if parsed:
            return parsed
    return None


def capture_time(
    path: Path,
    *,
    exiftool: Path | None = None,
    min_year: int = DEFAULT_MIN_YEAR,
    now: datetime | None = None,
) -> datetime | None:
    """The plausible capture time of a photo: EXIF first, then ExifTool when it is available.

    Returns None when neither gives a plausible date. The caller decides what that means.
    """
    ensure_on_device(path)  # ExifTool reads the file too, so cloud-only files stay untouched
    info = read_exif(path)
    if is_plausible_date(info.capture_time, min_year=min_year, now=now):
        return info.capture_time
    if exiftool is not None:
        fallback = exiftool_capture_time(exiftool, path)
        if is_plausible_date(fallback, min_year=min_year, now=now):
            return fallback
    return None
