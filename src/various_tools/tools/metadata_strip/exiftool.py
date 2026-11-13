"""ExifTool calls for metadata_strip (and media_convert): run it safely, read tags, build arguments.

The program is passed in as a resolved path (``RunContext.deps.exiftool()``). This module never
looks it up.

Two Windows facts shape :func:`run_exiftool`. ExifTool reads its command line in the ANSI code
page, so a file name with letters outside it (accents, Chinese, emoji) cannot be passed as an
argument. Every call therefore passes its arguments through a UTF-8 argument file together with
``-charset filename=utf8``, which works for any name. The argument file lives in the system temp
folder, never in a folder the user chose.

Which tags count as removable metadata is decided here, once, so that the preview (``scan``), the
ExifTool arguments (``apply``) and the check of the written copy all agree.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from various_tools.core.deps import run_tool
from various_tools.core.jsonutil import as_dict, as_list

Scope = Literal["gps", "all"]

# ExifTool groups that describe the file itself, not who made it or where. Never removable.
STRUCTURAL_GROUPS = frozenset(
    {"System", "File", "ExifTool", "Composite", "JFIF", "RIFF", "QuickTime", "Meta", "Ducky"}
)
_TRACK_GROUP = re.compile(r"^(Track|Media)\d*$")
# PNG header fields and display chunks. Text chunks (Software, Comment, Author, ...) are removable.
PNG_STRUCTURAL = frozenset(
    {
        "ImageWidth",
        "ImageHeight",
        "BitDepth",
        "ColorType",
        "Compression",
        "Filter",
        "Interlace",
        "Gamma",
        "SRGBRendering",
        "WhitePoint",
        "PrimaryChromaticities",
        "Palette",
        "Transparency",
        "BackgroundColor",
        "SignificantBits",
    }
)
# ExifTool writes this one itself whenever it creates an EXIF block, so it is not "metadata".
ALWAYS_PRESENT = frozenset({("IFD0", "YCbCrPositioning")})
# Location tags in video containers that do not start with "GPS" but hold a position.
_VIDEO_LOCATION_GROUPS = frozenset({"Keys", "UserData"})
_VIDEO_LOCATION_NAMES = frozenset({"Location", "LocationInformation"})
_ICC_GROUP_PREFIX = "ICC"
_DUPLICATE_SUFFIX = re.compile(r"\s\(\d+\)$")

_OWNER_NAMES = frozenset(
    {"ownername", "cameraownername", "artist", "copyright", "creator", "author", "by-line"}
)


def run_exiftool(
    exiftool: Path, args: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ExifTool with ``args`` (UTF-8, one per line) so any Windows file name works.

    Can raise ``subprocess.TimeoutExpired`` or ``OSError``, like :func:`run_tool`.
    """
    fd, name = tempfile.mkstemp(prefix="various-tools-exiftool-", suffix=".args")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(args) + "\n")
        return run_tool([str(exiftool), "-charset", "filename=utf8", "-@", name], timeout=timeout)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


@dataclass(frozen=True)
class TagRead:
    """The tags of one file as ExifTool listed them, or why it could not."""

    tags: dict[str, Any]
    error: str | None = None


def _split(key: str) -> tuple[str, str] | None:
    """``("Group", "Name")`` from an ExifTool ``-G1`` key such as ``GPS:GPSLatitude``."""
    key = _DUPLICATE_SUFFIX.sub("", key)
    group, sep, name = key.partition(":")
    return (group, name) if sep and name else None


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def read_tags(exiftool: Path, paths: Sequence[Path], timeout: float) -> dict[Path, TagRead]:
    """Read the tags of ``paths`` in one ExifTool call. A file it cannot read has an ``error``.

    The caller must have checked that every file is on the device (AGENTS.md rule 3). Raises
    ``subprocess.TimeoutExpired`` or ``OSError`` when ExifTool itself cannot run.
    """
    if not paths:
        return {}
    result = run_exiftool(exiftool, ["-j", "-G1", "-a", "-s", *[str(p) for p in paths]], timeout)
    try:
        rows = [as_dict(row) for row in as_list(json.loads(result.stdout or "[]"))]
    except json.JSONDecodeError:
        rows = []
    by_source = {_norm(str(row.get("SourceFile", ""))): row for row in rows}
    found: dict[Path, TagRead] = {}
    for path in paths:
        row = by_source.get(_norm(path))
        if row is None:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            found[path] = TagRead({}, detail[0][:200] if detail else "ExifTool gave no result")
            continue
        fatal = row.get("ExifTool:Error")
        found[path] = TagRead(
            {k: v for k, v in row.items() if k != "SourceFile"},
            str(fatal) if fatal else None,
        )
    return found


