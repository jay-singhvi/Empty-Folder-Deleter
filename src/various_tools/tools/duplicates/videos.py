"""Video duplicates: a port of ``legacy/find_duplicate_videos.py``, with the same rules and order.

1. ``exact``: identical bytes (SHA-256). Only files of the same size are hashed.
2. ``re-encode``: the same footage in another file. Durations must match within
   ``max(dur_tol_s, dur_tol_pct × duration)``, aspect ratios within ``aspect_tol``, and the
   pHash of frames sampled along the clip must be close (mean and worst frame).

File names are not trusted. Only the content decides, and the matches go to review, not to
the duplicate folder, because "(1)" files are sometimes a different recording of the same moment.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from various_tools.core import hashing
from various_tools.core.media import ffmpeg
from various_tools.core.settings import DEFAULT_FFPROBE_TIMEOUT_S, DEFAULT_FRAME_TIMEOUT_S
from various_tools.tools.duplicates.keeper import COPY_MARK_PATTERN, Match, video_keep_key

# The values below are the defaults of the matching options on ``DuplicatesOptions``. The code
# reads the option values (through ``VideoRules`` and ``VideoReadParams``), never these directly.
VIDEO_EXT = frozenset({".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm"})
FRAME_WIDTH = 320  # frames are compared at this width, as in the legacy script
PHASH_SIZE = 16


@dataclass(frozen=True)
class VideoRules:
    """Tuned thresholds (AGENTS.md 🔒) and the other matching options.

    The first five defaults match the legacy script. ``copy_mark`` is the option default.
    """

    dur_tol_s: float = 0.6
    dur_tol_pct: float = 0.01
    aspect_tol: float = 0.02
    frame_mean_max: float = 10
    frame_worst_max: int = 24
    copy_mark: str = COPY_MARK_PATTERN


@dataclass(frozen=True)
class VideoReadParams:
    """How a clip is probed and its frames are grabbed and hashed."""

    frame_width: int = FRAME_WIDTH
    phash_size: int = PHASH_SIZE
    frame_retries: int = ffmpeg.FRAME_RETRIES
    frame_step_back: float = ffmpeg.FRAME_STEP_BACK
    probe_timeout_s: float = DEFAULT_FFPROBE_TIMEOUT_S
    frame_timeout_s: float = DEFAULT_FRAME_TIMEOUT_S


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    size: int
    mtime: float
    dur: float
    w: int
    h: int
    frames: tuple[int, ...]  # one pHash per sampled frame; empty when frames were not read


def read_video(
    ffprobe: Path,
    ffmpeg_exe: Path | None,
    path: Path,
    size: int,
    mtime: float,
    sample_at: Sequence[float],
    params: VideoReadParams | None = None,
) -> VideoInfo:
    """Probe one clip and, when ``ffmpeg_exe`` is given, hash its sampled frames.

    Raises :class:`~various_tools.core.media.ffmpeg.MediaError` (or OSError) when the file
    cannot be read. Such files are reported and never moved.
    """
    use = params or VideoReadParams()
    dur = ffmpeg.probe_duration(ffprobe, path, timeout=use.probe_timeout_s)
    w, h = ffmpeg.display_size(ffprobe, path, timeout=use.probe_timeout_s)
    if w <= 0 or h <= 0:
        raise ffmpeg.MediaError(f"no usable video size in {path.name}")
    frames: tuple[int, ...] = ()
    if ffmpeg_exe is not None:
        frames = tuple(
            hashing.phash(
                ffmpeg.grab_frame(
                    ffmpeg_exe,
                    path,
                    dur * f,
                    use.frame_width,
                    timeout=use.frame_timeout_s,
                    retries=use.frame_retries,
                    step_back=use.frame_step_back,
                ),
                use.phash_size,
            )
            for f in sample_at
        )
    return VideoInfo(path, size, mtime, dur, w, h, frames)


def same_video(a: VideoInfo, b: VideoInfo, rules: VideoRules) -> bool:
    if abs(a.dur - b.dur) > max(rules.dur_tol_s, rules.dur_tol_pct * max(a.dur, b.dur)):
        return False
    if min(a.w, a.h, b.w, b.h) <= 0:  # no usable size: not comparable (and no division by zero)
        return False
    ar = a.w / a.h
    if abs(ar - b.w / b.h) / ar > rules.aspect_tol:
        return False
    if not a.frames or len(a.frames) != len(b.frames):
        return False
    d = [hashing.hamming(x, y) for x, y in zip(a.frames, b.frames, strict=True)]
    return sum(d) / len(d) <= rules.frame_mean_max and max(d) <= rules.frame_worst_max


def keep_key(v: VideoInfo, rules: VideoRules) -> tuple[object, ...]:
    return video_keep_key(
        v.path, w=v.w, h=v.h, size=v.size, dur=v.dur, mtime=v.mtime, copy_mark=rules.copy_mark
    )


def find_matches(
    videos: list[VideoInfo],
    rules: VideoRules,
    shas: dict[Path, str],
    *,
    exact_only: bool = False,
) -> list[Match]:
    """Duplicates among ``videos``. ``shas`` holds the hash of each file that shares its size
    with another file (the only ones that need hashing). Files missing from ``shas`` are unique."""
    matches: list[Match] = []

    by_content: dict[tuple[str, str], list[VideoInfo]] = defaultdict(list)
    for v in videos:
        digest = shas.get(v.path)
        key = ("sha", digest) if digest is not None else ("unique", str(v.path))
        by_content[key].append(v)
    reps: list[VideoInfo] = []
    for members in by_content.values():
        members.sort(key=lambda video: keep_key(video, rules))
        reps.append(members[0])
        matches += [
            Match("exact", members[0].path, dup.path, "identical bytes (SHA-256)")
            for dup in members[1:]
        ]
    if exact_only:
        return matches

    reps.sort(key=lambda video: keep_key(video, rules))
    leaders: list[VideoInfo] = []
    for r in reps:
        for leader in leaders:
            if same_video(leader, r, rules):
                matches.append(
                    Match(
                        "re-encode",
                        leader.path,
                        r.path,
                        "same footage: duration, aspect and sampled frames match",
                    )
                )
                break
        else:
            leaders.append(r)
    return matches


def size_sharers(videos: list[VideoInfo]) -> list[Path]:
    """Paths of the videos whose size is shared with another video, in input order."""
    counts: dict[int, int] = defaultdict(int)
    for v in videos:
        counts[v.size] += 1
    return [v.path for v in videos if counts[v.size] > 1]
