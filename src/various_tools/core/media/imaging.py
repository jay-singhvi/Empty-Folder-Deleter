"""Opening and saving images. HEIF is registered on import, and RAW (CR3) is decoded with rawpy.

Opening a file reads its contents, so :func:`open_image` checks OneDrive state first.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pillow_heif
import rawpy
from PIL import Image

from various_tools.core.fs import rename_no_replace
from various_tools.core.onedrive import ensure_on_device

# Makes Image.open() read .heic and .heif. Safe to call more than once.
pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType] - untyped package

RAW_EXTS = frozenset({".cr3"})
IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
)

# rawpy is not safe to use from several threads at once (the legacy script used a lock too).
_RAW_LOCK = threading.Lock()


def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_EXTS


def open_image(path: Path) -> Image.Image:
    """Open an image, decoding RAW files. Use as ``with open_image(p) as im:`` where possible.

    Raises :class:`~various_tools.core.onedrive.CloudOnlyFileError` for cloud-only files.
    """
    ensure_on_device(path)
    if is_raw(path):
        with _RAW_LOCK, rawpy.imread(str(path)) as raw:
            # Keep the sensor orientation. Orientation is applied later with exif_transpose.
            rgb = raw.postprocess(use_camera_wb=True, output_bps=8, user_flip=0)
        return Image.fromarray(np.asarray(rgb))
    return Image.open(path)


def save_jpeg(
    image: Image.Image, destination: Path, quality: int = 95, exif: bytes | None = None
) -> None:
    """Save as JPEG through a temporary file, then rename it into place.

    A failed save never leaves a half-written file at ``destination``, and an existing file
    there is never replaced: :class:`FileExistsError` is raised instead. ``exif`` is copied
    over when given, so metadata survives the conversion.
    """
    options: dict[str, Any] = {"format": "JPEG", "quality": quality}
    if exif:
        options["exif"] = exif
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        image.convert("RGB").save(tmp_path, **options)
        rename_no_replace(tmp_path, destination)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
