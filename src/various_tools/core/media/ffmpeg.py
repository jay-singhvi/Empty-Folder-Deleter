"""ffmpeg and ffprobe wrappers: probe, duration, display size, creation time, frame grab, transcode.

Ported from find_duplicate_videos (probe, grab_frame, grab_size), app.py (rotation handling) and
metadata-8 (``get_video_date_taken``).

The binaries are passed in as resolved paths (``RunContext.deps``). This module never looks them
up. Every function that reads a file checks OneDrive state first.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from various_tools.core.deps import run_binary, run_tool
from various_tools.core.fs import rename_no_replace
from various_tools.core.jsonutil import as_dict, as_list
from various_tools.core.onedrive import ensure_on_device
from various_tools.core.settings import (
    DEFAULT_FFPROBE_TIMEOUT_S,
    DEFAULT_FRAME_TIMEOUT_S,
    DEFAULT_TRANSCODE_TIMEOUT_S,
)

log = logging.getLogger(__name__)

# Defaults only. Tools pass the user's values from settings (``ffprobe_timeout_s`` and so on).
PROBE_TIMEOUT_S: float = DEFAULT_FFPROBE_TIMEOUT_S
FRAME_TIMEOUT_S: float = DEFAULT_FRAME_TIMEOUT_S
TRANSCODE_TIMEOUT_S: float = DEFAULT_TRANSCODE_TIMEOUT_S
# How a frame grab steps back when the video track ends before the container (legacy values).
FRAME_RETRIES = 6
FRAME_STEP_BACK = 0.8


class MediaError(RuntimeError):
    """An ffmpeg or ffprobe call failed, or its output could not be used."""


def ffprobe_json(
    ffprobe: Path,
    path: Path,
    entries: list[str] | None = None,
    *,
    timeout: float = PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """Run ffprobe and return its JSON. ``entries`` are passed to ``-show_entries`` when given."""
    ensure_on_device(path)
    args = [str(ffprobe), "-v", "error", "-print_format", "json"]
    if entries is None:
        args += ["-show_format", "-show_streams", "-select_streams", "v:0"]
    else:
        args += ["-show_entries", ":".join(entries)]
    args.append(str(path))
    try:
        result = run_tool(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        log.warning("ffprobe timed out on %s", path)
        raise MediaError(f"ffprobe timed out on {path.name}") from exc
    except (OSError, ValueError) as exc:
        raise MediaError(f"ffprobe could not run on {path.name}: {exc}") from exc
    if result.returncode != 0:
        raise MediaError(f"ffprobe failed for {path.name}: {result.stderr.strip()[:300]}")
    try:
        return as_dict(json.loads(result.stdout or "{}"))
    except json.JSONDecodeError as exc:
        log.warning("ffprobe returned invalid JSON for %s", path)
        raise MediaError(f"ffprobe returned invalid JSON for {path.name}") from exc


def probe_duration(ffprobe: Path, path: Path, *, timeout: float = PROBE_TIMEOUT_S) -> float:
    """Container duration in seconds."""
    data = ffprobe_json(ffprobe, path, timeout=timeout)
    try:
        return float(as_dict(data.get("format"))["duration"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise MediaError(f"no duration for {path.name}") from exc


def _rotation_degrees(stream: dict[str, Any]) -> int:
    """Rotation from side data or the ``rotate`` tag, normalised to 0-359."""
    for side in as_list(stream.get("side_data_list")):
        side_map = as_dict(side)
        if "rotation" in side_map:
            try:
                return int(round(float(side_map["rotation"]))) % 360
            except TypeError, ValueError, OverflowError:
                continue
    tags = as_dict(stream.get("tags"))
    if "rotate" in tags:
        try:
            return int(round(float(tags["rotate"]))) % 360
        except TypeError, ValueError, OverflowError:
            return 0
    return 0


def display_size(ffprobe: Path, path: Path, *, timeout: float = PROBE_TIMEOUT_S) -> tuple[int, int]:
    """Width and height as the viewer shows them: swapped for a 90 or 270 degree rotation."""
    streams = as_list(ffprobe_json(ffprobe, path, timeout=timeout).get("streams"))
    if not streams:
        raise MediaError(f"no video stream in {path.name}")
    stream = as_dict(streams[0])
    try:
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaError(f"no usable video size in {path.name}") from exc
    if _rotation_degrees(stream) in (90, 270):
        width, height = height, width
    return width, height


def creation_time(
    ffprobe: Path, path: Path, *, timeout: float = PROBE_TIMEOUT_S
) -> datetime | None:
    """The container's ``creation_time`` as a naive local datetime, or None when missing.

    This is the video equivalent of EXIF DateTimeOriginal. Failures give None rather than an
    exception, as in the legacy script.
    """
    try:
        data = ffprobe_json(
            ffprobe,
            path,
            entries=["format_tags=creation_time", "stream_tags=creation_time"],
            timeout=timeout,
        )
    except MediaError as exc:
        log.warning("No creation time for %s: %s", path, exc)
        return None
    raw: object = as_dict(as_dict(data.get("format")).get("tags")).get("creation_time")
    if not raw:
        for stream in as_list(data.get("streams")):
            raw = as_dict(as_dict(stream).get("tags")).get("creation_time")
            if raw:
                break
    if not raw:
        return None
    try:
        # Usually UTC ISO 8601, e.g. "2026-03-15T14:30:22.000000Z"
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        return parsed.astimezone().replace(tzinfo=None)
    except OverflowError, OSError, ValueError:
        return None


def grab_frame(
    ffmpeg: Path,
    path: Path,
    seconds: float,
    width: int = 320,
    *,
    timeout: float = FRAME_TIMEOUT_S,
    retries: int = FRAME_RETRIES,
    step_back: float = FRAME_STEP_BACK,
) -> Image.Image:
    """One RGB frame near ``seconds``, scaled to ``width``.

    The video track can end a little before the audio, so an empty result steps back in time
    (up to ``retries`` times, each step x ``step_back``), as in find_duplicate_videos.
    """
    ensure_on_device(path)
    t = seconds
    for _ in range(max(1, retries)):
        try:
            result = _run_ffmpeg_frame(ffmpeg, path, t, width, timeout)
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"ffmpeg timed out grabbing a frame from {path.name}") from exc
        except (OSError, ValueError) as exc:
            raise MediaError(f"ffmpeg could not run on {path.name}: {exc}") from exc
        if result.returncode != 0:
            raise MediaError(f"ffmpeg failed to grab a frame from {path.name}")
        if result.stdout:
            try:
                with Image.open(io.BytesIO(result.stdout)) as img:
                    return img.convert("RGB")
            except (OSError, ValueError) as exc:
                raise MediaError(f"ffmpeg gave an unreadable frame for {path.name}") from exc
        t *= step_back
    raise MediaError(f"no video frame near {seconds:.2f}s in {path.name}")


def _run_ffmpeg_frame(
    ffmpeg: Path, path: Path, t: float, width: int, timeout: float = FRAME_TIMEOUT_S
) -> subprocess.CompletedProcess[bytes]:
    return run_binary(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-nostdin",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        timeout=timeout,
    )


def transcode(
    ffmpeg: Path,
    source: Path,
    destination: Path,
    output_args: list[str],
    *,
    timeout: float = TRANSCODE_TIMEOUT_S,
) -> None:
    """Encode ``source`` to ``destination`` with ``output_args`` (e.g. ``["-c:v", "libx264"]``).

    Writes to a temporary file in the destination folder and renames it at the end, so a failed
    encode leaves no partial output.
    """
    ensure_on_device(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=destination.suffix or ".mp4")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        try:
            result = _run_transcode(ffmpeg, source, tmp_path, output_args, timeout)
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"transcode timed out for {source.name}") from exc
        if result.returncode != 0:
            raise MediaError(f"transcode failed for {source.name}: {result.stderr.strip()[:300]}")
        rename_no_replace(tmp_path, destination)
    finally:
        tmp_path.unlink(missing_ok=True)


def _run_transcode(
    ffmpeg: Path,
    source: Path,
    tmp_path: Path,
    output_args: list[str],
    timeout: float = TRANSCODE_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    return run_tool(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            *output_args,
            str(tmp_path),
        ],
        timeout=timeout,
    )
