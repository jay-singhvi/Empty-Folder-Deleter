"""Media converter (plan §4 Phase 7, item 3): HEIC to JPG or PNG, WebP to JPG or PNG, MOV to MP4.

``scan()`` decides what happens to every file and writes nothing. Each conversion is a planned
``convert`` action, which carries the source and the output path. ``apply()`` runs the plan
through the generic engine, so every output is journaled. It shares its picture and video
helpers with ``media_resize`` where they fit.

Rules that always hold:
* an existing file is never overwritten, and output names are unique inside a batch (checked at
  scan and again at apply). The output goes to a new folder, or next to the original with the new
  extension;
* originals are kept unless the advanced ``trash_originals`` option is on. Then each original goes
  to the Recycle Bin, and only after its output was written and verified (a picture is reopened
  and its size checked, a video's length is compared through ffprobe). When the check fails the
  output is kept, the original is not touched, and the action fails;
* cloud-only OneDrive files are never read (AGENTS.md rule 3). They are reported as skipped;
* the selected folders are never changed beyond the outputs, junctions are not followed (rule 4)
  and protected folders are refused (rule 6). The output folder is never scanned, so a second run
  does not convert its own output;
* missing programs disable only the features that need them, with a note in the plan.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

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
from various_tools.core.media.ffmpeg import MediaError
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
from various_tools.tools.media_convert import images, videos
from various_tools.tools.media_convert.images import ImageRules
from various_tools.tools.media_convert.videos import Choice, SourceInfo, VideoCodec, VideoMode
from various_tools.tools.media_resize.geometry import VideoPreset
from various_tools.tools.media_resize.tool import VIDEO_DURATION_TOLERANCE_S

TOOL_ID = "media_convert"
OUTPUT_FOLDER = "converted"
HEIC_EXTS = frozenset({".heic", ".heif"})
WEBP_EXTS = frozenset({".webp"})
MOV_EXTS = frozenset({".mov"})
TRASH_NOTE = "original goes to the Recycle Bin after the output is verified"
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_FORBIDDEN_NAME_CHARS = '\\/:*?"<>|'

Kind = Literal["heic", "webp", "mov"]
KIND_LABELS: dict[str, str] = {"heic": "HEIC", "webp": "WebP", "mov": "MOV"}


def _hint(group: str, *, advanced: bool = False, locked: bool = False, **extra: Any) -> Any:
    """The ``json_schema_extra`` hints the GUI and CLI read (see ``core/settings.py``)."""
    hints: dict[str, Any] = {"group": group}
    if advanced:
        hints["advanced"] = True
    if locked:
        hints["locked"] = True
    hints.update(extra)
    return hints


class MediaConvertOptions(ToolOptions):
    # ----- files -----------------------------------------------------------------------
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to convert",
        description=(
            "The folders whose files are converted. A drive root or a protected folder is "
            "refused. Originals are kept unless 'Move originals to the Recycle Bin' is on."
        ),
        json_schema_extra=_hint("Files", widget="folders"),
    )
    recursive: bool = Field(
        default=False,
        title="Include sub-folders",
        description="Also convert files in the sub-folders of each chosen folder. Default: off.",
        json_schema_extra=_hint("Files"),
    )

    # ----- conversions -----------------------------------------------------------------
    convert_heic: bool = Field(
        default=True,
        title="Convert HEIC and HEIF pictures",
        description=(
            "Turn iPhone HEIC and HEIF pictures into a format every program opens. Multi-image "
            "files are left as they are. Default: on."
        ),
        json_schema_extra=_hint("Conversions"),
    )
    heic_target: Literal["jpg", "png"] = Field(
        default="jpg",
        title="HEIC becomes",
        description=(
            "JPG is smaller and the usual choice. PNG is lossless and keeps transparency but "
            "makes much bigger files. Default: JPG."
        ),
        json_schema_extra=_hint("Conversions", depends_on="convert_heic=true"),
    )
    convert_webp: bool = Field(
        default=True,
        title="Convert WebP pictures",
        description=(
            "Turn still WebP pictures into JPG or PNG. Animated WebP files are left as they "
            "are, because a single picture cannot hold the frames. Default: on."
        ),
        json_schema_extra=_hint("Conversions"),
    )
    webp_target: Literal["jpg", "png"] = Field(
        default="jpg",
        title="WebP becomes",
        description=(
            "JPG is smaller. PNG is lossless and keeps transparency, which JPG cannot. "
            "Default: JPG."
        ),
        json_schema_extra=_hint("Conversions", depends_on="convert_webp=true"),
    )
    convert_mov: bool = Field(
        default=True,
        title="Convert MOV videos to MP4",
        description=(
            "Turn QuickTime MOV videos into MP4, which more players and sites accept. Needs "
            "ffmpeg and ffprobe. Default: on."
        ),
        json_schema_extra=_hint("Conversions"),
    )

    # ----- picture quality -------------------------------------------------------------
    jpeg_quality: int = Field(
        default=90,
        ge=1,
        le=100,
        title="JPEG quality",
        description=(
            "Quality of JPEG outputs, from 1 (smallest) to 100 (best). 85 to 95 is the usual "
            "range. Default: 90."
        ),
        json_schema_extra=_hint("Pictures"),
    )
    background_color: str = Field(
        default="#ffffff",
        title="Background for transparent areas",
        description=(
            "A picture with transparency becomes a JPG by painting it onto this colour, as #RRGGBB "
            "(#ffffff is white, #000000 is black). PNG outputs keep the transparency instead. "
            "Default: #ffffff."
        ),
        json_schema_extra=_hint("Pictures"),
    )
    keep_metadata: bool = Field(
        default=True,
        title="Keep metadata",
        description=(
            "Keep the EXIF tags (date taken, camera, GPS) and the colour profile of pictures, "
            "and the tags of videos. The orientation is reset because the pixels are already "
            "turned. ExifTool, when installed, also carries over XMP and maker notes. "
            "Default: on."
        ),
        json_schema_extra=_hint("Pictures"),
    )
    optimize_output: bool = Field(
        default=True,
        title="Optimise JPEG and PNG",
        description=(
            "Spend a little more time to make JPEG and PNG outputs smaller, with no change to "
            "the picture. Default: on."
        ),
        json_schema_extra=_hint("Pictures", advanced=True),
    )
    jpeg_extension: Literal["jpg", "jpeg"] = Field(
        default="jpg",
        title="JPEG file extension",
        description="The extension given to JPEG outputs, jpg or jpeg. Default: jpg.",
        json_schema_extra=_hint("Pictures", advanced=True),
    )

    # ----- video -----------------------------------------------------------------------
    video_mode: VideoMode = Field(
        default="auto",
        title="How to convert videos",
        description=(
            "Remux copies the streams into an MP4 with no quality loss and takes seconds, but "
            "only works when MP4 can hold every stream. Re-encode always makes a new H.264 or "
            "H.265 video. Auto remuxes when possible and re-encodes only when it has to. "
            "Default: auto."
        ),
        json_schema_extra=_hint("Video"),
    )
    reencode_codec: VideoCodec = Field(
        default="h264",
        title="Re-encode codec",
        description=(
            "The codec used when a video is re-encoded. H.264 plays everywhere. H.265 makes "
            "smaller files but some older players cannot open it. Default: h264."
        ),
        json_schema_extra=_hint("Video", advanced=True),
    )
    video_crf: int | None = Field(
        default=None,
        ge=0,
        le=51,
        title="Re-encode CRF",
        description=(
            "Quality of a re-encoded video, from 0 (lossless, huge) to 51 (worst). Lower is "
            "sharper and bigger. Empty uses 18 for H.264 and 22 for H.265."
        ),
        json_schema_extra=_hint("Video", advanced=True),
    )
    video_preset: VideoPreset = Field(
        default="medium",
        title="Re-encode speed",
        description=(
            "The x264 and x265 speed preset. Slower presets take longer and make smaller files "
            "at the same quality. Default: medium."
        ),
        json_schema_extra=_hint("Video", advanced=True),
    )
    audio_bitrate_kbps: int = Field(
        default=192,
        ge=32,
        le=512,
        title="AAC audio bitrate (kbps)",
        description=(
            "Used only when a video's sound is a kind MP4 cannot hold, so it is turned into "
            "AAC. Sound MP4 can hold is copied unchanged. Default: 192."
        ),
        json_schema_extra=_hint("Video", advanced=True),
    )
    faststart: bool = Field(
        default=True,
        title="Make MP4 start fast",
        description=(
            "Move the index to the start of each MP4, so it begins playing before it has "
            "downloaded. Costs a short extra copy at the end. Default: on."
        ),
        json_schema_extra=_hint("Video", advanced=True),
    )
    duration_tolerance_s: float = Field(
        default=VIDEO_DURATION_TOLERANCE_S,
        ge=0.05,
        le=60.0,
        title="Video duration tolerance (s)",
        description=(
            "A converted video counts as verified when its length is within this many seconds "
            "of the original. Only a verified output allows the original to be recycled. "
            "Default: 0.5."
        ),
        json_schema_extra=_hint("Video", advanced=True),
    )

    # ----- output ----------------------------------------------------------------------
    output: Literal["new_folder", "next_to_original"] = Field(
        default="new_folder",
        title="Where the converted files go",
        description=(
            "Write converted files into a new folder inside each chosen folder, or next to the "
            "original with the new extension (photo.heic becomes photo.jpg). An existing file is "
            "never replaced. Default: new folder."
        ),
        json_schema_extra=_hint("Output"),
    )
    output_folder_name: str = Field(
        default=OUTPUT_FOLDER,
        min_length=1,
        title="Output folder name",
        description=(
            "Name of the new folder for converted files. This tool has its own name and does "
            "not use the 'Resized output folder name' setting. Default: converted."
        ),
        json_schema_extra=_hint("Output", depends_on="output=new_folder"),
    )
    trash_originals: bool = Field(
        default=False,
        title="Move originals to the Recycle Bin",
        description=(
            "After a file is converted and the output is verified, send the original to the "
            "Recycle Bin. A picture is verified by reopening it and checking its size, a video by "
            "comparing its length with ffprobe. If verification fails the original stays where "
            "it is. Undo does not restore it; use the Recycle Bin. Default: off."
        ),
        json_schema_extra=_hint("Output", advanced=True, replaces_originals=True),
    )

    @field_validator("background_color")
    @classmethod
    def _plain_hex_colour(cls, value: str) -> str:
        if not _HEX_COLOR.match(value.strip()):
            raise ValueError("the background colour must look like #RRGGBB, for example #ffffff")
        return value.strip().lower()


def _folder_name_problem(name: str) -> str | None:
    """Why ``name`` cannot be the output folder, or None. A plain name only, never a path."""
    if not name.strip() or name in (".", ".."):
        return "The output folder name is empty."
    if any(ch in name for ch in _FORBIDDEN_NAME_CHARS):
        return f"The output folder name {name!r} contains a character Windows does not allow."
    return None


def _options_problem(opts: MediaConvertOptions) -> str | None:
    if not opts.roots:
        return "Choose at least one folder."
    if not (opts.convert_heic or opts.convert_webp or opts.convert_mov):
        return "Turn on at least one conversion."
    if opts.output == "new_folder":
        return _folder_name_problem(opts.output_folder_name)
    return None


def _kind(path: Path, opts: MediaConvertOptions) -> Kind | None:
    """Which enabled conversion handles ``path``, by its extension, or None."""
    suffix = path.suffix.lower()
    if opts.convert_heic and suffix in HEIC_EXTS:
        return "heic"
    if opts.convert_webp and suffix in WEBP_EXTS:
        return "webp"
    if opts.convert_mov and suffix in MOV_EXTS:
        return "mov"
    return None


def _extensions(opts: MediaConvertOptions) -> set[str]:
    exts: set[str] = set()
    if opts.convert_heic:
        exts |= HEIC_EXTS
    if opts.convert_webp:
        exts |= WEBP_EXTS
    if opts.convert_mov:
        exts |= MOV_EXTS
    return exts


def _target(kind: Kind, opts: MediaConvertOptions) -> str:
    """The output extension, with its dot, for a kind of conversion."""
    if kind == "mov":
        return ".mp4"
    choice = opts.heic_target if kind == "heic" else opts.webp_target
    if choice == "png":
        return ".png"
    return f".{opts.jpeg_extension}"


def _rules(opts: MediaConvertOptions, out_suffix: str) -> ImageRules:
    red, green, blue = (int(opts.background_color[i : i + 2], 16) for i in (1, 3, 5))
    return ImageRules(
        fmt="PNG" if out_suffix == ".png" else "JPEG",
        jpeg_quality=opts.jpeg_quality,
        optimize=opts.optimize_output,
        background=(red, green, blue),
        keep_metadata=opts.keep_metadata,
    )


def _crf(opts: MediaConvertOptions) -> int:
    if opts.video_crf is not None:
        return opts.video_crf
    return videos.DEFAULT_CRF[opts.reencode_codec]


def _output_path(src: Path, root: Path, opts: MediaConvertOptions, out_suffix: str) -> Path:
    if opts.output == "next_to_original":
        return src.with_suffix(out_suffix)
    relative = src.relative_to(root) if opts.recursive else Path(src.name)
    return root / opts.output_folder_name / relative.with_suffix(out_suffix)


def _is_output(src: Path, root: Path, opts: MediaConvertOptions) -> bool:
    """True for a file this tool wrote, so that a second run does not convert it again."""
    return opts.output == "new_folder" and is_inside(src, root / opts.output_folder_name)


def _cloud_only(info: FileInfo) -> bool:
    return not is_on_device(status_from_attributes(info.attributes))


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


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
    roots: Iterable[Path], opts: MediaConvertOptions, exts: set[str]
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

    def __init__(self, opts: MediaConvertOptions, ctx: RunContext, plan: Plan) -> None:
        self.opts = opts
        self.ctx = ctx
        self.plan = plan
        self.claimed: set[str] = set()
        self.actions: list[PlannedAction] = []
        self.counts: dict[str, int] = {"heic": 0, "webp": 0, "mov": 0}
        self.skipped = 0
        self.ffmpeg = ctx.deps.ffmpeg()
        self.ffprobe = ctx.deps.ffprobe()
        self.exiftool = ctx.deps.exiftool()
        self.pictures = 0

    def skip(self, reason: str, path: Path) -> None:
        self.skipped += 1
        self.plan.notes.append(f"skipped: {reason}: {path}")

    def skip_cloud_only(self, path: Path) -> None:
        """A file that is not on this device: never read, and listed for the download action."""
        self.skip("cloud-only, not read", path)
        if path not in self.plan.cloud_only:
            self.plan.cloud_only.append(path)

    def add(self, candidate: _Candidate, kind: Kind, dst: Path, reason: str) -> None:
        """Add one conversion. An output that cannot be written is listed, but not selected."""
        if self.opts.trash_originals:
            reason = f"{reason}; {TRASH_NOTE}"
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
                    reason=f"not converted: {conflict}. {reason}",
                    selected=False,
                )
            )
            return
        self.claimed.add(key)
        self.counts[kind] += 1
        self.actions.append(
            PlannedAction(kind=ActionKind.CONVERT, src=candidate.info.path, dst=dst, reason=reason)
        )

    def picture(self, candidate: _Candidate, kind: Kind) -> None:
        src = candidate.info.path
        if _cloud_only(candidate.info):
            self.skip_cloud_only(src)
            return
        try:
            inspected = images.inspect(src)
        except CloudOnlyFileError:
            self.skip_cloud_only(src)
            return
        except Exception as exc:  # one unreadable file must not stop the batch; it is reported
            self.skip(f"could not read the picture ({exc})", src)
            return
        if isinstance(inspected, str):
            self.skip(inspected, src)
            return
        out_suffix = _target(kind, self.opts)
        dst = _output_path(src, candidate.root, self.opts, out_suffix)
        width, height = inspected.size
        label = KIND_LABELS[kind]
        text = (
            f"{label} -> {out_suffix[1:].upper()}, {width}x{height}, "
            f"{_size_label(candidate.info.size)}"
        )
        if inspected.has_alpha and out_suffix != ".png":
            text += f"; transparent areas become {self.opts.background_color}"
        self.pictures += 1
        self.add(candidate, kind, dst, text)

    def video(self, candidate: _Candidate, ffprobe: Path) -> None:
        src = candidate.info.path
        if _cloud_only(candidate.info):
            self.skip_cloud_only(src)
            return
        try:
            info = videos.probe(ffprobe, src, timeout=self.ctx.settings.ffprobe_timeout_s)
        except CloudOnlyFileError:
            self.skip_cloud_only(src)
            return
        except (MediaError, OSError) as exc:
            self.skip(f"could not read the video ({exc})", src)
            return
        choice = videos.choose(info, self.opts.video_mode, self.opts.reencode_codec)
        if isinstance(choice, str):
            self.skip(choice, src)
            return
        dst = _output_path(src, candidate.root, self.opts, ".mp4")
        reason = (
            f"MOV -> MP4 ({choice.why}), {info.duration:.1f} s, {_size_label(candidate.info.size)}"
        )
        self.add(candidate, "mov", dst, reason)


class _Converter:
    """Runs the planned conversions at apply time. Each call checks the paths again first."""

    def __init__(self, opts: MediaConvertOptions, roots: list[Path], ctx: RunContext) -> None:
        self.opts = opts
        self.roots = roots
        self.ctx = ctx

    def __call__(self, action: PlannedAction) -> Path | None:
        src, dst = action.src, action.dst
        if src is None or dst is None:
            raise ValueError("the conversion has no source or no output path")
        self._check_paths(src, dst)
        kind = _kind(src, self.opts)
        if kind is None:
            raise ValueError(f"not a file type this run converts: {src.name}")
        if dst.suffix.lower() != _target(kind, self.opts):
            raise ValueError(f"the output name does not match the chosen format: {dst.name}")
        if os.path.lexists(long_path(dst)):
            raise FileExistsError(f"the output file already exists, so it was not replaced: {dst}")
        refusal = destination_refusal(dst, stop=self._root_of(dst))
        if refusal is not None:
            raise ValueError(refusal)
        if kind == "mov":
            self._video(src, dst)
        else:
            self._picture(src, dst, kind)
        if self.opts.trash_originals:
            self._trash_original(src, dst)
        return dst

    def _trash_original(self, src: Path, dst: Path) -> None:
        """Only reached after the output was written and verified.

        The recycle is journaled as its own ``trash`` row (rule 5), so undo can bring the
        original back from the Recycle Bin.
        """
        try:
            trash_extra(
                src, reason=f"original recycled after the verified conversion to {dst.name}"
            )
        except Exception as exc:  # the output stays; the action fails so it shows in History
            raise OSError(
                f"converted to {dst.name}, but the original could not be moved to the "
                f"Recycle Bin and was left in place: {exc}"
            ) from exc

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

    def _picture(self, src: Path, dst: Path, kind: Kind) -> None:
        rules = _rules(self.opts, dst.suffix.lower())
        result = images.render(
            src,
            rules,
            ffmpeg=self.ctx.deps.ffmpeg(),
            ffmpeg_timeout=self.ctx.settings.frame_grab_timeout_s,
        )
        if isinstance(result, str):
            raise ValueError(result)
        images.write(
            result,
            dst,
            src,
            exiftool=self.ctx.deps.exiftool(),
            keep_metadata=self.opts.keep_metadata,
            exiftool_timeout=self.ctx.settings.exiftool_write_timeout_s,
        )
        # The gate before an original may go: the file at its final name must read back.
        self._verify_picture(dst, result.size)

    def _verify_picture(self, dst: Path, size: tuple[int, int]) -> None:
        images.verify_output(dst, size)

    def _video(self, src: Path, dst: Path) -> None:
        ffmpeg = self.ctx.deps.ffmpeg()
        ffprobe = self.ctx.deps.ffprobe()
        if ffmpeg is None or ffprobe is None:
            raise MediaError(
                "ffmpeg and ffprobe are needed to convert a video. Install them with "
                f"`{install_hint('ffmpeg')}`, or set their paths in Settings"
            )
        settings = self.ctx.settings
        info = videos.probe(ffprobe, src, timeout=settings.ffprobe_timeout_s)
        choice = videos.choose(info, self.opts.video_mode, self.opts.reencode_codec)
        if isinstance(choice, str):
            raise ValueError(choice)
        # The gate before an original may go: the encoded file is probed under its temporary
        # name, so one that fails never becomes ``dst`` (a later scan would call it done).
        videos.convert(
            ffmpeg,
            src,
            dst,
            self._video_args(choice, info),
            timeout=settings.transcode_timeout_s,
            verify=lambda encoded: self._verify_video(ffprobe, encoded, info.duration),
        )

    def _video_args(self, choice: Choice, info: SourceInfo) -> list[str]:
        return videos.ffmpeg_args(
            choice,
            info,
            codec=self.opts.reencode_codec,
            crf=_crf(self.opts),
            preset=self.opts.video_preset,
            audio_kbps=self.opts.audio_bitrate_kbps,
            keep_metadata=self.opts.keep_metadata,
            faststart=self.opts.faststart,
        )

    def _verify_video(self, ffprobe: Path, dst: Path, expected: float) -> None:
        videos.verify_output(
            ffprobe,
            dst,
            expected,
            self.opts.duration_tolerance_s,
            timeout=self.ctx.settings.ffprobe_timeout_s,
        )


class MediaConvert:
    id = TOOL_ID
    name = "Convert media"
    description = (
        "Turn HEIC pictures into JPG or PNG, WebP into JPG or PNG, and MOV videos into MP4. "
        "Metadata is kept, transparent areas are painted onto a colour for JPG, and videos are "
        "remuxed without re-encoding whenever MP4 can hold them. Originals are kept unless you "
        "choose to recycle them after each output is verified."
    )
    category: Category = "Media"
    Options = MediaConvertOptions

    def scan(self, opts: MediaConvertOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, MediaConvertOptions):
            raise TypeError("MediaConvert.scan needs MediaConvertOptions")
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
            plan.notes.append("Nothing to convert.")
            return plan
        plan.roots = roots

        planner = _Planner(opts, ctx, plan)
        videos_missing = planner.ffmpeg is None or planner.ffprobe is None
        if opts.convert_mov and videos_missing:
            plan.notes.append(
                "MOV videos are skipped: ffmpeg and ffprobe are needed. Install them with "
                f"`{install_hint('ffmpeg')}`, or set their paths in Settings."
            )
        candidates = _candidates(roots, opts, _extensions(opts))
        total = len(candidates)
        for index, candidate in enumerate(candidates, start=1):
            ctx.check_cancelled()
            ctx.report(index - 1, total, f"Checking {candidate.info.path.name}")
            kind = _kind(candidate.info.path, opts)
            if kind is None:
                continue
            if kind == "mov":
                if planner.ffmpeg is not None and planner.ffprobe is not None:
                    planner.video(candidate, planner.ffprobe)
                else:
                    planner.skip("ffmpeg and ffprobe are needed", candidate.info.path)
            else:
                planner.picture(candidate, kind)
        ctx.report(total, total, "Checked")
        if opts.keep_metadata and planner.pictures and planner.exiftool is None:
            plan.notes.append(
                "ExifTool was not found, so only the EXIF tags and colour profile of pictures are "
                "kept (XMP and maker notes are lost). Install it with "
                f"`{install_hint('exiftool')}` to keep everything."
            )

        plan.actions = planner.actions
        plan.notes.insert(0, _summary(opts, planner))
        ctx.log.info(
            "media_convert scan: %d candidate(s), %d to convert", total, _converted(planner)
        )
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        opts = MediaConvertOptions.model_validate_json(plan.options_json)
        converter = _Converter(opts, plan.roots, ctx)
        return apply_plan(plan, ctx, handlers={ActionKind.CONVERT: converter})


def _converted(planner: _Planner) -> int:
    return sum(planner.counts.values())


def _summary(opts: MediaConvertOptions, planner: _Planner) -> str:
    blocked = sum(1 for a in planner.actions if not a.selected)
    parts = [
        f"{planner.counts[k]} {KIND_LABELS[k]}"
        for k in ("heic", "webp", "mov")
        if planner.counts[k]
    ]
    detail = f" ({', '.join(parts)})" if parts else ""
    text = (
        f"Convert: {_converted(planner)} file(s) will be converted{detail}, "
        f"{planner.skipped} skipped, {blocked} not converted because the output already exists "
        "or a name is taken."
    )
    if opts.trash_originals and _converted(planner):
        text += " Originals go to the Recycle Bin after each output is verified."
    return text


TOOL = MediaConvert()
