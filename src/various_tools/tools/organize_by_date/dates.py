"""Capture-date rules for organize_by_date (the legacy metadata-8 rules 1-2).

The caller must have checked that the file is on this device (AGENTS.md rule 3). Nothing here
hydrates a file or writes to disk.

With ``name_first`` (the tool's default since 0.9) the date the camera or WhatsApp wrote into the
file name is tried first: an embedded date changes when a file is edited or re-encoded, the name
does not. Then the default order is EXIF, then the video's embedded creation time, then ExifTool
(RAW files only, as in the legacy script), then the modified time. A step is used only when its
date is plausible. A file with no usable date goes to the unknown-date folder. It is never moved
on a guess.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

from various_tools.core.media.exif import (
    ExifInfo,
    exiftool_capture_time,
    is_plausible_date,
    read_exif,
)
from various_tools.core.media.ffmpeg import creation_time
from various_tools.core.media.name_dates import date_from_name
from various_tools.core.onedrive import CloudOnlyFileError
from various_tools.core.settings import (
    DEFAULT_EXIFTOOL_READ_TIMEOUT_S,
    DEFAULT_FFPROBE_TIMEOUT_S,
)

log = logging.getLogger(__name__)

DateSource = Literal["exif", "video_tag", "exiftool", "modified"]
DATE_SOURCES: tuple[str, ...] = get_args(DateSource)
DEFAULT_PRIORITY: tuple[DateSource, ...] = ("exif", "video_tag", "exiftool", "modified")

UTC_SPREAD = timedelta(hours=14)  # the widest time zone offset from UTC
NAME_SOURCE = "name"  # not a ``date_priority`` entry: the ``name_date_first`` option
LABELS: dict[str, str] = {
    NAME_SOURCE: "Date in the file name",
    "exif": "Date Taken (EXIF)",
    "video_tag": "Date Taken (video metadata)",
    "exiftool": "Date Taken (ExifTool)",
    "modified": "Date Modified (fallback)",
}
UNKNOWN_LABEL = "Date Modified (implausible - unknown date)"
NO_DATE_LABEL = "No usable date from the chosen sources"

# The legacy script's lists, and the defaults of the tool's file-type options.
# CR3 is RAW: Pillow cannot read its EXIF, so ExifTool is used.
PHOTO_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp"}
)
RAW_EXTS = frozenset({".cr3"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv"})
PROCESSABLE_EXTS = PHOTO_EXTS | RAW_EXTS | VIDEO_EXTS


def _safe_date(label: str, path: Path, reader: Callable[[], datetime | None]) -> datetime | None:
    """Run one metadata reader. Any failure gives None, so the next source is tried.

    The legacy script catches everything here. A cloud-only file is the one exception: it is
    never read, so the error goes up to the caller, which reports it as skipped.
    """
    try:
        return reader()
    except CloudOnlyFileError:
        raise
    except Exception as exc:
        log.warning("%s could not be read for %s: %s", label, path, exc)
        return None


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
    photo_exts: frozenset[str] = PHOTO_EXTS,
    raw_exts: frozenset[str] = RAW_EXTS,
    video_exts: frozenset[str] = VIDEO_EXTS,
    ffprobe_timeout: float = DEFAULT_FFPROBE_TIMEOUT_S,
    exiftool_timeout: float = DEFAULT_EXIFTOOL_READ_TIMEOUT_S,
    name_first: bool = False,
) -> Reading:
    """Work out the date of one file on this device, using the sources in ``priority`` order.

    ``modified`` comes from the directory listing, so the file is not opened for it. Photos are
    read for their EXIF even when EXIF is not first, because the catalog keeps the camera and the
    size. The extension sets, and the timeouts for ffprobe and ExifTool, default to the legacy
    values and are passed by the tool from its options and the app settings.
    """
    suffix = path.suffix.lower()
    exif: ExifInfo | None = None
    if suffix in photo_exts:
        try:
            exif = read_exif(path)
        except CloudOnlyFileError:
            raise
        except Exception as exc:
            log.warning("EXIF could not be read for %s: %s", path, exc)

    def plausible(value: datetime | None) -> bool:
        return is_plausible_date(value, min_year=min_year, now=now)

    def by_metadata() -> Reading:
        for step in priority:
            if step == "exif":
                if exif is not None and plausible(exif.capture_time):
                    return Reading(exif.capture_time, LABELS[step], exif)
            elif step == "video_tag":
                if suffix in video_exts and ffprobe is not None:
                    probe: Path = ffprobe
                    taken = _safe_date(
                        "Video metadata",
                        path,
                        lambda probe=probe: creation_time(probe, path, timeout=ffprobe_timeout),
                    )
                    if plausible(taken):
                        return Reading(taken, LABELS[step], exif)
            elif step == "exiftool":
                if suffix in raw_exts and exiftool is not None:
                    reader: Path = exiftool
                    taken = _safe_date(
                        "ExifTool",
                        path,
                        lambda reader=reader: exiftool_capture_time(reader, path, exiftool_timeout),
                    )
                    if plausible(taken):
                        return Reading(taken, LABELS[step], exif)
            elif step == "modified" and plausible(modified):
                return Reading(modified, LABELS[step], exif)
        label = UNKNOWN_LABEL if "modified" in priority else NO_DATE_LABEL
        return Reading(None, label, exif)

    if name_first:
        named = date_from_name(path.name, min_year=min_year, now=now)
        if named is not None:
            by_name = Reading(named.when, LABELS[NAME_SOURCE], exif)
            if not named.utc:
                return by_name
            # A UTC time in the name (Pixel): an embedded local date that agrees with it within
            # a time zone is the right one; near midnight the name's day can be the next one.
            embedded = by_metadata()
            if (
                embedded.date is not None
                and embedded.label != LABELS["modified"]
                and abs(embedded.date - named.when) <= UTC_SPREAD
            ):
                return embedded
            return by_name
    return by_metadata()
