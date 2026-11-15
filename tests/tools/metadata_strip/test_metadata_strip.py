"""Metadata stripper: the tag rules, the plan, the written copies and the safety rules.

Every file is generated under ``tmp_path``. Tests that run ExifTool or ffmpeg use the session
fixtures in ``tests/conftest.py`` and are skipped when the program is missing. The Recycle Bin is
never touched: ``send2trash`` is replaced by a fake that moves the file into a folder in tmp_path.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageCms
from pydantic import ValidationError

from various_tools.core import onedrive
from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.media import ffmpeg as ffmpeg_media
from various_tools.core.media.imaging import open_image
from various_tools.core.plan import ActionKind, Plan, RunResult
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.metadata_strip import TOOL, MetadataStripOptions
from various_tools.tools.metadata_strip import exiftool as et
from various_tools.tools.metadata_strip import tool as strip_tool

GPS_IFD = 0x8825
EXIF_IFD = 0x8769


# ----- helpers -------------------------------------------------------------------------------


def _exif(*, gps: bool = True, orientation: int | None = 6) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "TestMake"
    exif[0x0110] = "TestModel"
    if orientation is not None:
        exif[0x0112] = orientation
    if gps:
        ifd = exif.get_ifd(GPS_IFD)
        ifd[1], ifd[2] = "N", (37.0, 46.0, 29.7)
        ifd[3], ifd[4] = "W", (122.0, 25.0, 9.8)
        ifd[5], ifd[6] = 0, 12.5
    sub = exif.get_ifd(EXIF_IFD)
    sub[0xA431] = "SN12345"
    sub[0x9003] = "2024:05:06 07:08:09"
    return bytes(exif.tobytes())


def _picture(
    path: Path,
    fmt: str | None = None,
    *,
    gps: bool = True,
    size: tuple[int, int] = (64, 48),
    icc: bool = False,
    exif: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {}
    if exif:
        options["exif"] = _exif(gps=gps)
    if icc:
        options["icc_profile"] = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", size, (200, 10, 10)).save(path, format=fmt, **options)
    return path


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
    opts: MetadataStripOptions,
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


def _read(exiftool: Path, path: Path) -> dict[str, Any]:
    result = et.read_tags(exiftool, [path], 30)[path]
    assert result.error is None
    return result.tags


def _gps_left(tags: dict[str, Any]) -> list[str]:
    return [k for k in tags if "GPS" in k.partition(":")[2].upper() and "Composite" not in k]


class FakeBin(list[Path]):
    """Stands in for the Recycle Bin: trashing writes a ``$I``/``$R`` pair into a folder under
    tmp_path. It is a list of the trashed paths, so ``bin == [src]`` checks what was recycled."""

    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def trash(self, path: str | os.PathLike[str]) -> None:
        source = Path(path)
        size = source.stat().st_size
        key = uuid.uuid4().hex[:6].upper()
        when = int(datetime.now(UTC).timestamp() * 10_000_000) + 116444736000000000
        text = str(source) + "\x00"
        index = struct.pack("<qqqi", 2, size, when, len(text)) + text.encode("utf-16-le")
        shutil.move(str(source), str(self.folder / f"$R{key}{source.suffix}"))
        (self.folder / f"$I{key}{source.suffix}").write_bytes(index)
        self.append(source)

    def locate(self, _path: Path) -> Path | None:
        return self.folder

    def items(self) -> list[Path]:
        return sorted(self.folder.glob("$R*"))


@pytest.fixture
def fake_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBin:
    """Replace the engine's ``send2trash`` with a fake Recycle Bin. Lists the trashed paths."""
    fake = FakeBin(tmp_path / "bin")
    monkeypatch.setattr(plan_module, "send2trash", fake.trash)
    return fake


@pytest.fixture
def no_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: object) -> None:
        raise AssertionError(f"the Recycle Bin must not be used here: {path}")

    monkeypatch.setattr(plan_module, "send2trash", refuse)


# ----- tag rules (pure) ----------------------------------------------------------------------

IMAGE_TAGS: dict[str, Any] = {
    "SourceFile": "a.jpg",
    "System:FileName": "a.jpg",
    "File:FileType": "JPEG",
    "JFIF:JFIFVersion": "1.01",
    "IFD0:Make": "Cam",
    "IFD0:Model": "X1",
    "IFD0:Orientation": "Rotate 90 CW",
    "IFD0:YCbCrPositioning": "Centered",
    "ExifIFD:SerialNumber": "SN1",
    "ExifIFD:DateTimeOriginal": "2024:05:06 07:08:09",
    "GPS:GPSLatitude": "37",
    "GPS:GPSLongitude": "-122",
    "GPS:GPSAltitude": "12 m",
    "GPS:GPSTimeStamp": "01:02:03",
    "XMP-exif:GPSLatitude": "37",
    "ICC_Profile:ProfileDescription": "sRGB",
    "ICC-header:ProfileVersion": "2.1",
    "Composite:GPSPosition": "37, -122",
    "Composite:ImageSize": "64x48",
}


