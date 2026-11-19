"""Corrupt media finder (plan §4 Phase 7): list pictures and videos that fail to open or decode.

``scan()`` checks each file and writes nothing. Every corrupt file becomes one planned action,
a ``move`` into a quarantine folder inside its selected folder (the relative path is kept, so
nothing collides and undo puts it back) or a ``trash`` to the Recycle Bin. Every action is
**unselected**, so nothing happens until the user ticks files and applies. ``apply()`` runs the
plan through the generic engine, so each action is journaled; a quarantine move can be undone
from History, and a trashed file is restored from the Recycle Bin by hand.

What counts as corrupt (see ``docs/tools/corrupt_media.md``):
* a zero-byte file;
* a picture Pillow cannot open, verify or fully decode (a truncated JPEG is corrupt);
* a video ffprobe cannot read, with no video stream, or with zero duration;
* with the deep check, a video ffmpeg cannot decode from end to end.

Rules that always hold:
* cloud-only OneDrive files are never opened (AGENTS.md rule 3). They are reported as skipped;
* a file that could not be checked (timeout, permission error, missing ffprobe) is only noted,
  never offered for removal;
* junctions and symlinks are never followed (rule 4), protected folders are refused (rule 6),
  and the selected folders themselves are never moved;
* the quarantine folder is never scanned, so a second run does not re-check quarantined files.
"""

from __future__ import annotations

import glob
import os
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from various_tools.core.context import RunContext
from various_tools.core.deps import install_hint
from various_tools.core.fs import FileInfo, is_inside, root_refusal, walk
from various_tools.core.media.imaging import IMAGE_EXTS, RAW_EXTS
from various_tools.core.onedrive import is_on_device, status_from_attributes
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.tools.base import Category, ToolOptions
from various_tools.tools.corrupt_media import checks
from various_tools.tools.corrupt_media.checks import CheckDepth, Verdict
from various_tools.tools.media_resize.tool import normalise_extensions

TOOL_ID = "corrupt_media"
DEFAULT_VIDEO_EXTS = (
    ".3gp",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
    ".wmv",
)
FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
_FORBIDDEN_NAME_CHARS = '\\/:*?"<>|'


def _hint(group: str, *, advanced: bool = False, locked: bool = False, **extra: Any) -> Any:
    """The ``json_schema_extra`` hints the GUI and CLI read (see ``core/settings.py``)."""
    hints: dict[str, Any] = {"group": group}
    if advanced:
        hints["advanced"] = True
    if locked:
        hints["locked"] = True
    hints.update(extra)
    return hints


