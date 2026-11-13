"""Media converter: the options, the plan, the written files and the safety rules.

Every file is generated under ``tmp_path``. Tests that run ffmpeg or ExifTool use the session
fixtures in ``tests/conftest.py`` and are skipped when the program is missing. The Recycle Bin is
never touched: ``send2trash`` is replaced by a fake that moves the file into a folder in tmp_path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageCms
from pydantic import ValidationError

from various_tools.core import onedrive
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.media import ffmpeg as ffmpeg_media
from various_tools.core.media.ffmpeg import MediaError
from various_tools.core.media.imaging import open_image
from various_tools.core.plan import ActionKind, Plan, RunResult
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.media_convert import TOOL, MediaConvertOptions, images, videos
from various_tools.tools.media_convert import tool as convert_tool
from various_tools.tools.media_convert.images import ImageError
from various_tools.tools.metadata_strip import exiftool as et

GPS_IFD = 0x8825
EXIF_IFD = 0x8769


# ----- helpers -------------------------------------------------------------------------------


def _exif(orientation: int | None = None) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "TestMake"
    exif[0x0110] = "TestModel"
    if orientation is not None:
        exif[0x0112] = orientation
    ifd = exif.get_ifd(GPS_IFD)
    ifd[1], ifd[2] = "N", (37.0, 46.0, 29.7)
    ifd[3], ifd[4] = "W", (122.0, 25.0, 9.8)
    exif.get_ifd(EXIF_IFD)[0x9003] = "2024:05:06 07:08:09"
    return bytes(exif.tobytes())


def _sRGB() -> bytes:
    return bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())


def _picture(
    path: Path,
    fmt: str,
    *,
    size: tuple[int, int] = (64, 48),
    exif: bool = True,
    orientation: int | None = None,
    icc: bool = False,
    **save: Any,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = dict(save)
    if exif:
        options["exif"] = _exif(orientation)
    if icc:
        options["icc_profile"] = _sRGB()
    Image.new("RGB", size, (200, 10, 10)).save(path, format=fmt, **options)
    return path


def _transparent_webp(path: Path, *, animated: bool = False) -> Path:
    """Left half transparent, right half opaque blue."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = Image.new("RGBA", (64, 48), (0, 0, 255, 255))
    frame.paste((255, 0, 0, 0), (0, 0, 32, 48))
    if animated:
        other = Image.new("RGBA", (64, 48), (0, 255, 0, 255))
        frame.save(path, format="WEBP", save_all=True, append_images=[other], duration=100)
    else:
        frame.save(path, format="WEBP", lossless=True)
    return path


def _pixel(image: Image.Image, xy: tuple[int, int]) -> tuple[int, ...]:
    value = image.getpixel(xy)
    assert isinstance(value, tuple)
    return value


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def _ctx(
    tmp_path: Path,
    journal: Journal | None,
    deps: ExternalTools | None = None,
    **settings: Any,
) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path, **settings}),
        deps=deps or ExternalTools(ollama_probe=lambda _u: None),
        journal=journal,
    )


def _run(
    tmp_path: Path,
    journal: Journal,
    opts: MediaConvertOptions,
    deps: ExternalTools | None = None,
) -> tuple[Plan, RunResult]:
    ctx = _ctx(tmp_path, journal, deps)
    plan = TOOL.scan(opts, ctx)
    return plan, TOOL.apply(plan, ctx)


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file's size and modified time. Scan must leave these unchanged."""
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _details(result: RunResult) -> list[str]:
    return [o.detail for o in result.outcomes]


