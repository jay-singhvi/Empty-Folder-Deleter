"""Capture-date rules for organize_by_date (the legacy metadata-8 rules 1-2).

The caller must have checked that the file is on this device (AGENTS.md rule 3). Nothing here
hydrates a file or writes to disk.

The default order is EXIF, then the video's embedded creation time, then ExifTool (RAW files only,
as in the legacy script), then the modified time. A step is used only when its date is plausible.
A file with no usable date goes to the unknown-date folder. It is never moved on a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

from various_tools.core.media.exif import (
    ExifInfo,
    exiftool_capture_time,
    is_plausible_date,
    read_exif,
)
from various_tools.core.media.ffmpeg import creation_time

DateSource = Literal["exif", "video_tag", "exiftool", "modified"]
DATE_SOURCES: tuple[str, ...] = get_args(DateSource)
DEFAULT_PRIORITY: tuple[DateSource, ...] = ("exif", "video_tag", "exiftool", "modified")

LABELS: dict[str, str] = {
    "exif": "Date Taken (EXIF)",
    "video_tag": "Date Taken (video metadata)",
    "exiftool": "Date Taken (ExifTool)",
    "modified": "Date Modified (fallback)",
}
UNKNOWN_LABEL = "Date Modified (implausible - unknown date)"
NO_DATE_LABEL = "No usable date from the chosen sources"

# The legacy script's lists. CR3 is RAW: Pillow cannot read its EXIF, so ExifTool is used.
PHOTO_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp"}
)
RAW_EXTS = frozenset({".cr3"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv"})
PROCESSABLE_EXTS = PHOTO_EXTS | RAW_EXTS | VIDEO_EXTS


@dataclass(frozen=True)
class Reading:
    """The date a file goes by, and how it was found. ``date`` is None when no source gave one."""

    date: datetime | None
    label: str
    exif: ExifInfo | None


def read_capture_date(
    path: Path,
    *,
    modified: datetime,
    priority: tuple[str, ...],
    min_year: int,
    now: datetime,
    ffprobe: Path | None,
    exiftool: Path | None,
) -> Reading:
    """Work out the date of one file on this device, using the sources in ``priority`` order.

    ``modified`` comes from the directory listing, so the file is not opened for it. Photos are
    read for their EXIF even when EXIF is not first, because the catalog keeps the camera and the
    size.
    """
    suffix = path.suffix.lower()
    exif = read_exif(path) if suffix in PHOTO_EXTS else None

    def plausible(value: datetime | None) -> bool:
        return is_plausible_date(value, min_year=min_year, now=now)

    for step in priority:
        if step == "exif":
            if exif is not None and plausible(exif.capture_time):
                return Reading(exif.capture_time, LABELS[step], exif)
        elif step == "video_tag":
            if suffix in VIDEO_EXTS and ffprobe is not None:
                taken = creation_time(ffprobe, path)
                if plausible(taken):
                    return Reading(taken, LABELS[step], exif)
        elif step == "exiftool":
            if suffix in RAW_EXTS and exiftool is not None:
                taken = exiftool_capture_time(exiftool, path)
                if plausible(taken):
                    return Reading(taken, LABELS[step], exif)
        elif step == "modified" and plausible(modified):
            return Reading(modified, LABELS[step], exif)
    label = UNKNOWN_LABEL if "modified" in priority else NO_DATE_LABEL
    return Reading(None, label, exif)