def _is_gps(group: str, name: str) -> bool:
    if group in {"Composite", "System", "File", "ExifTool"}:
        return False
    if name.upper().startswith("GPS"):
        return True
    return group in _VIDEO_LOCATION_GROUPS and name in _VIDEO_LOCATION_NAMES


def _is_color_profile(group: str, name: str) -> bool:
    return group.startswith(_ICC_GROUP_PREFIX) or (group == "PNG" and name == "ProfileName")


def _is_metadata(group: str, name: str) -> bool:
    """True for a tag that is personal or descriptive metadata, not file structure."""
    if group in STRUCTURAL_GROUPS or _TRACK_GROUP.match(group):
        return False
    if (group, name) in ALWAYS_PRESENT:
        return False
    return not (group == "PNG" and name in PNG_STRUCTURAL)


def removable_tags(
    tags: Mapping[str, Any],
    scope: Scope,
    *,
    keep_orientation: bool = True,
    keep_color_profile: bool = True,
) -> list[str]:
    """The ``Group:Name`` tags that stripping with ``scope`` removes, in file order, no repeats."""
    found: list[str] = []
    for key in tags:
        parts = _split(key)
        if parts is None:
            continue
        group, name = parts
        if scope == "gps":
            hit = _is_gps(group, name)
        else:
            hit = _is_gps(group, name) or _is_metadata(group, name)
            if hit and keep_orientation and group == "IFD0" and name == "Orientation":
                hit = False
            if hit and keep_color_profile and _is_color_profile(group, name):
                hit = False
        label = f"{group}:{name}"
        if hit and label not in found:
            found.append(label)
    return found


_GPS_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("position", ("latitude", "longitude", "coordinates", "position", "location", "map")),
    ("altitude", ("altitude",)),
    ("time", ("timestamp", "datestamp", "datetime")),
    ("direction and speed", ("speed", "track", "direction", "bearing", "distance")),
)


def _gps_summary(labels: Sequence[str]) -> list[str]:
    kinds: list[str] = []
    for label in labels:
        name = label.partition(":")[2].lower()
        if not (name.startswith("gps") or name in {"location", "locationinformation"}):
            continue
        kind = next((k for k, words in _GPS_KINDS if any(w in name for w in words)), "other")
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def describe_removal(labels: Sequence[str], scope: Scope) -> str:
    """Plain-English reason for the plan, e.g. ``remove GPS: position, altitude (4 tags)``."""
    count = len(labels)
    noun = "tag" if count == 1 else "tags"
    gps = _gps_summary(labels)
    if scope == "gps":
        return f"remove GPS: {', '.join(gps) or 'location'} ({count} {noun})"
    names = [label.partition(":")[2].lower() for label in labels]
    notes: list[str] = []
    if gps:
        notes.append("GPS")
    if any("serialnumber" in n for n in names):
        notes.append("camera serial")
    if any(n in _OWNER_NAMES for n in names):
        notes.append("owner name")
    if any(n in {"make", "model"} for n in names):
        notes.append("camera make and model")
    if any("thumbnail" in n for n in names):
        notes.append("thumbnail")
    tail = f" incl. {', '.join(notes)}" if notes else ""
    return f"remove all: {count} {noun}{tail}"


def strip_args(
    scope: Scope,
    *,
    keep_orientation: bool = True,
    keep_color_profile: bool = True,
    ignore_minor_errors: bool = False,
    video: bool = False,
) -> list[str]:
    """ExifTool arguments that remove the tags :func:`removable_tags` lists.

    The caller adds ``-o <new file>`` and the source, so the source is never changed in place.
    """
    args: list[str] = []
    if ignore_minor_errors:
        args.append("-m")
    if scope == "gps":
        args += ["-gps:all=", "-gps*="]
        if video:
            args += ["-Keys:Location=", "-UserData:LocationInformation="]
        return args
    args.append("-all=")
    if not video:
        keep: list[str] = []
        if keep_orientation:
            keep.append("-Orientation")
        if keep_color_profile:
            keep.append("-icc_profile")
        if keep:
            args += ["-tagsfromfile", "@", *keep]
    return args