def test_gps_scope_removes_only_gps_tags_from_any_group() -> None:
    labels = et.removable_tags(IMAGE_TAGS, "gps")
    assert labels == [
        "GPS:GPSLatitude",
        "GPS:GPSLongitude",
        "GPS:GPSAltitude",
        "GPS:GPSTimeStamp",
        "XMP-exif:GPSLatitude",
    ]


def test_all_scope_keeps_structure_and_the_two_kept_tags_by_default() -> None:
    labels = et.removable_tags(IMAGE_TAGS, "all")
    assert (
        "IFD0:Make" in labels and "ExifIFD:SerialNumber" in labels and "GPS:GPSLatitude" in labels
    )
    assert "IFD0:Orientation" not in labels
    assert not any(
        label.startswith(("ICC", "Composite", "System", "File", "JFIF")) for label in labels
    )
    assert "IFD0:YCbCrPositioning" not in labels


def test_all_scope_can_drop_orientation_and_colour_profile() -> None:
    labels = et.removable_tags(IMAGE_TAGS, "all", keep_orientation=False, keep_color_profile=False)
    assert "IFD0:Orientation" in labels
    assert "ICC_Profile:ProfileDescription" in labels


def test_png_text_chunks_are_metadata_but_header_fields_are_not() -> None:
    tags = {
        "PNG:ImageWidth": 64,
        "PNG:BitDepth": 8,
        "PNG:Software": "Painter",
        "PNG:Comment": "prompt",
        "PNG:ProfileName": "sRGB",
    }
    assert et.removable_tags(tags, "all") == ["PNG:Software", "PNG:Comment"]
    assert et.removable_tags(tags, "all", keep_color_profile=False)[-1] == "PNG:ProfileName"


def test_video_location_tags_that_do_not_start_with_gps_are_found() -> None:
    tags = {
        "Keys:Location": "+37.7-122.4/",
        "UserData:LocationInformation": "Lat=37",
        "Keys:Title": "Holiday",
        "QuickTime:CreateDate": "2024:05:06 07:08:09",
        "Track1:ImageWidth": 160,
    }
    assert et.removable_tags(tags, "gps") == ["Keys:Location", "UserData:LocationInformation"]
    assert et.removable_tags(tags, "all") == [
        "Keys:Location",
        "UserData:LocationInformation",
        "Keys:Title",
    ]


def test_duplicate_tag_suffixes_do_not_double_count() -> None:
    tags = {"GPS:GPSLatitude": 1, "GPS:GPSLatitude (1)": 2}
    assert et.removable_tags(tags, "gps") == ["GPS:GPSLatitude"]


def test_descriptions_name_what_goes() -> None:
    assert (
        et.describe_removal(["GPS:GPSLatitude", "GPS:GPSLongitude", "GPS:GPSAltitude"], "gps")
        == "remove GPS: position, altitude (3 tags)"
    )
    text = et.describe_removal(
        ["GPS:GPSLatitude", "ExifIFD:SerialNumber", "IFD0:Make", "IFD0:Artist"], "all"
    )
    assert text.startswith("remove all: 4 tags incl. ")
    assert "GPS" in text and "camera serial" in text and "owner name" in text


def test_strip_args_never_overwrite_and_depend_on_the_scope() -> None:
    gps = et.strip_args("gps")
    assert "-gps:all=" in gps and "-all=" not in gps
    assert "-overwrite_original" not in gps
    everything = et.strip_args("all")
    assert everything[:1] == ["-all="]
    assert "-Orientation" in everything and "-icc_profile" in everything
    bare = et.strip_args(
        "all", keep_orientation=False, keep_color_profile=False, ignore_minor_errors=True
    )
    assert bare == ["-m", "-all="]
    assert "-Keys:Location=" in et.strip_args("gps", video=True)
    assert "-tagsfromfile" not in et.strip_args("all", video=True)


# ----- options -------------------------------------------------------------------------------


def test_defaults_are_the_safe_choice() -> None:
    opts = MetadataStripOptions()
    assert opts.scope == "gps"
    assert opts.media == "images"
    assert opts.output == "new_folder"
    assert opts.output_folder_name == "stripped"
    assert opts.suffix_tag == "_stripped"
    assert opts.in_place is False
    assert opts.keep_orientation is True and opts.keep_color_profile is True
    assert opts.ignore_minor_errors is False


def test_unknown_option_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MetadataStripOptions.model_validate({"overwrite": True})


