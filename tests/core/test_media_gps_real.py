"""Real ExifTool and ffprobe on generated files: RAW batches and video place/size."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image

from various_tools.core.media import exif, ffmpeg

GPS_IFD = 0x8825
EXIF_IFD = 0x8769


def _jpeg(path: Path, taken: str, size: tuple[int, int], gps: bool) -> None:
    data = Image.Exif()
    data.get_ifd(EXIF_IFD)[0x9003] = taken
    if gps:
        ifd = data.get_ifd(GPS_IFD)
        ifd[1], ifd[2] = "S", (33.0, 52.0, 7.68)
        ifd[3], ifd[4] = "E", (151.0, 12.0, 33.48)
    Image.new("RGB", size).save(path, "JPEG", exif=data.tobytes())


def test_exiftool_reads_a_batch_with_signed_gps_size_and_odd_names(
    tmp_path: Path, exiftool_path: Path
) -> None:
    names = ["plain.jpg", "café 写真.jpg", "nodate.jpg"]
    _jpeg(tmp_path / names[0], "2021:02:03 04:05:06", (40, 30), gps=True)
    _jpeg(tmp_path / names[1], "2020:01:02 03:04:05", (50, 20), gps=False)
    Image.new("RGB", (8, 8)).save(tmp_path / names[2], "JPEG")
    paths = [tmp_path / n for n in names]

    found = exif.exiftool_raw_info(exiftool_path, paths, 60, batch_size=2)

    assert set(found) == set(paths)
    first = found[paths[0]]
    assert first.capture_time == datetime(2021, 2, 3, 4, 5, 6)
    assert first.gps is not None
    assert first.gps[0] < 0 < first.gps[1]  # south, east
    assert first.gps == (
        round(-(33 + 52 / 60 + 7.68 / 3600), 6),
        round(151 + 12 / 60 + 33.48 / 3600, 6),
    )
    assert (first.width, first.height) == (40, 30)
    assert found[paths[1]].gps is None
    assert found[paths[1]].capture_time == datetime(2020, 1, 2, 3, 4, 5)
    assert found[paths[2]].capture_time is None


def test_exiftool_raw_info_skips_files_it_cannot_find(tmp_path: Path, exiftool_path: Path) -> None:
    assert exif.exiftool_raw_info(exiftool_path, [tmp_path / "nope.cr3"], 30) == {}


def _run(ffmpeg_path: Path, *args: str) -> None:
    subprocess.run(
        [str(ffmpeg_path), "-v", "error", "-y", *args], check=True, capture_output=True, timeout=120
    )


def test_ffprobe_reads_location_tag_and_rotated_size(
    tmp_path: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    plain = tmp_path / "plain.mp4"
    target = tmp_path / "located.mp4"
    source = ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "1"]
    _run(ffmpeg_path, *source, "-pix_fmt", "yuv420p", "-c:v", "mpeg4", str(plain))
    _run(
        ffmpeg_path, "-display_rotation:v:0", "90", "-i", str(plain), "-c", "copy",
        "-metadata", "location=+37.4220-122.0840+010.0/",
        "-metadata", "creation_time=2024-06-15T12:00:00.000000Z", str(target),
    )  # fmt: skip

    info = ffmpeg.probe_video_info(ffprobe_path, target)

    assert info.gps == (37.422, -122.084)
    assert (info.width, info.height) == (120, 160)
    assert info.creation_time is not None


def test_ffprobe_reads_the_apple_quicktime_location_tag(
    tmp_path: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    target = tmp_path / "iphone.mov"
    _run(
        ffmpeg_path, "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10", "-t", "1", "-c:v", "mpeg4",
        "-movflags", "use_metadata_tags",
        "-metadata", "com.apple.quicktime.location.ISO6709=-33.8688+151.2093+020.0/", str(target),
    )  # fmt: skip

    assert ffmpeg.probe_video_info(ffprobe_path, target).gps == (-33.8688, 151.2093)


def test_ffprobe_info_without_tags_is_empty_but_has_size(
    tmp_path: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    target = tmp_path / "plain.mp4"
    subprocess.run(
        [str(ffmpeg_path), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10",
         "-t", "1", "-pix_fmt", "yuv420p", "-c:v", "mpeg4", str(target)],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    info = ffmpeg.probe_video_info(ffprobe_path, target)
    assert info.gps is None
    assert (info.width, info.height) == (64, 48)
