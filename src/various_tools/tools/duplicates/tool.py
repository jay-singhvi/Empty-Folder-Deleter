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
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import platformdirs
from pydantic import Field, field_validator

from various_tools.core import hashing
from various_tools.core.cache import FileCache
from various_tools.core.context import RunContext
from various_tools.core.fs import (
    FileInfo,
    destination_refusal,
    is_inside,
    root_refusal,
    walk,
)
from various_tools.core.onedrive import is_on_device, status_from_attributes
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.core.settings import APP_NAME
from various_tools.tools.base import ToolOptions
from various_tools.tools.duplicates import images, videos
from various_tools.tools.duplicates.images import (
    IMAGE_EXT,
    Confirmer,
    ImageInfo,
    ImageRules,
    image_from_cache,
    image_to_cache,
    read_image,
)
from various_tools.tools.duplicates.keeper import Match
from various_tools.tools.duplicates.verify_ollama import OllamaVerifier
from various_tools.tools.duplicates.videos import VIDEO_EXT, VideoInfo, VideoRules, read_video

TOOL_ID = "duplicates"
MediaKind = Literal["images", "videos", "both"]
IMAGE_CACHE_NAME = "duplicates.images"
# Bump when the hashing logic or the cached fields change. Old entries are then dropped.
IMAGE_CACHE_VERSION = 1
IMAGE_CACHE_FILE = "duplicates-images.sqlite"

# Where the image scan cache lives. Tests point this at a temporary folder.
CACHE_DIR: Path = Path(platformdirs.user_cache_dir(APP_NAME))


class DuplicatesOptions(ToolOptions):
    roots: list[Path] = Field(
        default_factory=list,
        json_schema_extra={"widget": "folders", "group": "Files"},
    )
    media: MediaKind = Field(
        default="both",
        json_schema_extra={"group": "Files"},
    )
    exact_only: bool = Field(
        default=False,
        json_schema_extra={"group": "Match"},
    )
    action: Literal["move", "trash"] = Field(
        default="move",
        json_schema_extra={"group": "Action"},
    )
    duplicate_folder: Path | None = Field(
        default=None,
        json_schema_extra={"widget": "folder", "group": "Action", "advanced": True},
    )
    review_folder: Path | None = Field(
        default=None,
        json_schema_extra={"widget": "folder", "group": "Action", "advanced": True},
    )
    use_cache: bool = Field(
        default=True,
        json_schema_extra={"group": "Speed", "advanced": True},
    )
    workers: int | None = Field(
        default=None,
        ge=1,
        json_schema_extra={"group": "Speed", "advanced": True},
    )
    verify_with_ollama: bool = Field(
        default=False,
        json_schema_extra={"group": "Match", "advanced": True},
    )
    ollama_model: str = Field(
        default="lfm3-vl:latest",
        json_schema_extra={"group": "Match", "advanced": True},
    )
    # Thresholds (AGENTS.md 🔒). Prefilled with the values tuned on real data.
    phash_max: int = Field(
        default=2, ge=0, le=64, json_schema_extra={"group": "Thresholds", "advanced": True}
    )
    dhash_max: int = Field(
        default=4, ge=0, le=64, json_schema_extra={"group": "Thresholds", "advanced": True}
    )
    aspect_tol: float = Field(
        default=0.02, ge=0, le=0.5, json_schema_extra={"group": "Thresholds", "advanced": True}
    )
    burst_phash_max: int = Field(
        default=40, ge=0, le=256, json_schema_extra={"group": "Thresholds", "advanced": True}
    )
    pixel_block_max: float = Field(
        default=3.0, ge=0, json_schema_extra={"group": "Thresholds", "advanced": True}
    )
    dur_tol_s: float = Field(
        default=0.6, ge=0, json_schema_extra={"group": "Thresholds", "advanced": True}
    )
    dur_tol_pct: float = Field(
        default=0.01, ge=0, le=1, json_schema_extra={"group": "Thresholds", "advanced": True}
    )
    frame_mean_max: float = Field(
        default=10, ge=0, le=256, json_schema_extra={"group": "Thresholds", "advanced": True}
    )
    frame_worst_max: int = Field(
        default=24, ge=0, le=256, json_schema_extra={"group": "Thresholds", "advanced": True}
    )
    sample_at: list[float] = Field(
        default_factory=lambda: [0.1, 0.3, 0.5, 0.7, 0.9],
        min_length=1,
        json_schema_extra={"group": "Thresholds", "advanced": True},
    )

    @field_validator("sample_at")
    @classmethod
    def _fractions_of_the_clip(cls, value: list[float]) -> list[float]:
        if any(not 0.0 <= point <= 1.0 for point in value):
            raise ValueError("sample points are fractions of the clip, between 0 and 1")
        return value

    def image_rules(self) -> ImageRules:
        return ImageRules(
            phash_max=self.phash_max,
            dhash_max=self.dhash_max,
            aspect_tol=self.aspect_tol,
            burst_phash_max=self.burst_phash_max,
            pixel_block_max=self.pixel_block_max,
        )

    def video_rules(self) -> VideoRules:
        return VideoRules(
            dur_tol_s=self.dur_tol_s,
            dur_tol_pct=self.dur_tol_pct,
            aspect_tol=self.aspect_tol,
            frame_mean_max=self.frame_mean_max,
            frame_worst_max=self.frame_worst_max,
        )


