"""Which copy of a group stays where it is (the "keeper"), and the shared match record.

Lower keys sort first, and the first file in a group is the one kept. The image and video
orderings differ, as they did in the two legacy scripts, so each keeps its own key.
"""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path

# The default of the ``copy_mark`` option: names like "photo (1).jpg" or "photo - copy.jpg".
COPY_MARK_PATTERN = r"(\(\d+\)|\bcopy\b|-\s*copy)"


@functools.lru_cache(maxsize=32)
def compile_copy_mark(pattern: str) -> re.Pattern[str]:
    """The copy-name pattern, compiled once. It ignores case, as in the legacy scripts."""
    return re.compile(pattern, re.I)


def _copy_flag(name: str, copy_mark: str) -> int:
    """1 for names that look like a copy, such as "(1)" or "copy", so they sort last."""
    return 1 if compile_copy_mark(copy_mark).search(name) else 0


def image_keep_key(
    path: Path,
    *,
    w: int,
    h: int,
    size: int,
    mtime: float,
    copy_mark: str = COPY_MARK_PATTERN,
) -> tuple[object, ...]:
    """Largest picture, then largest file, then a non-copy name, then the oldest."""
    name = os.path.basename(path)
    return (-(w * h), -size, _copy_flag(name, copy_mark), len(name), mtime, name)


def video_keep_key(
    path: Path,
    *,
    w: int,
    h: int,
    size: int,
    dur: float,
    mtime: float,
    copy_mark: str = COPY_MARK_PATTERN,
) -> tuple[object, ...]:
    """Largest frame, then highest bitrate (size per second), then a non-copy name, then oldest."""
    name = os.path.basename(path)
    return (-(w * h), -size / max(dur, 0.1), _copy_flag(name, copy_mark), len(name), mtime, name)


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