def test_extension_options_are_checked_against_what_exiftool_can_write() -> None:
    opts = MetadataStripOptions(image_extensions=["JPG", ".png"], video_extensions=["mp4"])
    assert opts.image_extensions == [".jpg", ".png"]
    assert opts.video_extensions == [".mp4"]
    with pytest.raises(ValidationError):
        MetadataStripOptions(image_extensions=[".bmp"])
    with pytest.raises(ValidationError):
        MetadataStripOptions(video_extensions=[".mkv"])


@pytest.mark.parametrize("bad", ["", " ", "a/b", "x:", "tag.", 'q"'])
def test_suffix_tag_must_be_a_plain_part_of_a_file_name(bad: str) -> None:
    with pytest.raises(ValidationError):
        MetadataStripOptions(suffix_tag=bad)


def test_tool_is_discovered_through_the_registry() -> None:
    found = discover()["metadata_strip"]
    assert found is TOOL
    assert found.category == "Media"
    assert found.Options is MetadataStripOptions


def test_bad_options_give_a_note_and_no_actions(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, None)
    assert TOOL.scan(MetadataStripOptions(), ctx).notes == ["Choose at least one folder."]
    plan = TOOL.scan(MetadataStripOptions(roots=[tmp_path], output_folder_name="a/b"), ctx)
    assert plan.actions == []
    assert any("contains a character Windows does not allow" in n for n in plan.notes)


# ----- plan contents -------------------------------------------------------------------------


def test_scan_lists_what_each_file_would_lose(tmp_path: Path, exiftool_path: Path) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "gps.jpg")
    _picture(folder / "clean.jpg", gps=False, exif=False)
    plan = TOOL.scan(MetadataStripOptions(roots=[folder]), _ctx(tmp_path, None))
    assert [a.src.name for a in plan.actions if a.src] == ["gps.jpg"]
    action = plan.actions[0]
    assert action.kind is ActionKind.CONVERT and action.selected
    assert action.dst == folder / "stripped" / "gps.jpg"
    assert action.reason.startswith("remove GPS: position, altitude")
    assert "GPS" in action.reason and "tags)" in action.reason
    assert "1 had nothing to remove" in plan.notes[0]


def test_scan_for_all_metadata_names_the_sensitive_parts(
    tmp_path: Path, exiftool_path: Path
) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "a.jpg")
    plan = TOOL.scan(MetadataStripOptions(roots=[folder], scope="all"), _ctx(tmp_path, None))
    reason = plan.actions[0].reason
    assert reason.startswith("remove all: ")
    assert "incl." in reason and "GPS" in reason and "camera serial" in reason


def test_a_file_with_only_other_metadata_is_not_an_action_in_gps_mode(
    tmp_path: Path, exiftool_path: Path
) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "nogps.jpg", gps=False)
    plan = TOOL.scan(MetadataStripOptions(roots=[folder]), _ctx(tmp_path, None))
    assert plan.actions == []
    assert "1 had nothing to remove" in plan.notes[0]


def test_scan_changes_nothing_on_disk(tmp_path: Path, exiftool_path: Path) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "a.jpg")
    _picture(folder / "sub" / "b.png", "PNG")
    before = _snapshot(tmp_path)
    plan = TOOL.scan(MetadataStripOptions(roots=[folder], recursive=True), _ctx(tmp_path, None))
    assert len(plan.selected_actions()) == 2
    assert _snapshot(tmp_path) == before
    assert not (folder / "stripped").exists()
    assert list(tmp_path.rglob("*.tmp")) == []


