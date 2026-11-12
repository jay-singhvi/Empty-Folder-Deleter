"""Explain a match: every check the visual pass would make for two images (legacy ``--explain``).

Pure: it reads the two files and returns the checks. The CLI and the GUI show the result.
Animated GIFs and unreadable files have no visual hash, so they raise ``ValueError``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from various_tools.tools.duplicates.images import (
    ImageInfo,
    ImageRules,
    hamming,
    max_block_diff,
    read_image,
    same_capture,
    thumb_array,
)


@dataclass(frozen=True)
class Check:
    name: str
    value: str
    rule: str
    ok: bool


@dataclass(frozen=True)
class ExplainReport:
    a: ImageInfo
    b: ImageInfo
    checks: list[Check]

    @property
    def matched(self) -> bool:
        return all(c.ok for c in self.checks)


def _read(path: Path) -> ImageInfo:
    stat = os.stat(path)
    return read_image(path, stat.st_size, stat.st_mtime)


def explain(path_a: Path, path_b: Path, rules: ImageRules) -> ExplainReport:
    a, b = _read(path_a), _read(path_b)
    if a.phash is None or b.phash is None or a.dhash is None or b.dhash is None:
        raise ValueError("Not a visually comparable image (an animated GIF, or unreadable).")
    if a.thumb_z is None or b.thumb_z is None:
        raise ValueError("Not a visually comparable image (no thumbnail).")

    ar = a.w / a.h
    ar_diff = abs(ar - b.w / b.h) / ar
    block = max_block_diff(thumb_array(a.thumb_z), thumb_array(b.thumb_z))
    p_gap = hamming(a.phash, b.phash)
    d_gap = hamming(a.dhash, b.dhash)
    checks = [
        Check(
            "aspect ratio diff",
            f"{ar_diff * 100:.2f}%",
            f"<= {rules.aspect_tol * 100:.0f}%",
            ar_diff <= rules.aspect_tol,
        ),
        Check("pHash distance", str(p_gap), f"<= {rules.phash_max}", p_gap <= rules.phash_max),
        Check("dHash distance", str(d_gap), f"<= {rules.dhash_max}", d_gap <= rules.dhash_max),
        Check(
            "block diff (max 8x8)",
            f"{block:.2f}",
            f"<= {rules.pixel_block_max}",
            block <= rules.pixel_block_max,
        ),
        Check(
            "capture time",
            f"{a.taken or '?'} vs {b.taken or '?'}",
            "same (or missing)",
            same_capture(a, b),
        ),
    ]
    return ExplainReport(a, b, checks)
