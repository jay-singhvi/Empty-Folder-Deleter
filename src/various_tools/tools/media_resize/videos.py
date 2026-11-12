"""Video resizing for media_resize: the ffmpeg arguments and the size estimate (plan §3.5).

The encode follows app.py: scale to the target, H.264 at the chosen CRF, audio copied without
re-encoding, and the metadata kept. The output is written to a temporary file and renamed, so a
failed encode leaves no partial file (see ``core/media/ffmpeg.transcode``).

Sizes for videos are estimates until the file is written. app.py's dry-run formula is kept.
"""

from __future__ import annotations

import os
from pathlib import Path

from various_tools.core.media import ffmpeg as ffmpeg_media
from various_tools.tools.media_resize.geometry import Target

VIDEO_EXTS = frozenset({".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm", ".m4v"})
# Only MP4-family containers take the movflags option app.py passes to every file.
MP4_FAMILY = frozenset({".mp4", ".mov", ".m4v"})
PRESET = "medium"
# app.py's dry-run compression factor, which the estimate keeps.
ESTIMATE_COMPRESSION = 0.8


def estimate_bytes(
    original_bytes: int, width: int, height: int, target_width: int, target_height: int
) -> int:
    """app.py's dry-run estimate: the pixel ratio times a fixed compression factor."""
    pixel_ratio = (target_width * target_height) / (width * height)
    return int(original_bytes * pixel_ratio * ESTIMATE_COMPRESSION)


def ffmpeg_args(target: Target, crf: int, keep_metadata: bool, suffix: str) -> list[str]:
    """The output arguments for one video, in app.py's order."""
    args = [
        "-vf",
        f"scale={target.width}:{target.height}",
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        PRESET,
        "-c:a",
        "copy",
    ]
    if keep_metadata:
        args += ["-map_metadata", "0"]
        if suffix.lower() in MP4_FAMILY:
            args += ["-movflags", "use_metadata_tags"]
    return args


def transcode_resized(
    ffmpeg: Path,
    source: Path,
    destination: Path,
    target: Target,
    *,
    crf: int,
    keep_metadata: bool,
) -> None:
    """Encode ``source`` to ``target`` size at ``destination``. Raises ``MediaError`` on failure.

    The caller checks that ``destination`` is free. ffmpeg applies the stored rotation first, so
    the target is given in the size the viewer sees.
    """
    args = ffmpeg_args(target, crf, keep_metadata, destination.suffix)
    ffmpeg_media.transcode(ffmpeg, source, destination, args)
    stat = os.stat(source)
    os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))
