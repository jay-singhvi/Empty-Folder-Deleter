"""MOV to MP4 for media_convert: choose remux or re-encode, build the ffmpeg arguments, verify.

A remux copies the streams into a new container, so quality is untouched and it takes seconds.
That only works when every stream is a kind MP4 can hold. Otherwise the video is re-encoded (H.264
or H.265) and audio is copied when MP4 can hold it, or turned into AAC when it cannot.

The ffmpeg and ffprobe programs are passed in as resolved paths (``RunContext.deps``). The output
is written through ``core/media/ffmpeg.transcode``, which uses a temporary file and a rename that
never replaces an existing file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from various_tools.core.jsonutil import as_dict, as_list
from various_tools.core.media import ffmpeg as ffmpeg_media
from various_tools.core.media.ffmpeg import MediaError
from various_tools.core.settings import DEFAULT_FFPROBE_TIMEOUT_S, DEFAULT_TRANSCODE_TIMEOUT_S
from various_tools.tools.media_resize.geometry import VideoPreset

VideoMode = Literal["auto", "remux", "reencode"]
VideoCodec = Literal["h264", "h265"]
Method = Literal["remux", "reencode"]

# Container facts, not preferences: what an MP4 can hold without converting it.
MP4_VIDEO_CODECS = frozenset({"h264", "hevc", "mpeg4", "av1"})
MP4_AUDIO_CODECS = frozenset({"aac", "mp3", "ac3", "eac3", "alac"})
ENCODERS: dict[str, str] = {"h264": "libx264", "h265": "libx265"}
# The CRF scales differ: x265 needs a higher number than x264 for similar quality.
DEFAULT_CRF: dict[str, int] = {"h264": 18, "h265": 22}


@dataclass(frozen=True)
class SourceInfo:
    duration: float
    video: tuple[str, ...]
    audio: tuple[str, ...]


@dataclass(frozen=True)
class Choice:
    """How one video will be converted, and the reason shown in the plan."""

    method: Method
    why: str


def probe(ffprobe: Path, path: Path, *, timeout: float = DEFAULT_FFPROBE_TIMEOUT_S) -> SourceInfo:
    """Duration and stream codecs in one ffprobe call. Raises ``MediaError`` when unusable."""
    data = ffmpeg_media.ffprobe_json(
        ffprobe,
        path,
        entries=["format=duration", "stream=codec_type,codec_name"],
        timeout=timeout,
    )
    try:
        duration = float(as_dict(data.get("format"))["duration"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise MediaError(f"no duration for {path.name}") from exc
    video: list[str] = []
    audio: list[str] = []
    for stream in as_list(data.get("streams")):
        info = as_dict(stream)
        codec = str(info.get("codec_name", "")).lower()
        if info.get("codec_type") == "video":
            video.append(codec)
        elif info.get("codec_type") == "audio":
            audio.append(codec)
    if not video:
        raise MediaError(f"no video stream in {path.name}")
    return SourceInfo(duration=duration, video=tuple(video), audio=tuple(audio))


def _incompatible(info: SourceInfo) -> list[str]:
    bad = [c for c in info.video if c not in MP4_VIDEO_CODECS]
    return bad + [c for c in info.audio if c not in MP4_AUDIO_CODECS]


def choose(info: SourceInfo, mode: VideoMode, codec: VideoCodec) -> Choice | str:
    """Remux or re-encode, or the reason the file is left alone."""
    bad = _incompatible(info)
    if mode == "reencode":
        return Choice("reencode", f"re-encode to {codec.upper()} (chosen)")
    if not bad:
        return Choice("remux", "remux, no re-encode")
    names = ", ".join(sorted(set(bad)))
    if mode == "remux":
        return (
            f"{names} cannot go into an MP4 without re-encoding; "
            "choose 'auto' or 're-encode' to convert it"
        )
    return Choice("reencode", f"re-encode to {codec.upper()}: {names} cannot go into an MP4 as is")


def ffmpeg_args(
    choice: Choice,
    info: SourceInfo,
    *,
    codec: VideoCodec,
    crf: int,
    preset: VideoPreset,
    audio_kbps: int,
    keep_metadata: bool,
    faststart: bool,
) -> list[str]:
    """The output arguments for one conversion. Data tracks (timecode, phone sensors) are left out.

    ``-map 0:v -map 0:a?`` keeps every picture and sound track and nothing else, because the MP4
    muxer cannot write the other kinds of QuickTime track. A rotation stored in the file is kept
    by a remux, and applied once by a re-encode.
    """
    args = ["-map", "0:v", "-map", "0:a?"]
    if choice.method == "remux":
        args += ["-c", "copy"]
        if "hevc" in info.video:
            args += ["-tag:v", "hvc1"]  # the tag Apple players need for H.265 in an MP4
    else:
        args += ["-c:v", ENCODERS[codec], "-crf", str(crf), "-preset", preset]
        args += ["-pix_fmt", "yuv420p"]
        if codec == "h265":
            args += ["-tag:v", "hvc1"]
        if info.audio:
            if all(c in MP4_AUDIO_CODECS for c in info.audio):
                args += ["-c:a", "copy"]
            else:
                args += ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
    flags: list[str] = []
    if faststart:
        flags.append("faststart")
    if keep_metadata:
        args += ["-map_metadata", "0"]
        flags.append("use_metadata_tags")
    else:
        args += ["-map_metadata", "-1", "-map_chapters", "-1"]
    if flags:
        args += ["-movflags", "+" + "+".join(flags)]
    return args


def convert(
    ffmpeg: Path,
    source: Path,
    destination: Path,
    args: list[str],
    *,
    timeout: float = DEFAULT_TRANSCODE_TIMEOUT_S,
) -> None:
    """Write ``destination`` and give it the source's modified time. Raises ``MediaError``."""
    ffmpeg_media.transcode(ffmpeg, source, destination, args, timeout=timeout)
    stat = os.stat(source)
    os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def verify_output(
    ffprobe: Path,
    destination: Path,
    expected_duration: float,
    tolerance: float,
    *,
    timeout: float = DEFAULT_FFPROBE_TIMEOUT_S,
) -> None:
    """Raise ``MediaError`` unless ``destination`` has a video stream and the expected length."""
    info = probe(ffprobe, destination, timeout=timeout)
    if abs(info.duration - expected_duration) > tolerance:
        raise MediaError(
            f"the converted video is {info.duration:.2f} s long, the original "
            f"{expected_duration:.2f} s (more than {tolerance:g} s apart)"
        )