@pytest.fixture
def fake_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace ``send2trash`` with a move into ``tmp_path/bin``. Returns the trashed paths."""
    trashed: list[Path] = []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def fake(path: str | os.PathLike[str]) -> None:
        source = Path(path)
        trashed.append(source)
        shutil.move(str(source), str(bin_dir / source.name))

    monkeypatch.setattr(convert_tool, "send2trash", fake)
    return trashed


@pytest.fixture
def no_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: object) -> None:
        raise AssertionError(f"the Recycle Bin must not be used here: {path}")

    monkeypatch.setattr(convert_tool, "send2trash", refuse)


def _ffmpeg_make(ffmpeg: Path, path: Path, *args: str, seconds: str = "1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-t",
            seconds,
            "-shortest",
            *args,
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return path


# A phone-like MOV: MPEG-4 video and AAC audio, which an MP4 can hold as they are.
COMPATIBLE = ("-pix_fmt", "yuv420p", "-c:v", "mpeg4", "-c:a", "aac")
# Motion JPEG and raw PCM audio, which this tool does not put into an MP4 as they are.
INCOMPATIBLE = ("-pix_fmt", "yuvj420p", "-c:v", "mjpeg", "-c:a", "pcm_s16le")


def _streams(ffprobe: Path, path: Path) -> videos.SourceInfo:
    return videos.probe(ffprobe, path)


# ----- options -------------------------------------------------------------------------------


def test_defaults_are_the_safe_choice() -> None:
    opts = MediaConvertOptions()
    assert (opts.convert_heic, opts.convert_webp, opts.convert_mov) == (True, True, True)
    assert (opts.heic_target, opts.webp_target) == ("jpg", "jpg")
    assert opts.jpeg_quality == 90
    assert opts.background_color == "#ffffff"
    assert opts.keep_metadata is True
    assert opts.video_mode == "auto"
    assert opts.output == "new_folder" and opts.output_folder_name == "converted"
    assert opts.trash_originals is False
    assert opts.faststart is True and opts.video_crf is None


def test_unknown_option_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MediaConvertOptions.model_validate({"overwrite": True})


def test_background_colour_must_be_a_hex_colour_and_is_normalised() -> None:
    assert MediaConvertOptions(background_color="#FF00aa").background_color == "#ff00aa"
    for bad in ("white", "#fff", "ffffff", "#gggggg", ""):
        with pytest.raises(ValidationError):
            MediaConvertOptions(background_color=bad)


def test_numbers_are_bounded() -> None:
    for field, value in (
        ("jpeg_quality", 0),
        ("jpeg_quality", 101),
        ("video_crf", 52),
        ("audio_bitrate_kbps", 8),
        ("duration_tolerance_s", 0.0),
    ):
        with pytest.raises(ValidationError):
            MediaConvertOptions.model_validate({field: value})


def test_tool_is_discovered_through_the_registry() -> None:
    found = discover()["media_convert"]
    assert found is TOOL
    assert found.category == "Media"
    assert found.Options is MediaConvertOptions


def test_bad_options_give_a_note_and_no_actions(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, None)
    assert TOOL.scan(MediaConvertOptions(), ctx).notes == ["Choose at least one folder."]
    none = MediaConvertOptions(
        roots=[tmp_path], convert_heic=False, convert_webp=False, convert_mov=False
    )
    assert TOOL.scan(none, ctx).notes == ["Turn on at least one conversion."]
    plan = TOOL.scan(MediaConvertOptions(roots=[tmp_path], output_folder_name="a/b"), ctx)
    assert plan.actions == []
    assert any("contains a character Windows does not allow" in n for n in plan.notes)


def test_target_formats_and_extensions_follow_the_options() -> None:
    opts = MediaConvertOptions(heic_target="png", webp_target="jpg", jpeg_extension="jpeg")
    assert convert_tool._target("heic", opts) == ".png"
    assert convert_tool._target("webp", opts) == ".jpeg"
    assert convert_tool._target("mov", opts) == ".mp4"


# ----- video decisions and arguments (pure) --------------------------------------------------

PHONE = videos.SourceInfo(duration=3.0, video=("hevc",), audio=("aac",))
PRO = videos.SourceInfo(duration=3.0, video=("prores",), audio=("pcm_s16le",))


def _args(choice: videos.Choice, info: videos.SourceInfo, **over: Any) -> list[str]:
    values: dict[str, Any] = {
        "codec": "h264",
        "crf": 18,
        "preset": "medium",
        "audio_kbps": 192,
        "keep_metadata": True,
        "faststart": True,
    }
    values.update(over)
    return videos.ffmpeg_args(choice, info, **values)


def test_auto_remuxes_when_mp4_can_hold_every_stream() -> None:
    choice = videos.choose(PHONE, "auto", "h264")
    assert isinstance(choice, videos.Choice) and choice.method == "remux"
    args = _args(choice, PHONE)
    assert args[:4] == ["-map", "0:v", "-map", "0:a?"]
    assert args[4:6] == ["-c", "copy"]
    assert "-tag:v" in args and "hvc1" in args  # Apple players need it for H.265 in an MP4
    assert "libx264" not in args


def test_auto_reencodes_only_when_it_has_to_and_says_why() -> None:
    choice = videos.choose(PRO, "auto", "h264")
    assert isinstance(choice, videos.Choice) and choice.method == "reencode"
    assert "prores" in choice.why and "pcm_s16le" in choice.why
    args = _args(choice, PRO)
    assert args[4:10] == ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]
    assert args[args.index("-c:a") : args.index("-c:a") + 4] == ["-c:a", "aac", "-b:a", "192k"]


def test_remux_mode_refuses_what_an_mp4_cannot_hold_and_reencode_mode_always_encodes() -> None:
    refused = videos.choose(PRO, "remux", "h264")
    assert isinstance(refused, str) and "prores" in refused and "re-encode" in refused
    forced = videos.choose(PHONE, "reencode", "h265")
    assert isinstance(forced, videos.Choice) and forced.method == "reencode"
    args = _args(forced, PHONE, codec="h265", crf=22)
    assert "libx265" in args and args[args.index("-crf") + 1] == "22"
    assert args[args.index("-tag:v") + 1] == "hvc1"
    assert args[args.index("-c:a") + 1] == "copy"  # AAC fits an MP4, so it is not re-encoded


def test_metadata_and_faststart_flags() -> None:
    choice = videos.Choice("remux", "remux, no re-encode")
    kept = _args(choice, PHONE)
    assert kept[kept.index("-map_metadata") + 1] == "0"
    assert kept[kept.index("-movflags") + 1] == "+faststart+use_metadata_tags"
    dropped = _args(choice, PHONE, keep_metadata=False, faststart=False)
    assert dropped[dropped.index("-map_metadata") + 1] == "-1"
    assert "-movflags" not in dropped
    assert _args(choice, PHONE, faststart=False)[-1] == "+use_metadata_tags"


def test_default_crf_depends_on_the_codec() -> None:
    assert convert_tool._crf(MediaConvertOptions()) == 18
    assert convert_tool._crf(MediaConvertOptions(reencode_codec="h265")) == 22
    assert convert_tool._crf(MediaConvertOptions(reencode_codec="h265", video_crf=30)) == 30


# ----- plan contents -------------------------------------------------------------------------


def test_scan_lists_each_conversion_with_a_reason(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "a.heic", "HEIF")
    _picture(folder / "b.webp", "WEBP")
    (folder / "notes.txt").write_text("not media", encoding="utf-8")
    plan = TOOL.scan(MediaConvertOptions(roots=[folder], convert_mov=False), _ctx(tmp_path, None))
    assert [(a.src.name, a.dst.name) for a in plan.actions if a.src and a.dst] == [
        ("a.heic", "a.jpg"),
        ("b.webp", "b.jpg"),
    ]
    first = plan.actions[0]
    assert first.kind is ActionKind.CONVERT and first.selected
    assert first.dst == folder / "converted" / "a.jpg"
    assert first.reason.startswith("HEIC -> JPG, 64x48")
    assert "Recycle Bin" not in first.reason
    assert "1 HEIC, 1 WebP" in plan.notes[0]


def test_scan_changes_nothing_on_disk(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "a.heic", "HEIF")
    _picture(folder / "sub" / "b.webp", "WEBP")
    before = _snapshot(tmp_path)
    plan = TOOL.scan(
        MediaConvertOptions(roots=[folder], recursive=True, convert_mov=False),
        _ctx(tmp_path, None),
    )
    assert len(plan.selected_actions()) == 2
    assert _snapshot(tmp_path) == before
    assert not (folder / "converted").exists()
    assert list(tmp_path.rglob("*.tmp")) == []


def test_only_the_enabled_conversions_are_planned(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "a.heic", "HEIF")
    _picture(folder / "b.webp", "WEBP")
    opts = MediaConvertOptions(roots=[folder], convert_heic=False, convert_mov=False)
    plan = TOOL.scan(opts, _ctx(tmp_path, None))
    assert [a.src.name for a in plan.actions if a.src] == ["b.webp"]


def test_the_plan_says_when_the_original_will_be_recycled(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "a.heic", "HEIF")
    opts = MediaConvertOptions(roots=[folder], trash_originals=True, convert_mov=False)
    plan = TOOL.scan(opts, _ctx(tmp_path, None))
    assert "original goes to the Recycle Bin after the output is verified" in plan.actions[0].reason
    assert "Recycle Bin" in plan.notes[0]


def test_transparency_is_mentioned_for_jpg_but_not_for_png(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    _transparent_webp(folder / "t.webp")
    jpg = TOOL.scan(
        MediaConvertOptions(roots=[folder], background_color="#00ff00", convert_mov=False),
        _ctx(tmp_path, None),
    )
    assert "transparent areas become #00ff00" in jpg.actions[0].reason
    png = TOOL.scan(
        MediaConvertOptions(roots=[folder], webp_target="png", convert_mov=False),
        _ctx(tmp_path, None),
    )
    assert "transparent" not in png.actions[0].reason


def test_animated_webp_is_left_alone_with_a_note(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    _transparent_webp(folder / "anim.webp", animated=True)
    plan = TOOL.scan(MediaConvertOptions(roots=[folder], convert_mov=False), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("animated" in n and "anim.webp" in n for n in plan.notes)


def test_an_unreadable_picture_is_skipped_with_a_note(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    folder.mkdir()
    (folder / "broken.heic").write_bytes(b"this is not a HEIC file at all")
    plan = TOOL.scan(MediaConvertOptions(roots=[folder], convert_mov=False), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("could not read the picture" in n and "broken.heic" in n for n in plan.notes)


# ----- apply: pictures -----------------------------------------------------------------------


def test_heic_becomes_jpg_and_the_original_is_untouched(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    original = src.read_bytes()
    os.utime(src, (1_700_000_000, 1_700_000_000))
    plan, result = _run(tmp_path, journal, MediaConvertOptions(roots=[src.parent]))
    assert result.status == "done" and result.count("done") == 1, _details(result)
    out = src.parent / "converted" / "a.jpg"
    with Image.open(out) as image:
        assert image.format == "JPEG"
        assert image.size == (64, 48)
    assert src.read_bytes() == original
    assert int(out.stat().st_mtime) == 1_700_000_000
    assert {a.kind for a in journal.actions(result.run_id)} == {"convert"}
    assert list(src.parent.rglob("*.tmp")) == []
    assert plan.selected_actions()


def test_heic_and_webp_can_become_png(tmp_path: Path, journal: Journal, no_trash: None) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "a.heic", "HEIF")
    _picture(folder / "b.webp", "WEBP", lossless=True)
    opts = MediaConvertOptions(
        roots=[folder], heic_target="png", webp_target="png", convert_mov=False
    )
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 2, _details(result)
    for name in ("a.png", "b.png"):
        with Image.open(folder / "converted" / name) as image:
            assert image.format == "PNG"


def test_exif_is_kept_with_the_orientation_applied_once(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.webp", "WEBP", orientation=6, icc=True)
    opts = MediaConvertOptions(roots=[src.parent], convert_mov=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, _details(result)
    with Image.open(src.parent / "converted" / "a.jpg") as image:
        assert image.size == (48, 64)  # turned once, so the stored 64x48 is now upright
        exif = image.getexif()
        assert exif.get(0x010F) == "TestMake" and exif.get(0x0110) == "TestModel"
        assert exif.get(0x0112, 1) == 1
        assert exif.get_ifd(GPS_IFD).get(1) == "N"
        assert exif.get_ifd(EXIF_IFD).get(0x9003) == "2024:05:06 07:08:09"
        assert image.info.get("icc_profile")


def test_keep_metadata_off_drops_exif_and_profile(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.webp", "WEBP", icc=True)
    opts = MediaConvertOptions(roots=[src.parent], keep_metadata=False, convert_mov=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1
    with Image.open(src.parent / "converted" / "a.jpg") as image:
        assert not image.getexif()
        assert not image.info.get("icc_profile")


def test_transparent_areas_are_painted_onto_the_chosen_colour(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _transparent_webp(tmp_path / "pics" / "t.webp")
    for name, colour, expected in (
        ("white", "#ffffff", (255, 255, 255)),
        ("red", "#ff0000", (255, 0, 0)),
    ):
        opts = MediaConvertOptions(
            roots=[src.parent],
            background_color=colour,
            output_folder_name=name,
            convert_mov=False,
        )
        _plan, result = _run(tmp_path, journal, opts)
        assert result.count("done") == 1, _details(result)
        with Image.open(src.parent / name / "t.jpg") as image:
            transparent, opaque = _pixel(image, (4, 4)), _pixel(image, (60, 40))
        assert all(abs(a - b) <= 8 for a, b in zip(transparent, expected, strict=True))
        assert opaque[2] > 200 and opaque[0] < 60  # the blue half is unchanged


def test_png_target_keeps_the_transparency(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _transparent_webp(tmp_path / "pics" / "t.webp")
    opts = MediaConvertOptions(roots=[src.parent], webp_target="png", convert_mov=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1
    with Image.open(src.parent / "converted" / "t.png") as image:
        assert image.mode == "RGBA"
        assert _pixel(image, (4, 4))[3] == 0


def test_jpeg_options_reach_the_encoder(tmp_path: Path, journal: Journal, no_trash: None) -> None:
    rng_image = Image.effect_noise((256, 256), 80).convert("RGB")
    src = tmp_path / "pics" / "n.webp"
    src.parent.mkdir()
    rng_image.save(src, format="WEBP", lossless=True)
    sizes: dict[str, int] = {}
    for name, quality in (("low", 10), ("high", 95)):
        opts = MediaConvertOptions(
            roots=[src.parent],
            jpeg_quality=quality,
            jpeg_extension="jpeg",
            output_folder_name=name,
            convert_mov=False,
        )
        _plan, result = _run(tmp_path, journal, opts)
        assert result.count("done") == 1
        sizes[name] = (src.parent / name / "n.jpeg").stat().st_size
    assert sizes["low"] < sizes["high"]


def test_next_to_original_uses_the_same_name_with_the_new_extension(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    opts = MediaConvertOptions(roots=[src.parent], output="next_to_original", convert_mov=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1
    assert (src.parent / "a.jpg").is_file() and src.exists()
    assert not (src.parent / "converted").exists()


def test_recursive_run_keeps_the_layout_and_skips_its_own_output(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    root = tmp_path / "pics"
    _picture(root / "top.heic", "HEIF")
    _picture(root / "deep" / "inner.webp", "WEBP")
    opts = MediaConvertOptions(
        roots=[root], recursive=True, output_folder_name="out", convert_mov=False
    )
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 2
    assert (root / "out" / "top.jpg").is_file()
    assert (root / "out" / "deep" / "inner.jpg").is_file()
    again = TOOL.scan(opts, _ctx(tmp_path, None))
    assert {a.src.name for a in again.actions if a.src} == {"top.heic", "inner.webp"}
    assert all(not a.selected for a in again.actions)  # the outputs exist, and are not inputs


def test_jpg_outputs_do_not_take_another_runs_files_as_input(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "already.jpg", "JPEG")
    plan = TOOL.scan(MediaConvertOptions(roots=[folder], convert_mov=False), _ctx(tmp_path, None))
    assert plan.actions == []


# ----- ExifTool and metadata -----------------------------------------------------------------


def test_exiftool_carries_xmp_across_when_it_is_installed(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.webp", "WEBP")
    written = et.run_exiftool(
        exiftool_path, ["-overwrite_original", "-XMP-dc:Creator=Alice", str(src)], 30
    )
    assert written.returncode == 0
    opts = MediaConvertOptions(roots=[src.parent], convert_mov=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, _details(result)
    tags = et.read_tags(exiftool_path, [src.parent / "converted" / "a.jpg"], 30)
    out = next(iter(tags.values())).tags
    assert out.get("XMP-dc:Creator") == "Alice"
    assert out.get("IFD0:Make") == "TestMake"
    assert out.get("IFD0:Orientation") in (None, "Horizontal (normal)", 1)


def test_heic_keeps_its_gps_through_exiftool(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    opts = MediaConvertOptions(roots=[src.parent], convert_mov=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, _details(result)
    out = next(
        iter(et.read_tags(exiftool_path, [src.parent / "converted" / "a.jpg"], 30).values())
    ).tags
    assert "GPS:GPSLatitude" in out and "GPS:GPSLongitude" in out


def test_non_ascii_file_names_work_with_exiftool(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "café 写真 🙂.webp", "WEBP")
    opts = MediaConvertOptions(roots=[src.parent], convert_mov=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, _details(result)
    assert (src.parent / "converted" / "café 写真 🙂.jpg").is_file()


def test_without_exiftool_pictures_still_convert_and_the_plan_explains(
    tmp_path: Path, journal: Journal, no_trash: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _picture(tmp_path / "pics" / "a.webp", "WEBP")
    deps = ExternalTools({"exiftool": r"C:\no\such\exiftool.exe"}, ollama_probe=lambda _u: None)
    monkeypatch.setattr(deps, "exiftool", lambda: None)
    opts = MediaConvertOptions(roots=[src.parent], convert_mov=False)
    plan, result = _run(tmp_path, journal, opts, deps)
    assert any("winget install OliverBetz.ExifTool" in n for n in plan.notes)
    assert result.count("done") == 1
    with Image.open(src.parent / "converted" / "a.jpg") as image:
        assert image.getexif().get(0x010F) == "TestMake"  # the EXIF still travels through Pillow


def test_exiftool_timeout_comes_from_the_settings(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    no_trash: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.webp", "WEBP")
    seen: list[float] = []
    real = images.run_exiftool

    def spy(exiftool: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        seen.append(timeout)
        return real(exiftool, args, timeout)

    monkeypatch.setattr(images, "run_exiftool", spy)
    ctx = _ctx(tmp_path, journal, exiftool_write_timeout_s=77)
    plan = TOOL.scan(MediaConvertOptions(roots=[src.parent], convert_mov=False), ctx)
    assert TOOL.apply(plan, ctx).count("done") == 1
    assert seen == [77]


def test_an_exiftool_failure_fails_the_action_and_leaves_no_output(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    no_trash: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.webp", "WEBP")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MediaConvertOptions(roots=[src.parent], convert_mov=False), ctx)

    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("exiftool", 1)

    monkeypatch.setattr(images, "run_exiftool", hang)
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1 and "timed out" in result.outcomes[0].detail
    assert list((src.parent / "converted").iterdir()) == []


# ----- never overwrite -----------------------------------------------------------------------


def test_existing_output_is_never_overwritten(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    out = src.parent / "converted" / "a.jpg"
    out.parent.mkdir()
    out.write_bytes(b"keep me")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MediaConvertOptions(roots=[src.parent], convert_mov=False), ctx)
    assert [a.selected for a in plan.actions] == [False]
    assert "the output file already exists" in plan.actions[0].reason
    # Even a plan edited to select the row must not overwrite the file.
    plan.actions[0].selected = True
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert out.read_bytes() == b"keep me"
    assert [p.name for p in out.parent.iterdir()] == ["a.jpg"]


def test_next_to_original_never_replaces_a_jpg_that_is_already_there(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    existing = _picture(tmp_path / "pics" / "a.jpg", "JPEG")
    before = existing.read_bytes()
    opts = MediaConvertOptions(roots=[src.parent], output="next_to_original", convert_mov=False)
    plan, result = _run(tmp_path, journal, opts)
    assert [a.selected for a in plan.actions] == [False]
    assert result.outcomes == []
    assert existing.read_bytes() == before


def test_two_files_wanting_one_output_name_are_listed_but_only_the_first_is_selected(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "a.heic", "HEIF")
    _picture(folder / "a.heif", "HEIF")
    plan = TOOL.scan(MediaConvertOptions(roots=[folder], convert_mov=False), _ctx(tmp_path, None))
    assert [a.selected for a in plan.actions] == [True, False]
    assert "another file in this batch takes this output name" in plan.actions[1].reason


def test_output_created_between_scan_and_apply_is_never_replaced(
    tmp_path: Path, journal: Journal, no_trash: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    out = src.parent / "converted" / "a.jpg"
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MediaConvertOptions(roots=[src.parent], convert_mov=False), ctx)
    assert plan.selected_actions()
    real_rename = images.rename_no_replace

    def late_file(tmp: Path, destination: Path) -> None:
        destination.write_bytes(b"keep me")  # appears right before the final rename
        real_rename(tmp, destination)

    monkeypatch.setattr(images, "rename_no_replace", late_file)
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0 and result.count("failed") == 1
    assert out.read_bytes() == b"keep me"
    assert [p.name for p in out.parent.iterdir()] == ["a.jpg"]  # no temp file left behind
    assert src.exists()


# ----- originals: trash_originals ------------------------------------------------------------


def test_the_original_is_recycled_only_after_the_output_is_verified(
    tmp_path: Path, journal: Journal, fake_trash: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    original = src.read_bytes()
    out = src.parent / "converted" / "a.jpg"
    state: dict[str, Any] = {}
    real_fake = convert_tool.send2trash

    def watching(path: str | os.PathLike[str]) -> None:
        state["output_ok"] = out.is_file()
        with Image.open(out) as image:  # the output reads back at the moment the original goes
            state["size"] = image.size
        real_fake(path)

    monkeypatch.setattr(convert_tool, "send2trash", watching)
    opts = MediaConvertOptions(roots=[src.parent], trash_originals=True, convert_mov=False)
    plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, _details(result)
    assert state == {"output_ok": True, "size": (64, 48)}
    assert fake_trash == [src] and not src.exists()
    assert (tmp_path / "bin" / "a.heic").read_bytes() == original
    assert "original goes to the Recycle Bin after the output is verified" in plan.actions[0].reason
    recorded = journal.actions(result.run_id)[0]
    assert "Recycle Bin" in recorded.reason  # the journal says what happened to the original


def test_originals_are_kept_by_default(tmp_path: Path, journal: Journal, no_trash: None) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    _plan, result = _run(tmp_path, journal, MediaConvertOptions(roots=[src.parent]))
    assert result.count("done") == 1
    assert src.exists()  # no_trash would have failed the test had the Recycle Bin been used


def test_a_failed_verification_keeps_the_output_and_the_original(
    tmp_path: Path, journal: Journal, fake_trash: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    original = src.read_bytes()

    def bad_check(self: object, dst: Path, size: tuple[int, int]) -> None:
        raise ImageError("the written picture is 1x1, expected 64x48")

    monkeypatch.setattr(convert_tool._Converter, "_verify_picture", bad_check)
    opts = MediaConvertOptions(roots=[src.parent], trash_originals=True, convert_mov=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("failed") == 1 and "expected 64x48" in result.outcomes[0].detail
    assert fake_trash == []
    assert src.read_bytes() == original
    assert (src.parent / "converted" / "a.jpg").is_file()  # kept, for the owner to inspect


def test_the_verification_really_checks_the_size(tmp_path: Path) -> None:
    path = _picture(tmp_path / "x.jpg", "JPEG")
    images.verify_output(path, (64, 48))
    with pytest.raises(ImageError, match="expected 10x10"):
        images.verify_output(path, (10, 10))
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(path.read_bytes()[:40])
    with pytest.raises(ImageError, match="cannot be read back"):
        images.verify_output(broken, (64, 48))


def test_a_conversion_that_fails_never_recycles_the_original(
    tmp_path: Path, journal: Journal, fake_trash: list[Path]
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    ctx = _ctx(tmp_path, journal)
    opts = MediaConvertOptions(roots=[src.parent], trash_originals=True, convert_mov=False)
    plan = TOOL.scan(opts, ctx)
    src.write_bytes(b"damaged after the scan")
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert fake_trash == []
    assert src.read_bytes() == b"damaged after the scan"
    assert not (src.parent / "converted" / "a.jpg").exists()


def test_when_the_recycle_bin_refuses_the_original_stays_and_the_action_fails(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    original = src.read_bytes()

    def refuse(path: object) -> None:
        raise OSError("the Recycle Bin is not available")

    monkeypatch.setattr(convert_tool, "send2trash", refuse)
    opts = MediaConvertOptions(roots=[src.parent], trash_originals=True, convert_mov=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("failed") == 1
    assert "left in place" in result.outcomes[0].detail
    assert src.read_bytes() == original
    assert (src.parent / "converted" / "a.jpg").is_file()


# ----- safety rules --------------------------------------------------------------------------


def test_protected_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile"
    _picture(profile / "a.heic", "HEIF")
    monkeypatch.setenv("USERPROFILE", str(profile))
    plan = TOOL.scan(MediaConvertOptions(roots=[profile]), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("user profile folder" in n for n in plan.notes)


def test_cloud_only_files_are_skipped_and_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _picture(tmp_path / "pics" / "a.heic", "HEIF")
    monkeypatch.setattr(convert_tool, "_cloud_only", lambda info: True)

    def must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cloud-only file was read")

    monkeypatch.setattr(images, "inspect", must_not_read)
    plan = TOOL.scan(
        MediaConvertOptions(roots=[tmp_path / "pics"], convert_mov=False), _ctx(tmp_path, None)
    )
    assert plan.actions == []
    assert any("skipped: cloud-only, not read" in n for n in plan.notes)


def test_apply_never_reads_a_file_that_became_cloud_only_after_scan(
    tmp_path: Path, journal: Journal, fake_trash: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    ctx = _ctx(tmp_path, journal)
    opts = MediaConvertOptions(roots=[src.parent], trash_originals=True, convert_mov=False)
    plan = TOOL.scan(opts, ctx)
    assert len(plan.selected_actions()) == 1
    real_attributes = onedrive.get_attributes

    def attributes(path: Path) -> int | None:
        if path.name == "a.heic":
            return onedrive.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        return real_attributes(path)

    def must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cloud-only file was read")

    monkeypatch.setattr(onedrive, "get_attributes", attributes)
    monkeypatch.setattr(images, "open_image", must_not_read)
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0 and result.count("failed") == 1
    assert "cloud-only" in result.outcomes[0].detail
    assert fake_trash == []
    assert not (src.parent / "converted").exists()


def _make_junction(link: Path, target: Path) -> bool:
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    return made.returncode == 0


def test_a_junction_inside_the_folder_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "pics"
    root.mkdir()
    outside = tmp_path / "outside"
    _picture(outside / "secret.heic", "HEIF")
    if not _make_junction(root / "link", outside):
        pytest.skip("could not create a junction in this environment")
    try:
        opts = MediaConvertOptions(roots=[root], recursive=True, convert_mov=False)
        assert TOOL.scan(opts, _ctx(tmp_path, None)).actions == []
    finally:
        os.rmdir(root / "link")  # removes the junction only, never its target


def test_output_through_a_junction_is_refused(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "sub" / "a.heic", "HEIF")
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = _ctx(tmp_path, journal)
    opts = MediaConvertOptions(roots=[folder], recursive=True, convert_mov=False)
    plan = TOOL.scan(opts, ctx)
    assert len(plan.selected_actions()) == 1
    # After the scan, the output folder is swapped for a junction that points outside the root.
    if not _make_junction(folder / "converted", outside):
        pytest.skip("could not create a junction in this environment")
    try:
        result = TOOL.apply(plan, ctx)
        assert result.count("done") == 0 and result.count("failed") == 1
        assert "junction or symlink" in result.outcomes[0].detail
        assert list(outside.rglob("*")) == []
    finally:
        os.rmdir(folder / "converted")


def test_a_plan_cannot_reach_outside_the_selected_folders(
    tmp_path: Path, journal: Journal, fake_trash: list[Path]
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    elsewhere = _picture(tmp_path / "elsewhere" / "b.heic", "HEIF")
    ctx = _ctx(tmp_path, journal)
    opts = MediaConvertOptions(roots=[src.parent], trash_originals=True, convert_mov=False)
    plan = TOOL.scan(opts, ctx)
    plan.actions[0].src = elsewhere
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert "outside the selected folders" in result.outcomes[0].detail
    assert fake_trash == [] and elsewhere.exists()


def test_a_plan_cannot_pick_a_different_output_format(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MediaConvertOptions(roots=[src.parent], convert_mov=False), ctx)
    plan.actions[0].dst = src.parent / "converted" / "a.png"
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert "does not match the chosen format" in result.outcomes[0].detail


def test_apply_then_undo_says_outputs_are_manual(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    _plan, result = _run(tmp_path, journal, MediaConvertOptions(roots=[src.parent]))
    report = undo_run(journal, result.run_id)
    assert report.reverted == 0
    assert any("not reversible" in line for line in report.skipped)
    assert (src.parent / "converted" / "a.jpg").exists()


def test_saved_plan_can_be_applied_later(tmp_path: Path, journal: Journal, no_trash: None) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    ctx = _ctx(tmp_path, journal)
    saved = TOOL.scan(MediaConvertOptions(roots=[src.parent], convert_mov=False), ctx).to_json()
    assert TOOL.apply(Plan.from_json(saved), ctx).count("done") == 1


def test_open_image_reads_the_output_of_a_heic_roundtrip(
    tmp_path: Path, journal: Journal, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    _plan, result = _run(tmp_path, journal, MediaConvertOptions(roots=[src.parent]))
    assert result.count("done") == 1
    with open_image(src) as before, open_image(src.parent / "converted" / "a.jpg") as after:
        assert before.size == after.size


# ----- videos --------------------------------------------------------------------------------


def test_a_compatible_mov_is_remuxed_without_re_encoding(
    tmp_path: Path, journal: Journal, ffmpeg_path: Path, ffprobe_path: Path, no_trash: None
) -> None:
    src = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "a.mov", *COMPATIBLE)
    original = src.read_bytes()
    os.utime(src, (1_700_000_000, 1_700_000_000))
    opts = MediaConvertOptions(roots=[src.parent], convert_heic=False, convert_webp=False)
    plan, result = _run(tmp_path, journal, opts)
    assert "remux, no re-encode" in plan.actions[0].reason
    assert result.count("done") == 1, _details(result)
    out = src.parent / "converted" / "a.mp4"
    before, after = _streams(ffprobe_path, src), _streams(ffprobe_path, out)
    assert (after.video, after.audio) == (before.video, before.audio)  # streams were copied
    assert abs(after.duration - before.duration) < 0.1
    assert src.read_bytes() == original
    assert int(out.stat().st_mtime) == 1_700_000_000
    assert list(src.parent.rglob("*.tmp")) == []


def test_an_incompatible_mov_is_re_encoded_to_h264_with_aac(
    tmp_path: Path, journal: Journal, ffmpeg_path: Path, ffprobe_path: Path, no_trash: None
) -> None:
    src = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "p.mov", *INCOMPATIBLE)
    opts = MediaConvertOptions(roots=[src.parent], convert_heic=False, convert_webp=False)
    plan, result = _run(tmp_path, journal, opts)
    assert "re-encode to H264" in plan.actions[0].reason and "mjpeg" in plan.actions[0].reason
    assert result.count("done") == 1, _details(result)
    info = _streams(ffprobe_path, src.parent / "converted" / "p.mp4")
    assert info.video == ("h264",) and info.audio == ("aac",)


def test_remux_only_mode_skips_what_an_mp4_cannot_hold(
    tmp_path: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    src = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "p.mov", *INCOMPATIBLE)
    opts = MediaConvertOptions(
        roots=[src.parent], convert_heic=False, convert_webp=False, video_mode="remux"
    )
    plan = TOOL.scan(opts, _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("cannot go into an MP4 without re-encoding" in n for n in plan.notes)


def test_re_encode_mode_encodes_even_a_compatible_mov(
    tmp_path: Path, journal: Journal, ffmpeg_path: Path, ffprobe_path: Path, no_trash: None
) -> None:
    src = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "a.mov", *COMPATIBLE)
    opts = MediaConvertOptions(
        roots=[src.parent],
        convert_heic=False,
        convert_webp=False,
        video_mode="reencode",
        video_crf=30,
        video_preset="ultrafast",
    )
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, _details(result)
    info = _streams(ffprobe_path, src.parent / "converted" / "a.mp4")
    assert info.video == ("h264",)  # the source was MPEG-4


def test_a_phone_rotation_survives_a_remux(
    tmp_path: Path,
    journal: Journal,
    make_video: Callable[..., Path],
    ffprobe_path: Path,
    no_trash: None,
) -> None:
    src = make_video("r.mov", rotate=90)
    assert ffmpeg_media.display_size(ffprobe_path, src) == (120, 160)
    opts = MediaConvertOptions(roots=[tmp_path], convert_heic=False, convert_webp=False)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("failed") == 0, _details(result)
    out = tmp_path / "converted" / "r.mp4"
    assert ffmpeg_media.display_size(ffprobe_path, out) == (120, 160)


def test_video_metadata_is_kept_or_dropped_by_the_option(
    tmp_path: Path, ffmpeg_path: Path, ffprobe_path: Path, no_trash: None
) -> None:
    src = _ffmpeg_make(
        ffmpeg_path, tmp_path / "clips" / "a.mov", *COMPATIBLE, "-metadata", "title=Holiday"
    )
    for name, keep in (("kept", True), ("dropped", False)):
        journal = Journal(tmp_path / f"{name}.sqlite")
        try:
            opts = MediaConvertOptions(
                roots=[src.parent],
                convert_heic=False,
                convert_webp=False,
                keep_metadata=keep,
                output_folder_name=name,
            )
            _plan, result = _run(tmp_path, journal, opts)
            assert result.count("done") == 1, _details(result)
        finally:
            journal.close()
        tags = ffmpeg_media.ffprobe_json(
            ffprobe_path, src.parent / name / "a.mp4", entries=["format_tags"]
        )
        title = tags.get("format", {}).get("tags", {}).get("title")
        assert title == ("Holiday" if keep else None)


def test_mov_originals_are_recycled_only_after_the_length_is_verified(
    tmp_path: Path,
    journal: Journal,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    fake_trash: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "a.mov", *COMPATIBLE)
    out = src.parent / "converted" / "a.mp4"
    seen: dict[str, Any] = {}
    real_fake = convert_tool.send2trash

    def watching(path: str | os.PathLike[str]) -> None:
        seen["duration"] = ffmpeg_media.probe_duration(ffprobe_path, out)
        real_fake(path)

    monkeypatch.setattr(convert_tool, "send2trash", watching)
    opts = MediaConvertOptions(
        roots=[src.parent], convert_heic=False, convert_webp=False, trash_originals=True
    )
    plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, _details(result)
    assert seen["duration"] > 0.5
    assert fake_trash == [src] and not src.exists()
    assert "Recycle Bin" in plan.actions[0].reason


def test_a_video_that_fails_verification_keeps_the_original(
    tmp_path: Path,
    journal: Journal,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    fake_trash: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "a.mov", *COMPATIBLE)
    original = src.read_bytes()

    def bad_check(self: object, ffprobe: Path, dst: Path, expected: float) -> None:
        raise MediaError("the converted video is 0.20 s long, the original 1.00 s")

    monkeypatch.setattr(convert_tool._Converter, "_verify_video", bad_check)
    opts = MediaConvertOptions(
        roots=[src.parent], convert_heic=False, convert_webp=False, trash_originals=True
    )
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("failed") == 1 and "0.20 s long" in result.outcomes[0].detail
    assert fake_trash == []
    assert src.read_bytes() == original
    assert (src.parent / "converted" / "a.mp4").is_file()


def test_the_duration_check_uses_the_tolerance(
    tmp_path: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    clip = _ffmpeg_make(ffmpeg_path, tmp_path / "a.mp4", *COMPATIBLE)
    actual = ffmpeg_media.probe_duration(ffprobe_path, clip)
    videos.verify_output(ffprobe_path, clip, actual + 0.3, 0.5)
    with pytest.raises(MediaError, match="more than 0.1 s apart"):
        videos.verify_output(ffprobe_path, clip, actual + 0.3, 0.1)


def test_mov_files_are_skipped_with_an_install_hint_when_ffmpeg_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "clips"
    folder.mkdir()
    (folder / "a.mov").write_bytes(b"not really a video")
    deps = ExternalTools(ollama_probe=lambda _u: None)
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    monkeypatch.setattr(deps, "ffprobe", lambda: None)
    opts = MediaConvertOptions(roots=[folder], convert_heic=False, convert_webp=False)
    plan = TOOL.scan(opts, _ctx(tmp_path, None, deps))
    assert plan.actions == []
    assert any("winget install Gyan.FFmpeg" in n for n in plan.notes)
    assert any("ffmpeg and ffprobe are needed" in n and "a.mov" in n for n in plan.notes)


def test_apply_without_ffmpeg_fails_the_action_and_keeps_the_original(
    tmp_path: Path,
    journal: Journal,
    ffmpeg_path: Path,
    fake_trash: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "a.mov", *COMPATIBLE)
    opts = MediaConvertOptions(
        roots=[src.parent], convert_heic=False, convert_webp=False, trash_originals=True
    )
    plan = TOOL.scan(opts, _ctx(tmp_path, None))
    deps = ExternalTools(ollama_probe=lambda _u: None)
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    result = TOOL.apply(plan, _ctx(tmp_path, journal, deps))
    assert result.count("failed") == 1
    assert "winget install Gyan.FFmpeg" in result.outcomes[0].detail
    assert fake_trash == [] and src.exists()


def test_video_timeouts_come_from_the_settings(
    tmp_path: Path,
    journal: Journal,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    no_trash: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "a.mov", *COMPATIBLE)
    transcode_timeouts: list[float] = []
    probe_timeouts: list[float] = []
    real_transcode = ffmpeg_media.transcode
    real_probe = ffmpeg_media.ffprobe_json

    def transcode(*args: Any, timeout: float, **kwargs: Any) -> None:
        transcode_timeouts.append(timeout)
        real_transcode(*args, timeout=timeout, **kwargs)

    def probe(*args: Any, timeout: float, **kwargs: Any) -> dict[str, Any]:
        probe_timeouts.append(timeout)
        return real_probe(*args, timeout=timeout, **kwargs)

    monkeypatch.setattr(ffmpeg_media, "transcode", transcode)
    monkeypatch.setattr(ffmpeg_media, "ffprobe_json", probe)
    ctx = _ctx(tmp_path, journal, transcode_timeout_s=123, ffprobe_timeout_s=17)
    opts = MediaConvertOptions(roots=[src.parent], convert_heic=False, convert_webp=False)
    plan = TOOL.scan(opts, ctx)
    assert TOOL.apply(plan, ctx).count("done") == 1
    assert transcode_timeouts == [123]
    assert probe_timeouts and set(probe_timeouts) == {17}


def test_an_existing_mp4_is_never_replaced_next_to_the_original(
    tmp_path: Path, journal: Journal, ffmpeg_path: Path, no_trash: None
) -> None:
    src = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "a.mov", *COMPATIBLE)
    existing = _ffmpeg_make(ffmpeg_path, tmp_path / "clips" / "a.mp4", *COMPATIBLE)
    before = existing.read_bytes()
    opts = MediaConvertOptions(
        roots=[src.parent], convert_heic=False, convert_webp=False, output="next_to_original"
    )
    plan, result = _run(tmp_path, journal, opts)
    assert [a.selected for a in plan.actions] == [False]
    assert result.outcomes == []
    assert existing.read_bytes() == before


def test_a_junction_as_the_selected_folder_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _picture(real / "a.heic", "HEIF")
    link = tmp_path / "linkroot"
    if not _make_junction(link, real):
        pytest.skip("could not create a junction in this environment")
    try:
        opts = MediaConvertOptions(roots=[link], recursive=True, convert_mov=False)
        plan = TOOL.scan(opts, _ctx(tmp_path, None))
        assert plan.actions == [] and plan.roots == []
    finally:
        os.rmdir(link)  # removes the junction only, never its target
    assert [p.name for p in real.iterdir()] == ["a.heic"]
