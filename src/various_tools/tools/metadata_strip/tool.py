"""Metadata stripper (plan §4 Phase 7, item 4): remove GPS, or all metadata, before sharing.

``scan()`` reads the tags of every file and writes nothing. The plan lists what each file would
lose (for example ``remove GPS: position, altitude (6 tags)``), and a file with nothing to remove
is not an action. Each stripped file is a planned ``convert`` action carrying the source and the
output path. ``apply()`` runs the plan through the generic engine, so every output is journaled.

Rules that always hold:
* the work is done by ExifTool, which edits the metadata blocks and leaves the picture or video
  data as it is (no re-encode, no quality loss). Without ExifTool nothing is changed and the plan
  says how to install it: a Pillow re-encode would lose quality, so it is not offered;
* the original is never changed. The stripped copy goes to a new folder, or next to the original
  with a suffix. An existing file is never overwritten (checked at scan and again at apply);
* the optional in-place mode is recoverable: the stripped copy is written to a temporary name and
  checked (the metadata is really gone and the picture or video is intact), the original goes to
  the Recycle Bin, and only then does the copy take the original's name;
* cloud-only OneDrive files are never read (AGENTS.md rule 3);
* the selected folders are never changed beyond the files named in the plan, junctions are not
  followed (rule 4) and protected folders are refused (rule 6). The output folder is never
  scanned, so a second run does not strip its own output.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from send2trash import send2trash

from various_tools.core.context import RunContext
from various_tools.core.deps import install_hint
from various_tools.core.fs import (
    FileInfo,
    destination_refusal,
    is_inside,
    long_path,
    protected_reason,
    rename_no_replace,
    root_refusal,
    walk,
)
from various_tools.core.media import ffmpeg as ffmpeg_media
from various_tools.core.media.ffmpeg import MediaError
from various_tools.core.media.imaging import open_image
from various_tools.core.onedrive import ensure_on_device, is_on_device, status_from_attributes
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.tools.base import Category, ToolOptions
from various_tools.tools.media_resize.tool import normalise_extensions
from various_tools.tools.metadata_strip.exiftool import (
    TagRead,
    describe_removal,
    read_tags,
    removable_tags,
    run_exiftool,
    strip_args,
)

TOOL_ID = "metadata_strip"
SUFFIX_TAG = "_stripped"
OUTPUT_FOLDER = "stripped"
# File types ExifTool can write without touching the picture or video data.
WRITABLE_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff")
WRITABLE_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".3gp", ".3g2")
# ExifTool cannot delete the main metadata block of a TIFF, so only GPS can be removed there.
GPS_ONLY_EXTS = frozenset({".tif", ".tiff"})
SCOPE_LABELS: dict[str, str] = {"gps": "GPS", "all": "all metadata"}
IN_PLACE_NOTE = (
    "in place: the original goes to the Recycle Bin, then the stripped copy takes its name"
)


def _hint(group: str, *, advanced: bool = False, locked: bool = False, **extra: Any) -> Any:
    """The ``json_schema_extra`` hints the GUI and CLI read (see ``core/settings.py``)."""
    hints: dict[str, Any] = {"group": group}
    if advanced:
        hints["advanced"] = True
    if locked:
        hints["locked"] = True
    hints.update(extra)
    return hints


_FORBIDDEN_NAME_CHARS = '\\/:*?"<>|'


class MetadataStripOptions(ToolOptions):
    # ----- files -----------------------------------------------------------------------
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to clean",
        description=(
            "The folders whose pictures and videos lose their metadata. A drive root or a "
            "protected folder is refused. Originals are not changed unless 'Replace the "
            "originals' is on."
        ),
        json_schema_extra=_hint("Files", widget="folders"),
    )
    recursive: bool = Field(
        default=False,
        title="Include sub-folders",
        description="Also clean files in the sub-folders of each chosen folder. Default: off.",
        json_schema_extra=_hint("Files"),
    )
    media: Literal["images", "videos", "both"] = Field(
        default="images",
        title="What to clean",
        description="Clean pictures only, videos only, or both. Default: pictures only.",
        json_schema_extra=_hint("Files"),
    )
    image_extensions: list[str] = Field(
        default_factory=lambda: list(WRITABLE_IMAGE_EXTS),
        min_length=1,
        title="Picture file types",
        description=(
            "The picture file types that are cleaned, each lower-case with a leading dot. Only "
            "types ExifTool can edit without re-encoding are allowed: .jpg, .jpeg, .png, .webp, "
            ".heic, .heif, .tif and .tiff. Default: all of them. TIFF files can only lose GPS."
        ),
        json_schema_extra=_hint("Files", advanced=True, widget="ext_list"),
    )
    video_extensions: list[str] = Field(
        default_factory=lambda: [".mp4", ".mov", ".m4v"],
        min_length=1,
        title="Video file types",
        description=(
            "The video file types that are cleaned, each lower-case with a leading dot. Only "
            "MP4 and QuickTime family types are allowed: .mp4, .mov, .m4v, .3gp and .3g2. "
            "Default: .mp4, .mov and .m4v."
        ),
        json_schema_extra=_hint("Files", advanced=True, widget="ext_list"),
    )

    # ----- what to remove -------------------------------------------------------------
    scope: Literal["gps", "all"] = Field(
        default="gps",
        title="What to remove",
        description=(
            "GPS removes only the location (coordinates, altitude, GPS time) and keeps the "
            "camera details and dates. All removes every EXIF, XMP and IPTC tag, the thumbnail "
            "and the maker notes. Video container dates and track details always stay. "
            "Default: GPS."
        ),
        json_schema_extra=_hint("What to remove"),
    )
    keep_orientation: bool = Field(
        default=True,
        title="Keep picture orientation",
        description=(
            "When removing all metadata, keep the orientation tag so rotated phone photos still "
            "show the right way up. Turning it off can make some pictures appear sideways. "
            "Default: on."
        ),
        json_schema_extra=_hint("What to remove", depends_on="scope=all"),
    )
    keep_color_profile: bool = Field(
        default=True,
        title="Keep colour profile",
        description=(
            "When removing all metadata, keep the embedded ICC colour profile so colours look "
            "the same. Turning it off removes the profile and can shift colours slightly. "
            "Default: on."
        ),
        json_schema_extra=_hint("What to remove", depends_on="scope=all"),
    )

    # ----- output ----------------------------------------------------------------------
    output: Literal["new_folder", "suffix"] = Field(
        default="new_folder",
        title="Where the clean copies go",
        description=(
            "Write the clean copies into a new folder inside each chosen folder, or next to the "
            "original with a suffix added to the name. Ignored when 'Replace the originals' is "
            "on. Default: new folder."
        ),
        json_schema_extra=_hint("Output", depends_on="in_place=false"),
    )
    output_folder_name: str = Field(
        default=OUTPUT_FOLDER,
        min_length=1,
        title="Output folder name",
        description=(
            "Name of the new folder for the clean copies. This tool has its own name and does "
            "not use the 'Resized output folder name' setting. Default: stripped."
        ),
        json_schema_extra=_hint("Output", depends_on="output=new_folder"),
    )
    suffix_tag: str = Field(
        default=SUFFIX_TAG,
        min_length=1,
        title="File name suffix",
        description=(
            "Text added to the end of a file name, before the extension, for the suffix output "
            "(photo.jpg becomes photo_stripped.jpg). Files already ending with it are skipped. "
            "Default: _stripped."
        ),
        json_schema_extra=_hint("Output", depends_on="output=suffix"),
    )
    in_place: bool = Field(
        default=False,
        title="Replace the originals",
        description=(
            "Instead of making copies, put each clean file under the original's name. This is "
            "recoverable: the clean copy is written and checked first, the original goes to the "
            "Recycle Bin, then the copy is renamed. Undo does not restore it automatically; "
            "restore the original from the Recycle Bin by hand. Default: off."
        ),
        json_schema_extra=_hint("Output", advanced=True),
    )

    # ----- advanced --------------------------------------------------------------------
    ignore_minor_errors: bool = Field(
        default=False,
        title="Ignore minor ExifTool errors",
        description=(
            "Pass ExifTool's -m option, which lets it carry on past minor problems in damaged "
            "metadata. The written copy is still checked afterwards. Default: off."
        ),
        json_schema_extra=_hint("Advanced", advanced=True),
    )
    read_batch_size: int = Field(
        default=50,
        ge=1,
        le=500,
        title="Files read per ExifTool call",
        description=(
            "How many files the preview reads in one ExifTool run. Bigger batches are faster but "
            "a slow file delays more of them. Default: 50."
        ),
        json_schema_extra=_hint("Advanced", advanced=True),
    )
    duration_tolerance_s: float = Field(
        default=0.5,
        ge=0.05,
        le=60.0,
        title="Video duration tolerance (s)",
        description=(
            "After stripping a video, its length must match the original within this many "
            "seconds (needs ffprobe), or the copy is rejected. Default: 0.5."
        ),
        json_schema_extra=_hint("Advanced", advanced=True),
    )

    @field_validator("image_extensions")
    @classmethod
    def _editable_image_types(cls, value: list[str]) -> list[str]:
        exts = normalise_extensions(value)
        unsupported = [ext for ext in exts if ext not in WRITABLE_IMAGE_EXTS]
        if unsupported:
            raise ValueError(
                f"cannot edit {', '.join(unsupported)}; allowed: {', '.join(WRITABLE_IMAGE_EXTS)}"
            )
        return exts

    @field_validator("video_extensions")
    @classmethod
    def _editable_video_types(cls, value: list[str]) -> list[str]:
        exts = normalise_extensions(value)
        unsupported = [ext for ext in exts if ext not in WRITABLE_VIDEO_EXTS]
        if unsupported:
            raise ValueError(
                f"cannot edit {', '.join(unsupported)}; allowed: {', '.join(WRITABLE_VIDEO_EXTS)}"
            )
        return exts

    @field_validator("suffix_tag")
    @classmethod
    def _suffix_is_a_plain_name_part(cls, value: str) -> str:
        if not value.strip() or any(ch in value for ch in _FORBIDDEN_NAME_CHARS):
            raise ValueError('the suffix needs text, and cannot contain any of \\ / : * ? " < > |')
        if value != value.rstrip(" ."):
            raise ValueError("the suffix cannot end with a space or a dot, which Windows drops")
        return value


class StripError(RuntimeError):
    """A stripped copy could not be made or failed its check. The message goes to the journal."""


def _folder_name_problem(name: str) -> str | None:
    if not name.strip() or name in (".", ".."):
        return "The output folder name is empty."
    if any(ch in name for ch in _FORBIDDEN_NAME_CHARS):
        return f"The output folder name {name!r} contains a character Windows does not allow."
    return None


def _options_problem(opts: MetadataStripOptions) -> str | None:
    if not opts.roots:
        return "Choose at least one folder."
    if opts.output == "new_folder" and not opts.in_place:
        return _folder_name_problem(opts.output_folder_name)
    return None


def _is_video(path: Path, opts: MetadataStripOptions) -> bool:
    return path.suffix.lower() in opts.video_extensions


def _extensions(opts: MetadataStripOptions) -> set[str]:
    exts: set[str] = set()
    if opts.media in ("images", "both"):
        exts |= set(opts.image_extensions)
    if opts.media in ("videos", "both"):
        exts |= set(opts.video_extensions)
    return exts


def _cloud_only(info: FileInfo) -> bool:
    return not is_on_device(status_from_attributes(info.attributes))


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _output_path(src: Path, root: Path, opts: MetadataStripOptions) -> Path:
    if opts.in_place:
        return src
    if opts.output == "suffix":
        return src.with_name(f"{src.stem}{opts.suffix_tag}{src.suffix}")
    relative = src.relative_to(root) if opts.recursive else Path(src.name)
    return root / opts.output_folder_name / relative


def _is_output(src: Path, root: Path, opts: MetadataStripOptions) -> bool:
    """True for a file this tool wrote, so that a second run does not strip it again."""
    if opts.in_place:
        return False
    if opts.output == "suffix":
        return src.stem.endswith(opts.suffix_tag)
    return is_inside(src, root / opts.output_folder_name)


def _size_label(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


@dataclass(frozen=True)
class _Candidate:
    root: Path
    info: FileInfo


def _candidates(
    roots: Iterable[Path], opts: MetadataStripOptions, exts: set[str]
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


def _chunks(items: list[_Candidate], size: int) -> Iterable[list[_Candidate]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class _Planner:
    """Builds the actions for one scan. Keeps the output names claimed so far in the batch."""

    def __init__(self, opts: MetadataStripOptions, plan: Plan) -> None:
        self.opts = opts
        self.plan = plan
        self.claimed: set[str] = set()
        self.actions: list[PlannedAction] = []
        self.stripped = 0
        self.clean = 0
        self.skipped = 0

    def skip(self, reason: str, path: Path) -> None:
        self.skipped += 1
        self.plan.notes.append(f"skipped: {reason}: {path}")

    def consider(self, candidate: _Candidate, read: TagRead) -> None:
        src = candidate.info.path
        if read.error is not None:
            self.skip(f"could not read the tags ({read.error})", src)
            return
        labels = removable_tags(
            read.tags,
            self.opts.scope,
            keep_orientation=self.opts.keep_orientation,
            keep_color_profile=self.opts.keep_color_profile,
        )
        if not labels:
            self.clean += 1
            return
        reason = describe_removal(labels, self.opts.scope)
        if self.opts.in_place:
            reason += f"; {IN_PLACE_NOTE}"
        self._add(candidate, _output_path(src, candidate.root, self.opts), reason)

    def _add(self, candidate: _Candidate, dst: Path, reason: str) -> None:
        """Add one strip. An output that cannot be written is listed, but not selected."""
        src = candidate.info.path
        key = os.path.normcase(str(dst)).casefold()
        conflict = None
        if not self.opts.in_place and os.path.lexists(long_path(dst)):
            conflict = "the output file already exists"
        elif key in self.claimed:
            conflict = "another file in this batch takes this output name"
        if conflict is not None:
            self.actions.append(
                PlannedAction(
                    kind=ActionKind.CONVERT,
                    src=src,
                    dst=dst,
                    reason=f"not stripped: {conflict}. {reason}",
                    selected=False,
                )
            )
            return
        self.claimed.add(key)
        self.stripped += 1
        self.actions.append(PlannedAction(kind=ActionKind.CONVERT, src=src, dst=dst, reason=reason))


def _chunk_read(
    exiftool: Path,
    batch: list[_Candidate],
    ctx: RunContext,
) -> dict[Path, TagRead]:
    """Read one batch. If ExifTool itself fails, every file in the batch gets the error."""
    paths = [c.info.path for c in batch]
    try:
        return read_tags(exiftool, paths, ctx.settings.exiftool_read_timeout_s)
    except subprocess.TimeoutExpired:
        return {p: TagRead({}, "ExifTool timed out") for p in paths}
    except (OSError, ValueError) as exc:
        return {p: TagRead({}, f"ExifTool could not run: {exc}") for p in paths}


class _KeptCopyError(StripError):
    """The original was recycled but the copy could not take its name; the copy must stay."""


class _Stripper:
    """Runs the planned strips at apply time. Each call checks the paths again first."""

    def __init__(self, opts: MetadataStripOptions, roots: list[Path], ctx: RunContext) -> None:
        self.opts = opts
        self.roots = roots
        self.ctx = ctx

    def __call__(self, action: PlannedAction) -> Path | None:
        src, dst = action.src, action.dst
        if src is None or dst is None:
            raise ValueError("the strip has no source or no output path")
        self._check_paths(src, dst)
        ensure_on_device(src)  # rule 3, checked again: the file may have been recalled since scan
        suffix = src.suffix.lower()
        if suffix not in self.opts.image_extensions and suffix not in self.opts.video_extensions:
            raise ValueError(f"not a file type this tool cleans: {src.name}")
        if self.opts.scope == "all" and suffix in GPS_ONLY_EXTS:
            raise ValueError("ExifTool can remove only GPS from a TIFF, not all metadata")
        if self.opts.in_place:
            if not _same(src, dst):
                raise ValueError("a replace-in-place strip must keep the original's path")
        elif os.path.lexists(long_path(dst)):
            raise FileExistsError(f"the output file already exists, so it was not replaced: {dst}")
        refusal = destination_refusal(dst, stop=self._root_of(dst))
        if refusal is not None:
            raise ValueError(refusal)
        exiftool = self.ctx.deps.exiftool()
        if exiftool is None:
            raise StripError(
                f"ExifTool is needed. Install it with `{install_hint('exiftool')}`, "
                "or set its path in Settings"
            )
        return self._strip(exiftool, src, dst, video=_is_video(src, self.opts))

    def _root_of(self, dst: Path) -> Path | None:
        return next((root for root in self.roots if is_inside(dst, root)), None)

    def _check_paths(self, src: Path, dst: Path) -> None:
        if not any(is_inside(src, root) and not _same(src, root) for root in self.roots):
            raise ValueError(f"source is outside the selected folders: {src}")
        if not any(is_inside(dst, root) for root in self.roots):
            raise ValueError(f"output is outside the selected folders: {dst}")
        if not self.ctx.settings.allow_protected_paths:
            rules = self.ctx.settings.protected_paths()
            for path in (src, dst):
                reason = protected_reason(path, rules=rules)
                if reason:
                    raise ValueError(f"protected path ({reason}): {path}")

    def _strip(self, exiftool: Path, src: Path, dst: Path, *, video: bool) -> Path:
        dst.parent.mkdir(parents=True, exist_ok=True)
        staging = dst.parent / f".strip-{uuid.uuid4().hex[:12]}.tmp"
        keep_staging = False
        try:
            self._write_copy(exiftool, src, staging, video=video)
            self._verify(exiftool, src, staging, video=video)
            stat = os.stat(src)
            os.utime(staging, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            if self.opts.in_place:
                self._replace_original(src, staging)
            else:
                rename_no_replace(staging, dst)
        except _KeptCopyError:
            keep_staging = True  # the original is already recycled; this copy is all that is left
            raise
        finally:
            if not keep_staging:
                staging.unlink(missing_ok=True)
        return dst

    def _write_copy(self, exiftool: Path, src: Path, staging: Path, *, video: bool) -> None:
        args = [
            "-o",
            str(staging),
            *strip_args(
                self.opts.scope,
                keep_orientation=self.opts.keep_orientation,
                keep_color_profile=self.opts.keep_color_profile,
                ignore_minor_errors=self.opts.ignore_minor_errors,
                video=video,
            ),
            str(src),
        ]
        try:
            result = run_exiftool(exiftool, args, self.ctx.settings.exiftool_write_timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise StripError(f"ExifTool timed out on {src.name}") from exc
        except (OSError, ValueError) as exc:
            raise StripError(f"ExifTool could not run on {src.name}: {exc}") from exc
        if result.returncode != 0 or not staging.is_file():
            detail = (result.stderr or result.stdout or "").strip()[:300]
            raise StripError(f"ExifTool could not write the clean copy of {src.name}: {detail}")

    def _verify(self, exiftool: Path, src: Path, staging: Path, *, video: bool) -> None:
        """The copy must have no removable tags left and must still be the same picture or video."""
        try:
            read = read_tags(exiftool, [staging], self.ctx.settings.exiftool_read_timeout_s)[
                staging
            ]
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            raise StripError(f"could not check the clean copy of {src.name}: {exc}") from exc
        if read.error is not None:
            raise StripError(f"the clean copy of {src.name} cannot be read back: {read.error}")
        left = removable_tags(
            read.tags,
            self.opts.scope,
            keep_orientation=self.opts.keep_orientation,
            keep_color_profile=self.opts.keep_color_profile,
        )
        if left:
            shown = ", ".join(left[:5]) + (" ..." if len(left) > 5 else "")
            raise StripError(
                f"metadata is still in the clean copy of {src.name} ({shown}); it was not kept"
            )
        if video:
            self._verify_duration(src, staging)
        else:
            with open_image(src) as before, open_image(staging) as after:
                if before.size != after.size:
                    raise StripError(f"the clean copy of {src.name} has a different picture size")

    def _verify_duration(self, src: Path, staging: Path) -> None:
        ffprobe = self.ctx.deps.ffprobe()
        if ffprobe is None:
            if staging.stat().st_size == 0:
                raise StripError(f"the clean copy of {src.name} is empty")
            return
        timeout = self.ctx.settings.ffprobe_timeout_s
        try:
            before = ffmpeg_media.probe_duration(ffprobe, src, timeout=timeout)
            after = ffmpeg_media.probe_duration(ffprobe, staging, timeout=timeout)
        except MediaError as exc:
            raise StripError(f"could not check the clean copy of {src.name}: {exc}") from exc
        if abs(before - after) > self.opts.duration_tolerance_s:
            raise StripError(
                f"the clean copy of {src.name} is {after:.2f} s long, the original {before:.2f} s"
            )

    def _replace_original(self, src: Path, staging: Path) -> None:
        """Recycle the original, then rename the checked copy to its name.

        Raises :class:`_KeptCopyError` when the original is already in the Recycle Bin but the
        rename failed, so the caller does not delete the only remaining copy.
        """
        send2trash(str(src))
        try:
            rename_no_replace(staging, src)
        except OSError as exc:
            raise _KeptCopyError(
                f"the original is in the Recycle Bin, and the clean copy is at {staging}; "
                f"rename it to {src.name} to finish ({exc})"
            ) from exc


class MetadataStrip:
    id = TOOL_ID
    name = "Strip metadata"
    description = (
        "Remove GPS location, or all metadata, from pictures and videos before you share them. "
        "A preview lists what each file would lose. Needs ExifTool, which edits the metadata "
        "without re-encoding, so quality is unchanged."
    )
    category: Category = "Media"
    Options = MetadataStripOptions

    def scan(self, opts: MetadataStripOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, MetadataStripOptions):
            raise TypeError("MetadataStrip.scan needs MetadataStripOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        problem = _options_problem(opts)
        if problem is not None:
            plan.notes.append(problem)
            return plan

        roots: list[Path] = []
        for raw in opts.roots:
            root = Path(os.path.abspath(raw))
            refusal = root_refusal(root, ctx.settings.protected_paths())
            if refusal is None:
                roots.append(root)
            else:
                plan.notes.append(refusal)
        if not roots:
            plan.notes.append("Nothing to clean.")
            return plan
        plan.roots = roots

        exiftool = ctx.deps.exiftool()
        if exiftool is None:
            plan.notes.append(
                "ExifTool is needed to remove metadata without re-encoding, and it was not found. "
                f"Install it with `{install_hint('exiftool')}`, or set its path in Settings. "
                "Nothing was changed."
            )
            return plan

        planner = _Planner(opts, plan)
        candidates = _candidates(roots, opts, _extensions(opts))
        eligible: list[_Candidate] = []
        for candidate in candidates:
            path = candidate.info.path
            if _cloud_only(candidate.info):
                planner.skip("cloud-only, not read", path)
            elif opts.scope == "all" and path.suffix.lower() in GPS_ONLY_EXTS:
                planner.skip("ExifTool can remove only GPS from a TIFF, not all metadata", path)
            else:
                eligible.append(candidate)

        total = len(eligible)
        done = 0
        for batch in _chunks(eligible, opts.read_batch_size):
            ctx.check_cancelled()
            ctx.report(done, total, f"Reading tags of {batch[0].info.path.name}")
            reads = _chunk_read(exiftool, batch, ctx)
            for candidate in batch:
                planner.consider(candidate, reads[candidate.info.path])
            done += len(batch)
        ctx.report(total, total, "Checked")

        plan.actions = planner.actions
        plan.notes.insert(0, _summary(opts, planner))
        ctx.log.info("metadata_strip scan: %d file(s) read, %d to strip", total, planner.stripped)
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        opts = MetadataStripOptions.model_validate_json(plan.options_json)
        stripper = _Stripper(opts, plan.roots, ctx)
        return apply_plan(plan, ctx, handlers={ActionKind.CONVERT: stripper})


def _summary(opts: MetadataStripOptions, planner: _Planner) -> str:
    blocked = sum(1 for a in planner.actions if not a.selected)
    text = (
        f"Strip {SCOPE_LABELS[opts.scope]}: {planner.stripped} file(s) will be cleaned, "
        f"{planner.clean} had nothing to remove, {planner.skipped} skipped, "
        f"{blocked} not cleaned because the output already exists or a name is taken."
    )
    if opts.in_place and planner.stripped:
        text += " Originals go to the Recycle Bin after the clean copy is checked."
    return text


TOOL = MetadataStrip()
