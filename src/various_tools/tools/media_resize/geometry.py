"""Target sizes for media_resize (plan §3.5). Pure functions: no files, no Pillow, no ffmpeg.

The rules are ported from the legacy scripts, and the formulas are kept as written:

* ``fit_box`` is app.py (legacy/app.py lines 252-278): a file is resized only when BOTH of its
  dimensions are larger than the box, and it is scaled by the smaller of the two factors, so the
  result fits inside the box. Integer parts are truncated, as the legacy code does.
* ``shorter_side`` is metadata-8 (legacy/python-extract_file_metadata-8.py lines 740-760): scale
  until the shorter side equals the target, rounding each side.
* ``longer_side`` is the same rule applied to the longer side.
* ``percent`` is app.py's percentage mode (lines 244-247).

Files are never enlarged (🔒 never_upscale). Every mode returns a target at or below the original
size, and ``plan_target`` checks this again, so a future rule cannot enlarge by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ResizeMode = Literal["fit_box", "shorter_side", "longer_side", "percent"]
SizePreset = Literal["360p", "480p", "720p", "1080p", "2k", "4k", "custom"]
VideoQuality = Literal["high", "medium", "low"]

# app.py's RESOLUTION_PRESETS, without the "Custom" entry (which is the custom box below).
PRESET_BOXES: dict[str, tuple[int, int]] = {
    "360p": (640, 360),
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "2k": (2560, 1440),
    "4k": (3840, 2160),
}

# app.py's "Video quality preset" choices and their CRF values.
CRF_BY_QUALITY: dict[str, int] = {"high": 18, "medium": 23, "low": 28}

# The skip reasons are app.py's wording, so the two tools read the same.
ALREADY_SMALLER = "already smaller than the target resolution"
ONE_SIDE_ONLY = "only one dimension is larger than the target (both must be)"
ALREADY_AT_TARGET = "already at the target size"
WOULD_ENLARGE = "the target would enlarge the file, and files are never enlarged"


@dataclass(frozen=True)
class Target:
    width: int
    height: int


def fit_box(width: int, height: int, box_width: int, box_height: int) -> Target | str:
    """app.py's rule. Returns the target size, or the reason the file is left alone."""
    if width > box_width and height > box_height:
        scale = min(box_width / width, box_height / height)
        return Target(max(1, int(width * scale)), max(1, int(height * scale)))
    if width <= box_width and height <= box_height:
        return ALREADY_SMALLER
    return ONE_SIDE_ONLY


def shorter_side(width: int, height: int, target: int) -> Target | str:
    """metadata-8's rule: the shorter side becomes ``target``."""
    short = min(width, height)
    if short <= target:
        return ALREADY_SMALLER
    scale = target / short
    return Target(max(1, round(width * scale)), max(1, round(height * scale)))


def longer_side(width: int, height: int, target: int) -> Target | str:
    """The same rule, for the longer side."""
    long_edge = max(width, height)
    if long_edge <= target:
        return ALREADY_SMALLER
    scale = target / long_edge
    return Target(max(1, round(width * scale)), max(1, round(height * scale)))


def percent(width: int, height: int, amount: int) -> Target | str:
    """app.py's percentage rule: each side is cut to ``amount`` percent (truncated)."""
    target = Target(max(1, int(width * amount / 100)), max(1, int(height * amount / 100)))
    if target == Target(width, height):
        return ALREADY_AT_TARGET
    return target


def even(value: int) -> int:
    """Video dimensions must be even for most codecs. app.py drops one pixel when odd."""
    return max(2, value - (value % 2))


def plan_target(
    mode: ResizeMode,
    width: int,
    height: int,
    *,
    box: tuple[int, int] | None,
    target_px: int,
    amount: int,
) -> Target | str:
    """The target for one file, or the reason to leave it alone. Never enlarges.

    ``box`` is used by ``fit_box``, ``target_px`` by the side modes and ``amount`` by ``percent``.
    """
    result: Target | str
    if mode == "fit_box":
        if box is None:
            return "no size box is set"
        result = fit_box(width, height, box[0], box[1])
    elif mode == "shorter_side":
        result = shorter_side(width, height, target_px)
    elif mode == "longer_side":
        result = longer_side(width, height, target_px)
    else:
        result = percent(width, height, amount)
    if isinstance(result, str):
        return result
    if result.width > width or result.height > height:
        return WOULD_ENLARGE
    return result


def box_for(
    size: SizePreset, max_width: int | None, max_height: int | None
) -> tuple[int, int] | None:
    """The fit box for a preset, or the custom box. None when the custom box is incomplete."""
    if size == "custom":
        if max_width is None or max_height is None:
            return None
        return (max_width, max_height)
    return PRESET_BOXES[size]
