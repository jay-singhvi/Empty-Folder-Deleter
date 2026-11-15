"""Media resize (plan §3.5): merges app.py (the resolution reducer) with metadata-8's shorter-side
resize and CR3 conversion.

``scan()`` decides what happens to every file and writes nothing. Each resize is a planned
``convert`` action, which carries the source and the output path. ``apply()`` runs the plan
through the generic engine, so every output is journaled.

Rules that always hold:
* a file is never enlarged unless the owner turns off the locked ``never_upscale`` option, and a
  file is resized only by the rule its mode names (for ``fit_box`` with ``never_upscale``, only
  when both sides are larger than the box);
* by default originals are never changed or removed. The output goes to a new folder, or next to
  the original with a suffix (``_resized`` by default). An existing file is never overwritten.
  Only the advanced ``overwrite`` output (with its switch on) replaces an original, and only
  through the Recycle Bin: the new file is written and checked first, the original goes to the
  bin as its own journaled row, then the new file takes its name, so undo can restore it;
* a RAW original is removed only when ``remove_raw_originals`` is on, and only after its JPEG
  was written, checked and given the RAW's metadata (🔒 plan §3.5). It goes to the Recycle Bin
  as its own journaled row;
* cloud-only OneDrive files are never read (AGENTS.md rule 3). They are reported as skipped and
  listed in ``plan.cloud_only``;
* the selected folders are never changed, junctions are not followed (rule 4), and protected
  folders are refused (rule 6). The output folder is never scanned, so a second run does not
  resize its own output;
* missing programs disable only the features that need them, with a note in the plan.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from various_tools.core.context import RunContext
from various_tools.core.deps import install_hint
from various_tools.core.fs import (
    FileInfo,
    destination_refusal,
    is_inside,
    long_path,
    protected_reason,
    root_refusal,
    walk,
)
from various_tools.core.media import ffmpeg as ffmpeg_media
from various_tools.core.media.ffmpeg import MediaError
from various_tools.core.media.imaging import RAW_EXTS, is_raw
from various_tools.core.onedrive import CloudOnlyFileError, is_on_device, status_from_attributes
from various_tools.core.plan import (
    ActionKind,
    Plan,
    PlannedAction,
    RunResult,
    apply_plan,
    trash_extra,
)
from various_tools.tools.base import Category, ToolOptions
from various_tools.tools.media_resize import images, videos
from various_tools.tools.media_resize.geometry import (
    CRF_BY_QUALITY,
    ResizeMode,
    SizePreset,
    Target,
    VideoPreset,
    VideoQuality,
    box_for,
    even,
    plan_target,
)
from various_tools.tools.media_resize.images import (
    EXTRAS_SCAN_BYTES,
    ImageError,
    ImageRules,
    Rendered,
    Resample,
)

TOOL_ID = "media_resize"
SUFFIX_TAG = "_resized"
ESTIMATE_NOTE = "(estimate)"
REPLACE_NOTE = "replaces the original, which goes to the Recycle Bin"
RAW_REMOVED_REASON = "RAW original removed after its JPEG was checked and its metadata copied"
MEDIA_OPTIONS = ("images", "videos", "both")
MODE_LABELS: dict[str, str] = {
    "fit_box": "fit inside the box",
    "shorter_side": "shorter side",
    "longer_side": "longer side",
    "percent": "percentage",
}


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


class MediaResizeOptions(ToolOptions):
    # ----- files -----------------------------------------------------------------------
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to resize",
        description=(
            "The folders whose pictures and videos are resized. A drive root or a protected "
            "folder is refused. Originals are never changed."
        ),
        json_schema_extra=_hint("Files", widget="folders"),
    )
    recursive: bool = Field(
        default=False,
        title="Include sub-folders",
        description="Also resize files in the sub-folders of each chosen folder. Default: off.",
        json_schema_extra=_hint("Files"),
    )
    media: Literal["images", "videos", "both"] = Field(
        default="images",
        title="What to resize",
        description="Resize pictures only, videos only, or both. Default: images only.",
        json_schema_extra=_hint("Files"),
    )
    image_extensions: list[str] = Field(
        default_factory=lambda: list(images.IMAGE_FORMATS),
        min_length=1,
        title="Picture file types",
        description=(
            "The picture file types that are resized, each lower-case with a leading dot. Only "
            "types this tool can write are allowed: .jpg, .jpeg, .png, .bmp, .gif, .tiff and "
            ".webp. Default: all of them."
        ),
        json_schema_extra=_hint("Files", advanced=True, widget="ext_list"),
    )
    video_extensions: list[str] = Field(
        default_factory=lambda: sorted(videos.VIDEO_EXTS),
        min_length=1,
        title="Video file types",
        description=(
            "The video file types that are resized, each lower-case with a leading dot. The "
            "output keeps the same type, so choose types ffmpeg can write. Default: .mp4, .avi, "
            ".mov, .mkv, .wmv, .flv, .webm and .m4v."
        ),
        json_schema_extra=_hint("Files", advanced=True, widget="ext_list"),
    )

    # ----- size ------------------------------------------------------------------------
    mode: ResizeMode = Field(
        default="fit_box",
        title="How to choose the new size",
        description=(
            "Fit inside a box (a file shrinks only when both sides are larger than the box), set "
            "the shorter or longer side to a number of pixels, or scale by a percentage. "
            "Default: fit inside the box."
        ),
        json_schema_extra=_hint("Size"),
    )
    size: SizePreset = Field(
        default="1080p",
        title="Box size",
        description=(
            "The box a file must fit inside, as a standard resolution. Choose custom to enter "
            "your own width and height. Default: 1080p (1920x1080)."
        ),
        json_schema_extra=_hint("Size", depends_on="mode=fit_box"),
    )
    max_width: int | None = Field(
        default=None,
        ge=100,
        le=16384,
        title="Custom box width (px)",
        description="Width of your own box, in pixels. Needed when the box size is custom.",
        json_schema_extra=_hint("Size", depends_on="size=custom"),
    )
    max_height: int | None = Field(
        default=None,
        ge=100,
        le=16384,
        title="Custom box height (px)",
        description="Height of your own box, in pixels. Needed when the box size is custom.",
        json_schema_extra=_hint("Size", depends_on="size=custom"),
    )
    target_px: int = Field(
        default=1080,
        ge=16,
        le=16384,
        title="Side length (px)",
        description=(
            "For the shorter-side and longer-side modes: the length, in pixels, that side "
            "becomes. Default: 1080."
        ),
        json_schema_extra=_hint("Size", depends_on="mode=shorter_side|longer_side"),
    )
    percent: int = Field(
        default=50,
        ge=10,
        le=400,
        title="Percentage",
        description=(
            "For the percentage mode: each side becomes this share of its length. Values over "
            "100 enlarge, which needs 'Never enlarge' turned off. Default: 50."
        ),
        json_schema_extra=_hint("Size", depends_on="mode=percent"),
    )
    never_upscale: bool = Field(
        default=True,
        title="Never enlarge",
        description=(
            "🔒 Locked default: files are never made bigger. This was a fixed rule of the "
            "original tools. Turning it off lets small files be enlarged to the box or side "
            "length, which makes bigger files and softer pictures. Default: on."
        ),
        json_schema_extra=_hint("Size", advanced=True, locked=True),
    )

    # ----- quality ---------------------------------------------------------------------
    video_quality: VideoQuality = Field(
        default="high",
        title="Video quality",
        description=(
            "How closely a resized video keeps the original look. High is CRF 18, medium is "
            "CRF 23 and low is CRF 28, and a lower CRF makes a bigger, sharper file. "
            "Default: high."
        ),
        json_schema_extra=_hint("Quality"),
    )
    video_crf: int | None = Field(
        default=None,
        ge=0,
        le=51,
        title="Video CRF override",
        description=(
            "Your own x264 CRF value, from 0 (lossless, huge) to 51 (worst). Empty uses the "
            "video quality above: high 18, medium 23, low 28."
        ),
        json_schema_extra=_hint("Quality", advanced=True),
    )
    video_preset: VideoPreset = Field(
        default=videos.PRESET,
        title="Video encoding speed",
        description=(
            "The x264 speed preset. Slower presets take longer and make smaller files at the "
            "same quality; faster ones do the reverse. Default: medium."
        ),
        json_schema_extra=_hint("Quality", advanced=True),
    )
    jpeg_quality: int = Field(
        default=85,
        ge=1,
        le=100,
        title="JPEG quality",
        description=(
            "Quality of resized JPEG pictures, from 1 (smallest) to 100 (best). 85 to 90 is "
            "the usual range. Default: 85."
        ),
        json_schema_extra=_hint("Quality"),
    )
    optimize_output: bool = Field(
        default=True,
        title="Optimise JPEG and PNG",
        description=(
            "Spend a little more time to make resized JPEG and PNG files smaller, with no "
            "change to the picture. Default: on."
        ),
        json_schema_extra=_hint("Quality", advanced=True),
    )
    resample: Resample = Field(
        default="lanczos",
        title="Resize filter",
        description=(
            "The method used to shrink pictures. Lanczos is the sharpest for photos, bicubic "
            "and bilinear are softer, and nearest is fast and blocky. Default: lanczos."
        ),
        json_schema_extra=_hint("Quality", advanced=True),
    )
    keep_metadata: bool = Field(
        default=True,
        title="Keep metadata",
        description=(
            "Keep the EXIF block, colour profile and resolution of pictures, and the metadata "
            "of videos. The orientation is reset because the pixels are already turned. "
            "Default: on."
        ),
        json_schema_extra=_hint("Quality", advanced=True),
    )
    estimate_compression: float = Field(
        default=videos.ESTIMATE_COMPRESSION,
        ge=0.05,
        le=2.0,
        title="Video size estimate factor",
        description=(
            "Only for the preview: the expected video file size is the pixel ratio times this "
            "number. The real size is known after the encode. Default: 0.8."
        ),
        json_schema_extra=_hint("Quality", advanced=True),
    )
    extras_scan_kb: int = Field(
        default=EXTRAS_SCAN_BYTES // 1024,
        ge=16,
        le=4096,
        title="Motion-photo scan size (KB)",
        description=(
            "How much of the start of each JPEG is searched for a motion-photo or HDR marker. "
            "Such files are left as they are. A larger value finds markers placed later in the "
            "file. Default: 256 KB."
        ),
        json_schema_extra=_hint("Quality", advanced=True),
    )

    # ----- output ----------------------------------------------------------------------
    output: Literal["new_folder", "suffix", "overwrite"] = Field(
        default="new_folder",
        title="Where the copies go",
        description=(
            "Write the resized copies into a new folder inside each chosen folder, or next to "
            "the original with a suffix added to the name. overwrite replaces each original "
            "with its resized version: the original goes to the Recycle Bin, and it needs "
            "'Allow replacing the originals' (advanced). Default: new folder, originals kept."
        ),
        json_schema_extra=_hint("Output"),
    )
    allow_overwrite: bool = Field(
        default=False,
        title="Allow replacing the originals",
        description=(
            "Needed for the overwrite output. Each resized picture or video is written and "
            "checked first, then the original goes to the Recycle Bin and the new file takes "
            "its name. Undo restores the original from the Recycle Bin while it is still "
            "there. RAW files are never replaced; they get a new JPEG. Default: off."
        ),
        json_schema_extra=_hint("Output", advanced=True, depends_on="output=overwrite"),
    )
    output_folder_name: str | None = Field(
        default=None,
        title="Output folder name",
        description=(
            "Name of the new folder for the copies. Empty uses the 'Resized output folder name' "
            "setting (resized unless you changed it)."
        ),
        json_schema_extra=_hint("Output", depends_on="output=new_folder"),
    )
    suffix_tag: str = Field(
        default=SUFFIX_TAG,
        min_length=1,
        title="File name suffix",
        description=(
            "Text added to the end of a file name, before the extension, for the suffix output "
            "(photo.jpg becomes photo_resized.jpg). Files already ending with it are skipped. "
            "Default: _resized."
        ),
        json_schema_extra=_hint("Output", depends_on="output=suffix"),
    )
    convert_raw_to_jpeg: bool = Field(
        default=False,
        title="Convert RAW (CR3) to JPEG",
        description=(
            "Also turn Canon RAW (CR3) files into resized JPEGs. Needs ExifTool to keep their "
            "metadata. The RAW originals are kept unless 'Remove RAW originals' is on. "
            "Default: off."
        ),
        json_schema_extra=_hint("Output", advanced=True),
    )
    remove_raw_originals: bool = Field(
        default=False,
        title="Remove RAW originals",
        description=(
            "After a RAW file's JPEG is written, checked and given the RAW's metadata by "
            "ExifTool, send the RAW to the Recycle Bin (undo restores it while it is there). "
            "If the metadata copy fails, ExifTool is missing or 'Keep metadata' is off, the RAW "
            "is kept and the plan says why. Default: off, RAW originals are kept."
        ),
        json_schema_extra=_hint("Output", advanced=True, depends_on="convert_raw_to_jpeg=true"),
    )

    @field_validator("image_extensions")
    @classmethod
    def _writable_image_types(cls, value: list[str]) -> list[str]:
        exts = normalise_extensions(value)
        unsupported = [ext for ext in exts if ext not in images.IMAGE_FORMATS]
        if unsupported:
            raise ValueError(
                f"cannot write {', '.join(unsupported)}; allowed: {', '.join(images.IMAGE_FORMATS)}"
            )
        return exts

    @field_validator("video_extensions")
    @classmethod
    def _video_types(cls, value: list[str]) -> list[str]:
        return normalise_extensions(value)

    @field_validator("suffix_tag")
    @classmethod
    def _suffix_is_a_plain_name_part(cls, value: str) -> str:
        if not value.strip() or any(ch in value for ch in _FORBIDDEN_NAME_CHARS):
            raise ValueError('the suffix needs text, and cannot contain any of \\ / : * ? " < > |')
        if value != value.rstrip(" ."):
            raise ValueError("the suffix cannot end with a space or a dot, which Windows drops")
        return value

    @model_validator(mode="after")
    def _picture_and_video_types_differ(self) -> MediaResizeOptions:
        shared = sorted(set(self.image_extensions) & set(self.video_extensions))
        if shared:
            raise ValueError(f"{', '.join(shared)} cannot be both a picture and a video type")
        if RAW_EXTS & set(self.video_extensions):
            raise ValueError("RAW types are handled by 'Convert RAW to JPEG', not as videos")
        return self


def _folder_name_problem(name: str) -> str | None:
    """Why ``name`` cannot be the output folder, or None. A plain name only, never a path."""
    if not name.strip() or name in (".", ".."):
        return "The output folder name is empty."
    if any(ch in name for ch in '\\/:*?"<>|'):
        return f"The output folder name {name!r} contains a character Windows does not allow."
    return None


def _folder_name(opts: MediaResizeOptions, ctx: RunContext) -> str:
    """The output folder name: the option, or the ``default_output_folder`` setting."""
    return opts.output_folder_name or ctx.settings.default_output_folder


def _crf(opts: MediaResizeOptions) -> int:
    """The CRF override, or the one the video quality preset names."""
    return opts.video_crf if opts.video_crf is not None else CRF_BY_QUALITY[opts.video_quality]


def _is_video(path: Path, opts: MediaResizeOptions) -> bool:
    return path.suffix.lower() in opts.video_extensions


def _options_problem(opts: MediaResizeOptions, ctx: RunContext) -> str | None:
    if not opts.roots:
        return "Choose at least one folder."
    if opts.mode == "fit_box" and box_for(opts.size, opts.max_width, opts.max_height) is None:
        return "The custom size needs both a maximum width and a maximum height."
    if opts.output == "overwrite" and not opts.allow_overwrite:
        return (
            "Replacing the originals needs 'Allow replacing the originals' (advanced) turned on. "
            "Nothing was planned."
        )
    if opts.output == "new_folder":
        return _folder_name_problem(_folder_name(opts, ctx))
    return None


def _cloud_only(info: FileInfo) -> bool:
    return not is_on_device(status_from_attributes(info.attributes))


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _extensions(opts: MediaResizeOptions) -> set[str]:
    exts: set[str] = set()
    if opts.media in ("images", "both"):
        exts |= set(opts.image_extensions)
        if opts.convert_raw_to_jpeg:
            exts |= RAW_EXTS
    if opts.media in ("videos", "both"):
        exts |= set(opts.video_extensions)
    return exts


def _rules(opts: MediaResizeOptions) -> ImageRules:
    return ImageRules(
        mode=opts.mode,
        box=box_for(opts.size, opts.max_width, opts.max_height),
        target_px=opts.target_px,
        amount=opts.percent,
        jpeg_quality=opts.jpeg_quality,
        keep_metadata=opts.keep_metadata,
        never_upscale=opts.never_upscale,
        optimize=opts.optimize_output,
        resample=opts.resample,
        extras_scan_bytes=opts.extras_scan_kb * 1024,
    )


def _output_path(
    src: Path, root: Path, opts: MediaResizeOptions, out_suffix: str, folder: str
) -> Path:
    if opts.output == "suffix":
        return src.with_name(f"{src.stem}{opts.suffix_tag}{out_suffix}")
    if opts.output == "overwrite":
        # The original's own path. A RAW file cannot become a JPEG under its own name, so its
        # JPEG goes next to it, and only a file that is not there yet is ever written.
        return src if out_suffix == src.suffix.lower() else src.with_suffix(out_suffix)
    relative = src.relative_to(root) if opts.recursive else Path(src.name)
    return root / folder / relative.with_suffix(out_suffix)


def _raw_removal_note(opts: MediaResizeOptions) -> str:
    """What happens to a RAW original when ``remove_raw_originals`` is on (🔒 plan §3.5)."""
    if not opts.keep_metadata:
        return "RAW original kept: its metadata is not copied when 'Keep metadata' is off"
    return (
        "then the RAW original goes to the Recycle Bin, once the JPEG is checked and its "
        "metadata copied"
    )


def _out_suffix(src: Path) -> str:
    """RAW files become JPEGs. Every other file keeps its extension."""
    return ".jpg" if is_raw(src) else src.suffix.lower()


def _is_output(src: Path, root: Path, opts: MediaResizeOptions, folder: str) -> bool:
    """True for a file this tool wrote, so that a second run does not resize it again."""
    if opts.output == "suffix":
        return src.stem.endswith(opts.suffix_tag)
    if opts.output == "overwrite":
        return False  # an output has the original's name; never enlarging stops a second resize
    return is_inside(src, root / folder)


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
    roots: Iterable[Path], opts: MediaResizeOptions, exts: set[str], folder: str
) -> list[_Candidate]:
    """Files to consider, once each even when roots overlap. Output files are left out."""
    seen: set[str] = set()
    found: list[_Candidate] = []
    for root in roots:
        for info in walk(root, exts=exts, recursive=opts.recursive):
            key = os.path.normcase(os.path.abspath(info.path))
            if key in seen or _is_output(info.path, root, opts, folder):
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
        self.folder = _folder_name(opts, ctx)
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

    def add(
        self, candidate: _Candidate, dst: Path, reason: str, before: int = 0, after: int = 0
    ) -> None:
        """Add one resize. An output that cannot be written is listed, but not selected.

        Only a selected resize counts towards the totals in the summary.
        """
        key = os.path.normcase(str(dst)).casefold()
        conflict = None
        in_place = _same(candidate.info.path, dst)
        if in_place:
            reason += f"; {REPLACE_NOTE}"
        elif os.path.lexists(long_path(dst)):
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
        self.before += before
        self.after += after
        self.resized += 1
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
            self.plan.cloud_only.append(src)
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
        dst = _output_path(src, candidate.root, self.opts, out_suffix, self.folder)
        before = candidate.info.size
        after = len(result.data)
        width, height = result.source_size
        target = result.target
        reason = (
            f"resize {_dims(width, height)} -> {_dims(target.width, target.height)}; "
            f"{_size_label(before)} -> {_size_label(after)}"
        )
        if result.from_raw and self.opts.remove_raw_originals:
            reason += f"; {_raw_removal_note(self.opts)}"
        self.add(candidate, dst, reason, before, after)

    def video(self, candidate: _Candidate, ffprobe: Path) -> None:
        src = candidate.info.path
        if _cloud_only(candidate.info):
            self.skip("cloud-only, not read", src)
            self.plan.cloud_only.append(src)
            return
        try:
            width, height = ffmpeg_media.display_size(
                ffprobe, src, timeout=self.ctx.settings.ffprobe_timeout_s
            )
        except (MediaError, OSError, CloudOnlyFileError) as exc:
            self.skip(f"could not read the video size ({exc})", src)
            return
        target = _video_target(self.opts, width, height)
        if isinstance(target, str):
            self.skip(target, src)
            return
        before = candidate.info.size
        after = videos.estimate_bytes(
            before, width, height, target.width, target.height, self.opts.estimate_compression
        )
        self.estimated = True
        dst = _output_path(src, candidate.root, self.opts, src.suffix.lower(), self.folder)
        reason = (
            f"resize {_dims(width, height)} -> {_dims(target.width, target.height)}; "
            f"{_size_label(before)} -> about {_size_label(after)} {ESTIMATE_NOTE}"
        )
        self.add(candidate, dst, reason, before, after)


def _video_target(opts: MediaResizeOptions, width: int, height: int) -> Target | str:
    """The even-sized target for a video, or the reason to leave it alone."""
    target = plan_target(
        opts.mode,
        width,
        height,
        box=box_for(opts.size, opts.max_width, opts.max_height),
        target_px=opts.target_px,
        amount=opts.percent,
        never_upscale=opts.never_upscale,
    )
    if isinstance(target, str):
        return target
    adjusted = Target(even(target.width), even(target.height))
    if opts.never_upscale and (adjusted.width > width or adjusted.height > height):
        return "the target would enlarge the video, and files are never enlarged"
    return adjusted


class _Converter:
    """Runs the planned resizes at apply time. Each call checks the paths again first."""

    def __init__(
        self, opts: MediaResizeOptions, roots: list[Path], ctx: RunContext, notes: list[str]
    ) -> None:
        self.opts = opts
        self.roots = roots
        self.ctx = ctx
        self.notes = notes
        self.rules = _rules(opts)

    def __call__(self, action: PlannedAction) -> Path | None:
        src, dst = action.src, action.dst
        if src is None or dst is None:
            raise ValueError("the resize has no source or no output path")
        self._check_paths(src, dst)
        in_place = _same(src, dst)
        if self.opts.output == "overwrite" and not self.opts.allow_overwrite:
            raise ValueError("replacing the originals needs 'Allow replacing the originals'")
        if in_place and self.opts.output != "overwrite":
            raise ValueError("only the overwrite output may replace an original")
        if not in_place and os.path.lexists(long_path(dst)):
            raise FileExistsError(f"the output file already exists, so it was not replaced: {dst}")
        refusal = destination_refusal(dst, stop=self._root_of(dst))
        if refusal is not None:
            raise ValueError(refusal)
        if _is_video(src, self.opts):
            return self._video(src, dst, in_place)
        return self._image(src, dst, in_place)

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

    def _image(self, src: Path, dst: Path, in_place: bool) -> Path:
        raw = is_raw(src)
        removing_raw = raw and self.opts.remove_raw_originals
        kept = "; the RAW original was kept" if removing_raw else ""
        if raw and self.opts.keep_metadata and self.ctx.deps.exiftool() is None:
            raise ValueError(f"ExifTool is needed to keep the metadata of a RAW conversion{kept}")
        result = images.render(src, _out_suffix(src), self.rules, self.opts.convert_raw_to_jpeg)
        if isinstance(result, str):
            raise ValueError(result)
        if in_place:
            images.replace_in_place(result, src)
            return src
        try:
            images.write(
                result,
                dst,
                src,
                exiftool=self.ctx.deps.exiftool(),
                keep_metadata=self.opts.keep_metadata,
                exiftool_timeout=self.ctx.settings.exiftool_write_timeout_s,
            )
        except ImageError as exc:
            if not kept:
                raise
            raise ImageError(f"{exc}{kept}") from exc
        if removing_raw and result.from_raw:
            self._remove_raw(src, dst, result)
        return dst

    def _remove_raw(self, src: Path, jpeg: Path, result: Rendered) -> None:
        """🔒 plan §3.5: the RAW goes only after its JPEG is written, checked and given the
        RAW's metadata. Without the metadata copy it is kept. A failure here keeps the RAW and
        the JPEG both; it never fails the conversion that already succeeded."""
        if not self.opts.keep_metadata:
            return  # no metadata was copied; the plan's reason says the RAW is kept
        try:
            images.check_written(jpeg, result)
            trash_extra(src, reason=RAW_REMOVED_REASON)
        except Exception as exc:  # the RAW stays; report it
            message = f"RAW original kept: {src} ({exc})"
            self.notes.append(message)
            self.ctx.log.warning("media_resize: %s", message)

    def _video(self, src: Path, dst: Path, in_place: bool) -> Path:
        ffmpeg = self.ctx.deps.ffmpeg()
        ffprobe = self.ctx.deps.ffprobe()
        if ffmpeg is None or ffprobe is None:
            raise MediaError("ffmpeg and ffprobe are needed to resize a video")
        timeout = self.ctx.settings.ffprobe_timeout_s
        width, height = ffmpeg_media.display_size(ffprobe, src, timeout=timeout)
        target = _video_target(self.opts, width, height)
        if isinstance(target, str):
            raise ValueError(target)
        out = dst
        if in_place:
            # A name of our own that does not exist yet; the encode never replaces anything.
            out = src.with_name(f".{src.stem}.resize-{uuid.uuid4().hex[:12]}{src.suffix}")
        try:
            videos.transcode_resized(
                ffmpeg,
                src,
                out,
                target,
                crf=_crf(self.opts),
                keep_metadata=self.opts.keep_metadata,
                preset=self.opts.video_preset,
                timeout=self.ctx.settings.transcode_timeout_s,
            )
            stat = os.stat(src)
            os.utime(out, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            if not in_place:
                return dst
            if ffmpeg_media.display_size(ffprobe, out, timeout=timeout) != (
                target.width,
                target.height,
            ):
                raise MediaError("the resized video has the wrong size; it was not kept")
            images.swap_into_place(out, src)
        except images.KeptCopyError:
            raise  # the original is in the Recycle Bin; the resized copy must stay
        except BaseException:
            if in_place and os.path.lexists(long_path(out)):
                os.remove(long_path(out))
            raise
        return src


class MediaResize:
    id = TOOL_ID
    name = "Resize media"
    description = (
        "Reduce the resolution of images and videos to a box, a shorter or longer side, or a "
        "percentage. Files are never enlarged unless you turn that locked option off. "
        "Originals stay as they are, unless you choose to replace them (they then go to the "
        "Recycle Bin)."
    )
    category: Category = "Media"
    Options = MediaResizeOptions

    def scan(self, opts: MediaResizeOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, MediaResizeOptions):
            raise TypeError("MediaResize.scan needs MediaResizeOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        problem = _options_problem(opts, ctx)
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
        candidates = _candidates(roots, opts, _extensions(opts), planner.folder)
        total = len(candidates)
        for index, candidate in enumerate(candidates, start=1):
            ctx.check_cancelled()
            ctx.report(index - 1, total, f"Checking {candidate.info.path.name}")
            if _is_video(candidate.info.path, opts):
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
        converter = _Converter(opts, plan.roots, ctx, plan.notes)
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
            f", about {share}% smaller ({_size_label(saved)} saved)"
            + (" (video sizes are estimates)." if planner.estimated else ".")
        )
    return text


TOOL = MediaResize()