@dataclass
class Findings:
    """What a scan found, before it is turned into plan actions."""

    roots: list[Path] = field(default_factory=list[Path])
    matches: list[Match] = field(default_factory=list[Match])
    notes: list[str] = field(default_factory=list[str])
    cloud_only: int = 0
    unreadable: int = 0


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
    cache: FileCache | None = None
    if opts.use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache = FileCache(
            CACHE_DIR / IMAGE_CACHE_FILE, name=IMAGE_CACHE_NAME, version=IMAGE_CACHE_VERSION
        )
    results: list[ImageInfo] = []
    todo: list[FileInfo] = []
    try:
        for info in files:
            if _cloud_only(info):
                out.cloud_only += 1
                out.notes.append(f"skipped: cloud-only, not read: {info.path}")
                continue
            cached = cache.get(info.path, info.size, info.mtime) if cache else None
            if cached is not None:
                results.append(image_from_cache(info.path, info.size, info.mtime, cached))
            else:
                todo.append(info)

        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                pool.submit(read_image, info.path, info.size, info.mtime): info for info in todo
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
    for info in files:
        ctx.check_cancelled()
        if _cloud_only(info):
            out.cloud_only += 1
            out.notes.append(f"skipped: cloud-only, not read: {info.path}")
            continue
        try:
            results.append(
                read_video(ffprobe, ffmpeg_exe, info.path, info.size, info.mtime, opts.sample_at)
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
    verifier = OllamaVerifier(location.url or ctx.settings.ollama_url, opts.ollama_model)
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
        refusal = root_refusal(root, ctx.settings.allow_protected_paths)
        if refusal is None:
            out.roots.append(root)
        else:
            out.notes.append(refusal)
    if not out.roots:
        out.notes.append("Nothing to check.")
        return out

    exts: set[str] = set()
    if opts.media in ("images", "both"):
        exts |= IMAGE_EXT
    if opts.media in ("videos", "both"):
        exts |= VIDEO_EXT
    files = _list_files(out.roots, exts, _managed_folders(out.roots, opts, ctx))
    image_files = [f for f in files if f.path.suffix.lower() in IMAGE_EXT]
    video_files = [f for f in files if f.path.suffix.lower() in VIDEO_EXT]

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
                "`winget install Gyan.FFmpeg`."
            )
        else:
            ffmpeg_exe = ctx.deps.path("ffmpeg")
            if ffmpeg_exe is None and not opts.exact_only:
                out.notes.append(
                    "Same-footage check skipped: ffmpeg was not found. Exact copies are still "
                    "found. Install it with `winget install Gyan.FFmpeg`."
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
            for path in videos.size_sharers(vinfos):
                try:
                    shas[path] = hashing.sha256(path)
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
    description = (
        "Find identical images and videos, and the same picture or footage in another file. "
        "Burst frames and re-encoded videos go to review. Nothing moves until you apply."
    )
    category = "Clean up"
    Options = DuplicatesOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, DuplicatesOptions):
            raise TypeError("Duplicates.scan needs DuplicatesOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        findings = collect(opts, ctx)
        plan.roots = findings.roots
        plan.notes.extend(findings.notes)
        if findings.roots:
            _to_plan(opts, findings, ctx, plan)
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        return apply_plan(plan, ctx)


TOOL = Duplicates()
