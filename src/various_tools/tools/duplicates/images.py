"""Image duplicates: a port of ``legacy/python-find_duplicates.py``, with the same rules and order.

Three passes, as in the legacy script:

1. ``exact``: identical bytes (SHA-256). Each group keeps its best copy.
2. ``visual``: the same picture re-saved or resized. Both perceptual hashes must agree, the
   aspect ratio must match, the capture times must agree when both files have one, and the
   pixel check on 8×8 blocks must pass. A band index finds candidates without comparing pairs.
3. ``burst``: frames taken in the same second (from the file name) that show the same scene.
   The sharpest frame stays, and the rest go to review.

Thresholds come from :class:`ImageRules`, prefilled with the tuned values (AGENTS.md).
"""

from __future__ import annotations

import base64
import functools
import re
import zlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from various_tools.core import hashing
from various_tools.core.media.exif import EXIF_IFD, TAG_DATETIME_ORIGINAL
from various_tools.core.media.imaging import open_image
from various_tools.tools.duplicates.keeper import Match, image_keep_key

IMAGE_EXT = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
)
# Animated GIFs are compared by their bytes only.
VISUAL_SKIP_EXT = frozenset({".gif"})
THUMB = 128
PHASH_SIZE = 16  # 16x16 hash grid, so 256 bits
# A visual match above this block difference, or near a hash limit, gets a model check.
VERIFY_BORDERLINE_BLOCK = 2.0
BURST_NAME = re.compile(r"^(?:IMG_)?(\d{8}_\d{6})")  # timestamp to the second


@dataclass(frozen=True)
class ImageRules:
    """Tuned thresholds (AGENTS.md 🔒). Defaults match the legacy script."""

    phash_max: int = 2
    dhash_max: int = 4
    aspect_tol: float = 0.02
    burst_phash_max: int = 40
    pixel_block_max: float = 3.0


@dataclass(frozen=True)
class ImageInfo:
    """Everything the comparison needs, computed once per file (or read from the cache)."""

    path: Path
    size: int
    mtime: float
    w: int
    h: int
    sha: str
    phash: int | None
    dhash: int | None
    sharp: float | None
    taken: str | None  # EXIF DateTimeOriginal as written, or None
    thumb_z: bytes | None  # 128×128 grey thumbnail, zlib-compressed


def _taken_text(im: Image.Image) -> str | None:
    """The EXIF capture time exactly as written. The legacy script compared these strings."""
    value = im.getexif().get_ifd(EXIF_IFD).get(TAG_DATETIME_ORIGINAL)
    return str(value).strip() if value else None


def read_image(path: Path, size: int, mtime: float) -> ImageInfo:
    """Hash and measure one image. Raises on unreadable files, and for cloud-only files
    (``open_image`` and ``hashing.sha256`` both check OneDrive state first)."""
    sha = hashing.sha256(path)
    with open_image(path) as im:
        w, h = im.size
        taken = _taken_text(im)
        if path.suffix.lower() in VISUAL_SKIP_EXT:
            return ImageInfo(path, size, mtime, w, h, sha, None, None, None, taken, None)
        oriented = ImageOps.exif_transpose(im).convert("RGB")
        w, h = oriented.size
        phash = hashing.phash(oriented, PHASH_SIZE)
        dhash = hashing.dhash(oriented, PHASH_SIZE)
        grey = oriented.convert("L")
        thumb = np.asarray(grey.resize((THUMB, THUMB), Image.Resampling.LANCZOS), dtype=np.uint8)
        grey.thumbnail((1600, 1600))
        sharp = float(np.asarray(grey.filter(ImageFilter.FIND_EDGES), dtype=np.float32).var())
        return ImageInfo(
            path,
            size,
            mtime,
            w,
            h,
            sha,
            phash,
            dhash,
            sharp,
            taken,
            zlib.compress(thumb.tobytes(), 1),
        )


