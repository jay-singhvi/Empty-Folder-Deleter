"""Duplicate Finder (plan §3.4): merges the legacy image and video finders.

``scan()`` finds the duplicates and writes nothing. Each copy that is not kept becomes a planned
action: a move to the duplicate folder (or to the review folder for burst and re-encode matches),
or a trash action for exact and visual copies when ``action="trash"`` is chosen. ``apply()`` runs
the plan through the generic engine, so every action is journaled and undoable (the trash action
is journaled but cannot be undone by the engine; see the user page).

Rules that always hold:
* cloud-only OneDrive files are never read or hashed (AGENTS.md rule 3). They are reported;
* junctions and symlinks are not followed, the root is never changed, and protected folders are
  refused (rules 4 and 6). The engine checks each action again at apply time;
* the duplicate and review folders are never scanned, so a second run does not see moved files;
* a file that cannot be read is reported and never moved.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from various_tools.core import hashing
from various_tools.core.cache import FileCache
from various_tools.core.context import RunContext
from various_tools.core.deps import install_hint
from various_tools.core.fs import (
    FileInfo,
    destination_refusal,
    is_inside,
    root_refusal,
    walk,
)
from various_tools.core.media import ffmpeg
from various_tools.core.onedrive import is_on_device, status_from_attributes
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.tools.base import Category, ToolOptions
from various_tools.tools.duplicates import images, verify_ollama, videos
from various_tools.tools.duplicates.images import (
    BURST_NAME_PATTERN,
    IMAGE_EXT,
    PHASH_SIZE,
    SHARPNESS_PX,
    THUMB,
    VERIFY_BORDERLINE_BLOCK,
    VISUAL_SKIP_EXT,
    Confirmer,
    ImageInfo,
    ImageRules,
    ReadParams,
    image_from_cache,
    image_to_cache,
    read_image,
)
from various_tools.tools.duplicates.keeper import COPY_MARK_PATTERN, Match
from various_tools.tools.duplicates.verify_ollama import OllamaVerifier
from various_tools.tools.duplicates.videos import (
    FRAME_WIDTH,
    VIDEO_EXT,
    VideoInfo,
    VideoReadParams,
    VideoRules,
    read_video,
)
from various_tools.tools.duplicates.videos import PHASH_SIZE as VIDEO_PHASH_SIZE

TOOL_ID = "duplicates"
MediaKind = Literal["images", "videos", "both"]
IMAGE_CACHE_NAME = "duplicates.images"
# Bump when the cached fields change. Old entries are then dropped. A change to the hash grid,
# the thumbnail size, the sharpness size or the compare-by-bytes types is not a version change:
# it gives a different cache name and file (see ``image_cache_identity``).
IMAGE_CACHE_VERSION = 1
IMAGE_CACHE_FILE_PREFIX = "duplicates-images"
FRAME_RETRIES = ffmpeg.FRAME_RETRIES
FRAME_STEP_BACK = ffmpeg.FRAME_STEP_BACK


def image_cache_path(cache_dir: Path, params: ReadParams) -> Path:
    """The cache file for these hashing settings. Each combination has its own file, so a
    changed thumbnail or hash size never reads results computed with the old one."""
    return cache_dir / f"{IMAGE_CACHE_FILE_PREFIX}-{params.cache_identity()}.sqlite"


def _hint(group: str, *, advanced: bool = False, locked: bool = False, **extra: Any) -> Any:
    """The ``json_schema_extra`` hints the GUI and CLI read (see ``core/settings.py``)."""
    hints: dict[str, Any] = {"group": group}
    if advanced:
        hints["advanced"] = True
    if locked:
        hints["locked"] = True
    hints.update(extra)
    return hints


_TUNED = (
    "🔒 Locked default. Tuned by hand on real photos and videos, so change it only if you know why."
)


def normalise_extensions(values: list[str]) -> list[str]:
    """Lower-case extensions with a leading dot, without repeats, in the order given."""
    result: list[str] = []
    for raw in values:
        ext = raw.strip().lower()
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        if len(ext) < 2 or any(ch in ext[1:] for ch in '.\\/:*?"<>| \t'):
            raise ValueError(f"{raw!r} is not a file extension such as .jpg")
        if ext not in result:
            result.append(ext)
    return result


class DuplicatesOptions(ToolOptions):
    # ----- files -----------------------------------------------------------------------
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to check",
        description=(
            "The folders to search for duplicates, including their sub-folders. A drive root, a "
            "protected folder or a junction is refused."
        ),
        json_schema_extra=_hint("Files", widget="folders"),
    )
    media: MediaKind = Field(
        default="both",
        title="What to check",
        description="Check photos only, videos only, or both. Default: both.",
        json_schema_extra=_hint("Files"),
    )
    image_extensions: list[str] = Field(
        default_factory=lambda: sorted(IMAGE_EXT),
        min_length=1,
        title="Photo file types",
        description=(
            "The file extensions that count as photos. Each one is lower-case with a leading "
            "dot. A type you remove is never looked at. Default: the common photo types."
        ),
        json_schema_extra=_hint("Files", advanced=True, widget="ext_list"),
    )
    video_extensions: list[str] = Field(
        default_factory=lambda: sorted(VIDEO_EXT),
        min_length=1,
        title="Video file types",
        description=(
            "The file extensions that count as videos. Each one is lower-case with a leading "
            "dot. A type you remove is never looked at. Default: the common video types."
        ),
        json_schema_extra=_hint("Files", advanced=True, widget="ext_list"),
    )
    visual_skip_extensions: list[str] = Field(
        default_factory=lambda: sorted(VISUAL_SKIP_EXT),
        title="Compare only by bytes",
        description=(
            "Photo types that are compared by their exact bytes only, with no look-alike check. "
            "Default: .gif, because an animated GIF has no single picture to compare."
        ),
        json_schema_extra=_hint("Match", advanced=True, widget="ext_list"),
    )

    # ----- match -----------------------------------------------------------------------
    exact_only: bool = Field(
        default=False,
        title="Exact copies only",
        description=(
            "Find files with identical bytes only. Skips the look-alike, burst and re-encode "
            "checks, which is much faster. Default: off."
        ),
        json_schema_extra=_hint("Match"),
    )
    burst_name: str = Field(
        default=BURST_NAME_PATTERN,
        min_length=1,
        title="Burst file-name pattern",
        description=(
            "A regular expression, with exactly one group, that reads the timestamp from the "
            "start of a file name. Photos with the same timestamp are one burst. Default: "
            "an optional IMG_ then yyyymmdd_hhmmss."
        ),
        json_schema_extra=_hint("Match", advanced=True),
    )
    copy_mark: str = Field(
        default=COPY_MARK_PATTERN,
        min_length=1,
        title="Copy-name pattern",
        description=(
            "A regular expression (case does not matter) for names that look like a copy, such "
            "as 'photo (1)' or 'photo - copy'. When copies are equal otherwise, the copy is the "
            "one set aside. Default: (1), copy and - copy."
        ),
        json_schema_extra=_hint("Match", advanced=True),
    )
    verify_with_ollama: bool = Field(
        default=False,
        title="Verify borderline matches with Ollama",
        description=(
            "Ask a local Ollama vision model about close look-alike matches. Only a clear YES "
            "confirms one, and a failed check moves nothing. Needs Ollama running. Default: off."
        ),
        json_schema_extra=_hint("Ollama", advanced=True),
    )
    ollama_model: str = Field(
        default="lfm3-vl:latest",
        min_length=1,
        title="Ollama model",
        description=(
            "The Ollama vision model that checks borderline matches. It must already be pulled "
            "(ollama pull). Pick one of the models Ollama reports, or type a name. "
            "Default: lfm3-vl:latest."
        ),
        json_schema_extra=_hint(
            "Ollama",
            advanced=True,
            depends_on="verify_with_ollama=true",
            choices_from="ollama_models",
        ),
    )
    ollama_prompt: str = Field(
        default=verify_ollama.PROMPT,
        min_length=1,
        title="Ollama question",
        description=(
            "The question shown to the model with the two photos. Only an answer that starts "
            "with YES confirms a match, so keep asking for YES or NO. Default: 'Are they the "
            "same photo?'"
        ),
        json_schema_extra=_hint("Ollama", advanced=True, depends_on="verify_with_ollama=true"),
    )
    ollama_image_px: int = Field(
        default=verify_ollama.JPEG_SIZE,
        ge=64,
        le=4096,
        title="Ollama picture size (px)",
        description=(
            "Each photo is shrunk to fit inside this many pixels per side before it is sent to "
            "the model. Larger is slower and may be more accurate. Default: 512."
        ),
        json_schema_extra=_hint("Ollama", advanced=True, depends_on="verify_with_ollama=true"),
    )
    ollama_jpeg_quality: int = Field(
        default=verify_ollama.JPEG_QUALITY,
        ge=30,
        le=100,
        title="Ollama picture quality",
        description=(
            "JPEG quality, from 30 to 100, of the pictures sent to the model. Higher is a "
            "bigger upload and a sharper picture. Default: 85."
        ),
        json_schema_extra=_hint("Ollama", advanced=True, depends_on="verify_with_ollama=true"),
    )
    verify_borderline_block: float = Field(
        default=VERIFY_BORDERLINE_BLOCK,
        ge=0,
        le=255,
        title="Borderline block difference",
        description=(
            "A look-alike match whose largest 8x8 block differs by more than this many grey "
            "levels (0 to 255) is sent to the Ollama check, as is any match at its hash limit. "
            "Only used when the Ollama check is on. Default: 2.0."
        ),
        json_schema_extra=_hint("Ollama", advanced=True, depends_on="verify_with_ollama=true"),
    )

    # ----- action ----------------------------------------------------------------------
    action: Literal["move", "trash"] = Field(
        default="move",
        title="Action for exact and visual copies",
        description=(
            "Move exact and look-alike copies to the duplicate folder, or send them to the "
            "Recycle Bin. Burst and re-encode matches always move to the review folder. "
            "Default: move."
        ),
        json_schema_extra=_hint("Action"),
    )
    duplicate_folder: Path | None = Field(
        default=None,
        title="Duplicate folder",
        description=(
            "One folder for every exact and visual copy. Empty puts them in the "
            "'Duplicate folder name' setting, inside each checked folder."
        ),
        json_schema_extra=_hint("Action", advanced=True, widget="folder"),
    )
    review_folder: Path | None = Field(
        default=None,
        title="Review folder",
        description=(
            "One folder for every burst and re-encode match. Empty puts them in the "
            "'Review folder name' setting, inside each checked folder."
        ),
        json_schema_extra=_hint("Action", advanced=True, widget="folder"),
    )

    # ----- speed -----------------------------------------------------------------------
    use_cache: bool = Field(
        default=True,
        title="Use the scan cache",
        description=(
            "Reuse each photo's hashes until its size or modified time changes, so a second "
            "scan is much faster. The cache is only used for the same hash settings. Default: on."
        ),
        json_schema_extra=_hint("Speed", advanced=True),
    )
    workers: int | None = Field(
        default=None,
        ge=1,
        le=256,
        title="Worker threads",
        description=(
            "How many photos are hashed at the same time. Empty uses the 'Worker threads' "
            "setting (the number of CPU cores unless you changed it)."
        ),
        json_schema_extra=_hint("Speed", advanced=True),
    )

    # ----- hashing (advanced) ----------------------------------------------------------
    thumb_size: int = Field(
        default=THUMB,
        ge=16,
        le=512,
        multiple_of=8,
        title="Pixel-check thumbnail (px)",
        description=(
            "Side, in pixels, of the grey thumbnail used for the block-by-block pixel check. "
            "Must be a multiple of 8. The block threshold was tuned at 128, so a different "
            "size shifts how strict it is. Default: 128."
        ),
        json_schema_extra=_hint("Hashing", advanced=True),
    )
    phash_size: int = Field(
        default=PHASH_SIZE,
        ge=4,
        le=32,
        title="Photo hash grid",
        description=(
            "Side of the grid for the photo perceptual hashes, so 16 gives 256 bits. The hash "
            "limits below were tuned for 16, so a different grid needs different limits. "
            "Default: 16."
        ),
        json_schema_extra=_hint("Hashing", advanced=True),
    )
    video_phash_size: int = Field(
        default=VIDEO_PHASH_SIZE,
        ge=4,
        le=32,
        title="Video frame hash grid",
        description=(
            "Side of the grid for the perceptual hash of each sampled video frame. The frame "
            "limits below were tuned for 16. Default: 16."
        ),
        json_schema_extra=_hint("Hashing", advanced=True),
    )
    sharpness_px: int = Field(
        default=SHARPNESS_PX,
        ge=64,
        le=8192,
        title="Sharpness measure size (px)",
        description=(
            "A photo is shrunk to fit inside this many pixels per side before its sharpness is "
            "measured. Burst shots keep the sharpest frame. Default: 1600."
        ),
        json_schema_extra=_hint("Hashing", advanced=True),
    )
    frame_width: int = Field(
        default=FRAME_WIDTH,
        ge=32,
        le=3840,
        title="Video frame width (px)",
        description=(
            "Each sampled video frame is scaled to this width before it is hashed. Default: 320."
        ),
        json_schema_extra=_hint("Hashing", advanced=True),
    )
    frame_retries: int = Field(
        default=FRAME_RETRIES,
        ge=1,
        le=20,
        title="Frame grab attempts",
        description=(
            "How many times to try for a frame, stepping back in time each try, when the video "
            "track ends before the file does. Default: 6."
        ),
        json_schema_extra=_hint("Hashing", advanced=True),
    )
    frame_step_back: float = Field(
        default=FRAME_STEP_BACK,
        ge=0.1,
        le=0.99,
        title="Frame step-back factor",
        description=(
            "Each retry looks for the frame at this fraction of the previous time (0.8 means 80%). "
            "Smaller steps back faster. Default: 0.8."
        ),
        json_schema_extra=_hint("Hashing", advanced=True),
    )

    # ----- thresholds (🔒) -------------------------------------------------------------
    phash_max: int = Field(
        default=2,
        ge=0,
        le=1024,
        title="Perceptual hash limit",
        description=(
            f"{_TUNED} Most bits two photos' perceptual hashes may differ and still match. "
            "Higher finds more look-alikes but risks false matches. Default: 2."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )
    dhash_max: int = Field(
        default=4,
        ge=0,
        le=1024,
        title="Difference hash limit",
        description=(
            f"{_TUNED} Most bits two photos' difference hashes may differ and still match. "
            "Higher finds more look-alikes but risks false matches. Default: 4."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )
    aspect_tol: float = Field(
        default=0.02,
        ge=0,
        le=0.5,
        title="Aspect ratio tolerance",
        description=(
            f"{_TUNED} How much the width-to-height ratio may differ (0.02 is 2%) for photos or "
            "videos to match. Higher risks matching a crop to its original. Default: 0.02."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )
    burst_phash_max: int = Field(
        default=40,
        ge=0,
        le=1024,
        title="Burst hash limit",
        description=(
            f"{_TUNED} Most bits the hashes of two burst frames may differ for the same scene. "
            "Higher puts more different shots in review, lower misses real bursts. Default: 40."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )
    pixel_block_max: float = Field(
        default=3.0,
        ge=0,
        le=255,
        title="Pixel block limit",
        description=(
            f"{_TUNED} Largest average grey-level difference allowed in any 8x8 block of the "
            "thumbnails. It catches local changes the hashes miss. Higher risks matching photos "
            "with a moved object. Default: 3.0."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )
    dur_tol_s: float = Field(
        default=0.6,
        ge=0,
        le=3600,
        title="Video length tolerance (s)",
        description=(
            f"{_TUNED} Seconds two videos' lengths may differ and still be the same footage "
            "(or the percentage below, whichever is larger). Higher risks matching different "
            "clips. Default: 0.6."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )
    dur_tol_pct: float = Field(
        default=0.01,
        ge=0,
        le=1,
        title="Video length tolerance (share)",
        description=(
            f"{_TUNED} Share of the longer video's length it may differ by (0.01 is 1%). "
            "Higher risks matching different clips. Default: 0.01."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )
    frame_mean_max: float = Field(
        default=10,
        ge=0,
        le=1024,
        title="Frame hash average limit",
        description=(
            f"{_TUNED} Largest average hash difference over the sampled frames of two videos. "
            "Higher risks false matches, lower misses re-encodes. Default: 10."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )
    frame_worst_max: int = Field(
        default=24,
        ge=0,
        le=1024,
        title="Frame hash worst-frame limit",
        description=(
            f"{_TUNED} Largest hash difference allowed for any single sampled frame. Higher "
            "risks false matches, lower misses re-encodes. Default: 24."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )
    sample_at: list[float] = Field(
        default_factory=lambda: [0.1, 0.3, 0.5, 0.7, 0.9],
        min_length=1,
        title="Video sample points",
        description=(
            f"{_TUNED} Where frames are taken from each video, as fractions of its length "
            "(0 to 1). More points are slower but safer, fewer risk false matches. "
            "Default: 0.1, 0.3, 0.5, 0.7, 0.9."
        ),
        json_schema_extra=_hint("Thresholds", advanced=True, locked=True),
    )

    @field_validator("image_extensions", "video_extensions", "visual_skip_extensions")
    @classmethod
    def _extensions(cls, value: list[str]) -> list[str]:
        return normalise_extensions(value)

    @field_validator("burst_name")
    @classmethod
    def _burst_pattern(cls, value: str) -> str:
        try:
            groups = re.compile(value).groups
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc
        if groups != 1:
            raise ValueError("the pattern needs exactly one group, around the timestamp")
        return value

    @field_validator("copy_mark")
    @classmethod
    def _copy_pattern(cls, value: str) -> str:
        try:
            re.compile(value, re.I)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc
        return value

    @field_validator("sample_at")
    @classmethod
    def _fractions_of_the_clip(cls, value: list[float]) -> list[float]:
        if any(not 0.0 <= point <= 1.0 for point in value):
            raise ValueError("sample points are fractions of the clip, between 0 and 1")
        return value

    @model_validator(mode="after")
    def _photo_and_video_types_differ(self) -> DuplicatesOptions:
        shared = sorted(set(self.image_extensions) & set(self.video_extensions))
        if shared:
            raise ValueError(f"{', '.join(shared)} cannot be both a photo and a video type")
        return self

    def image_rules(self) -> ImageRules:
        return ImageRules(
            phash_max=self.phash_max,
            dhash_max=self.dhash_max,
            aspect_tol=self.aspect_tol,
            burst_phash_max=self.burst_phash_max,
            pixel_block_max=self.pixel_block_max,
            phash_size=self.phash_size,
            verify_borderline_block=self.verify_borderline_block,
            burst_name=self.burst_name,
            copy_mark=self.copy_mark,
        )

    def video_rules(self) -> VideoRules:
        return VideoRules(
            dur_tol_s=self.dur_tol_s,
            dur_tol_pct=self.dur_tol_pct,
            aspect_tol=self.aspect_tol,
            frame_mean_max=self.frame_mean_max,
            frame_worst_max=self.frame_worst_max,
            copy_mark=self.copy_mark,
        )

    def read_params(self, ctx: RunContext) -> ReadParams:
        """How photos are hashed. The hash read size comes from the app settings."""
        return ReadParams(
            phash_size=self.phash_size,
            thumb_size=self.thumb_size,
            sharpness_px=self.sharpness_px,
            visual_skip_ext=frozenset(self.visual_skip_extensions),
            chunk_bytes=ctx.settings.hash_chunk_kb * 1024,
        )

    def video_params(self, ctx: RunContext) -> VideoReadParams:
        """How videos are probed and sampled. The timeouts come from the app settings."""
        return VideoReadParams(
            frame_width=self.frame_width,
            phash_size=self.video_phash_size,
            frame_retries=self.frame_retries,
            frame_step_back=self.frame_step_back,
            probe_timeout_s=ctx.settings.ffprobe_timeout_s,
            frame_timeout_s=ctx.settings.frame_grab_timeout_s,
        )


@dataclass
class Findings:
    """What a scan found, before it is turned into plan actions."""

    roots: list[Path] = field(default_factory=list[Path])
    matches: list[Match] = field(default_factory=list[Match])
    notes: list[str] = field(default_factory=list[str])
    cloud_only: int = 0
    unreadable: int = 0
    # The cloud-only files themselves, for the plan's "download these files" list.
    cloud_paths: list[Path] = field(default_factory=list[Path])

    def skip_cloud_only(self, path: Path) -> None:
        """Count a file that is not on this device and note it. It is never read (rule 3)."""
        self.cloud_only += 1
        self.notes.append(f"skipped: cloud-only, not read: {path}")
        if path not in self.cloud_paths:
            self.cloud_paths.append(path)


def _cloud_only(info: FileInfo) -> bool:
    return not is_on_device(status_from_attributes(info.attributes))


def _managed_folders(roots: list[Path], opts: DuplicatesOptions, ctx: RunContext) -> list[Path]:
    """Folders the tool writes into. They are never scanned, so moved files are not re-listed."""
    managed: list[Path] = []
    for root in roots:
        managed.append(root / ctx.settings.duplicate_folder)
        managed.append(root / ctx.settings.review_folder)
    for chosen in (opts.duplicate_folder, opts.review_folder):
        if chosen is not None:
            managed.append(Path(os.path.abspath(chosen)))
    return managed


def _list_files(roots: list[Path], exts: Iterable[str], managed: list[Path]) -> list[FileInfo]:
    """Matching files under the roots, each once even when roots overlap."""
    seen: set[str] = set()
    found: list[FileInfo] = []
    extensions = list(exts)
    for root in roots:
        for info in walk(root, exts=extensions, recursive=True):
            if any(is_inside(info.path, folder) for folder in managed):
                continue
            key = os.path.normcase(os.path.abspath(info.path))
            if key in seen:
                continue
            seen.add(key)
            found.append(info)
    return found


def _image_infos(
    files: list[FileInfo], opts: DuplicatesOptions, ctx: RunContext, workers: int, out: Findings
) -> list[ImageInfo]:
    """Hash the images that are on this device, reusing cached results when they are valid."""
    params = opts.read_params(ctx)
    cache: FileCache | None = None
    if opts.use_cache:
        cache_dir = ctx.settings.resolved_cache_folder()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = FileCache(
            image_cache_path(cache_dir, params),
            name=f"{IMAGE_CACHE_NAME}:{params.cache_identity()}",
            version=IMAGE_CACHE_VERSION,
        )
    results: list[ImageInfo] = []
    todo: list[FileInfo] = []
    try:
        for info in files:
            if _cloud_only(info):
                out.skip_cloud_only(info.path)
                continue
            cached = cache.get(info.path, info.size, info.mtime) if cache else None
            if cached is not None:
                image = image_from_cache(info.path, info.size, info.mtime, cached)
                if image.w > 0 and image.h > 0:
                    results.append(image)
                    continue
                # An old cache entry without a size: read the file again, which reports it.
            todo.append(info)

        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                pool.submit(read_image, info.path, info.size, info.mtime, params): info
                for info in todo
            }
            for done, future in enumerate(as_completed(futures), start=1):
                ctx.check_cancelled()
                info = futures[future]
                ctx.report(done, len(todo), f"Hashing {info.path.name}")
                try:
                    image = future.result()
                except Exception as exc:  # one unreadable file must not stop the scan
                    out.unreadable += 1
                    out.notes.append(f"unreadable: {info.path}: {exc}")
                    continue
                results.append(image)
                if cache is not None:
                    cache.put(info.path, info.size, info.mtime, image_to_cache(image))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    finally:
        if cache is not None:
            cache.close()
    results.sort(key=lambda image: str(image.path).casefold())
    return results


def _video_infos(
    files: list[FileInfo],
    opts: DuplicatesOptions,
    ctx: RunContext,
    ffprobe: Path,
    ffmpeg_exe: Path | None,
    out: Findings,
) -> list[VideoInfo]:
    results: list[VideoInfo] = []
    params = opts.video_params(ctx)
    for info in files:
        ctx.check_cancelled()
        if _cloud_only(info):
            out.skip_cloud_only(info.path)
            continue
        try:
            results.append(
                read_video(
                    ffprobe,
                    ffmpeg_exe,
                    info.path,
                    info.size,
                    info.mtime,
                    opts.sample_at,
                    params,
                )
            )
        except Exception as exc:  # one unreadable clip must not stop the scan
            out.unreadable += 1
            out.notes.append(f"unreadable: {info.path}: {exc}")
    return results


def _confirmer(
    opts: DuplicatesOptions,
    ctx: RunContext,
    confirm: Callable[[Path, Path], bool] | None,
    out: Findings,
) -> Confirmer | None:
    """The model check for borderline visual matches. When the model cannot be used, every
    borderline match is refused, so nothing moves on a failed check."""
    if confirm is not None:
        return Confirmer(opts.ollama_model, confirm)
    if not opts.verify_with_ollama:
        return None

    refuse = Confirmer(opts.ollama_model, lambda _a, _b: False)
    # The saved ``ollama_url`` setting is tried first (after any explicit override in deps).
    location = ctx.deps.resolve_ollama(ctx.settings.ollama_url)
    if not location.valid:
        out.notes.append(
            "Ollama is not reachable, so borderline matches were not moved. "
            f"Start Ollama or check its address ({ctx.settings.ollama_url})."
        )
        return refuse
    verifier = OllamaVerifier(
        location.url or ctx.settings.ollama_url,
        opts.ollama_model,
        timeout=ctx.settings.ollama_chat_timeout_s,
        check_timeout=ctx.settings.ollama_tags_timeout_s,
        prompt=opts.ollama_prompt,
        image_px=opts.ollama_image_px,
        jpeg_quality=opts.ollama_jpeg_quality,
    )
    problem = verifier.check_model()
    if problem is not None:
        out.notes.append(f"{problem}. Borderline matches were not moved.")
        return refuse

    def confirms(a: Path, b: Path) -> bool:
        same = verifier.confirms(a, b)
        if verifier.last_error is not None:  # one bad pair is noted, and the scan goes on
            out.notes.append(
                f"Ollama check failed for {a.name} vs {b.name}: {verifier.last_error}. Not moved."
            )
        return same

    return Confirmer(opts.ollama_model, confirms)


def collect(
    opts: DuplicatesOptions,
    ctx: RunContext,
    confirm: Callable[[Path, Path], bool] | None = None,
) -> Findings:
    """Find every duplicate under the roots. Changes nothing except the scan cache.

    ``confirm`` replaces the Ollama check (used by tests and by callers with their own model).
    """
    out = Findings()
    if not opts.roots:
        out.notes.append("Choose at least one folder.")
        return out
    for raw in opts.roots:
        root = Path(os.path.abspath(raw))
        refusal = root_refusal(root, ctx.settings.protected_paths())
        if refusal is None:
            out.roots.append(root)
        else:
            out.notes.append(refusal)
    if not out.roots:
        out.notes.append("Nothing to check.")
        return out

    image_exts = set(opts.image_extensions)
    video_exts = set(opts.video_extensions)
    exts: set[str] = set()
    if opts.media in ("images", "both"):
        exts |= image_exts
    if opts.media in ("videos", "both"):
        exts |= video_exts
    files = _list_files(out.roots, exts, _managed_folders(out.roots, opts, ctx))
    image_files = [f for f in files if f.path.suffix.lower() in image_exts]
    video_files = [f for f in files if f.path.suffix.lower() in video_exts]

    if opts.media in ("images", "both"):
        workers = opts.workers or ctx.settings.workers
        infos = _image_infos(image_files, opts, ctx, workers, out)
        confirmer = None if opts.exact_only else _confirmer(opts, ctx, confirm, out)
        out.matches += images.find_matches(
            infos,
            opts.image_rules(),
            exact_only=opts.exact_only,
            confirmer=confirmer,
            ctx=ctx,
        )

    if opts.media in ("videos", "both") and video_files:
        ffprobe = ctx.deps.path("ffprobe")
        if ffprobe is None:
            out.notes.append(
                "Videos skipped: ffprobe was not found. Install it with "
                f"`{install_hint('ffprobe')}`."
            )
        else:
            ffmpeg_exe = ctx.deps.path("ffmpeg")
            if ffmpeg_exe is None and not opts.exact_only:
                out.notes.append(
                    "Same-footage check skipped: ffmpeg was not found. Exact copies are still "
                    f"found. Install it with `{install_hint('ffmpeg')}`."
                )
            vinfos = _video_infos(
                video_files,
                opts,
                ctx,
                ffprobe,
                None if opts.exact_only else ffmpeg_exe,
                out,
            )
            shas: dict[Path, str] = {}
            failed: set[Path] = set()
            chunk_bytes = ctx.settings.hash_chunk_kb * 1024
            for path in videos.size_sharers(vinfos):
                try:
                    shas[path] = hashing.sha256(path, chunk_bytes)
                except Exception as exc:  # unreadable: report and leave the file alone
                    failed.add(path)
                    out.unreadable += 1
                    out.notes.append(f"unreadable: {path}: {exc}")
            usable = [v for v in vinfos if v.path not in failed]
            out.matches += videos.find_matches(
                usable, opts.video_rules(), shas, exact_only=opts.exact_only
            )
    ctx.log.info("duplicates: %d matches", len(out.matches))
    return out


def _destination(
    match: Match, opts: DuplicatesOptions, findings: Findings, ctx: RunContext
) -> Path | None:
    """The folder a duplicate moves to. Review matches use the review folder."""
    chosen = opts.review_folder if match.review else opts.duplicate_folder
    if chosen is not None:
        return Path(os.path.abspath(chosen))
    name = ctx.settings.review_folder if match.review else ctx.settings.duplicate_folder
    root = next((r for r in findings.roots if is_inside(match.duplicate, r)), None)
    return None if root is None else root / name


def _to_plan(opts: DuplicatesOptions, findings: Findings, ctx: RunContext, plan: Plan) -> None:
    kinds: dict[str, int] = {}
    moves = trashes = 0
    refused: dict[str, str | None] = {}  # one answer per destination folder
    for match in findings.matches:
        kinds[match.kind] = kinds.get(match.kind, 0) + 1
        folder = _destination(match, opts, findings, ctx)
        if folder is None:
            plan.notes.append(f"skipped: no folder for {match.duplicate}")
            continue
        reason = f"{match.reason}; keeps {match.keeper.name}"
        if not match.review and opts.action == "trash":
            plan.actions.append(
                PlannedAction(
                    kind=ActionKind.TRASH,
                    src=match.duplicate,
                    reason=reason,
                    group_id=str(match.keeper),
                )
            )
            trashes += 1
        else:
            # Rule 4 on the output side: never move files through a junction or symlink. The
            # engine checks again at apply time.
            key = os.path.normcase(str(folder))
            if key not in refused:
                containing = [r for r in findings.roots if is_inside(folder, r)]
                stop = max(containing, key=lambda r: len(str(r))) if containing else None
                refused[key] = destination_refusal(folder, stop)
                if refused[key] is not None:
                    plan.notes.append(f"skipped: {refused[key]} ({folder})")
            if refused[key] is not None:
                continue
            plan.actions.append(
                PlannedAction(
                    kind=ActionKind.MOVE,
                    src=match.duplicate,
                    dst=folder / match.duplicate.name,
                    reason=reason,
                    group_id=str(match.keeper),
                )
            )
            moves += 1
    summary = (
        f"Duplicate finder: {kinds.get('exact', 0)} exact, {kinds.get('visual', 0)} visual, "
        f"{kinds.get('burst', 0)} burst, {kinds.get('re-encode', 0)} re-encode. "
        f"{moves} to move, {trashes} to the Recycle Bin. "
        f"{findings.cloud_only} cloud-only skipped, {findings.unreadable} unreadable."
    )
    plan.notes.insert(0, summary)


class Duplicates:
    id = TOOL_ID
    name = "Duplicate Finder"
    # Plain words for the GUI (gui/wording.py).
    scan_label = "Find duplicates"
    found_nouns = ("duplicate", "duplicates")
    description = (
        "Find identical images and videos, and the same picture or footage in another file. "
        "Burst frames and re-encoded videos go to review. Nothing moves until you apply."
    )
    category: Category = "Clean up"
    Options = DuplicatesOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, DuplicatesOptions):
            raise TypeError("Duplicates.scan needs DuplicatesOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        findings = collect(opts, ctx)
        plan.roots = findings.roots
        plan.notes.extend(findings.notes)
        plan.cloud_only.extend(findings.cloud_paths)
        if findings.roots:
            _to_plan(opts, findings, ctx, plan)
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        return apply_plan(plan, ctx)


TOOL = Duplicates()