class CorruptMediaOptions(ToolOptions):
    # ----- files -----------------------------------------------------------------------
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to check",
        description=(
            "The folders whose pictures and videos are checked. A drive root or a protected "
            "folder is refused. Nothing is changed until you tick files and apply."
        ),
        json_schema_extra=_hint("Files", widget="folders"),
    )
    recursive: bool = Field(
        default=True,
        title="Include sub-folders",
        description=("Also check the files in the sub-folders of each chosen folder. Default: on."),
        json_schema_extra=_hint("Files"),
    )
    media: Literal["images", "videos", "both"] = Field(
        default="both",
        title="What to check",
        description="Check pictures only, videos only, or both. Default: both.",
        json_schema_extra=_hint("Files"),
    )
    image_extensions: list[str] = Field(
        default_factory=lambda: sorted(IMAGE_EXTS),
        min_length=1,
        title="Picture file types",
        description=(
            "The picture file types that are checked, each lower-case with a leading dot. "
            "Default: .jpg .jpeg .png .heic .heif .tif .tiff .bmp .webp .gif."
        ),
        json_schema_extra=_hint("Files", advanced=True, widget="ext_list"),
    )
    video_extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_VIDEO_EXTS),
        min_length=1,
        title="Video file types",
        description=(
            "The video file types that are checked, each lower-case with a leading dot. "
            "Default: .3gp .avi .flv .m4v .mkv .mov .mp4 .mpeg .mpg .webm .wmv."
        ),
        json_schema_extra=_hint("Files", advanced=True, widget="ext_list"),
    )
    include_raw: bool = Field(
        default=False,
        title="Also check RAW photos",
        description=(
            "Also check RAW photos (.cr3) by decoding them completely, which is slow. "
            "Only used when pictures are checked. Default: off."
        ),
        json_schema_extra=_hint("Files"),
    )
    exclude_globs: list[str] = Field(
        default_factory=list,
        title="Never check",
        description=(
            "File or folder names or relative paths (patterns allowed) that are skipped, for "
            "example Thumbnails or *.preview.jpg. A matching folder is skipped with everything "
            "in it. Default: empty."
        ),
        json_schema_extra=_hint("Files", advanced=True, widget="glob_list"),
    )
    include_hidden: bool = Field(
        default=False,
        title="Include hidden files",
        description=(
            "Also check files and folders that Windows marks hidden or system, and names that "
            "start with a dot. Default: off."
        ),
        json_schema_extra=_hint("Files", advanced=True),
    )

    # ----- the check -------------------------------------------------------------------
    check_depth: CheckDepth = Field(
        default="standard",
        title="How thoroughly to check",
        description=(
            "quick: pictures are opened and verified, videos are read with ffprobe. standard: "
            "pictures are also decoded completely (this finds cut-off JPEGs). deep: videos are "
            "also decoded from end to end with ffmpeg, which can take as long as playing them. "
            "Default: standard."
        ),
        json_schema_extra=_hint("Check"),
    )
    require_duration: bool = Field(
        default=True,
        title="Videos need a duration",
        description=(
            "Count a video with no duration at all as corrupt, as well as one with a zero "
            "duration. Turn it off if your folder holds valid streams or recordings that have "
            "no duration in the file (some .webm and .mkv files). Default: on."
        ),
        json_schema_extra=_hint("Check", advanced=True),
    )
    decode_timeout_s: float | None = Field(
        default=None,
        gt=0,
        le=86400,
        title="Deep decode timeout (s)",
        description=(
            "Seconds ffmpeg may spend decoding one video in the deep check before the file is "
            "skipped with a note (it is not called corrupt). Empty uses the app setting "
            "transcode_timeout_s (default 3600)."
        ),
        json_schema_extra=_hint("Check", advanced=True, depends_on="check_depth=deep"),
    )
    workers: int | None = Field(
        default=None,
        ge=1,
        le=256,
        title="Files checked at once",
        description=(
            "How many files are checked at the same time. Empty uses the app setting workers "
            "(default: the number of CPU cores). Lower it to keep the PC responsive."
        ),
        json_schema_extra=_hint("Check", advanced=True),
    )

    # ----- what to do with corrupt files -----------------------------------------------
    action: Literal["quarantine", "trash"] = Field(
        default="quarantine",
        title="What to do with corrupt files",
        description=(
            "quarantine moves a file into the quarantine folder inside its chosen folder, "
            "keeping its relative path, and can be undone from History. trash sends it to the "
            "Recycle Bin, where you restore it by hand. Either way every file starts unticked. "
            "Default: quarantine."
        ),
        json_schema_extra=_hint("Corrupt files"),
    )
    quarantine_folder: str = Field(
        default="_quarantine",
        title="Quarantine folder name",
        description=(
            "The name of the folder, inside each chosen folder, that receives quarantined "
            "files (a plain name, not a path). It is never scanned. Default: _quarantine."
        ),
        json_schema_extra=_hint("Corrupt files", depends_on="action=quarantine"),
    )

    @field_validator("image_extensions", "video_extensions")
    @classmethod
    def _extensions(cls, value: list[str]) -> list[str]:
        return normalise_extensions(value)

    @field_validator("quarantine_folder")
    @classmethod
    def _plain_folder_name(cls, value: str) -> str:
        problem = folder_name_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value

    @model_validator(mode="after")
    def _picture_and_video_types_differ(self) -> CorruptMediaOptions:
        shared = sorted(set(self.image_extensions) & set(self.video_extensions))
        if shared:
            raise ValueError(f"{', '.join(shared)} cannot be both a picture and a video type")
        return self


def folder_name_problem(name: str) -> str | None:
    """Why ``name`` cannot be the quarantine folder, or None. A plain name only, never a path."""
    if not name.strip() or name in (".", ".."):
        return "The quarantine folder name is empty."
    if any(ch in name for ch in _FORBIDDEN_NAME_CHARS):
        return f'The quarantine folder name "{name}" cannot contain any of {_FORBIDDEN_NAME_CHARS}.'
    if name != name.rstrip(" ."):
        return "The quarantine folder name cannot end with a space or a dot, which Windows drops."
    return None


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _cloud_only(info: FileInfo) -> bool:
    return not is_on_device(status_from_attributes(info.attributes))


