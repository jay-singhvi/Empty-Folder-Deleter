"""The checks behind the corrupt media finder: does this picture or video open and decode?

Each check returns a :class:`Verdict`. "corrupt" means the file itself is damaged or is not what
its name says. "skipped" means the check could not be made (a timeout, a permission error, a
cloud-only file), which says nothing about the file, so such files are never offered for removal.

Pictures use Pillow (HEIC through pillow-heif, RAW through ``core.media.imaging``). Videos use
ffprobe and, for the deep check, ffmpeg. Both programs are passed in as resolved paths from
``RunContext.deps``; this module never looks anything up. Every check refuses a cloud-only file
before opening it (AGENTS.md rule 3).
"""

from __future__ import annotations

import json
import subprocess
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageFile

from various_tools.core.deps import run_tool
from various_tools.core.jsonutil import as_dict, as_list
from various_tools.core.media.imaging import is_raw, open_image
from various_tools.core.onedrive import CloudOnlyFileError, ensure_on_device

CheckDepth = Literal["quick", "standard", "deep"]
MAX_DETAIL_CHARS = 200


@dataclass(frozen=True)
class Verdict:
    """``status`` is ``ok``, ``corrupt`` or ``skipped``; ``detail`` says why in plain English."""

    status: Literal["ok", "corrupt", "skipped"]
    detail: str = ""


OK = Verdict("ok")


def _short(text: str, path: Path | None = None) -> str:
    """First line of an error message, without the file path, limited in length."""
    line = (text.strip().splitlines() or [""])[0]
    if path is not None:
        line = line.replace(str(path), path.name)
    return line[:MAX_DETAIL_CHARS].strip()


@contextmanager
def strict_pillow() -> Iterator[None]:
    """Hold Pillow to strict decoding for the whole run, and keep its warnings quiet.

    ``ImageFile.LOAD_TRUNCATED_IMAGES`` is a process-wide switch that makes Pillow accept cut-off
    files. A truncated picture is corrupt, so the switch is forced off here and put back
    afterwards, whatever another part of the program set it to.
    """
    previous = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with warnings.catch_warnings():
            # Warnings such as "possibly corrupt EXIF data" or a huge image are not decode errors.
            warnings.simplefilter("ignore")
            yield
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous


def check_image(path: Path, depth: CheckDepth) -> Verdict:
    """Open a picture and, unless ``depth`` is ``quick``, decode every pixel.

    quick: open and ``verify()`` (header and, for PNG, the chunk checksums). standard and deep:
    also reopen and ``load()`` the whole image, which is what finds a truncated JPEG, because
    ``verify()`` alone does not. RAW files are always fully decoded. Call inside
    :func:`strict_pillow`.
    """
    try:
        ensure_on_device(path)
        if is_raw(path):
            with open_image(path):
                pass
            return OK
        with Image.open(path) as image:
            image.verify()
        if depth != "quick":
            with Image.open(path) as image:  # verify() leaves the image unusable: reopen
                image.load()
    except CloudOnlyFileError:
        return Verdict("skipped", "cloud-only")
    except Image.DecompressionBombError:
        return Verdict("skipped", "larger than the image safety limit, so it was not decoded")
    except (PermissionError, FileNotFoundError) as exc:
        return Verdict("skipped", f"could not be read: {exc.strerror or exc}")
    except MemoryError:
        return Verdict("skipped", "not enough memory to decode it, so it could not be checked")
    except Image.UnidentifiedImageError:
        return Verdict("corrupt", "not a readable picture (cannot identify the image format)")
    except Exception as exc:  # noqa: BLE001 - every decoder raises its own error types
        if is_system_error(exc):
            return Verdict("skipped", f"could not be read: {_short(_os_message(exc), path)}")
        message = _short(str(exc), path) or type(exc).__name__
        return Verdict("corrupt", f"picture does not decode: {message}")
    return OK


def is_system_error(exc: BaseException) -> bool:
    """True for an ``OSError`` raised by the system (it carries an ``errno`` or a ``winerror``):
    a network glitch, a sharing violation, WinError 1450 and the like. That says nothing about
    the file, so the check is skipped. Pillow's own decode errors (``image file is truncated``,
    ``broken data stream``) are ``OSError`` without either, and stay corrupt."""
    if not isinstance(exc, OSError):
        return False
    return exc.errno is not None or getattr(exc, "winerror", None) is not None


def _os_message(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or type(exc).__name__


def _duration(data: dict[str, Any], video: dict[str, Any]) -> float | None:
    """The container duration, else the video stream's, or None when neither is a number."""
    for source in (as_dict(data.get("format")), video):
        try:
            return float(str(source["duration"]))
        except KeyError, ValueError, OverflowError:
            continue
    return None


def check_video_probe(
    ffprobe: Path, path: Path, *, timeout: float, require_duration: bool
) -> Verdict:
    """Ask ffprobe about a video. Corrupt when it cannot read the file, finds no video stream,
    or reports a zero duration (a missing duration too, when ``require_duration``)."""
    try:
        ensure_on_device(path)
        result = run_tool(
            [
                str(ffprobe),
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            timeout=timeout,
        )
    except CloudOnlyFileError:
        return Verdict("skipped", "cloud-only")
    except subprocess.TimeoutExpired:
        return Verdict("skipped", f"ffprobe took longer than {timeout:g} s")
    except (OSError, ValueError) as exc:
        return Verdict("skipped", f"ffprobe could not run: {exc}")
    if result.returncode != 0:
        message = _short(result.stderr, path) or f"exit code {result.returncode}"
        return Verdict("corrupt", f"ffprobe cannot read the video: {message}")
    try:
        data = as_dict(json.loads(result.stdout or "{}"))
    except json.JSONDecodeError:
        return Verdict("corrupt", "ffprobe gave no usable details for the video")
    videos = [
        as_dict(s) for s in as_list(data.get("streams")) if as_dict(s).get("codec_type") == "video"
    ]
    if not videos:
        return Verdict("corrupt", "the file has no video stream")
    duration = _duration(data, videos[0])
    if duration is None:
        if require_duration:
            return Verdict("corrupt", "the video reports no duration")
    elif duration <= 0:
        return Verdict("corrupt", "the video has zero duration")
    return OK


def check_video_decode(ffmpeg: Path, path: Path, *, timeout: float) -> Verdict:
    """Decode a whole video with ffmpeg and throw the frames away (``-f null``).

    Any error ffmpeg prints (it is run at ``-v error``, so only errors) or a non-zero exit means
    the video does not decode cleanly. Slow: it reads the entire file.
    """
    try:
        ensure_on_device(path)
        result = run_tool(
            [
                str(ffmpeg),
                "-v",
                "error",
                "-nostdin",
                "-xerror",
                "-i",
                str(path),
                "-f",
                "null",
                "-",
            ],
            timeout=timeout,
        )
    except CloudOnlyFileError:
        return Verdict("skipped", "cloud-only")
    except subprocess.TimeoutExpired:
        return Verdict("skipped", f"decoding took longer than {timeout:g} s")
    except (OSError, ValueError) as exc:
        return Verdict("skipped", f"ffmpeg could not run: {exc}")
    errors = result.stderr.strip()
    if result.returncode != 0 or errors:
        message = _short(errors, path) or f"exit code {result.returncode}"
        return Verdict("corrupt", f"the video does not decode cleanly: {message}")
    return OK
