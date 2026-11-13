"""Image conversion for media_convert: decode, flatten alpha, encode, and write atomically.

HEIC and HEIF are opened through ``core.media.imaging.open_image``, which registers pillow-heif.
``inspect()`` only reads the header, so ``scan()`` stays cheap and writes nothing. ``render()``
does the work in memory. ``write()`` puts the bytes into place through a temporary file, adds the
tags Pillow cannot carry with ExifTool (when it is installed), checks the result and only then
renames it. ``verify_output()`` is the check that gates removing an original.
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageOps

from various_tools.core.fs import rename_no_replace
from various_tools.core.media.exif import run_exiftool
from various_tools.core.media.imaging import open_image
from various_tools.core.onedrive import ensure_on_device
from various_tools.core.settings import DEFAULT_EXIFTOOL_WRITE_TIMEOUT_S

ImageFormat = Literal["JPEG", "PNG"]

# File-format facts, not preferences: these stay in the code.
ORIENTATION_TAG = 0x0112  # the EXIF tag number for orientation
EXIF_IFD = 0x8769  # the EXIF sub-IFD that holds the stored pixel dimensions
EXIF_WIDTH_TAG = 0xA002
EXIF_HEIGHT_TAG = 0xA003
ALPHA_MODES = frozenset({"RGBA", "LA", "La", "PA", "RGBa"})
PNG_MODES = frozenset({"RGB", "RGBA", "L", "LA", "P", "1", "I;16"})


class ImageError(RuntimeError):
    """An image could not be rendered, written or verified. The message goes to the journal."""


@dataclass(frozen=True)
class ImageRules:
    fmt: ImageFormat
    jpeg_quality: int
    optimize: bool
    background: tuple[int, int, int]
    keep_metadata: bool


@dataclass(frozen=True)
class Inspected:
    """What the header of a source picture says, without decoding it."""

    size: tuple[int, int]
    has_alpha: bool


@dataclass(frozen=True)
class Rendered:
    """A converted picture, encoded in memory and ready to be written."""

    size: tuple[int, int]
    fmt: ImageFormat
    data: bytes
    flattened_alpha: bool


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ALPHA_MODES or "transparency" in image.info


def inspect(source: Path) -> Inspected | str:
    """Header facts about ``source``, or the reason it is left alone. Reads no pixels.

    Raises :class:`~various_tools.core.onedrive.CloudOnlyFileError` for a cloud-only file, before
    anything is opened (AGENTS.md rule 3).
    """
    ensure_on_device(source)
    with open_image(source) as image:
        if getattr(image, "is_animated", False):
            return "animated or multi-image file, left as-is (a single picture cannot hold it)"
        return Inspected(size=image.size, has_alpha=_has_alpha(image))


def _kept_exif(original: Image.Image, size: tuple[int, int]) -> bytes | None:
    """The EXIF block to keep. The orientation is reset, because the pixels are turned already,
    and the stored pixel size is corrected to match."""
    exif = original.getexif()
    if not exif:
        return None
    if exif.get(ORIENTATION_TAG):
        exif[ORIENTATION_TAG] = 1
    sub = exif.get_ifd(EXIF_IFD)
    if EXIF_WIDTH_TAG in sub:
        sub[EXIF_WIDTH_TAG], sub[EXIF_HEIGHT_TAG] = size
    return bytes(exif.tobytes())


def _kept_icc(original: Image.Image) -> bytes | None:
    value = original.info.get("icc_profile")
    return value if isinstance(value, bytes) and value else None


def _kept_dpi(original: Image.Image) -> tuple[float, float] | None:
    value: object = original.info.get("dpi")
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except TypeError, ValueError:
        return None


def _flatten(image: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """Composite onto a solid colour, so transparent areas do not turn black in a JPEG."""
    rgba = image.convert("RGBA")
    base = Image.new("RGBA", rgba.size, (*background, 255))
    return Image.alpha_composite(base, rgba).convert("RGB")


def _for_format(image: Image.Image, rules: ImageRules, alpha: bool) -> Image.Image:
    if rules.fmt == "JPEG":
        if alpha:
            return _flatten(image, rules.background)
        return image if image.mode in ("RGB", "L") else image.convert("RGB")
    if image.mode in PNG_MODES:
        return image
    return image.convert("RGBA" if alpha else "RGB")


def render(source: Path, rules: ImageRules) -> Rendered | str:
    """Convert ``source`` in memory. Returns the result, or the reason the file is left alone.

    The caller must have checked that the file is on the device (rule 3). This checks again,
    before anything is read, so a cloud-only file raises instead of being read.
    """
    ensure_on_device(source)
    with open_image(source) as original:
        if getattr(original, "is_animated", False):
            return "animated or multi-image file, left as-is (a single picture cannot hold it)"
        alpha = _has_alpha(original)
        # The pixels are turned first. Only then is the orientation reset in the kept EXIF,
        # because _kept_exif changes the EXIF object that exif_transpose reads.
        displayed = ImageOps.exif_transpose(original)
        displayed.load()
        exif = _kept_exif(original, displayed.size) if rules.keep_metadata else None
        icc = _kept_icc(original) if rules.keep_metadata else None
        dpi = _kept_dpi(original) if rules.keep_metadata else None
    image = _for_format(displayed, rules, alpha)
    buffer = io.BytesIO()
    image.save(buffer, format=rules.fmt, **_save_options(rules, exif, icc, dpi))
    return Rendered(
        size=displayed.size,
        fmt=rules.fmt,
        data=buffer.getvalue(),
        flattened_alpha=alpha and rules.fmt == "JPEG",
    )


def _save_options(
    rules: ImageRules,
    exif: bytes | None,
    icc: bytes | None,
    dpi: tuple[float, float] | None,
) -> dict[str, Any]:
    options: dict[str, Any] = {"optimize": rules.optimize}
    if rules.fmt == "JPEG":
        options["quality"] = rules.jpeg_quality
    if exif is not None:
        options["exif"] = exif
    if icc is not None:
        options["icc_profile"] = icc
    if dpi is not None:
        options["dpi"] = dpi
    return options


def write(
    rendered: Rendered,
    destination: Path,
    source: Path,
    *,
    exiftool: Path | None,
    keep_metadata: bool,
    exiftool_timeout: float = DEFAULT_EXIFTOOL_WRITE_TIMEOUT_S,
) -> None:
    """Write a rendered picture to ``destination`` and keep the source's modified time.

    The bytes go to a temporary file first. With ``keep_metadata`` and ExifTool, the tags Pillow
    cannot carry (XMP, maker notes) are copied from the source, and a failure there fails the
    write, so a picture is never saved without the metadata the owner asked to keep. The result is
    checked (it opens and has the right size) before the final rename, so a failed step leaves no
    half-written file. The rename never replaces an existing file: when ``destination`` appeared
    since the scan, :class:`FileExistsError` is raised.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(rendered.data)
        if keep_metadata and exiftool is not None:
            _copy_tags(exiftool, source, tmp, rendered.size, exiftool_timeout)
        verify_output(tmp, rendered.size)
        stat = os.stat(source)
        rename_no_replace(tmp, destination)
        os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    finally:
        tmp.unlink(missing_ok=True)