def _hidden_attributes(path: Path) -> int:
    try:
        return getattr(os.stat(path, follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return 0


def _is_hidden(info: FileInfo, root: Path, folders: dict[str, bool]) -> bool:
    """Hidden or system by attribute, or a dot name, for the file or any folder above it."""
    flags = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
    if info.attributes & flags or info.path.name.startswith("."):
        return True
    folder = root
    for part in info.path.relative_to(root).parts[:-1]:
        folder = folder / part
        key = _norm(folder)
        if key not in folders:
            folders[key] = part.startswith(".") or bool(_hidden_attributes(folder) & flags)
        if folders[key]:
            return True
    return False


def _usable_roots(opts: CorruptMediaOptions, ctx: RunContext, notes: list[str]) -> list[Path]:
    """Selected folders that can be scanned. Duplicates and folders inside another root drop out."""
    candidates: list[Path] = []
    for raw in opts.roots:
        root = Path(os.path.abspath(raw))
        refusal = root_refusal(root, ctx.settings.protected_paths())
        if refusal is not None:
            notes.append(refusal)
            continue
        if any(_norm(root) == _norm(other) for other in candidates):
            continue
        candidates.append(root)
    return [
        root
        for root in candidates
        if not any(_norm(root) != _norm(other) and is_inside(root, other) for other in candidates)
    ]


def _extensions(opts: CorruptMediaOptions) -> tuple[set[str], set[str]]:
    """The picture and the video extensions to check."""
    images: set[str] = set()
    videos: set[str] = set()
    if opts.media in ("images", "both"):
        images |= set(opts.image_extensions)
        if opts.include_raw:
            images |= RAW_EXTS
    if opts.media in ("videos", "both"):
        videos |= set(opts.video_extensions)
    return images, videos


@dataclass(frozen=True)
class _Candidate:
    root: Path
    info: FileInfo
    is_video: bool


def _candidates(
    roots: list[Path], opts: CorruptMediaOptions, images: set[str], videos: set[str]
) -> list[_Candidate]:
    exclude = [*opts.exclude_globs, glob.escape(opts.quarantine_folder)]
    found: list[_Candidate] = []
    for root in roots:
        hidden_cache: dict[str, bool] = {}
        for info in walk(
            root, exts=images | videos, exclude_globs=exclude, recursive=opts.recursive
        ):
            if not opts.include_hidden and _is_hidden(info, root, hidden_cache):
                continue
            found.append(_Candidate(root, info, info.path.suffix.lower() in videos))
    found.sort(key=lambda c: _norm(c.info.path))
    return found


@dataclass(frozen=True)
class _Programs:
    ffprobe: Path | None
    ffmpeg: Path | None
    probe_timeout: float
    decode_timeout: float


def _check(candidate: _Candidate, opts: CorruptMediaOptions, programs: _Programs) -> Verdict:
    """Check one file. Runs on a worker thread; it only reads."""
    info = candidate.info
    if info.size == 0:
        return Verdict("corrupt", "empty file (0 bytes)")
    if not candidate.is_video:
        return checks.check_image(info.path, opts.check_depth)
    if programs.ffprobe is None:
        return Verdict("skipped", "ffprobe is needed to check videos")
    verdict = checks.check_video_probe(
        programs.ffprobe,
        info.path,
        timeout=programs.probe_timeout,
        require_duration=opts.require_duration,
    )
    if verdict.status == "ok" and opts.check_depth == "deep" and programs.ffmpeg is not None:
        return checks.check_video_decode(
            programs.ffmpeg, info.path, timeout=programs.decode_timeout
        )
    return verdict


def _quarantine_path(candidate: _Candidate, folder: str) -> Path:
    return candidate.root / folder / candidate.info.path.relative_to(candidate.root)


def _run_checks(
    candidates: list[_Candidate],
    opts: CorruptMediaOptions,
    programs: _Programs,
    ctx: RunContext,
) -> list[Verdict]:
    """Check every candidate on a thread pool. Results are in the order of ``candidates``."""
    workers = max(1, min(opts.workers or ctx.settings.workers, len(candidates) or 1))
    total = len(candidates)
    verdicts: list[Verdict] = []
    with checks.strict_pillow(), ThreadPoolExecutor(max_workers=workers) as pool:
        futures: list[Future[Verdict]] = [
            pool.submit(_check, c, opts, programs) for c in candidates
        ]
        try:
            for index, (candidate, future) in enumerate(
                zip(candidates, futures, strict=True), start=1
            ):
                ctx.check_cancelled()
                ctx.report(index - 1, total, f"Checking {candidate.info.path.name}")
                verdicts.append(future.result())
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    ctx.report(total, total, "Checked")
    return verdicts


class CorruptMedia:
    id = TOOL_ID
    name = "Corrupt media finder"
    # Plain words for the GUI (gui/wording.py).
    scan_label = "Check photos and videos"
    found_nouns = ("damaged file", "damaged files")
    description = (
        "Find pictures and videos that fail to open or decode (empty, truncated or damaged "
        "files). Corrupt files are listed unticked, and can be moved to a quarantine folder or "
        "sent to the Recycle Bin."
    )
    category: Category = "Media"
    Options = CorruptMediaOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, CorruptMediaOptions):
            raise TypeError("CorruptMedia.scan needs CorruptMediaOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        if not opts.roots:
            plan.notes.append("Choose at least one folder.")
            return plan
        roots = _usable_roots(opts, ctx, plan.notes)
        if not roots:
            plan.notes.append("Nothing to check: no usable folder.")
            return plan
        plan.roots = roots

        images, videos = _extensions(opts)
        programs = _Programs(
            ffprobe=ctx.deps.ffprobe(),
            ffmpeg=ctx.deps.ffmpeg(),
            probe_timeout=ctx.settings.ffprobe_timeout_s,
            decode_timeout=opts.decode_timeout_s or ctx.settings.transcode_timeout_s,
        )
        candidates = _candidates(roots, opts, images, videos)

        notes: list[str] = []
        videos_present = any(c.is_video for c in candidates)
        if videos_present and programs.ffprobe is None:
            notes.append(
                "Videos are not checked: ffprobe is needed. Install it with "
                f"`{install_hint('ffmpeg')}`, or set its path in Settings."
            )
        elif videos_present and opts.check_depth == "deep" and programs.ffmpeg is None:
            notes.append(
                "The deep video check needs ffmpeg, so videos were only read with ffprobe. "
                f"Install it with `{install_hint('ffmpeg')}`, or set its path in Settings."
            )

        cloud_only = [c for c in candidates if _cloud_only(c.info)]
        for candidate in cloud_only:
            notes.append(f"skipped: cloud-only: {_relative(candidate)}")
            if candidate.info.path not in plan.cloud_only:
                plan.cloud_only.append(candidate.info.path)
        to_check = [c for c in candidates if not _cloud_only(c.info)]

        verdicts = _run_checks(to_check, opts, programs, ctx)
        corrupt = 0
        unchecked = 0
        for candidate, verdict in zip(to_check, verdicts, strict=True):
            if verdict.status == "skipped":
                unchecked += 1
                if candidate.is_video and programs.ffprobe is None:
                    continue  # one note above says why; no line per video
                notes.append(f"could not check: {_relative(candidate)} ({verdict.detail})")
            elif verdict.status == "corrupt":
                corrupt += 1
                plan.actions.append(self._action(candidate, verdict, opts))

        checked = len(to_check) - unchecked
        plan.notes.insert(0, _summary(checked, corrupt, len(cloud_only), unchecked, opts))
        plan.notes.extend(notes)
        ctx.log.info("corrupt_media scan: %d checked, %d corrupt", checked, corrupt)
        return plan

    @staticmethod
    def _action(
        candidate: _Candidate, verdict: Verdict, opts: CorruptMediaOptions
    ) -> PlannedAction:
        kind = "video" if candidate.is_video else "picture"
        reason = f"corrupt {kind}: {verdict.detail}"
        if opts.action == "trash":
            return PlannedAction(
                kind=ActionKind.TRASH, src=candidate.info.path, reason=reason, selected=False
            )
        return PlannedAction(
            kind=ActionKind.MOVE,
            src=candidate.info.path,
            dst=_quarantine_path(candidate, opts.quarantine_folder),
            reason=reason,
            selected=False,
        )

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        return apply_plan(plan, ctx)


def _relative(candidate: _Candidate) -> str:
    return candidate.info.path.relative_to(candidate.root).as_posix()


def _summary(
    checked: int, corrupt: int, cloud_only: int, unchecked: int, opts: CorruptMediaOptions
) -> str:
    what = "quarantined" if opts.action == "quarantine" else "sent to the Recycle Bin"
    text = (
        f"Checked {checked} file(s) ({opts.check_depth}): {corrupt} corrupt, "
        f"{cloud_only} skipped (cloud-only), {unchecked} could not be checked."
    )
    if corrupt:
        text += f" Tick the files to be {what}; none is ticked yet."
    return text


TOOL = CorruptMedia()
