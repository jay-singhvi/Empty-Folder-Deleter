"""The date a camera or a messaging app wrote into a photo's or video's file name.

Pure: nothing here opens a file or touches the disk, so it is safe for cloud-only files too.

A file's embedded date (EXIF, a video's creation time) changes when the file is edited,
re-encoded or exported, and its modified time changes on a copy. The name the camera or
WhatsApp gave it keeps the original date, so organize_by_date may try the name first.

Recognised (the first date in the name wins; digits that belong to a longer number are never
used, and the date must be a real calendar date between ``min_year`` and tomorrow):

* compact, as cameras write it: ``20220624_005115.mp4`` (Samsung), ``IMG_20220624_005115.jpg``,
  ``VID_…``, ``MVIMG_…``, ``PXL_20220624_005115123.jpg`` (Pixel, with milliseconds),
  ``DJI_20220624005115_0001_D.JPG`` (DJI), ``Screenshot_20220624-005115.png``, and WhatsApp's
  ``IMG-20220624-WA0001.jpg`` / ``VID-…`` (a date without a time);
* dashed: ``2022-06-24 00.51.15.jpg``, ``photo_2022-06-24_00-51-15.jpg`` (Telegram),
  ``signal-2022-06-24-005115.jpg``, ``WhatsApp Image 2022-06-24 at 00.51.15.jpeg`` and
  ``Screenshot 2022-06-24 at 1.05.09 PM.png`` (a 12-hour clock).

A time is used only when it is a real time of day; otherwise the date alone (midnight).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PureWindowsPath

_COMPACT = re.compile(
    r"(?<!\d)(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})"
    r"(?:[_\- T]?(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})\d{0,3})?"
    r"(?!\d)"
)
_DASHED = re.compile(
    r"(?<!\d)(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
    r"(?:"
    r"(?:[ _T]|[ _]at[ _]|-)(?P<H>\d{1,2})[.:\-](?P<M>\d{2})[.:\-](?P<S>\d{2})"
    r"(?:[ _]?(?P<ap>[AaPp][Mm]))?"
    r"|-(?P<H2>\d{2})(?P<M2>\d{2})(?P<S2>\d{2})"
    r")?"
    r"(?!\d)"
)


@dataclass(frozen=True)
class NameDate:
    """A date found in a file name. ``has_time`` is False when the name holds only a date."""

    when: datetime
    has_time: bool


def _time_of_day(match: re.Match[str]) -> tuple[int, int, int] | None:
    groups = match.groupdict()
    hour_text = groups.get("H") or groups.get("H2")
    if hour_text is None:
        return None
    hour = int(hour_text)
    minute = int(groups.get("M") or groups.get("M2") or "")
    second = int(groups.get("S") or groups.get("S2") or "")
    ampm = (groups.get("ap") or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour, minute, second


def date_from_name(name: str, *, min_year: int, now: datetime) -> NameDate | None:
    """The first plausible date in the file name of ``name`` (a folder part is ignored)."""
    base = PureWindowsPath(name).name
    matches = sorted(
        (m for regex in (_COMPACT, _DASHED) for m in regex.finditer(base)),
        key=lambda m: m.start(),
    )
    latest = now + timedelta(days=1)
    for match in matches:
        try:
            day = datetime(int(match["y"]), int(match["m"]), int(match["d"]))
        except ValueError:
            continue
        if day.year < min_year or day > latest:
            continue
        clock = _time_of_day(match)
        if clock is None:
            return NameDate(day, False)
        return NameDate(day.replace(hour=clock[0], minute=clock[1], second=clock[2]), True)
    return None