def _copy_tags(
    exiftool: Path, source: Path, target: Path, size: tuple[int, int], timeout: float
) -> None:
    """Copy every tag ExifTool can carry across formats. Orientation is 1: pixels are turned."""
    args = [
        "-overwrite_original",
        "-TagsFromFile",
        str(source),
        "-all:all",
        "-Orientation#=1",
        f"-ExifIFD:ExifImageWidth={size[0]}",
        f"-ExifIFD:ExifImageHeight={size[1]}",
        str(target),
    ]
    try:
        result = run_exiftool(exiftool, args, timeout)
    except subprocess.TimeoutExpired as exc:
        raise ImageError(f"ExifTool timed out copying the metadata of {source.name}") from exc
    except (OSError, ValueError) as exc:
        raise ImageError(f"ExifTool could not run on {source.name}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise ImageError(f"ExifTool could not copy the metadata: {detail}")


def verify_output(path: Path, expected_size: tuple[int, int]) -> None:
    """Raise :class:`ImageError` unless ``path`` opens as a picture of ``expected_size``.

    Reopens the file from disk with Pillow and checks the size. The pixels are decoded too, so a
    truncated file is caught.
    """
    try:
        with Image.open(path) as check:
            if check.size != expected_size:
                raise ImageError(
                    f"the written picture is {check.size[0]}x{check.size[1]}, expected "
                    f"{expected_size[0]}x{expected_size[1]}"
                )
            check.load()
    except ImageError:
        raise
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImageError(f"the written picture cannot be read back: {exc}") from exc