def image_to_cache(info: ImageInfo) -> dict[str, Any]:
    """JSON-safe form for :class:`~various_tools.core.cache.FileCache`. Path, size and mtime are
    the cache key and are not stored again."""
    return {
        "w": info.w,
        "h": info.h,
        "sha": info.sha,
        "phash": None if info.phash is None else format(info.phash, "064x"),
        "dhash": None if info.dhash is None else format(info.dhash, "064x"),
        "sharp": info.sharp,
        "taken": info.taken,
        "thumb": None if info.thumb_z is None else base64.b64encode(info.thumb_z).decode("ascii"),
    }


def image_from_cache(path: Path, size: int, mtime: float, value: dict[str, Any]) -> ImageInfo:
    def hex_int(raw: object) -> int | None:
        return None if raw is None else int(str(raw), 16)

    thumb = value.get("thumb")
    return ImageInfo(
        path=path,
        size=size,
        mtime=mtime,
        w=int(value["w"]),
        h=int(value["h"]),
        sha=str(value["sha"]),
        phash=hex_int(value.get("phash")),
        dhash=hex_int(value.get("dhash")),
        sharp=None if value.get("sharp") is None else float(value["sharp"]),
        taken=None if value.get("taken") is None else str(value["taken"]),
        thumb_z=None if thumb is None else base64.b64decode(str(thumb)),
    )


@functools.lru_cache(maxsize=4096)
def thumb_array(thumb_z: bytes) -> np.ndarray:
    raw = zlib.decompress(thumb_z)
    return np.frombuffer(raw, dtype=np.uint8).reshape(THUMB, THUMB).astype(np.float32)


def same_burst(a: ImageInfo, b: ImageInfo) -> bool:
    """Two different shots taken in the same second (burst mode)."""
    ma = BURST_NAME.match(a.path.name)
    mb = BURST_NAME.match(b.path.name)
    return bool(ma and mb and ma.group(1) == mb.group(1))


def burst_key(info: ImageInfo) -> str | None:
    m = BURST_NAME.match(info.path.name)
    return m.group(1) if m else None


def same_capture(a: ImageInfo, b: ImageInfo) -> bool:
    """False only when both photos have a capture time and the times differ."""
    return a.taken is None or b.taken is None or a.taken == b.taken


def hamming(a: int, b: int) -> int:
    return hashing.hamming(a, b)


def cheap_match(a: ImageInfo, b: ImageInfo, rules: ImageRules) -> bool:
    """Everything except the pixel check: aspect ratio and both hashes."""
    if a.phash is None or b.phash is None or a.dhash is None or b.dhash is None:
        return False
    ar = a.w / a.h
    return (
        abs(ar - b.w / b.h) / ar <= rules.aspect_tol
        and hamming(a.phash, b.phash) <= rules.phash_max
        and hamming(a.dhash, b.dhash) <= rules.dhash_max
    )


