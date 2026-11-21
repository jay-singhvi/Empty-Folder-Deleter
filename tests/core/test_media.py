"""Imaging, EXIF and ffmpeg helpers on generated fixtures. ffmpeg and ExifTool tests may skip."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
import rawpy
from PIL import Image

from various_tools.core.media import exif, ffmpeg, imaging
from various_tools.core.onedrive import CloudOnlyFileError

ImageMaker = Callable[..., Path]
VideoMaker = Callable[..., Path]


# ----- imaging -------------------------------------------------------------


def test_is_raw_recognises_cr3_only(tmp_path: Path) -> None:
    assert imaging.is_raw(tmp_path / "IMG_0001.CR3")
    assert not imaging.is_raw(tmp_path / "IMG_0001.jpg")


def test_open_image_reads_png(make_image: ImageMaker) -> None:
    path = make_image("pic.png", size=(40, 30))
    with imaging.open_image(path) as img:
        assert img.size == (40, 30)


def test_open_image_refuses_cloud_only_files(
    make_image: ImageMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_image("cloud.png")
    monkeypatch.setattr("various_tools.core.onedrive.get_attributes", lambda _p: 0x1000)
    with pytest.raises(CloudOnlyFileError):
        imaging.open_image(path)


def test_open_image_raises_for_a_broken_raw_file(tmp_path: Path) -> None:
    fake_raw = tmp_path / "broken.cr3"
    fake_raw.write_bytes(b"this is not a camera raw file")
    with pytest.raises(rawpy.LibRawError):
        imaging.open_image(fake_raw)


def test_save_jpeg_writes_atomically_and_keeps_no_temp_files(tmp_path: Path) -> None:
    img = Image.new("RGB", (32, 24), (200, 10, 10))
    target = tmp_path / "out" / "small.jpg"
    imaging.save_jpeg(img, target, quality=90)
    assert target.is_file()
    with Image.open(target) as saved:
        assert saved.format == "JPEG"
        assert saved.size == (32, 24)
    assert [p.name for p in target.parent.iterdir()] == ["small.jpg"]


def test_save_jpeg_never_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "taken.jpg"
    target.write_bytes(b"someone else's file")
    with pytest.raises(FileExistsError):
        imaging.save_jpeg(Image.new("RGB", (8, 8)), target)
    assert target.read_bytes() == b"someone else's file"
    assert [p.name for p in tmp_path.iterdir()] == ["taken.jpg"]


def test_save_jpeg_failure_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = Image.new("RGB", (8, 8))

    def disk_full(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Image.Image, "save", disk_full)
    target = tmp_path / "x.jpg"
    with pytest.raises(OSError):
        imaging.save_jpeg(img, target)
    assert list(tmp_path.iterdir()) == []


# ----- exif ----------------------------------------------------------------


def _jpeg_with_capture_time(path: Path, text: str | None, make: str | None = None) -> Path:
    img = Image.new("RGB", (20, 20), (1, 2, 3))
    exif_obj = Image.Exif()
    if text is not None:
        exif_obj.get_ifd(exif.EXIF_IFD)[exif.TAG_DATETIME_ORIGINAL] = text
    if make is not None:
        exif_obj[exif.TAG_MAKE] = make
    img.save(path, format="JPEG", exif=exif_obj)
    return path


def test_parse_exif_datetime_accepts_exif_and_iso_forms() -> None:
    assert exif.parse_exif_datetime("2024:05:01 10:20:30") == datetime(2024, 5, 1, 10, 20, 30)
    assert exif.parse_exif_datetime("2024-05-01 10:20:30") == datetime(2024, 5, 1, 10, 20, 30)
    assert exif.parse_exif_datetime(b"2024:05:01 10:20:30\x00") == datetime(2024, 5, 1, 10, 20, 30)


@pytest.mark.parametrize("value", [None, "", "0000:00:00 00:00:00", "not a date", 42])
def test_parse_exif_datetime_rejects_blank_or_garbled(value: object) -> None:
    assert exif.parse_exif_datetime(value) is None


def test_plausible_date_window() -> None:
    now = datetime(2026, 11, 12)
    assert exif.is_plausible_date(datetime(2024, 1, 1), now=now)
    assert exif.is_plausible_date(datetime(1990, 1, 1), now=now)
    assert not exif.is_plausible_date(datetime(1970, 1, 1), now=now)  # reset camera clock
    assert not exif.is_plausible_date(datetime(2040, 1, 1), now=now)  # far future
    assert not exif.is_plausible_date("2024", now=now)


def test_convert_gps_handles_hemispheres_and_bad_input() -> None:
    gps = {1: "S", 2: (33.0, 52.0, 0.0), 3: "W", 4: (118.0, 30.0, 36.0)}
    assert exif.convert_gps(gps) == (-33.866667, -118.51)
    assert exif.convert_gps({}) is None
    assert exif.convert_gps({2: "bad", 4: "bad"}) is None


def test_read_exif_reads_capture_time_camera_and_size(tmp_path: Path) -> None:
    path = _jpeg_with_capture_time(tmp_path / "cam.jpg", "2024:05:01 10:20:30", make="Acme")
    info = exif.read_exif(path)
    assert info.capture_time == datetime(2024, 5, 1, 10, 20, 30)
    assert info.camera_make == "Acme"
    assert (info.width, info.height) == (20, 20)


def test_read_exif_on_plain_png_has_no_capture_time(make_image: ImageMaker) -> None:
    info = exif.read_exif(make_image("plain.png"))
    assert info.capture_time is None
    assert info.width == 96


def test_read_exif_ignores_non_images(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not an image")
    assert exif.read_exif(clip) == exif.ExifInfo()


def test_capture_time_uses_exif_when_plausible(tmp_path: Path) -> None:
    path = _jpeg_with_capture_time(tmp_path / "a.jpg", "2024:05:01 10:20:30")
    assert exif.capture_time(path, now=datetime(2026, 1, 1)) == datetime(2024, 5, 1, 10, 20, 30)


def test_capture_time_rejects_implausible_exif_and_returns_none(tmp_path: Path) -> None:
    path = _jpeg_with_capture_time(tmp_path / "reset.jpg", "1970:01:01 00:00:00")
    assert exif.capture_time(path, now=datetime(2026, 1, 1)) is None


def test_capture_time_refuses_cloud_only_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _jpeg_with_capture_time(tmp_path / "c.jpg", "2024:05:01 10:20:30")
    monkeypatch.setattr("various_tools.core.onedrive.get_attributes", lambda _p: 0x1000)
    with pytest.raises(CloudOnlyFileError):
        exif.capture_time(path)


def test_capture_time_falls_back_to_exiftool(tmp_path: Path, fake_exe: Callable[..., Path]) -> None:
    raw_like = tmp_path / "IMG_0002.CR3"
    raw_like.write_bytes(b"not decodable by Pillow")
    tool = fake_exe(tmp_path / "bin", "exiftool", output="2023:03:04 05:06:07")
    assert exif.capture_time(raw_like, exiftool=tool, now=datetime(2026, 1, 1)) == datetime(
        2023, 3, 4, 5, 6, 7
    )


def test_capture_time_without_exiftool_is_none_for_raw(tmp_path: Path) -> None:
    raw_like = tmp_path / "IMG_0003.CR3"
    raw_like.write_bytes(b"x")
    assert exif.capture_time(raw_like) is None


def test_exiftool_failure_is_none(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-exiftool.bat"
    assert exif.exiftool_capture_time(missing, tmp_path / "x.cr3") is None


# ----- ffmpeg --------------------------------------------------------------


def test_probe_duration_and_display_size(make_video: VideoMaker, ffprobe_path: Path) -> None:
    clip = make_video("plain.mp4", seconds=2.0, size="160x120")
    assert ffmpeg.probe_duration(ffprobe_path, clip) == pytest.approx(2.0, abs=0.3)
    assert ffmpeg.display_size(ffprobe_path, clip) == (160, 120)


def test_display_size_swaps_for_a_quarter_turn(make_video: VideoMaker, ffprobe_path: Path) -> None:
    clip = make_video("rotated.mp4", size="160x120", rotate=90)
    assert ffmpeg.display_size(ffprobe_path, clip) == (120, 160)


def test_creation_time_is_read_from_the_container(
    make_video: VideoMaker, ffprobe_path: Path
) -> None:
    clip = make_video("dated.mp4", creation_time="2024-06-15T12:00:00.000000Z")
    value = ffmpeg.creation_time(ffprobe_path, clip)
    assert value is not None
    assert value.year == 2024 and value.month == 6


def test_creation_time_is_none_without_tag(make_video: VideoMaker, ffprobe_path: Path) -> None:
    clip = make_video("undated.mp4")
    assert ffmpeg.creation_time(ffprobe_path, clip) is None


def test_creation_time_is_none_for_a_non_video(tmp_path: Path, ffprobe_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    assert ffmpeg.creation_time(ffprobe_path, junk) is None


def test_grab_frame_returns_a_scaled_rgb_frame(make_video: VideoMaker, ffmpeg_path: Path) -> None:
    clip = make_video("frames.mp4", seconds=2.0, size="160x120")
    frame = ffmpeg.grab_frame(ffmpeg_path, clip, seconds=0.5, width=64)
    assert frame.mode == "RGB"
    assert frame.size[0] == 64


def test_grab_frame_raises_for_broken_video(tmp_path: Path, ffmpeg_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"nope")
    with pytest.raises(ffmpeg.MediaError):
        ffmpeg.grab_frame(ffmpeg_path, junk, seconds=0.1)


def test_probe_raises_media_error_for_broken_file(tmp_path: Path, ffprobe_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"nope")
    with pytest.raises(ffmpeg.MediaError):
        ffmpeg.probe_duration(ffprobe_path, junk)


def test_transcode_writes_destination_and_leaves_no_temp(
    make_video: VideoMaker, ffmpeg_path: Path, tmp_path: Path
) -> None:
    clip = make_video("source.mp4", seconds=1.0, size="160x120")
    out = tmp_path / "converted" / "small.mp4"
    ffmpeg.transcode(ffmpeg_path, clip, out, ["-c:v", "mpeg4", "-vf", "scale=80:60"])
    assert out.is_file()
    assert sorted(p.name for p in out.parent.iterdir()) == ["small.mp4"]


def test_transcode_failure_leaves_no_partial_output(tmp_path: Path) -> None:
    source = tmp_path / "in.mp4"
    source.write_bytes(b"x")
    # A stand-in encoder that always fails, so nothing should be left at the destination.
    failing = tmp_path / "bin" / "ffmpeg.bat"
    failing.parent.mkdir(parents=True)
    failing.write_text("@echo off\r\necho encode error 1>&2\r\nexit /b 1\r\n", encoding="ascii")
    out = tmp_path / "out" / "x.mp4"
    with pytest.raises(ffmpeg.MediaError):
        ffmpeg.transcode(failing, source, out, [])
    assert not out.exists()
    assert list(out.parent.iterdir()) == []


def _fake_encode(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """An encoder stand-in that writes its output file (the last argument) and succeeds."""
    command = args[0]
    assert isinstance(command, list)
    Path(str(command[-1])).write_bytes(b"encoded")
    return subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr="")


def test_transcode_verifies_the_temp_output_before_it_gets_its_final_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "in.mov"
    source.write_bytes(b"x")
    out = tmp_path / "out" / "photo.mp4"
    checked: list[Path] = []

    def verify(candidate: Path) -> None:
        checked.append(candidate)
        assert candidate.read_bytes() == b"encoded"
        assert not out.exists()  # still under its temporary name

    monkeypatch.setattr(ffmpeg, "run_tool", _fake_encode)
    ffmpeg.transcode(tmp_path / "ffmpeg.exe", source, out, [], verify=verify)
    assert len(checked) == 1 and checked[0] != out and checked[0].parent == out.parent
    assert checked[0].suffix == ".mp4"
    assert out.read_bytes() == b"encoded"
    assert sorted(p.name for p in out.parent.iterdir()) == ["photo.mp4"]


def test_a_failed_verify_leaves_no_output_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "in.mov"
    source.write_bytes(b"x")
    out = tmp_path / "out" / "photo.mp4"
    out.parent.mkdir()
    neighbour = out.parent / "other.mp4"
    neighbour.write_bytes(b"not ours")

    def verify(_candidate: Path) -> None:
        raise ffmpeg.MediaError("the converted video is 0.20 s long")

    monkeypatch.setattr(ffmpeg, "run_tool", _fake_encode)
    with pytest.raises(ffmpeg.MediaError, match="0.20 s long"):
        ffmpeg.transcode(tmp_path / "ffmpeg.exe", source, out, [], verify=verify)
    assert not out.exists()
    assert sorted(p.name for p in out.parent.iterdir()) == ["other.mp4"]
    assert neighbour.read_bytes() == b"not ours"


# ----- degrade on timeouts and malformed output ------------------------------------------------


def _timeout(*_args: object, **_kwargs: object) -> object:
    raise subprocess.TimeoutExpired(cmd="tool", timeout=1)


def _garbage(stdout: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["tool"], returncode=0, stdout=stdout, stderr="")

    return run


def test_ffprobe_timeout_becomes_media_error_and_no_creation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg, "run_tool", _timeout)
    with pytest.raises(ffmpeg.MediaError, match="timed out"):
        ffmpeg.display_size(tmp_path / "ffprobe.exe", clip)
    assert ffmpeg.creation_time(tmp_path / "ffprobe.exe", clip) is None


@pytest.mark.parametrize(
    "stdout",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"streams": [{"width": "wide", "height": null}], "format": {"tags": 5}}',
        '{"streams": [5], "format": {"duration": "n/a", "tags": {"creation_time": 7}}}',
    ],
)
def test_malformed_ffprobe_output_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg, "run_tool", _garbage(stdout))
    probe = tmp_path / "ffprobe.exe"
    with pytest.raises(ffmpeg.MediaError):
        ffmpeg.display_size(probe, clip)
    with pytest.raises(ffmpeg.MediaError):
        ffmpeg.probe_duration(probe, clip)
    assert ffmpeg.creation_time(probe, clip) is None


def test_ffmpeg_timeouts_become_media_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg, "run_binary", _timeout)
    with pytest.raises(ffmpeg.MediaError, match="timed out"):
        ffmpeg.grab_frame(tmp_path / "ffmpeg.exe", clip, 1.0)
    monkeypatch.setattr(ffmpeg, "run_tool", _timeout)
    out = tmp_path / "out" / "x.mp4"
    with pytest.raises(ffmpeg.MediaError, match="timed out"):
        ffmpeg.transcode(tmp_path / "ffmpeg.exe", clip, out, [])
    assert list(out.parent.iterdir()) == []


def test_transcode_never_replaces_an_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "in.mp4"
    source.write_bytes(b"x")
    out = tmp_path / "out" / "x.mp4"
    out.parent.mkdir()

    def encode(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        out.write_bytes(b"keep me")  # appears while the encode is running
        return subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg, "run_tool", encode)
    with pytest.raises(FileExistsError):
        ffmpeg.transcode(tmp_path / "ffmpeg.exe", source, out, [])
    assert out.read_bytes() == b"keep me"
    assert [p.name for p in out.parent.iterdir()] == ["x.mp4"]


def test_exiftool_timeout_and_garbage_give_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = tmp_path / "IMG.cr3"
    photo.write_bytes(b"x")
    monkeypatch.setattr(exif, "run_tool", _timeout)
    assert exif.exiftool_capture_time(tmp_path / "exiftool.exe", photo) is None
    monkeypatch.setattr(exif, "run_tool", _garbage("\x00\xff garbage\nnot a date\n"))
    assert exif.exiftool_capture_time(tmp_path / "exiftool.exe", photo) is None

    def failed(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["exiftool"], returncode=1, stdout="2024:01:02 03:04:05", stderr="boom"
        )

    monkeypatch.setattr(exif, "run_tool", failed)
    assert exif.exiftool_capture_time(tmp_path / "exiftool.exe", photo) is None


def test_read_exif_degrades_on_unexpected_pillow_errors(
    make_image: ImageMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_image("pic.jpg")

    def broken(_img: object) -> object:
        raise KeyError("odd tag")

    monkeypatch.setattr(exif, "_exif_from_image", broken)
    assert exif.read_exif(path) == exif.ExifInfo()


def test_probe_video_info_reads_every_stream_for_the_creation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as json_module
    import subprocess as sp

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    seen: list[list[str]] = []
    payload = {
        "format": {"tags": {}},
        "streams": [
            {"codec_type": "audio", "tags": {"creation_time": "2020-06-01T10:00:00.000000Z"}},
            {"codec_type": "video", "width": 640, "height": 480, "tags": {}},
        ],
    }

    def fake(args: list[str], timeout: float = 60) -> sp.CompletedProcess[str]:
        seen.append(args)
        return sp.CompletedProcess(args, 0, json_module.dumps(payload), "")

    monkeypatch.setattr(ffmpeg, "run_tool", fake)
    info = ffmpeg.probe_video_info(Path("ffprobe"), clip)
    assert "-select_streams" not in seen[0]  # not only v:0
    assert info.creation_time is not None and info.creation_time.year in (2020,)
    assert (info.width, info.height) == (640, 480)  # the video stream, not stream 0


def test_a_failed_exiftool_chunk_is_read_file_by_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as json_module
    import subprocess as sp

    paths = [tmp_path / f"{n}.cr3" for n in range(4)]
    bad = paths[2]

    def fake(_tool: Path, args: list[str], _timeout: float) -> sp.CompletedProcess[str]:
        files = [a for a in args if a.endswith(".cr3")]
        if len(files) > 1 or files == [str(bad)]:
            raise sp.TimeoutExpired(args, 1)  # the chunk, and the one bad file, time out
        record = {
            "SourceFile": files[0].replace("\\", "/"),
            "DateTimeOriginal": "2021:01:02 03:04:05",
        }
        return sp.CompletedProcess(args, 0, json_module.dumps([record]), "")

    monkeypatch.setattr(exif, "run_exiftool", fake)
    found = exif.exiftool_raw_info(Path("exiftool"), paths, 5, batch_size=4)
    assert set(found) == {p for p in paths if p != bad}  # one bad file does not blank the rest