def test_scan_reads_files_in_batches_of_the_chosen_size(
    tmp_path: Path, exiftool_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "pics"
    for index in range(5):
        _picture(folder / f"p{index}.jpg")
    sizes: list[int] = []
    real = strip_tool.read_tags

    def spy(exiftool: Path, paths: list[Path], timeout: float) -> dict[Path, et.TagRead]:
        sizes.append(len(paths))
        return real(exiftool, paths, timeout)

    monkeypatch.setattr(strip_tool, "read_tags", spy)
    plan = TOOL.scan(MetadataStripOptions(roots=[folder], read_batch_size=2), _ctx(tmp_path, None))
    assert sizes == [2, 2, 1]
    assert len(plan.actions) == 5


# ----- apply: images -------------------------------------------------------------------------


def test_gps_is_removed_and_everything_else_is_kept(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    original = src.read_bytes()
    os.utime(src, (1_700_000_000, 1_700_000_000))
    plan, result = _run(tmp_path, journal, MetadataStripOptions(roots=[src.parent]))
    assert result.status == "done" and result.count("done") == 1
    out = src.parent / "stripped" / "a.jpg"
    tags = _read(exiftool_path, out)
    assert _gps_left(tags) == []
    assert tags["IFD0:Make"] == "TestMake"
    assert tags["ExifIFD:SerialNumber"] == "SN12345"
    assert src.read_bytes() == original  # the original is never changed
    assert int(out.stat().st_mtime) == 1_700_000_000
    with Image.open(out) as image:
        assert image.size == (64, 48)
    assert {a.kind for a in journal.actions(result.run_id)} == {"convert"}
    assert list(src.parent.rglob("*.tmp")) == []
    assert plan.selected_actions()


@pytest.mark.parametrize(
    ("name", "fmt"), [("a.jpg", "JPEG"), ("a.png", "PNG"), ("a.webp", "WEBP"), ("a.heic", "HEIF")]
)
def test_all_metadata_is_removed_from_every_picture_type(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None, name: str, fmt: str
) -> None:
    src = _picture(tmp_path / "pics" / name, fmt, icc=fmt != "HEIF")
    opts = MetadataStripOptions(roots=[src.parent], scope="all")
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, [o.detail for o in result.outcomes]
    tags = _read(exiftool_path, src.parent / "stripped" / name)
    assert et.removable_tags(tags, "all") == []
    assert _gps_left(tags) == []
    assert "IFD0:Make" not in tags and "ExifIFD:SerialNumber" not in tags
    assert "IFD0:Orientation" in tags  # kept by default so rotated photos stay upright
    if fmt != "HEIF":
        assert any(k.startswith("ICC_Profile") for k in tags)  # the colour profile is kept


def test_orientation_and_profile_can_be_dropped(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg", icc=True)
    opts = MetadataStripOptions(
        roots=[src.parent], scope="all", keep_orientation=False, keep_color_profile=False
    )
    _run(tmp_path, journal, opts)
    tags = _read(exiftool_path, src.parent / "stripped" / "a.jpg")
    assert "IFD0:Orientation" not in tags
    assert not any(k.startswith("ICC_Profile") for k in tags)


def test_xmp_gps_is_removed_too(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg", gps=False)
    written = et.run_exiftool(
        exiftool_path,
        [
            "-overwrite_original",
            "-XMP-exif:GPSLatitude=37.5",
            "-XMP-exif:GPSLongitude=-122.1",
            str(src),
        ],
        30,
    )
    assert written.returncode == 0
    assert _gps_left(_read(exiftool_path, src)) != []
    plan, result = _run(tmp_path, journal, MetadataStripOptions(roots=[src.parent]))
    assert "XMP" not in plan.actions[0].reason or "position" in plan.actions[0].reason
    assert result.count("done") == 1
    assert _gps_left(_read(exiftool_path, src.parent / "stripped" / "a.jpg")) == []


def test_tiff_loses_gps_but_all_metadata_is_not_offered_for_it(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.tiff", "TIFF")
    gps_plan, gps_result = _run(tmp_path, journal, MetadataStripOptions(roots=[src.parent]))
    assert gps_result.count("done") == 1
    assert _gps_left(_read(exiftool_path, src.parent / "stripped" / "a.tiff")) == []
    all_plan = TOOL.scan(
        MetadataStripOptions(roots=[src.parent], scope="all", output="suffix"),
        _ctx(tmp_path, None),
    )
    assert all_plan.actions == []
    assert any("only GPS from a TIFF" in n for n in all_plan.notes)
    assert gps_plan.selected_actions()


def test_non_ascii_file_names_work(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    name = "café ünï 写真 🙂.jpg"
    src = _picture(tmp_path / "pics" / name)
    _plan, result = _run(tmp_path, journal, MetadataStripOptions(roots=[src.parent]))
    assert result.count("done") == 1, [o.detail for o in result.outcomes]
    assert _gps_left(_read(exiftool_path, src.parent / "stripped" / name)) == []


def test_suffix_output_sits_beside_the_original_and_is_not_rescanned(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    opts = MetadataStripOptions(roots=[src.parent], output="suffix", suffix_tag="_clean")
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1
    assert (src.parent / "a_clean.jpg").is_file()
    again = TOOL.scan(opts, _ctx(tmp_path, None))
    # Only the original is listed (its output exists, so it is not selected). The output is not.
    assert [a.src.name for a in again.actions if a.src] == ["a.jpg"]
    assert [a.selected for a in again.actions] == [False]


def test_recursive_run_keeps_the_folder_layout_and_skips_its_own_output(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    root = tmp_path / "pics"
    _picture(root / "top.jpg")
    _picture(root / "deep" / "inner.jpg")
    opts = MetadataStripOptions(roots=[root], recursive=True, output_folder_name="clean")
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 2
    assert (root / "clean" / "top.jpg").is_file()
    assert (root / "clean" / "deep" / "inner.jpg").is_file()
    second = TOOL.scan(opts, _ctx(tmp_path, None))
    # The originals still have GPS, so they are listed again, but nothing inside clean/ is.
    assert all("clean" not in a.src.parts for a in second.actions if a.src)
    assert {a.src.name for a in second.actions if a.src} == {"top.jpg", "inner.jpg"}
    assert all(not a.selected for a in second.actions)  # their outputs already exist


# ----- never overwrite -----------------------------------------------------------------------


def test_existing_output_is_never_overwritten(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    out = src.parent / "stripped" / "a.jpg"
    out.parent.mkdir()
    out.write_bytes(b"keep me")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MetadataStripOptions(roots=[src.parent]), ctx)
    assert [a.selected for a in plan.actions] == [False]
    assert "the output file already exists" in plan.actions[0].reason
    # Even a plan edited to select the row must not overwrite the file.
    plan.actions[0].selected = True
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert out.read_bytes() == b"keep me"
    assert [p.name for p in out.parent.iterdir()] == ["a.jpg"]  # no temp file left behind


def test_output_created_between_scan_and_apply_is_never_replaced(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    no_trash: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    out = src.parent / "stripped" / "a.jpg"
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MetadataStripOptions(roots=[src.parent]), ctx)
    assert plan.selected_actions()
    real_rename = strip_tool.rename_no_replace

    def late_file(staging: Path, destination: Path) -> None:
        destination.write_bytes(b"keep me")  # appears right before the final rename
        real_rename(staging, destination)

    monkeypatch.setattr(strip_tool, "rename_no_replace", late_file)
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0 and result.count("failed") == 1
    assert out.read_bytes() == b"keep me"
    assert [p.name for p in out.parent.iterdir()] == ["a.jpg"]


def test_two_files_wanting_one_output_name_are_listed_but_only_the_first_is_selected(
    tmp_path: Path, exiftool_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "a.jpg")
    _picture(folder / "b.jpg")
    taken = folder / "stripped" / "same.jpg"
    monkeypatch.setattr(strip_tool, "_output_path", lambda src, root, opts: taken)
    plan = TOOL.scan(MetadataStripOptions(roots=[folder]), _ctx(tmp_path, None))
    assert [a.selected for a in plan.actions] == [True, False]
    assert "another file in this batch takes this output name" in plan.actions[1].reason


# ----- safety rules --------------------------------------------------------------------------


def test_protected_root_is_refused(
    tmp_path: Path, exiftool_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    _picture(profile / "a.jpg")
    monkeypatch.setenv("USERPROFILE", str(profile))
    plan = TOOL.scan(MetadataStripOptions(roots=[profile]), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("user profile folder" in n for n in plan.notes)


def test_cloud_only_files_are_skipped_and_never_read(
    tmp_path: Path, exiftool_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _picture(tmp_path / "pics" / "a.jpg")
    monkeypatch.setattr(strip_tool, "_cloud_only", lambda info: True)

    def must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cloud-only file was read")

    monkeypatch.setattr(strip_tool, "read_tags", must_not_read)
    plan = TOOL.scan(MetadataStripOptions(roots=[tmp_path / "pics"]), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("skipped: cloud-only, not read" in n for n in plan.notes)
    assert plan.cloud_only == [tmp_path / "pics" / "a.jpg"]  # for the download action, once


def test_a_onedrive_placeholder_that_shows_the_reparse_bit_is_not_a_link(
    tmp_path: Path, exiftool_path: Path, cloud_placeholder: Callable[..., None]
) -> None:
    _picture(tmp_path / "pics" / "a.jpg")
    _picture(tmp_path / "pics" / "sub" / "b.jpg")
    cloud_placeholder("a.jpg", "sub")  # a placeholder file and a placeholder folder
    plan = TOOL.scan(
        MetadataStripOptions(roots=[tmp_path / "pics"], recursive=True), _ctx(tmp_path, None)
    )
    assert sorted(a.src.name for a in plan.selected_actions() if a.src) == ["a.jpg", "b.jpg"]
    assert plan.cloud_only == []


def test_apply_never_reads_a_file_that_became_cloud_only_after_scan(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    no_trash: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MetadataStripOptions(roots=[src.parent]), ctx)
    assert len(plan.selected_actions()) == 1
    real_attributes = onedrive.get_attributes

    def attributes(path: Path) -> int | None:
        if path.name == "a.jpg":
            return onedrive.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        return real_attributes(path)

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("ExifTool was run on a cloud-only file")

    monkeypatch.setattr(onedrive, "get_attributes", attributes)
    monkeypatch.setattr(strip_tool, "run_exiftool", must_not_run)
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0
    assert result.count("failed") == 1
    assert "cloud-only" in result.outcomes[0].detail
    assert not (src.parent / "stripped").exists()


def _make_junction(link: Path, target: Path) -> bool:
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    return made.returncode == 0


def test_a_junction_inside_the_folder_is_not_followed(tmp_path: Path, exiftool_path: Path) -> None:
    root = tmp_path / "pics"
    root.mkdir()
    outside = tmp_path / "outside"
    _picture(outside / "secret.jpg")
    if not _make_junction(root / "link", outside):
        pytest.skip("could not create a junction in this environment")
    try:
        plan = TOOL.scan(MetadataStripOptions(roots=[root], recursive=True), _ctx(tmp_path, None))
        assert plan.actions == []
    finally:
        os.rmdir(root / "link")  # removes the junction only, never its target


def test_output_through_a_junction_is_refused(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    folder = tmp_path / "pics"
    _picture(folder / "sub" / "a.jpg")
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MetadataStripOptions(roots=[folder], recursive=True), ctx)
    assert len(plan.selected_actions()) == 1
    # After the scan, the output folder is swapped for a junction that points outside the root.
    if not _make_junction(folder / "stripped", outside):
        pytest.skip("could not create a junction in this environment")
    try:
        result = TOOL.apply(plan, ctx)
        # The engine's guard refuses it before the handler runs, and journals it as skipped.
        assert result.count("done") == 0 and result.count("skipped") == 1
        assert "junction or symlink" in result.outcomes[0].detail
        assert list(outside.rglob("*")) == []
    finally:
        os.rmdir(folder / "stripped")


def test_a_plan_cannot_reach_outside_the_selected_folders(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    elsewhere = _picture(tmp_path / "elsewhere" / "b.jpg")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MetadataStripOptions(roots=[src.parent]), ctx)
    plan.actions[0].src = elsewhere
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert "outside the selected folders" in result.outcomes[0].detail
    assert not (tmp_path / "pics" / "stripped").exists()


def test_undo_recycles_an_unchanged_output_and_lists_a_changed_one(
    tmp_path: Path, journal: Journal, exiftool_path: Path, fake_trash: FakeBin
) -> None:
    first = _picture(tmp_path / "pics" / "a.jpg")
    _picture(tmp_path / "pics" / "b.jpg")
    _plan, result = _run(tmp_path, journal, MetadataStripOptions(roots=[first.parent]))
    assert result.count("done") == 2
    untouched = first.parent / "stripped" / "a.jpg"
    edited = first.parent / "stripped" / "b.jpg"
    edited.write_bytes(b"the user changed this output")

    report = undo_run(journal, result.run_id, send_to_trash=fake_trash.trash)
    assert report.reverted == 1
    assert not untouched.exists() and fake_trash == [untouched]
    assert edited.read_bytes() == b"the user changed this output"
    assert any("changed after the run" in line for line in report.skipped)
    assert first.exists()  # originals are never touched


# ----- ExifTool missing, timeouts ------------------------------------------------------------


def _deps_without_exiftool(monkeypatch: pytest.MonkeyPatch) -> ExternalTools:
    deps = ExternalTools({"exiftool": r"C:\no\such\exiftool.exe"}, ollama_probe=lambda _u: None)
    monkeypatch.setattr(deps, "exiftool", lambda: None)
    return deps


def test_without_exiftool_nothing_is_planned_and_the_note_says_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _picture(tmp_path / "pics" / "a.jpg")
    deps = _deps_without_exiftool(monkeypatch)
    plan = TOOL.scan(MetadataStripOptions(roots=[tmp_path / "pics"]), _ctx(tmp_path, None, deps))
    assert plan.actions == []
    assert any("winget install OliverBetz.ExifTool" in n for n in plan.notes)
    assert any("Nothing was changed" in n for n in plan.notes)


def test_apply_without_exiftool_fails_the_action_and_changes_nothing(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    no_trash: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    plan = TOOL.scan(MetadataStripOptions(roots=[src.parent]), _ctx(tmp_path, None))
    deps = _deps_without_exiftool(monkeypatch)
    result = TOOL.apply(plan, _ctx(tmp_path, journal, deps))
    assert result.count("failed") == 1
    assert "ExifTool is needed" in result.outcomes[0].detail
    assert not (src.parent / "stripped").exists()


def test_timeouts_come_from_the_settings(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    no_trash: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    seen: list[float] = []
    real = et.run_exiftool

    def spy(exiftool: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        seen.append(timeout)
        return real(exiftool, args, timeout)

    monkeypatch.setattr(strip_tool, "run_exiftool", spy)
    monkeypatch.setattr(et, "run_exiftool", spy)
    ctx = _ctx(tmp_path, journal, exiftool_read_timeout_s=11, exiftool_write_timeout_s=77)
    plan = TOOL.scan(MetadataStripOptions(roots=[src.parent]), ctx)
    TOOL.apply(plan, ctx)
    # scan: one read (11). apply: the write (77), then the read-back (11).
    assert seen == [11, 77, 11]


def test_an_exiftool_timeout_fails_the_action_and_leaves_no_file(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    no_trash: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MetadataStripOptions(roots=[src.parent]), ctx)

    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("exiftool", 1)

    monkeypatch.setattr(strip_tool, "run_exiftool", hang)
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1 and "timed out" in result.outcomes[0].detail
    assert list((src.parent / "stripped").iterdir()) == []


# ----- in place ------------------------------------------------------------------------------


def test_in_place_checks_the_copy_recycles_the_original_then_renames(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    fake_trash: FakeBin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    original = src.read_bytes()
    at_trash_time: dict[str, Any] = {}
    real_fake = plan_module.send2trash

    def watching(path: str | os.PathLike[str]) -> None:
        staging = [p for p in src.parent.iterdir() if p.name.endswith(".tmp")]
        at_trash_time["staging"] = staging
        at_trash_time["clean"] = _gps_left(_read(exiftool_path, staging[0])) == []
        real_fake(path)

    monkeypatch.setattr(plan_module, "send2trash", watching)
    opts = MetadataStripOptions(roots=[src.parent], in_place=True)
    plan, result = _run(tmp_path, journal, opts)
    assert "Recycle Bin" in plan.actions[0].reason
    assert plan.actions[0].src == plan.actions[0].dst == src
    assert result.count("done") == 1, [o.detail for o in result.outcomes]
    # The copy was complete and clean before the original went away.
    assert len(at_trash_time["staging"]) == 1 and at_trash_time["clean"] is True
    assert fake_trash == [src]
    assert [item.read_bytes() for item in fake_trash.items()] == [original]
    assert _gps_left(_read(exiftool_path, src)) == []
    assert _read(exiftool_path, src)["IFD0:Make"] == "TestMake"
    assert list(src.parent.iterdir()) == [src]  # no stripped/ folder, no temp file
    assert "Recycle Bin" in plan.notes[0]
    # Rule 5: recycling the original is its own journal row, in the same step as the strip.
    rows = journal.actions(result.run_id)
    assert [(r.kind, r.status, r.seq) for r in rows] == [
        ("convert", "done", 1),
        ("trash", "done", 1),
    ]
    assert rows[1].src == str(src)


def test_undoing_an_in_place_strip_brings_the_original_back_from_the_recycle_bin(
    tmp_path: Path, journal: Journal, exiftool_path: Path, fake_trash: FakeBin
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    original = src.read_bytes()
    opts = MetadataStripOptions(roots=[src.parent], in_place=True)
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1 and src.read_bytes() != original

    report = undo_run(
        journal, result.run_id, send_to_trash=fake_trash.trash, locate_bin=fake_trash.locate
    )
    assert report.reverted == 2, (report.skipped, report.failed)
    assert src.read_bytes() == original
    clean_copies = [item.read_bytes() for item in fake_trash.items()]
    assert len(clean_copies) == 1 and clean_copies[0] != original  # the clean copy was recycled
    assert [a.status for a in journal.actions(result.run_id)] == ["undone", "undone"]


def test_in_place_with_a_copy_that_fails_its_check_leaves_the_original_alone(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    fake_trash: FakeBin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    original = src.read_bytes()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MetadataStripOptions(roots=[src.parent], in_place=True), ctx)
    monkeypatch.setattr(strip_tool, "strip_args", lambda *a, **k: [])  # removes nothing
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert "metadata is still in the clean copy" in result.outcomes[0].detail
    assert fake_trash == []
    assert src.read_bytes() == original
    assert list(src.parent.iterdir()) == [src]


def test_in_place_when_the_recycle_bin_refuses_keeps_the_original(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    original = src.read_bytes()

    def refuse(path: object) -> None:
        raise OSError("the Recycle Bin is not available")

    monkeypatch.setattr(plan_module, "send2trash", refuse)
    _plan, result = _run(tmp_path, journal, MetadataStripOptions(roots=[src.parent], in_place=True))
    assert result.count("failed") == 1
    assert [(r.kind, r.status) for r in journal.actions(result.run_id)] == [
        ("convert", "failed"),
        ("trash", "failed"),
    ]
    assert src.read_bytes() == original
    assert list(src.parent.iterdir()) == [src]


def test_in_place_when_the_final_rename_fails_the_copy_is_kept_and_reported(
    tmp_path: Path,
    journal: Journal,
    exiftool_path: Path,
    fake_trash: FakeBin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MetadataStripOptions(roots=[src.parent], in_place=True), ctx)

    def broken(staging: Path, destination: Path) -> None:
        raise PermissionError("locked by another program")

    monkeypatch.setattr(strip_tool, "rename_no_replace", broken)
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    detail = result.outcomes[0].detail
    assert "original is in the Recycle Bin" in detail and "clean copy is at" in detail
    kept = [p for p in src.parent.iterdir() if p.name.endswith(".tmp")]
    assert len(kept) == 1  # the only clean copy is not deleted
    assert _gps_left(_read(exiftool_path, kept[0])) == []


# ----- videos --------------------------------------------------------------------------------


def _video(ffmpeg: Path, path: Path, *args: str) -> Path:
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
            "-t",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            *args,
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return path


def test_video_gps_is_removed_without_touching_the_video(
    tmp_path: Path,
    journal: Journal,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    exiftool_path: Path,
    no_trash: None,
) -> None:
    folder = tmp_path / "clips"
    _video(
        ffmpeg_path,
        folder / "a.mov",
        "-movflags",
        "use_metadata_tags",
        "-metadata",
        "title=Holiday",
        "-metadata",
        "com.apple.quicktime.location.ISO6709=+37.7749-122.4194+010.000/",
    )
    before = _read(exiftool_path, folder / "a.mov")
    assert _gps_left(before) != []
    opts = MetadataStripOptions(roots=[folder], media="videos")
    plan, result = _run(tmp_path, journal, opts)
    assert plan.actions[0].reason.startswith("remove GPS: position")
    assert result.count("done") == 1, [o.detail for o in result.outcomes]
    out = _read(exiftool_path, folder / "stripped" / "a.mov")
    assert _gps_left(out) == []
    assert out["Keys:Title"] == "Holiday"
    before_duration = ffmpeg_media.probe_duration(ffprobe_path, folder / "a.mov")
    after_duration = ffmpeg_media.probe_duration(ffprobe_path, folder / "stripped" / "a.mov")
    assert abs(before_duration - after_duration) < 0.05


def test_video_location_from_an_mp4_is_found_and_removed(
    tmp_path: Path, journal: Journal, ffmpeg_path: Path, exiftool_path: Path, no_trash: None
) -> None:
    folder = tmp_path / "clips"
    _video(ffmpeg_path, folder / "b.mp4", "-metadata", "location=+37.7749-122.4194/")
    opts = MetadataStripOptions(roots=[folder], media="videos")
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, [o.detail for o in result.outcomes]
    assert _gps_left(_read(exiftool_path, folder / "stripped" / "b.mp4")) == []


def test_video_all_metadata_removes_title_and_keeps_the_video_playable(
    tmp_path: Path,
    journal: Journal,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    exiftool_path: Path,
    no_trash: None,
) -> None:
    folder = tmp_path / "clips"
    _video(ffmpeg_path, folder / "c.mp4", "-metadata", "title=Secret", "-metadata", "artist=Me")
    opts = MetadataStripOptions(roots=[folder], media="videos", scope="all")
    _plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1, [o.detail for o in result.outcomes]
    out = folder / "stripped" / "c.mp4"
    assert et.removable_tags(_read(exiftool_path, out), "all") == []
    assert ffmpeg_media.probe_duration(ffprobe_path, out) > 0.5


def test_a_video_whose_length_changes_is_rejected(
    tmp_path: Path,
    journal: Journal,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    exiftool_path: Path,
    no_trash: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "clips"
    _video(ffmpeg_path, folder / "d.mp4", "-metadata", "location=+37.7749-122.4194/")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MetadataStripOptions(roots=[folder], media="videos"), ctx)
    calls: list[Path] = []

    def probe(ffprobe: Path, path: Path, *, timeout: float = 30) -> float:
        calls.append(path)
        return 1.0 if len(calls) == 1 else 5.0

    monkeypatch.setattr(strip_tool.ffmpeg_media, "probe_duration", probe)
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert "is 5.00 s long" in result.outcomes[0].detail
    assert list((folder / "stripped").iterdir()) == []


# ----- plan round trip -----------------------------------------------------------------------


def test_saved_plan_can_be_applied_later(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.jpg")
    ctx = _ctx(tmp_path, journal)
    saved = TOOL.scan(MetadataStripOptions(roots=[src.parent]), ctx).to_json()
    assert json.loads(saved)["tool"] == "metadata_strip"
    result = TOOL.apply(Plan.from_json(saved), ctx)
    assert result.count("done") == 1


def test_open_image_reads_the_stripped_copy(
    tmp_path: Path, journal: Journal, exiftool_path: Path, no_trash: None
) -> None:
    src = _picture(tmp_path / "pics" / "a.heic", "HEIF")
    _plan, result = _run(tmp_path, journal, MetadataStripOptions(roots=[src.parent]))
    assert result.count("done") == 1
    with open_image(src) as before, open_image(src.parent / "stripped" / "a.heic") as after:
        assert after.size == before.size


def test_a_junction_as_the_selected_folder_is_refused(tmp_path: Path, exiftool_path: Path) -> None:
    real = tmp_path / "real"
    _picture(real / "a.jpg")
    link = tmp_path / "linkroot"
    if not _make_junction(link, real):
        pytest.skip("could not create a junction in this environment")
    try:
        plan = TOOL.scan(MetadataStripOptions(roots=[link], recursive=True), _ctx(tmp_path, None))
        assert plan.actions == [] and plan.roots == []
    finally:
        os.rmdir(link)  # removes the junction only, never its target
    assert [p.name for p in real.iterdir()] == ["a.jpg"]
