"""Which copy of a group stays where it is (the "keeper"), and the shared match record.

Lower keys sort first, and the first file in a group is the one kept. The image and video
orderings differ, as they did in the two legacy scripts, so each keeps its own key.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

COPY_MARK = re.compile(r"(\(\d+\)|\bcopy\b|-\s*copy)", re.I)


def _copy_flag(name: str) -> int:
    """1 for names that look like a copy, such as "(1)" or "copy", so they sort last."""
    return 1 if COPY_MARK.search(name) else 0


def image_keep_key(path: Path, *, w: int, h: int, size: int, mtime: float) -> tuple[object, ...]:
    """Largest picture, then largest file, then a non-copy name, then the oldest."""
    name = os.path.basename(path)
    return (-(w * h), -size, _copy_flag(name), len(name), mtime, name)


def video_keep_key(
    path: Path, *, w: int, h: int, size: int, dur: float, mtime: float
) -> tuple[object, ...]:
    """Largest frame, then highest bitrate (size per second), then a non-copy name, then oldest."""
    name = os.path.basename(path)
    return (-(w * h), -size / max(dur, 0.1), _copy_flag(name), len(name), mtime, name)


@dataclass(frozen=True)
class Match:
    """One planned move: ``duplicate`` is set aside because ``keeper`` stays where it is.

    ``kind`` is one of ``exact``, ``visual``, ``burst`` or ``re-encode``. Exact and visual
    matches go to the duplicate folder, and burst and re-encode matches go to the review folder.
    """

    kind: str
    keeper: Path
    duplicate: Path
    reason: str

    @property
    def review(self) -> bool:
        return self.kind in ("burst", "re-encode")
