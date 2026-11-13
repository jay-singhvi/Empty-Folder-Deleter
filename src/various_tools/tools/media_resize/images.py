"""Image rendering for media_resize: decode, orient, resize, encode, and write atomically.

``render()`` does all the work in memory and writes nothing, so ``scan()`` can report the exact
output size. ``write()`` puts the rendered bytes into place: through a temporary file, with the
metadata copied from a RAW file by ExifTool and the size checked, before the final rename.

The formats are app.py's list (JPEG, PNG, BMP, GIF, TIFF, WebP). Animated images and JPEGs with
embedded extras (motion photos, HDR gain maps) are left as they are, because resizing them would
drop data (metadata-8 does the same).
"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageOps

from various_tools.core.deps import run_tool
from various_tools.core.fs import rename_no_replace
from various_tools.core.media.imaging import is_raw, open_image
from various_tools.core.onedrive import ensure_on_device
from various_tools.core.settings import DEFAULT_EXIFTOOL_WRITE_TIMEOUT_S
from various_tools.tools.media_resize.geometry import ResizeMode, Target, plan_target

IMAGE_FORMATS: dict[str, str] = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".bmp": "BMP",
    ".gif": "GIF",
    ".tiff": "TIFF",
    ".webp": "WEBP",
}

# The resampling filters the ``resample`` option offers, best for photos first.
Resample = Literal["lanczos", "bicubic", "bilinear", "box", "hamming", "nearest"]
RESAMPLE_FILTERS: dict[str, Image.Resampling] = {
    "lanczos": Image.Resampling.LANCZOS,
    "bicubic": Image.Resampling.BICUBIC,
    "bilinear": Image.Resampling.BILINEAR,
    "box": Image.Resampling.BOX,
    "hamming": Image.Resampling.HAMMING,
    "nearest": Image.Resampling.NEAREST,
}
# The defaults of the ``optimize_output``, ``resample`` and ``extras_scan_kb`` options.
EXTRAS_SCAN_BYTES = 262144

# File-format facts, not preferences: these stay in the code.
ORIENTATION_TAG = 0x0112  # the EXIF tag number for orientation
EXTRAS_MARKERS = (b"MotionPhoto", b"MicroVideo", b"hdrgm")  # byte signatures in the file


class ImageError(RuntimeError):
    """An image could not be rendered or written. The message goes to the plan or the journal."""


@dataclass(frozen=True)
class ImageRules:
    mode: ResizeMode
    box: tuple[int, int] | None
    target_px: int
    amount: int
    jpeg_quality: int
    keep_metadata: bool
    never_upscale: bool = True
    optimize: bool = True
    resample: Resample = "lanczos"
    extras_scan_bytes: int = EXTRAS_SCAN_BYTES


@dataclass(frozen=True)
class Rendered:
    """A resized image, encoded in memory and ready to be written."""

    source_size: tuple[int, int]
    target: Target
    fmt: str
    data: bytes
    from_raw: bool


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_FORMATS


def has_embedded_extras(path: Path, scan_bytes: int = EXTRAS_SCAN_BYTES) -> bool:
    """True for Motion Photo and Ultra HDR JPEGs, which Pillow cannot re-save intact.

    Reads the first ``scan_bytes`` of the file, so a cloud-only file raises
    :class:`CloudOnlyFileError` first.
    """
    ensure_on_device(path)
    try:
        with open(path, "rb") as handle:
            head = handle.read(scan_bytes)
    except OSError:
        return False
    return any(marker in head for marker in EXTRAS_MARKERS)


def _kept_exif(original: Image.Image) -> bytes | None:
    """The EXIF block to keep. The orientation is reset, because the pixels are rotated already."""
    exif = original.getexif()
    if not exif:
        return None
    if exif.get(ORIENTATION_TAG):
        exif[ORIENTATION_TAG] = 1
    return bytes(exif.tobytes())


def _save_options(
    fmt: str,
    rules: ImageRules,
    exif: bytes | None,
    icc_profile: bytes | None = None,
    dpi: tuple[float, float] | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if fmt == "JPEG":
        options["quality"] = rules.jpeg_quality
        options["optimize"] = rules.optimize
    elif fmt == "PNG":
        options["optimize"] = rules.optimize
    if exif is not None and fmt in ("JPEG", "PNG", "WEBP", "TIFF"):
        options["exif"] = exif
    if icc_profile and fmt in ("JPEG", "PNG", "WEBP", "TIFF"):
        options["icc_profile"] = icc_profile
    if dpi is not None and fmt in ("JPEG", "PNG", "TIFF"):
        options["dpi"] = dpi
    return options


def _kept_dpi(original: Image.Image) -> tuple[float, float] | None:
    value: object = original.info.get("dpi")
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except TypeError, ValueError:
        return None


def _kept_icc(original: Image.Image) -> bytes | None:
    value = original.info.get("icc_profile")
    return value if isinstance(value, bytes) and value else None


def render(source: Path, out_suffix: str, rules: ImageRules, raw_to_jpeg: bool) -> Rendered | str:
    """Resize ``source`` in memory. Returns the result, or the reason the file is left alone.

    The caller must have checked that the file is on the device (rule 3). This checks again,
    before anything is read, so a cloud-only file raises instead of being read.
    """
    ensure_on_device(source)
    from_raw = is_raw(source)
    if from_raw:
        if not raw_to_jpeg:
            return "RAW file, and RAW conversion is off"
        out_fmt = "JPEG"
    else:
        if not is_image_name(source.name):
            return "not an image format this tool resizes"
        is_jpeg = IMAGE_FORMATS.get(source.suffix.lower()) == "JPEG"
        if is_jpeg and has_embedded_extras(source, rules.extras_scan_bytes):
            return "motion photo or HDR image, left as-is (resizing would drop embedded data)"
        out_fmt = IMAGE_FORMATS[out_suffix.lower()]

    with open_image(source) as original:
        if getattr(original, "is_animated", False) or original.format == "MPO":
            return "animated or multi-picture image, left as-is"
        # A RAW image keeps its sensor orientation. ExifTool later copies the orientation tag,
        # so viewers turn the JPEG the same way they turned the RAW.
        # The pixels are turned first. Only then is the orientation reset in the kept EXIF,
        # because _kept_exif changes the EXIF object that exif_transpose reads.
        displayed = original if from_raw else ImageOps.exif_transpose(original)
        keep_exif = rules.keep_metadata and not from_raw
        exif = _kept_exif(original) if keep_exif else None
        icc = _kept_icc(original) if keep_exif else None
        dpi = _kept_dpi(original) if keep_exif else None
        width, height = displayed.size
        result = plan_target(
            rules.mode,
            width,
            height,
            box=rules.box,
            target_px=rules.target_px,
            amount=rules.amount,
            never_upscale=rules.never_upscale,
        )
        if isinstance(result, str):
            return result
        image = displayed.resize((result.width, result.height), RESAMPLE_FILTERS[rules.resample])
    if out_fmt == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format=out_fmt, **_save_options(out_fmt, rules, exif, icc, dpi))
    return Rendered(
        source_size=(width, height),
        target=result,
        fmt=out_fmt,
        data=buffer.getvalue(),
        from_raw=from_raw,
    )


def write(
    rendered: Rendered,
    destination: Path,
    source: Path,
    *,
    exiftool: Path | None,
    keep_metadata: bool,
    exiftool_timeout: float = DEFAULT_EXIFTOOL_WRITE_TIMEOUT_S,
) -> None:
    """Write a rendered image to ``destination`` and keep the source's modified time.

    The bytes go to a temporary file first. For a RAW source the metadata is copied with ExifTool,
    and the copy is checked (size and format) before the final rename, so a failed step leaves
    no half-written file at ``destination``. The final rename never replaces an existing file:
    when ``destination`` appeared since the scan, :class:`FileExistsError` is raised.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(rendered.data)
        if rendered.from_raw and keep_metadata:
            if exiftool is None:
                raise ImageError("ExifTool is needed to keep the metadata of a RAW conversion")
            _copy_raw_metadata(exiftool, source, tmp, rendered.target, exiftool_timeout)
        with Image.open(tmp) as check:
            if check.size != (rendered.target.width, rendered.target.height):
                raise ImageError("the written image has the wrong size; it was not kept")
        stat = os.stat(source)
        rename_no_replace(tmp, destination)
        os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    finally:
        tmp.unlink(missing_ok=True)


def _copy_raw_metadata(
    exiftool: Path, source: Path, target: Path, size: Target, timeout: float
) -> None:
    """Copy every tag ExifTool can write into the JPEG (metadata-8's arguments)."""
    result = run_tool(
        [
            str(exiftool),
            "-overwrite_original",
            "-TagsFromFile",
            str(source),
            "-all:all",
            "-unsafe",
            # The RAW's dimension tags would disagree with the smaller JPEG.
            f"-ExifIFD:ExifImageWidth={size.width}",
            f"-ExifIFD:ExifImageHeight={size.height}",
            str(target),
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise ImageError(f"ExifTool could not copy the metadata: {detail}")
