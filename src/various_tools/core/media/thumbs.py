"""Small previews and facts for one file, shared by the GUI compare view and the HTML report.

Rule 3: a cloud-only file is never opened. :func:`file_facts` reads attributes and ``lstat``
only; :func:`thumbnail_jpeg` returns ``None`` for a cloud-only file without reading it.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from various_tools.core.media.imaging import IMAGE_EXTS, RAW_EXTS, open_image
from various_tools.core.onedrive import get_attributes, is_on_device, status_from_attributes

THUMB_EXTS = IMAGE_EXTS | RAW_EXTS


@dataclass(frozen=True)
class FileFacts:
    """What can be known about a file without opening it. ``exists`` is False when it is gone."""

    path: Path
    exists: bool
    size: int | None
    modified: datetime | None
    cloud_only: bool


def file_facts(path: Path) -> FileFacts:
    attributes = get_attributes(path)
    if attributes is None:
        return FileFacts(path, False, None, None, False)
    cloud_only = not is_on_device(status_from_attributes(attributes))
    try:
        stat = os.lstat(path)
    except OSError:
        return FileFacts(path, False, None, None, cloud_only)
    return FileFacts(path, True, stat.st_size, datetime.fromtimestamp(stat.st_mtime), cloud_only)


def image_size(path: Path) -> tuple[int, int] | None:
    """Pixel width and height, or None for a cloud-only, unreadable or non-image file."""
    if path.suffix.lower() not in THUMB_EXTS or file_facts(path).cloud_only:
        return None
    try:
        with open_image(path) as image:
            transposed = ImageOps.exif_transpose(image)
            return transposed.size
    except Exception:  # noqa: BLE001 - any decode problem just means "no size"
        return None


def thumbnail_jpeg(path: Path, max_px: int = 256, quality: int = 80) -> bytes | None:
    """A JPEG preview no larger than ``max_px`` on its longer side, or None.

    None for a cloud-only file (never read), a file that is not a still image, or one that
    cannot be decoded. EXIF orientation is applied.
    """
    if path.suffix.lower() not in THUMB_EXTS or file_facts(path).cloud_only:
        return None
    try:
        with open_image(path) as image:
            image.seek(0)
            preview = ImageOps.exif_transpose(image).convert("RGB")
            preview.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            preview.save(buffer, format="JPEG", quality=quality)
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a preview is optional
        return None
