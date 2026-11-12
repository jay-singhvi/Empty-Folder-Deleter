"""Content hashing: exact (SHA-256) groups and perceptual hashes.

Thresholds are not defined here. Each tool supplies its own (AGENTS.md: thresholds are
requirements, tuned on real data). This module only computes and compares.

Every function that opens a file calls :func:`onedrive.ensure_on_device` first, so a
cloud-only file is never downloaded by hashing.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import imagehash
import numpy as np
from PIL import Image

from various_tools.core.fs import FileInfo
from various_tools.core.onedrive import (
    CloudOnlyFileError,
    ensure_on_device,
    is_on_device,
    status_from_attributes,
)

CHUNK_SIZE = 1 << 20


def sha256(path: Path) -> str:
    """Streaming SHA-256 of a file's contents. Raises :class:`CloudOnlyFileError` if cloud-only."""
    ensure_on_device(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ExactGroups:
    """Result of :func:`group_exact`.

    ``groups`` holds sets of two or more files with identical contents. Files that could not be
    hashed without downloading them are listed in ``skipped_cloud_only``, never hashed.
    """

    groups: list[list[Path]] = field(default_factory=list[list[Path]])
    skipped_cloud_only: list[Path] = field(default_factory=list[Path])
    errors: list[tuple[Path, str]] = field(default_factory=list[tuple[Path, str]])


def group_exact(files: Iterable[FileInfo]) -> ExactGroups:
    """Find files with identical contents: bucket by size, then hash only the buckets with 2+.

    A file in a bucket is hashed only when it is on this device. Cloud-only files are reported
    as skipped, which satisfies AGENTS.md rule 3.
    """
    by_size: dict[int, list[FileInfo]] = defaultdict(list)
    for info in files:
        by_size[info.size].append(info)

    result = ExactGroups()
    for members in by_size.values():
        if len(members) < 2:
            continue
        by_digest: dict[str, list[Path]] = defaultdict(list)
        for info in members:
            if not is_on_device(status_from_attributes(info.attributes)):
                result.skipped_cloud_only.append(info.path)
                continue
            try:
                digest = sha256(info.path)
            except CloudOnlyFileError:
                result.skipped_cloud_only.append(info.path)
            except OSError as exc:
                result.errors.append((info.path, str(exc)))
            else:
                by_digest[digest].append(info.path)
        result.groups.extend(sorted(p) for p in by_digest.values() if len(p) > 1)

    result.groups.sort(key=lambda group: str(group[0]).casefold())
    result.skipped_cloud_only.sort(key=lambda p: str(p).casefold())
    return result


def _to_int(bits: Any) -> int:
    """An imagehash array as one integer, so Hamming distance is an XOR plus a popcount.

    ``imagehash`` ships without type information, so its result is typed ``Any`` here.
    """
    return int.from_bytes(
        np.packbits(np.asarray(bits.hash, dtype=bool).reshape(-1)).tobytes(), "big"
    )


def phash(image: Image.Image, hash_size: int = 16) -> int:
    """Perceptual hash (DCT). With ``hash_size=16`` it is 256 bits.

    The caller opens and orients the image (``ImageOps.exif_transpose``) and converts it to RGB.
    """
    return _to_int(imagehash.phash(image, hash_size=hash_size))


def dhash(image: Image.Image, hash_size: int = 16) -> int:
    """Difference hash (gradient). With ``hash_size=16`` it is 256 bits."""
    return _to_int(imagehash.dhash(image, hash_size=hash_size))


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two hashes."""
    return (a ^ b).bit_count()


def band_keys(value: int, bands: int, bits: int = 256) -> list[tuple[int, int]]:
    """Split a hash into ``bands`` slices as ``(band_index, slice_value)`` keys.

    Two hashes within ``bands - 1`` bit flips must agree exactly on at least one band. That is
    what lets a band index find candidates without comparing every pair.
    """
    if bands < 1:
        raise ValueError("bands must be at least 1")
    edges = [bits * i // bands for i in range(bands + 1)]
    return [(i, (value >> edges[i]) & ((1 << (edges[i + 1] - edges[i])) - 1)) for i in range(bands)]