def max_block_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Largest mean absolute difference over 8×8 blocks, which catches local motion."""
    return float(np.abs(a - b).reshape(16, 8, 16, 8).mean(axis=(1, 3)).max())


def is_borderline(a: ImageInfo, b: ImageInfo, block: float, rules: ImageRules) -> bool:
    """Matches close to a limit are the ones worth a second opinion."""
    assert a.phash is not None and b.phash is not None
    assert a.dhash is not None and b.dhash is not None
    return (
        block > VERIFY_BORDERLINE_BLOCK
        or hamming(a.phash, b.phash) >= rules.phash_max
        or hamming(a.dhash, b.dhash) >= rules.dhash_max - 1
    )


def visual_reason(a: ImageInfo, b: ImageInfo, block: float, rules: ImageRules) -> str:
    assert a.phash is not None and b.phash is not None
    assert a.dhash is not None and b.dhash is not None
    return (
        f"pHash {hamming(a.phash, b.phash)}/{rules.phash_max}, "
        f"dHash {hamming(a.dhash, b.dhash)}/{rules.dhash_max}, "
        f"block diff {block:.2f}/{rules.pixel_block_max}, "
        f"taken {a.taken or '?'} vs {b.taken or '?'}"
    )


def keep_key(info: ImageInfo) -> tuple[object, ...]:
    return image_keep_key(info.path, w=info.w, h=info.h, size=info.size, mtime=info.mtime)


@dataclass(frozen=True)
class Confirmer:
    """A model check for borderline visual matches. ``label`` is written into the reason."""

    label: str
    confirms: Callable[[Path, Path], bool]


def _find_visual(
    r: ImageInfo,
    leaders: list[ImageInfo],
    bands: dict[tuple[int, int], list[int]],
    rules: ImageRules,
    confirmer: Confirmer | None,
) -> tuple[int, str] | None:
    """The first leader, in keep order, that looks like ``r``: its index and the reason."""
    if r.phash is None:
        return None
    candidates: set[int] = set()
    for key in hashing.band_keys(r.phash, rules.phash_max + 1):
        candidates.update(bands.get(key, ()))
    for idx in sorted(candidates):
        leader = leaders[idx]
        if same_burst(leader, r) or not cheap_match(leader, r, rules):
            continue
        if not same_capture(leader, r):
            continue
        if leader.thumb_z is None or r.thumb_z is None:
            continue
        block = max_block_diff(thumb_array(leader.thumb_z), thumb_array(r.thumb_z))
        if block > rules.pixel_block_max:
            continue
        reason = visual_reason(leader, r, block, rules)
        if confirmer is not None and is_borderline(leader, r, block, rules):
            if not confirmer.confirms(leader.path, r.path):
                continue
            reason += f", {confirmer.label} confirmed"
        return idx, reason
    return None


def find_matches(
    infos: list[ImageInfo],
    rules: ImageRules,
    *,
    exact_only: bool = False,
    confirmer: Confirmer | None = None,
) -> list[Match]:
    """Every duplicate found among ``infos``, in the same order as the legacy script."""
    matches: list[Match] = []

    # Pass 1: identical bytes.
    by_sha: dict[str, list[ImageInfo]] = defaultdict(list)
    for info in infos:
        by_sha[info.sha].append(info)
    reps: list[ImageInfo] = []
    for members in by_sha.values():
        members.sort(key=keep_key)
        reps.append(members[0])
        matches += [
            Match("exact", members[0].path, dup.path, "identical bytes (SHA-256)")
            for dup in members[1:]
        ]
    if exact_only:
        return matches

    # Pass 2: the same picture, re-saved or resized. Burst frames stay leaders for pass 3.
    reps.sort(key=keep_key)
    leaders: list[ImageInfo] = []
    bands: dict[tuple[int, int], list[int]] = defaultdict(list)
    for r in reps:
        hit = _find_visual(r, leaders, bands, rules, confirmer)
        if hit is not None:
            idx, reason = hit
            matches.append(Match("visual", leaders[idx].path, r.path, reason))
            continue
        leaders.append(r)
        if r.phash is not None:
            for key in hashing.band_keys(r.phash, rules.phash_max + 1):
                bands[key].append(len(leaders) - 1)

    # Pass 3: burst frames. The sharpest frame stays and the rest go to review.
    bursts: dict[str, list[ImageInfo]] = defaultdict(list)
    for leader in leaders:
        key = burst_key(leader)
        if key and leader.phash is not None:
            bursts[key].append(leader)
    for key, frames in bursts.items():
        frames.sort(key=lambda f: -(f.sharp or 0.0))
        best: list[ImageInfo] = []
        for f in frames:
            for b in best:
                if (b.w, b.h) == (f.w, f.h) and b.phash is not None and f.phash is not None:
                    gap = hamming(b.phash, f.phash)
                    if gap <= rules.burst_phash_max:
                        reason = (
                            f"burst {key}, pHash {gap}/{rules.burst_phash_max}, sharper frame kept"
                        )
                        matches.append(Match("burst", b.path, f.path, reason))
                        break
            else:
                best.append(f)
    return matches
