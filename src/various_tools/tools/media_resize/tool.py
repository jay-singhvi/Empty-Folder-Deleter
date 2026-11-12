"""Media resize (plan §3.5): merges app.py (the resolution reducer) with metadata-8's shorter-side
resize and CR3 conversion.

``scan()`` decides what happens to every file and writes nothing. Each resize is a planned
``convert`` action, which carries the source and the output path. ``apply()`` runs the plan
through the generic engine, so every output is journaled.

Rules that always hold:
* a file is never enlarged (🔒 ``never_upscale``), and a file is resized only by the rule its
  mode names (for ``fit_box``, only when both sides are larger than the box);
* originals are never changed or removed. The output goes to a new folder, or next to the original
  with a ``_resized`` suffix. An existing file is never overwritten;
* cloud-only OneDrive files are never read (AGENTS.md rule 3). They are reported as skipped;
* the selected folders are never changed, junctions are not followed (rule 4), and protected
  folders are refused (rule 6). The output folder is never scanned, so a second run does not
  resize its own output;
* missing programs disable only the features that need them, with a note in the plan.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from various_tools.core.context import RunContext
from various_tools.core.deps import install_hint
from various_tools.core.fs import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    FileInfo,
    is_inside,
    long_path,
    protected_reason,
    walk,
)
from various_tools.core.media import ffmpeg as ffmpeg_media
from various_tools.core.media.ffmpeg import MediaError
from various_tools.core.media.imaging import is_raw
from various_tools.core.onedrive import CloudOnlyFileError, is_on_device, status_from_attributes
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.tools.base import ToolOptions
from various_tools.tools.media_resize import images, videos
from various_tools.tools.media_resize.geometry import (
    CRF_BY_QUALITY,
    ResizeMode,
    SizePreset,
    Target,
    VideoQuality,
    box_for,
    even,
    plan_target,
)
from various_tools.tools.media_resize.images import ImageRules, Rendered

TOOL_ID = "media_resize"
SUFFIX_TAG = "_resized"
ESTIMATE_NOTE = "(estimate)"
MEDIA_OPTIONS = ("images", "videos", "both")
MODE_LABELS: dict[str, str] = {
    "fit_box": "fit inside the box",
    "shorter_side": "shorter side",
    "longer_side": "longer side",
    "percent": "percentage",
}


class MediaResizeOptions(ToolOptions):
    roots: list[Path] = Field(
        default_factory=list,
        json_schema_extra={"widget": "folders", "group": "Files"},
    )
    recursive: bool = Field(
        default=False,
        json_schema_extra={"group": "Files"},
    )
    media: Literal["images", "videos", "both"] = Field(
        default="images",
        json_schema_extra={"group": "Files"},
    )
    mode: ResizeMode = Field(
        default="fit_box",
        json_schema_extra={"group": "Size"},
    )
    size: SizePreset = Field(
        default="1080p",
        json_schema_extra={"group": "Size"},
    )
    max_width: int | None = Field(
        default=None,
        ge=100,
        json_schema_extra={"group": "Size", "depends_on": "size=custom"},
    )
    max_height: int | None = Field(
        default=None,
        ge=100,
        json_schema_extra={"group": "Size", "depends_on": "size=custom"},
    )
    target_px: int = Field(
        default=1080,
        ge=16,
        json_schema_extra={"group": "Size", "depends_on": "mode=shorter_side|longer_side"},
    )
    percent: int = Field(
        default=50,
        ge=10,
        le=100,
        json_schema_extra={"group": "Size", "depends_on": "mode=percent"},
    )
    video_quality: VideoQuality = Field(
        default="high",
        json_schema_extra={"group": "Quality"},
    )
    jpeg_quality: int = Field(
        default=85,
        ge=1,
        le=100,
        json_schema_extra={"group": "Quality"},
    )
    keep_metadata: bool = Field(
        default=True,
        json_schema_extra={"group": "Quality", "advanced": True},
    )
    # 🔒 Files are never enlarged. The field exists so the rule is visible, and it cannot be False.
    never_upscale: Literal[True] = Field(
        default=True,
        json_schema_extra={"group": "Size", "locked": True},
    )
    output: Literal["new_folder", "suffix"] = Field(
        default="new_folder",
        json_schema_extra={"group": "Output"},
    )
    output_folder_name: str = Field(
        default="resized",
        json_schema_extra={"group": "Output", "depends_on": "output=new_folder"},
    )
    convert_raw_to_jpeg: bool = Field(
        default=False,
        json_schema_extra={"group": "Output", "advanced": True},
    )


def _folder_name_problem(name: str) -> str | None:
    """Why ``name`` cannot be the output folder, or None. A plain name only, never a path."""
    if not name.strip() or name in (".", ".."):
        return "The output folder name is empty."
    if any(ch in name for ch in '\\/:*?"<>|'):
        return f"The output folder name {name!r} contains a character Windows does not allow."
    return None


def _options_problem(opts: MediaResizeOptions) -> str | None:
    if not opts.roots:
        return "Choose at least one folder."
    if opts.mode == "fit_box" and box_for(opts.size, opts.max_width, opts.max_height) is None:
        return "The custom size needs both a maximum width and a maximum height."
    if opts.output == "new_folder":
        return _folder_name_problem(opts.output_folder_name)
    return None


def _root_refusal(root: Path, ctx: RunContext) -> str | None:
    """Why ``root`` cannot be used, or None. Opens no file; lists nothing."""
    if not root.is_dir():
        return f"Not a folder: {root}"
    try:
        attributes = getattr(
            os.stat(long_path(root), follow_symlinks=False), "st_file_attributes", 0
        )
    except OSError as exc:
        return f"Could not read the folder: {exc.strerror or exc}"
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return f"Junction or symlink, not followed: {root}"
    reason = protected_reason(root)
    if reason and not ctx.settings.allow_protected_paths:
        return f"Protected folder refused ({reason}): {root}"
    return None


def _cloud_only(info: FileInfo) -> bool:
    return not is_on_device(status_from_attributes(info.attributes))


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _extensions(opts: MediaResizeOptions) -> set[str]:
    exts: set[str] = set()
    if opts.media in ("images", "both"):
        exts |= set(images.IMAGE_FORMATS)
        if opts.convert_raw_to_jpeg:
            exts.add(".cr3")
    if opts.media in ("videos", "both"):
        exts |= videos.VIDEO_EXTS
    return exts


def _rules(opts: MediaResizeOptions) -> ImageRules:
    return ImageRules(
        mode=opts.mode,
        box=box_for(opts.size, opts.max_width, opts.max_height),
        target_px=opts.target_px,
        amount=opts.percent,
        jpeg_quality=opts.jpeg_quality,
        keep_metadata=opts.keep_metadata,
    )


def _output_path(src: Path, root: Path, opts: MediaResizeOptions, out_suffix: str) -> Path:
    if opts.output == "suffix":
        return src.with_name(f"{src.stem}{SUFFIX_TAG}{out_suffix}")
    relative = src.relative_to(root) if opts.recursive else Path(src.name)
    return root / opts.output_folder_name / relative.with_suffix(out_suffix)


def _out_suffix(src: Path) -> str:
    """RAW files become JPEGs. Every other file keeps its extension."""
    return ".jpg" if is_raw(src) else src.suffix.lower()


def _is_output(src: Path, root: Path, opts: MediaResizeOptions) -> bool:
    """True for a file this tool wrote, so that a second run does not resize it again."""
    if opts.output == "suffix":
        return src.stem.endswith(SUFFIX_TAG)
    return is_inside(src, root / opts.output_folder_name)


def _size_label(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _dims(width: int, height: int) -> str:
    return f"{width}x{height}"


@dataclass(frozen=True)
class _Candidate:
    root: Path
    info: FileInfo


def _candidates(
    roots: Iterable[Path], opts: MediaResizeOptions, exts: set[str]
) -> list[_Candidate]:
    """Files to consider, once each even when roots overlap. Output files are left out."""
    seen: set[str] = set()
    found: list[_Candidate] = []
    for root in roots:
        for info in walk(root, exts=exts, recursive=opts.recursive):
            key = os.path.normcase(os.path.abspath(info.path))
            if key in seen or _is_output(info.path, root, opts):
                continue
            seen.add(key)
            found.append(_Candidate(root, info))
    found.sort(key=lambda c: os.path.normcase(str(c.info.path)))
    return found


class _Planner:
    """Builds the actions for one scan. Keeps the output names claimed so far in the batch."""

    def __init__(self, opts: MediaResizeOptions, ctx: RunContext, plan: Plan) -> None:
        self.opts = opts
        self.ctx = ctx
        self.plan = plan
        self.rules = _rules(opts)
        self.claimed: set[str] = set()
        self.actions: list[PlannedAction] = []
        self.before = 0
        self.after = 0
        self.resized = 0
        self.estimated = False
        self.skipped = 0
        self.ffmpeg = ctx.deps.ffmpeg()
        self.ffprobe = ctx.deps.ffprobe()
        self.exiftool = ctx.deps.exiftool()

    def skip(self, reason: str, path: Path) -> None:
        self.skipped += 1
        self.plan.notes.append(f"skipped: {reason}: {path}")

    def add(self, candidate: _Candidate, dst: Path, reason: str) -> None:
        """Add one resize. An output that cannot be written is listed, but not selected."""
        key = os.path.normcase(str(dst)).casefold()
        conflict = None
        if os.path.lexists(long_path(dst)):
            conflict = "the output file already exists"
        elif key in self.claimed:
            conflict = "another file in this batch takes this output name"
        if conflict is not None:
            self.actions.append(
                PlannedAction(
                    kind=ActionKind.CONVERT,
                    src=candidate.info.path,
                    dst=dst,
                    reason=f"not resized: {conflict}. {reason}",
                    selected=False,
                )
            )
            return
        self.claimed.add(key)
        self.actions.append(
            PlannedAction(kind=ActionKind.CONVERT, src=candidate.info.path, dst=dst, reason=reason)
        )

    def image(self, candidate: _Candidate) -> None:
        src = candidate.info.path
        out_suffix = _out_suffix(src)
        if is_raw(src) and not self.opts.convert_raw_to_jpeg:
            self.skip("RAW file, and RAW conversion is off", src)
            return
        if is_raw(src) and self.opts.keep_metadata and self.exiftool is None:
            self.skip(
                "RAW conversion needs ExifTool to keep the metadata (set it in Settings)", src
            )
            return
        if _cloud_only(candidate.info):
            self.skip("cloud-only, not read", src)
            return
        try:
            result = images.render(src, out_suffix, self.rules, self.opts.convert_raw_to_jpeg)
        except Exception as exc:  # one unreadable file must not stop the batch; it is reported
            self.skip(f"could not read the image ({exc})", src)
            return
        if isinstance(result, str):
            self.skip(result, src)
            return
        self._record_image(candidate, out_suffix, result)

    def _record_image(self, candidate: _Candidate, out_suffix: str, result: Rendered) -> None:
        src = candidate.info.path
        dst = _output_path(src, candidate.root, self.opts, out_suffix)
        before = candidate.info.size
        after = len(result.data)
        self.before += before
        self.after += after
        self.resized += 1
        width, height = result.source_size
        target = result.target
        reason = (
            f"resize {_dims(width, height)} -> {_dims(target.width, target.height)}; "
            f"{_size_label(before)} -> {_size_label(after)}"
        )
        self.add(candidate, dst, reason)

    def video(self, candidate: _Candidate, ffprobe: Path) -> None:
        src = candidate.info.path
        if _cloud_only(candidate.info):
            self.skip("cloud-only, not read", src)
            return
        try:
            width, height = ffmpeg_media.display_size(ffprobe, src)
        except (MediaError, OSError, CloudOnlyFileError) as exc:
            self.skip(f"could not read the video size ({exc})", src)
            return
        target = _video_target(self.opts, width, height)
        if isinstance(target, str):
            self.skip(target, src)
            return
        before = candidate.info.size
        after = videos.estimate_bytes(before, width, height, target.width, target.height)
        self.before += before
        self.after += after
        self.resized += 1
        self.estimated = True
        dst = _output_path(src, candidate.root, self.opts, src.suffix.lower())
        reason = (
            f"resize {_dims(width, height)} -> {_dims(target.width, target.height)}; "
            f"{_size_label(before)} -> about {_size_label(after)} {ESTIMATE_NOTE}"
        )
        self.add(candidate, dst, reason)


def _video_target(opts: MediaResizeOptions, width: int, height: int) -> Target | str:
    """The even-sized target for a video, or the reason to leave it alone."""
    target = plan_target(
        opts.mode,
        width,
        height,
        box=box_for(opts.size, opts.max_width, opts.max_height),
        target_px=opts.target_px,
        amount=opts.percent,
    )
    if isinstance(target, str):
        return target
    adjusted = Target(even(target.width), even(target.height))
    if adjusted.width > width or adjusted.height > height:
        return "the target would enlarge the video, and files are never enlarged"
    return adjusted


class _Converter:
    """Runs the planned resizes at apply time. Each call checks the paths again first."""

    def __init__(self, opts: MediaResizeOptions, roots: list[Path], ctx: RunContext) -> None:
        self.opts = opts
        self.roots = roots
        self.ctx = ctx
        self.rules = _rules(opts)

    def __call__(self, action: PlannedAction) -> Path | None:
        src, dst = action.src, action.dst
        if src is None or dst is None:
            raise ValueError("the resize has no source or no output path")
        self._check_paths(src, dst)
        if os.path.lexists(long_path(dst)):
            raise FileExistsError(f"the output file already exists, so it was not replaced: {dst}")
        if src.suffix.lower() in videos.VIDEO_EXTS:
            return self._video(src, dst)
        return self._image(src, dst)

    def _check_paths(self, src: Path, dst: Path) -> None:
        if not any(is_inside(src, root) and not _same(src, root) for root in self.roots):
            raise ValueError(f"source is outside the selected folders: {src}")
        if not any(is_inside(dst, root) for root in self.roots):
            raise ValueError(f"output is outside the selected folders: {dst}")
        if not self.ctx.settings.allow_protected_paths:
            for path in (src, dst):
                reason = protected_reason(path)
                if reason:
                    raise ValueError(f"protected path ({reason}): {path}")

    def _image(self, src: Path, dst: Path) -> Path:
        if is_raw(src) and self.opts.keep_metadata and self.ctx.deps.exiftool() is None:
            raise ValueError("ExifTool is needed to keep the metadata of a RAW conversion")
        result = images.render(src, _out_suffix(src), self.rules, self.opts.convert_raw_to_jpeg)
        if isinstance(result, str):
            raise ValueError(result)
        images.write(
            result,
            dst,
            src,
            exiftool=self.ctx.deps.exiftool(),
            keep_metadata=self.opts.keep_metadata,
        )
        return dst

    def _video(self, src: Path, dst: Path) -> Path:
        ffmpeg = self.ctx.deps.ffmpeg()
        ffprobe = self.ctx.deps.ffprobe()
        if ffmpeg is None or ffprobe is None:
            raise MediaError("ffmpeg and ffprobe are needed to resize a video")
        width, height = ffmpeg_media.display_size(ffprobe, src)
        target = _video_target(self.opts, width, height)
        if isinstance(target, str):
            raise ValueError(target)
        videos.transcode_resized(
            ffmpeg,
            src,
            dst,
            target,
            crf=CRF_BY_QUALITY[self.opts.video_quality],
            keep_metadata=self.opts.keep_metadata,
        )
        stat = os.stat(src)
        os.utime(dst, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return dst


class MediaResize:
    id = TOOL_ID
    name = "Resize media"
    description = (
        "Reduce the resolution of images and videos to a box, a shorter or longer side, or a "
        "percentage. Files are never enlarged. Originals stay as they are."
    )
    category = "Media"
    Options = MediaResizeOptions

    def scan(self, opts: MediaResizeOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, MediaResizeOptions):
            raise TypeError("MediaResize.scan needs MediaResizeOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        problem = _options_problem(opts)
        if problem is not None:
            plan.notes.append(problem)
            return plan

        roots: list[Path] = []
        for raw in opts.roots:
            root = Path(os.path.abspath(raw))
            refusal = _root_refusal(root, ctx)
            if refusal is None:
                roots.append(root)
            else:
                plan.notes.append(refusal)
        if not roots:
            plan.notes.append("Nothing to resize.")
            return plan
        plan.roots = roots

        planner = _Planner(opts, ctx, plan)
        videos_missing = planner.ffmpeg is None or planner.ffprobe is None
        if opts.media in ("videos", "both") and videos_missing:
            plan.notes.append(
                "Videos are skipped: ffmpeg and ffprobe are needed. Install them with "
                f"`{install_hint('ffmpeg')}`, or set their paths in Settings."
            )
        candidates = _candidates(roots, opts, _extensions(opts))
        total = len(candidates)
        for index, candidate in enumerate(candidates, start=1):
            ctx.check_cancelled()
            ctx.report(index - 1, total, f"Checking {candidate.info.path.name}")
            if candidate.info.path.suffix.lower() in videos.VIDEO_EXTS:
                if planner.ffmpeg is not None and planner.ffprobe is not None:
                    planner.video(candidate, planner.ffprobe)
                else:
                    planner.skip("ffmpeg and ffprobe are needed", candidate.info.path)
            else:
                planner.image(candidate)
        ctx.report(total, total, "Checked")

        plan.actions = planner.actions
        plan.notes.insert(0, _summary(opts, planner))
        ctx.log.info("media_resize scan: %d candidate(s), %d to resize", total, planner.resized)
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        opts = MediaResizeOptions.model_validate_json(plan.options_json)
        converter = _Converter(opts, plan.roots, ctx)
        return apply_plan(plan, ctx, handlers={ActionKind.CONVERT: converter})


def _summary(opts: MediaResizeOptions, planner: _Planner) -> str:
    label = MODE_LABELS[opts.mode]
    blocked = sum(1 for a in planner.actions if not a.selected)
    text = (
        f"Resize ({label}): {planner.resized} file(s) will be resized, {planner.skipped} skipped, "
        f"{blocked} not resized because the output already exists or a name is taken."
    )
    if planner.resized:
        saved = max(0, planner.before - planner.after)
        share = saved * 100 // planner.before if planner.before else 0
        text += (
            f" Total {_size_label(planner.before)} -> {_size_label(planner.after)}"
            f", about {share}% smaller"
            + (" (video sizes are estimates)." if planner.estimated else ".")
        )
    return text


TOOL = MediaResize()
