"""Media resize (plan §3.5): parity with ``legacy/app.py``, the size rules, and the safety rules.

Parity: the legacy Streamlit script is run as it is, with only the Streamlit and pandas modules
replaced by stubs (they are not project dependencies). The stub returns the same answers that the
tool's options carry, so both programs see the same folder. The two output folders are then
compared file by file, by name and by size. Trees are generated under ``tmp_path`` only.
"""

from __future__ import annotations

import dataclasses
import gc
import os
import random
import runpy
import subprocess
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.fs import rename_no_replace as fs_rename_no_replace
from various_tools.core.journal import Journal, undo_run
from various_tools.core.media import ffmpeg as ffmpeg_media
from various_tools.core.plan import ActionKind, Plan, RunResult
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.media_resize import TOOL, MediaResizeOptions
from various_tools.tools.media_resize import geometry as g
from various_tools.tools.media_resize import tool as resize_tool

REPO = Path(__file__).resolve().parents[3]
LEGACY = REPO / "tests" / "fixtures" / "legacy_reference" / "app.py"
ORIENTATION = 0x0112


# ----- helpers -------------------------------------------------------------------------------


class _Stub:
    """Stands in for any Streamlit, pandas or tkinter object. Calls and attributes return stubs."""

    def __getattr__(self, name: str) -> _Stub:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Stub()

    def __call__(self, *args: object, **kwargs: object) -> _Stub:
        return _Stub()

    def __enter__(self) -> _Stub:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def __iter__(self) -> Iterator[object]:
        return iter(())


class _FakeStreamlit(_Stub):
    """Answers the legacy script's widgets from ``answers``, keyed by the widget's label."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.session_state: dict[str, object] = {}

    def text_input(self, label: str, value: object = "", **kwargs: object) -> object:
        return self.answers.get(label, value)

    def radio(self, label: str, options: list[str], index: int = 0, **kwargs: object) -> object:
        return self.answers.get(label, options[index])

    def selectbox(self, label: str, options: list[str], index: int = 0, **kwargs: object) -> object:
        return self.answers.get(label, options[index])

    def checkbox(self, label: str, value: bool = False, **kwargs: object) -> object:
        return self.answers.get(label, value)

    def slider(self, label: str, *args: Any, **kwargs: Any) -> object:
        default = args[2] if len(args) > 2 else kwargs.get("value")
        return self.answers.get(label, default)

    def number_input(self, label: str, **kwargs: Any) -> object:
        return self.answers.get(label, kwargs.get("value"))

    def columns(self, spec: object, **kwargs: object) -> list[_Stub]:
        count = spec if isinstance(spec, int) else len(spec)  # type: ignore[arg-type]
        return [_Stub() for _ in range(count)]

    def button(self, label: str, **kwargs: object) -> bool:
        return label == "Process Files"


def _run_legacy(folder: Path, answers: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``legacy/app.py`` once, as the Streamlit page would, with the answers given."""
    monkeypatch.setitem(
        sys.modules, "streamlit", _FakeStreamlit({"Folder path:": str(folder), **answers})
    )
    monkeypatch.setitem(sys.modules, "pandas", _Stub())
    # The legacy script opens every image and never closes it. That is its code, which is not
    # changed here, so its ResourceWarning is ignored for this run only, and the handles are
    # released inside the block.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        runpy.run_path(str(LEGACY), run_name="legacy_app_under_test")
        gc.collect()


def _image(
    path: Path, size: tuple[int, int], fmt: str | None = None, exif: bytes | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    colour = (size[0] % 256, size[1] % 256, 120)
    image = Image.new("RGB", size, colour)
    if exif is not None:
        image.save(path, format=fmt, exif=exif)
    else:
        image.save(path, format=fmt)
    return path


def _orientation_exif(value: int) -> bytes:
    exif = Image.Exif()
    exif[ORIENTATION] = value
    return bytes(exif.tobytes())


# (file name, stored size, format, exif bytes) for the image parity tree.
IMAGE_TREE: list[tuple[str, tuple[int, int], str, bytes | None]] = [
    ("wide_2560x1440.jpg", (2560, 1440), "JPEG", None),
    ("big_4000x3000.png", (4000, 3000), "PNG", None),
    ("small_800x600.jpg", (800, 600), "JPEG", None),
    ("tall_1080x2400.jpg", (1080, 2400), "JPEG", None),
    ("edge_1920x2000.jpg", (1920, 2000), "JPEG", None),
    ("rotated_2400x3000_o6.jpg", (2400, 3000), "JPEG", _orientation_exif(6)),
    ("odd_2001x1601.jpg", (2001, 1601), "JPEG", None),
    ("bmp_2000x1500.bmp", (2000, 1500), "BMP", None),
    ("web_2200x1400.webp", (2200, 1400), "WEBP", None),
    ("gif_2100x1200.gif", (2100, 1200), "GIF", None),
]


def _make_image_tree(root: Path) -> Path:
    for name, size, fmt, exif in IMAGE_TREE:
        _image(root / name, size, fmt, exif)
    (root / "notes.txt").write_text("not an image", encoding="utf-8")
    return root


def _output_sizes(folder: Path) -> dict[str, tuple[int, int]]:
    sizes: dict[str, tuple[int, int]] = {}
    for path in sorted((folder / "resized").iterdir()):
        if path.is_file():
            with Image.open(path) as image:
                sizes[path.name] = image.size
    return sizes


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def _ctx(tmp_path: Path, journal: Journal | None, deps: ExternalTools | None = None) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}),
        deps=deps or ExternalTools(ollama_probe=lambda _u: None),
        journal=journal,
    )


def _run(
    tmp_path: Path, journal: Journal, opts: MediaResizeOptions, deps: ExternalTools | None = None
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


# ----- size rules (pure) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "box", "expected"),
    [
        ((2560, 1440), (1920, 1080), (1920, 1080)),
        ((4000, 3000), (1920, 1080), (1440, 1080)),
        ((2560, 800), (1920, 1080), g.ONE_SIDE_ONLY),  # app.py's own example: 800 < 1080
        ((1920, 2000), (1920, 1080), g.ONE_SIDE_ONLY),
        ((800, 600), (1920, 1080), g.ALREADY_SMALLER),
        ((1920, 1080), (1920, 1080), g.ALREADY_SMALLER),
    ],
)
def test_fit_box_follows_app_py(
    size: tuple[int, int], box: tuple[int, int], expected: object
) -> None:
    result = g.fit_box(size[0], size[1], box[0], box[1])
    if isinstance(expected, str):
        assert result == expected
    else:
        assert isinstance(expected, tuple)
        assert result == g.Target(expected[0], expected[1])


def test_shorter_and_longer_side_follow_metadata_8() -> None:
    assert g.shorter_side(4032, 3024, 1080) == g.Target(1440, 1080)
    assert g.shorter_side(800, 600, 1080) == g.ALREADY_SMALLER
    assert g.longer_side(1000, 4000, 1920) == g.Target(480, 1920)
    assert g.longer_side(1000, 800, 1920) == g.ALREADY_SMALLER


def test_percent_truncates_and_skips_no_change() -> None:
    assert g.percent(1001, 501, 50) == g.Target(500, 250)
    assert g.percent(100, 100, 100) == g.ALREADY_AT_TARGET


def test_plan_target_never_enlarges_and_reports_a_missing_box() -> None:
    assert g.plan_target("shorter_side", 800, 600, box=None, target_px=1200, amount=50) == (
        g.ALREADY_SMALLER
    )
    assert g.plan_target("fit_box", 800, 600, box=None, target_px=1, amount=50) == (
        "no size box is set"
    )


def test_video_sizes_are_even_and_never_enlarged() -> None:
    assert g.even(1921) == 1920
    assert g.even(1) == 2
    assert resize_tool._video_target(
        MediaResizeOptions(roots=[], size="720p"), 1921, 1081
    ) == g.Target(1278, 720)  # 1921 x 720/1081 = 1279.3 -> 1279, made even -> 1278


def test_defaults_match_app_py_and_the_plan() -> None:
    opts = MediaResizeOptions(roots=[])
    assert opts.size == "1080p"  # app.py's default preset (index 3)
    assert opts.jpeg_quality == 85
    assert opts.video_quality == "high"  # CRF 18
    assert opts.output == "new_folder"
    assert opts.output_folder_name is None  # use the default_output_folder setting ("resized")
    assert opts.never_upscale is True
    assert g.PRESET_BOXES["720p"] == (1280, 720)
    assert g.CRF_BY_QUALITY == {"high": 18, "medium": 23, "low": 28}


def test_unknown_option_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MediaResizeOptions(roots=[], overwrite=True)  # type: ignore[call-arg]


def test_output_choices_are_the_plans() -> None:
    assert MediaResizeOptions(roots=[], output="overwrite").output == "overwrite"
    with pytest.raises(ValidationError):
        MediaResizeOptions(roots=[], output="replace")  # type: ignore[arg-type]


def test_tool_is_discovered_through_the_registry() -> None:
    assert discover()["media_resize"] is not None


# ----- parity with legacy/app.py -------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "answers", "opts"),
    [
        (
            "preset 1080p",
            {
                "Process:": "Images Only",
                "Resize method:": "Resolution Preset",
                "Select resolution preset:": "1080p Full HD (1920×1080)",
                "Output location:": "Save to new folder",
                "Output folder name:": "resized",
                "JPEG quality (1-100):": 85,
            },
            {"size": "1080p"},
        ),
        (
            "custom 1280x720",
            {
                "Process:": "Images Only",
                "Resize method:": "Custom dimensions",
                "Max width (pixels):": 1280,
                "Max height (pixels):": 720,
                "Output location:": "Save to new folder",
                "Output folder name:": "resized",
                "JPEG quality (1-100):": 85,
            },
            {"size": "custom", "max_width": 1280, "max_height": 720},
        ),
        (
            "percentage 50",
            {
                "Process:": "Images Only",
                "Resize method:": "Percentage",
                "Scale percentage:": 50,
                "Output location:": "Save to new folder",
                "Output folder name:": "resized",
                "JPEG quality (1-100):": 85,
            },
            {"mode": "percent", "percent": 50},
        ),
    ],
)
def test_images_match_legacy_app_py_file_by_file(
    tmp_path: Path,
    journal: Journal,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    answers: dict[str, Any],
    opts: dict[str, Any],
) -> None:
    legacy_folder = _make_image_tree(tmp_path / "legacy")
    our_folder = _make_image_tree(tmp_path / "ours")
    _run_legacy(legacy_folder, answers, monkeypatch)

    plan, result = _run(
        tmp_path,
        journal,
        MediaResizeOptions(roots=[our_folder], media="images", **opts),
    )
    assert result.status == "done", label
    assert result.count("failed") == 0
    legacy_sizes = _output_sizes(legacy_folder)
    assert legacy_sizes, label
    assert _output_sizes(our_folder) == legacy_sizes, label


def test_videos_match_legacy_app_py_file_by_file(
    tmp_path: Path,
    journal: Journal,
    monkeypatch: pytest.MonkeyPatch,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    def make(root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        for name, size in (
            ("wide.mp4", "1920x1080"),
            ("clip.mov", "1600x900"),
            ("tall.mp4", "720x1280"),
        ):
            subprocess.run(
                [
                    str(ffmpeg_path),
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"testsrc=size={size}:rate=10",
                    "-t",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:v",
                    "mpeg4",
                    str(root / name),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        return root

    legacy_folder = make(tmp_path / "legacy")
    our_folder = make(tmp_path / "ours")
    # The legacy script calls ffmpeg and ffprobe by name, so their folder must be on PATH.
    monkeypatch.setenv("PATH", str(ffmpeg_path.parent) + os.pathsep + os.environ["PATH"])
    _run_legacy(
        legacy_folder,
        {
            "Process:": "Videos Only",
            "Resize method:": "Resolution Preset",
            "Select resolution preset:": "720p HD (1280×720)",
            "Output location:": "Save to new folder",
            "Output folder name:": "resized",
            "Video quality preset:": "High (CRF 18)",
        },
        monkeypatch,
    )

    _plan, result = _run(
        tmp_path,
        journal,
        MediaResizeOptions(roots=[our_folder], media="videos", size="720p", video_quality="high"),
    )
    assert result.count("failed") == 0
    legacy_sizes = {
        p.name: ffmpeg_media.display_size(ffprobe_path, p)
        for p in sorted((legacy_folder / "resized").iterdir())
    }
    our_sizes = {
        p.name: ffmpeg_media.display_size(ffprobe_path, p)
        for p in sorted((our_folder / "resized").iterdir())
    }
    assert legacy_sizes == {"wide.mp4": (1280, 720), "clip.mov": (1280, 720)}
    assert our_sizes == legacy_sizes


# ----- behaviour and safety ------------------------------------------------------------------


def test_scan_changes_nothing_on_disk(tmp_path: Path) -> None:
    folder = _make_image_tree(tmp_path / "pics")
    before = _snapshot(folder)
    plan = TOOL.scan(MediaResizeOptions(roots=[folder]), _ctx(tmp_path, None))
    assert any(a.selected for a in plan.actions)
    assert _snapshot(folder) == before
    assert not (folder / "resized").exists()


def test_apply_writes_outputs_and_leaves_originals_alone(tmp_path: Path, journal: Journal) -> None:
    folder = _make_image_tree(tmp_path / "pics")
    originals = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    plan, result = _run(tmp_path, journal, MediaResizeOptions(roots=[folder]))
    assert result.status == "done"
    assert result.count("done") == len(plan.selected_actions()) > 0
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == originals
    kinds = {a.kind for a in journal.actions(result.run_id)}
    assert kinds == {"convert"}


def test_output_keeps_the_source_modified_time(tmp_path: Path, journal: Journal) -> None:
    folder = _make_image_tree(tmp_path / "pics")
    os.utime(folder / "wide_2560x1440.jpg", (1_700_000_000, 1_700_000_000))
    _run(tmp_path, journal, MediaResizeOptions(roots=[folder]))
    out = folder / "resized" / "wide_2560x1440.jpg"
    assert int(out.stat().st_mtime) == 1_700_000_000


def test_apply_then_undo_recycles_the_unchanged_outputs_and_keeps_an_edited_one(
    tmp_path: Path, journal: Journal
) -> None:
    """Undo sends each output that is unchanged since the run to the Recycle Bin. An output the
    user changed afterwards is listed and kept. The originals are never touched."""
    folder = _make_image_tree(tmp_path / "pics")
    originals = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    plan, result = _run(tmp_path, journal, MediaResizeOptions(roots=[folder]))
    edited = folder / "resized" / "wide_2560x1440.jpg"
    edited.write_bytes(b"the user changed this")
    recycled: list[Path] = []

    def fake_trash(path: Path) -> None:
        recycled.append(path)
        path.unlink()

    report = undo_run(journal, result.run_id, send_to_trash=fake_trash)
    assert report.reverted == len(plan.selected_actions()) - 1
    assert any("changed" in line for line in report.skipped)
    assert edited.exists() and edited not in recycled
    assert [p.name for p in (folder / "resized").iterdir()] == ["wide_2560x1440.jpg"]
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == originals


def test_small_files_are_never_enlarged(tmp_path: Path, journal: Journal) -> None:
    folder = _image(tmp_path / "pics" / "small.jpg", (800, 600))
    plan, result = _run(tmp_path, journal, MediaResizeOptions(roots=[folder.parent]))
    assert plan.actions == []
    assert result.outcomes == []
    assert any("already smaller" in n for n in plan.notes)
    assert not (folder.parent / "resized").exists()


@pytest.mark.parametrize(
    ("opts", "expected"),
    [
        ({"mode": "shorter_side", "target_px": 1080}, "4032x3024 -> 1440x1080"),
        ({"mode": "longer_side", "target_px": 1512}, "4032x3024 -> 1512x1134"),
    ],
)
def test_side_modes_resize_the_named_side(
    tmp_path: Path, journal: Journal, opts: dict[str, Any], expected: str
) -> None:
    folder = _image(tmp_path / "pics" / "big.jpg", (4032, 3024))
    plan, result = _run(tmp_path, journal, MediaResizeOptions(roots=[folder.parent], **opts))
    assert result.count("done") == 1
    assert expected in plan.actions[0].reason
    with Image.open(folder.parent / "resized" / "big.jpg") as out:
        assert out.size == tuple(int(part) for part in expected.split(" -> ")[1].split("x"))


def test_percent_100_changes_nothing(tmp_path: Path, journal: Journal) -> None:
    folder = _image(tmp_path / "pics" / "a.jpg", (2000, 1000))
    plan, _result = _run(
        tmp_path, journal, MediaResizeOptions(roots=[folder.parent], mode="percent", percent=100)
    )
    assert plan.actions == []


def test_existing_output_is_never_overwritten(tmp_path: Path, journal: Journal) -> None:
    folder = _image(tmp_path / "pics" / "wide.jpg", (2560, 1440))
    out = folder.parent / "resized" / "wide.jpg"
    out.parent.mkdir()
    out.write_bytes(b"keep me")

    plan = TOOL.scan(MediaResizeOptions(roots=[folder.parent]), _ctx(tmp_path, journal))
    assert [a.selected for a in plan.actions] == [False]
    assert "the output file already exists" in plan.actions[0].reason

    # Even a plan edited to select the row must not overwrite the file.
    plan.actions[0].selected = True
    result = TOOL.apply(plan, _ctx(tmp_path, journal))
    assert result.count("failed") == 1
    assert out.read_bytes() == b"keep me"


def test_cloud_only_files_are_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    monkeypatch.setattr(resize_tool, "_cloud_only", lambda info: True)

    def must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cloud-only file was read")

    monkeypatch.setattr(resize_tool.images, "render", must_not_read)
    plan = TOOL.scan(MediaResizeOptions(roots=[folder.parent]), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("cloud-only, not read" in n for n in plan.notes)


def test_suffix_output_sits_beside_the_original_and_is_not_rescanned(
    tmp_path: Path, journal: Journal
) -> None:
    folder = _image(tmp_path / "pics" / "a.png", (2560, 1440))
    opts = MediaResizeOptions(roots=[folder.parent], output="suffix", mode="percent", percent=50)
    _run(tmp_path, journal, opts)
    assert (folder.parent / "a_resized.png").exists()
    second = TOOL.scan(opts, _ctx(tmp_path, journal))
    assert [a.src.name for a in second.actions if a.src] == ["a.png"]  # the output is not a source
    assert not any(a.selected for a in second.actions)  # ...and the original's output exists


def test_recursive_run_skips_its_own_output_folder(tmp_path: Path, journal: Journal) -> None:
    root = tmp_path / "pics"
    _image(root / "one" / "a.jpg", (2560, 1440))
    opts = MediaResizeOptions(roots=[root], recursive=True)
    _run(tmp_path, journal, opts)
    second = TOOL.scan(opts, _ctx(tmp_path, journal))
    # The output under resized/ is never a source. The original is listed, but its output exists.
    assert [a.src for a in second.actions] == [root / "one" / "a.jpg"]
    assert not any(a.selected for a in second.actions)


def test_protected_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile"
    _image(profile / "a.jpg", (2560, 1440))
    monkeypatch.setenv("USERPROFILE", str(profile))
    plan = TOOL.scan(MediaResizeOptions(roots=[profile]), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("user profile folder" in n for n in plan.notes)


def test_motion_photo_jpeg_is_left_as_it_is(tmp_path: Path, journal: Journal) -> None:
    path = _image(tmp_path / "pics" / "motion.jpg", (2560, 1440))
    with open(path, "ab") as handle:
        handle.write(b"MotionPhoto_Data")
    plan = TOOL.scan(MediaResizeOptions(roots=[path.parent]), _ctx(tmp_path, journal))
    assert plan.actions == []
    assert any("motion photo" in n for n in plan.notes)


def test_metadata_is_kept_and_orientation_is_applied_once(tmp_path: Path, journal: Journal) -> None:
    folder = tmp_path / "pics"
    # Stored 2400x3000 with orientation 6: the viewer shows 3000x2400, so the output is landscape.
    exif = Image.Exif()
    exif[ORIENTATION] = 6
    exif[0x010F] = "TestCamera"  # Make
    _image(folder / "p.jpg", (2400, 3000), fmt="JPEG", exif=exif.tobytes())
    opts = MediaResizeOptions(roots=[folder], size="custom", max_width=1500, max_height=1200)
    _run(tmp_path, journal, opts)
    with Image.open(folder / "resized" / "p.jpg") as out:
        # 3000x2400 as shown (landscape), scaled by 0.5 to fit 1500x1200. Not portrait.
        assert out.size == (1500, 1200)
        kept = out.getexif()
        assert kept.get(0x010F) == "TestCamera"
        assert kept.get(ORIENTATION, 1) == 1


def test_missing_ffmpeg_skips_videos_with_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "clips"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"not really a video")
    deps = ExternalTools(ollama_probe=lambda _u: None)
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    monkeypatch.setattr(deps, "ffprobe", lambda: None)
    plan = TOOL.scan(MediaResizeOptions(roots=[folder], media="videos"), _ctx(tmp_path, None, deps))
    assert plan.actions == []
    assert any("ffmpeg and ffprobe are needed" in n for n in plan.notes)


def test_raw_conversion_needs_exiftool_to_keep_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "raw"
    folder.mkdir()
    (folder / "IMG_1.CR3").write_bytes(b"not a real raw file")
    deps = ExternalTools(ollama_probe=lambda _u: None)
    monkeypatch.setattr(deps, "exiftool", lambda: None)
    plan = TOOL.scan(
        MediaResizeOptions(roots=[folder], convert_raw_to_jpeg=True), _ctx(tmp_path, None, deps)
    )
    assert plan.actions == []
    assert any("needs ExifTool" in n for n in plan.notes)


def test_bad_options_give_a_note_and_no_actions(tmp_path: Path) -> None:
    folder = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    plan = TOOL.scan(MediaResizeOptions(roots=[folder.parent], size="custom"), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("custom size needs" in n for n in plan.notes)
    plan = TOOL.scan(
        MediaResizeOptions(roots=[folder.parent], output_folder_name="a:b"), _ctx(tmp_path, None)
    )
    assert plan.actions == []
    assert any("Windows does not allow" in n for n in plan.notes)


def test_saved_plan_can_be_applied_later(tmp_path: Path, journal: Journal) -> None:
    folder = _make_image_tree(tmp_path / "pics")
    plan = TOOL.scan(MediaResizeOptions(roots=[folder]), _ctx(tmp_path, journal))
    loaded = Plan.from_json(plan.to_json())
    result = TOOL.apply(loaded, _ctx(tmp_path, journal))
    assert result.count("done") == len(plan.selected_actions())
    assert (folder / "resized" / "wide_2560x1440.jpg").exists()


def test_video_is_resized_to_the_size_the_viewer_shows(
    tmp_path: Path, journal: Journal, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    """A phone video stored 1280x720 with a 90 degree rotation is shown at 720x1280."""
    folder = tmp_path / "clips"
    folder.mkdir()
    plain = folder / "plain.mp4"
    subprocess.run(
        [
            str(ffmpeg_path),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1280x720:rate=10",
            "-t",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            str(plain),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    rotated = folder / "phone.mp4"
    subprocess.run(
        [
            str(ffmpeg_path),
            "-v",
            "error",
            "-y",
            "-display_rotation:v:0",
            "90",
            "-i",
            str(plain),
            "-c",
            "copy",
            str(rotated),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    plain.unlink()
    assert ffmpeg_media.display_size(ffprobe_path, rotated) == (720, 1280)

    opts = MediaResizeOptions(
        roots=[folder], media="videos", mode="fit_box", size="custom", max_width=480, max_height=640
    )
    plan, result = _run(tmp_path, journal, opts)
    assert result.count("failed") == 0, [a.reason for a in plan.actions]
    assert ffmpeg_media.display_size(ffprobe_path, folder / "resized" / "phone.mp4") == (360, 640)


def test_plan_reason_says_what_will_change(tmp_path: Path) -> None:
    folder = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    plan = TOOL.scan(MediaResizeOptions(roots=[folder.parent]), _ctx(tmp_path, None))
    reason = plan.actions[0].reason
    assert "2560x1440 -> 1920x1080" in reason
    assert plan.actions[0].kind is ActionKind.CONVERT
    assert any(note.startswith("Resize (fit inside the box)") for note in plan.notes)


def test_module_exposes_the_tool_and_options() -> None:
    assert resize_tool.TOOL is TOOL
    assert TOOL.id == "media_resize"
    assert TOOL.Options is MediaResizeOptions
    assert LEGACY.is_file()


# ----- review fixes ---------------------------------------------------------------------------


def test_apply_never_reads_a_file_that_became_cloud_only_after_scan(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.core import onedrive
    from various_tools.tools.media_resize import images as images_module

    src = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MediaResizeOptions(roots=[src.parent]), ctx)
    assert len(plan.selected_actions()) == 1

    # The file is recalled to the cloud between scan and apply.
    real_attributes = onedrive.get_attributes

    def attributes(path: Path) -> int | None:
        if path.name == "a.jpg":
            return onedrive.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        return real_attributes(path)

    def must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cloud-only file was read")

    monkeypatch.setattr(onedrive, "get_attributes", attributes)
    monkeypatch.setattr(images_module, "open", must_not_read, raising=False)
    monkeypatch.setattr(images_module, "open_image", must_not_read)

    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0
    assert result.count("failed") + result.count("skipped") == 1
    assert not (src.parent / "resized").exists()


def test_has_embedded_extras_refuses_a_cloud_only_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.core import onedrive
    from various_tools.core.onedrive import CloudOnlyFileError
    from various_tools.tools.media_resize import images as images_module

    src = _image(tmp_path / "a.jpg", (200, 100))
    monkeypatch.setattr(onedrive, "get_attributes", lambda _p: 0x1000)
    with pytest.raises(CloudOnlyFileError):
        images_module.has_embedded_extras(src)


def test_output_created_between_scan_and_apply_is_never_replaced(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.tools.media_resize import images as images_module

    src = _image(tmp_path / "pics" / "wide.jpg", (2560, 1440))
    out = src.parent / "resized" / "wide.jpg"
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(MediaResizeOptions(roots=[src.parent]), ctx)
    assert plan.selected_actions()

    # The file appears after the converter's own check, right before the final rename.
    real_rename = images_module.rename_no_replace

    def late_file(tmp: Path, destination: Path) -> None:
        destination.write_bytes(b"keep me")
        real_rename(tmp, destination)

    monkeypatch.setattr(images_module, "rename_no_replace", late_file)
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0
    assert result.count("failed") == 1
    assert out.read_bytes() == b"keep me"
    assert [p.name for p in out.parent.iterdir()] == ["wide.jpg"]  # no temp file left behind


def test_rename_no_replace_refuses_an_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "s.bin"
    target = tmp_path / "t.bin"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        fs_rename_no_replace(source, target)
    assert target.read_bytes() == b"old"
    assert source.read_bytes() == b"new"
    free = tmp_path / "free.bin"
    fs_rename_no_replace(source, free)
    assert free.read_bytes() == b"new"
    assert not source.exists()


def test_output_through_a_junction_is_refused(tmp_path: Path, journal: Journal) -> None:
    folder = tmp_path / "pics"
    _image(folder / "sub" / "wide.jpg", (2560, 1440))
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = _ctx(tmp_path, journal)
    opts = MediaResizeOptions(roots=[folder], recursive=True)
    plan = TOOL.scan(opts, ctx)
    assert len(plan.selected_actions()) == 1

    # After the scan, the output folder is swapped for a junction that points outside the root.
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(folder / "resized"), str(outside)],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction in this environment")
    try:
        result = TOOL.apply(plan, ctx)
        assert result.count("done") == 0
        # The engine's guard refuses it before the converter runs, so it is journaled as skipped.
        assert result.count("skipped") == 1
        assert "junction or symlink" in result.outcomes[0].detail
        assert list(outside.rglob("*")) == []
    finally:
        os.rmdir(folder / "resized")  # removes the junction only, never its target


def test_keep_metadata_keeps_the_icc_profile_and_dpi(tmp_path: Path, journal: Journal) -> None:
    from PIL import ImageCms

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    folder = tmp_path / "pics"
    folder.mkdir()
    Image.new("RGB", (2560, 1440), (10, 20, 30)).save(
        folder / "p.jpg", format="JPEG", icc_profile=profile, dpi=(300, 300)
    )
    _run(tmp_path, journal, MediaResizeOptions(roots=[folder]))
    with Image.open(folder / "resized" / "p.jpg") as out:
        assert out.info.get("icc_profile") == profile
        dpi = out.info.get("dpi")
        assert dpi is not None and round(dpi[0]) == 300

    # With metadata off, the profile is dropped.
    plain = MediaResizeOptions(roots=[folder], output_folder_name="plain", keep_metadata=False)
    _run(tmp_path, journal, plain)
    with Image.open(folder / "plain" / "p.jpg") as out:
        assert out.info.get("icc_profile") is None


def test_summary_counts_only_selected_rows_and_shows_bytes_saved(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    _image(folder / "a.jpg", (2560, 1440))
    _image(folder / "b.jpg", (2560, 1440))
    blocked = folder / "resized" / "b.jpg"
    blocked.parent.mkdir()
    blocked.write_bytes(b"keep me")

    plan = TOOL.scan(MediaResizeOptions(roots=[folder]), _ctx(tmp_path, None))
    assert sorted(a.selected for a in plan.actions) == [False, True]
    summary = plan.notes[0]
    assert "1 file(s) will be resized" in summary
    assert "1 not resized" in summary
    assert "saved" in summary
    # Totals are those of the one selected file only.
    size_a = (folder / "a.jpg").stat().st_size
    assert resize_tool._size_label(size_a) + " ->" in summary


# ----- configurable options (nothing is constant) -----------------------------------------------


def test_new_option_defaults_are_the_old_constants() -> None:
    opts = MediaResizeOptions(roots=[])
    assert opts.suffix_tag == "_resized"
    assert opts.output_folder_name is None  # None means the default_output_folder setting
    assert Settings().default_output_folder == "resized"
    assert opts.video_crf is None and opts.video_preset == "medium"
    assert opts.estimate_compression == 0.8
    assert opts.image_extensions == list(resize_tool.images.IMAGE_FORMATS)
    assert opts.video_extensions == sorted(resize_tool.videos.VIDEO_EXTS)
    assert opts.optimize_output is True and opts.resample == "lanczos"
    assert opts.extras_scan_kb == 256


def test_never_upscale_is_locked_but_can_be_turned_off() -> None:
    info = MediaResizeOptions.model_fields["never_upscale"]
    extra = info.json_schema_extra
    assert isinstance(extra, dict) and extra["locked"] is True and extra["advanced"] is True
    assert (info.description or "").startswith("🔒")
    assert MediaResizeOptions(roots=[]).never_upscale is True
    assert MediaResizeOptions(roots=[], never_upscale=False).never_upscale is False


def test_enlarging_rules_apply_only_when_never_upscale_is_off() -> None:
    # Locked default: nothing grows.
    assert g.plan_target("fit_box", 800, 600, box=(1920, 1080), target_px=1, amount=50) == (
        g.ALREADY_SMALLER
    )
    # Off: a small file is scaled up to fit the box (1.8 times, limited by the height).
    assert g.plan_target(
        "fit_box", 800, 600, box=(1920, 1080), target_px=1, amount=50, never_upscale=False
    ) == g.Target(1440, 1080)
    # Off: the rule "both sides must exceed the box" does not apply, a wide file just fits.
    assert g.fit_box(2560, 800, 1920, 1080, never_upscale=False) == g.Target(1920, 600)
    assert g.fit_box(1920, 1080, 1920, 1080, never_upscale=False) == g.ALREADY_AT_TARGET
    assert g.shorter_side(800, 600, 1080, never_upscale=False) == g.Target(1440, 1080)
    assert g.longer_side(1000, 800, 1920, never_upscale=False) == g.Target(1920, 1536)
    assert g.shorter_side(600, 800, 600, never_upscale=False) == g.ALREADY_AT_TARGET
    # A percentage above 100 needs the lock off.
    assert g.plan_target("percent", 800, 600, box=None, target_px=1, amount=200) == g.WOULD_ENLARGE
    assert g.plan_target(
        "percent", 800, 600, box=None, target_px=1, amount=200, never_upscale=False
    ) == g.Target(1600, 1200)


def test_with_never_upscale_off_a_small_image_is_enlarged(tmp_path: Path, journal: Journal) -> None:
    folder = _image(tmp_path / "pics" / "small.jpg", (800, 600))
    opts = MediaResizeOptions(roots=[folder.parent], never_upscale=False)
    plan, result = _run(tmp_path, journal, opts)
    assert result.count("done") == 1
    with Image.open(folder.parent / "resized" / "small.jpg") as out:
        assert out.size == (1440, 1080)
    assert "800x600 -> 1440x1080" in plan.actions[0].reason


def test_with_never_upscale_off_a_small_video_is_enlarged_to_an_even_size() -> None:
    opts = MediaResizeOptions(roots=[], never_upscale=False, size="1080p")
    assert resize_tool._video_target(opts, 800, 600) == g.Target(1440, 1080)
    assert resize_tool._video_target(MediaResizeOptions(roots=[]), 800, 600) == g.ALREADY_SMALLER


def test_a_custom_suffix_tag_names_the_copies_and_is_not_rescanned(
    tmp_path: Path, journal: Journal
) -> None:
    folder = _image(tmp_path / "pics" / "a.png", (2560, 1440))
    opts = MediaResizeOptions(
        roots=[folder.parent], output="suffix", suffix_tag="_small", mode="percent", percent=50
    )
    _run(tmp_path, journal, opts)
    assert (folder.parent / "a_small.png").exists()
    assert not (folder.parent / "a_resized.png").exists()
    second = TOOL.scan(opts, _ctx(tmp_path, journal))
    assert [a.src.name for a in second.actions if a.src] == ["a.png"]  # a_small.png is an output


@pytest.mark.parametrize("bad", ["", "  ", "a/b", "a:b", 'a"b', "a.", "a "])
def test_suffix_tag_must_be_a_plain_part_of_a_file_name(bad: str) -> None:
    with pytest.raises(ValidationError):
        MediaResizeOptions(roots=[], suffix_tag=bad)


def test_output_folder_name_comes_from_the_setting_unless_the_option_is_set(
    tmp_path: Path, journal: Journal
) -> None:
    folder = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    settings = Settings.model_validate(
        {"onedrive_root": tmp_path, "default_output_folder": "small"}
    )
    ctx = RunContext(
        settings=settings, deps=ExternalTools(ollama_probe=lambda _u: None), journal=journal
    )
    plan = TOOL.scan(MediaResizeOptions(roots=[folder.parent]), ctx)
    assert plan.actions[0].dst == folder.parent / "small" / "a.jpg"
    own = TOOL.scan(MediaResizeOptions(roots=[folder.parent], output_folder_name="mine"), ctx)
    assert own.actions[0].dst == folder.parent / "mine" / "a.jpg"
    # The setting is checked like the option: a bad name is a note, not a write.
    bad = Settings.model_validate({"onedrive_root": tmp_path, "default_output_folder": "a:b"})
    bad_ctx = RunContext(
        settings=bad, deps=ExternalTools(ollama_probe=lambda _u: None), journal=None
    )
    refused = TOOL.scan(MediaResizeOptions(roots=[folder.parent]), bad_ctx)
    assert refused.actions == [] and any("Windows does not allow" in n for n in refused.notes)


def test_the_setting_folder_is_not_rescanned(tmp_path: Path, journal: Journal) -> None:
    root = tmp_path / "pics"
    _image(root / "one" / "a.jpg", (2560, 1440))
    settings = Settings.model_validate(
        {"onedrive_root": tmp_path, "default_output_folder": "small"}
    )
    ctx = RunContext(
        settings=settings, deps=ExternalTools(ollama_probe=lambda _u: None), journal=journal
    )
    opts = MediaResizeOptions(roots=[root], recursive=True)
    TOOL.apply(TOOL.scan(opts, ctx), ctx)
    second = TOOL.scan(opts, ctx)
    assert [a.src for a in second.actions] == [root / "one" / "a.jpg"]
    assert not any(a.selected for a in second.actions)


def test_extension_options_are_checked_against_what_can_be_written() -> None:
    assert MediaResizeOptions(roots=[], image_extensions=["PNG", "jpg"]).image_extensions == [
        ".png",
        ".jpg",
    ]
    for bad in ([".heic"], [".tif"], [".cr3"], ["a/b"], [""]):
        with pytest.raises(ValidationError):
            MediaResizeOptions(roots=[], image_extensions=bad)
    with pytest.raises(ValidationError):
        MediaResizeOptions(roots=[], video_extensions=[".png"])  # also a picture type
    with pytest.raises(ValidationError):
        MediaResizeOptions(roots=[], video_extensions=[".cr3"])
    assert MediaResizeOptions(roots=[], video_extensions=["MP4"]).video_extensions == [".mp4"]


def test_a_custom_picture_type_list_limits_what_is_resized(
    tmp_path: Path, journal: Journal
) -> None:
    folder = tmp_path / "pics"
    _image(folder / "a.jpg", (2560, 1440))
    _image(folder / "b.png", (2560, 1440))
    plan, result = _run(
        tmp_path, journal, MediaResizeOptions(roots=[folder], image_extensions=[".png"])
    )
    assert result.count("done") == 1
    assert (folder / "resized" / "b.png").exists() and not (folder / "resized" / "a.jpg").exists()
    assert [a.src.name for a in plan.actions if a.src] == ["b.png"]


def test_image_options_reach_the_encoder(tmp_path: Path) -> None:
    opts = MediaResizeOptions(
        roots=[], optimize_output=False, resample="nearest", extras_scan_kb=16, never_upscale=False
    )
    rules = resize_tool._rules(opts)
    assert (rules.optimize, rules.resample, rules.extras_scan_bytes) == (False, "nearest", 16384)
    assert rules.never_upscale is False
    noise = Image.frombytes("RGB", (400, 200), random.Random(1).randbytes(400 * 200 * 3))
    path = tmp_path / "noise.png"
    noise.save(path)
    base = resize_tool._rules(MediaResizeOptions(roots=[], mode="percent", percent=50))
    data: dict[str, bytes] = {}
    for resample in ("lanczos", "nearest"):
        result = resize_tool.images.render(
            path, ".png", dataclasses.replace(base, resample=resample), False
        )
        assert not isinstance(result, str)
        data[resample] = result.data
    assert data["lanczos"] != data["nearest"]  # the filter really changes the pixels


def test_motion_photo_marker_beyond_the_scan_size_needs_a_bigger_scan(tmp_path: Path) -> None:
    path = _image(tmp_path / "late.jpg", (200, 100))
    with open(path, "ab") as handle:
        handle.write(b"\0" * 40000)
        handle.write(b"MotionPhoto_Data")
    images_module = resize_tool.images
    assert images_module.has_embedded_extras(path, 16 * 1024) is False
    assert images_module.has_embedded_extras(path, 64 * 1024) is True


class _FakeVideoDeps(ExternalTools):
    def ffmpeg(self) -> Path | None:
        return Path("fake-ffmpeg")

    def ffprobe(self) -> Path | None:
        return Path("fake-ffprobe")


def test_video_options_and_timeouts_reach_ffmpeg(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "clips"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"x" * 1000)
    seen: dict[str, Any] = {"probe_timeouts": []}

    def display_size(_ffprobe: Path, _path: Path, *, timeout: float) -> tuple[int, int]:
        seen["probe_timeouts"].append(timeout)
        return (3840, 2160)

    def transcode(_ffmpeg: Path, _src: Path, dst: Path, args: list[str], *, timeout: float) -> None:
        seen["args"], seen["timeout"] = args, timeout
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"out")

    monkeypatch.setattr(resize_tool.ffmpeg_media, "display_size", display_size)
    monkeypatch.setattr(resize_tool.videos.ffmpeg_media, "transcode", transcode)
    settings = Settings.model_validate(
        {"onedrive_root": tmp_path, "ffprobe_timeout_s": 7, "transcode_timeout_s": 99}
    )
    ctx = RunContext(
        settings=settings, deps=_FakeVideoDeps(ollama_probe=lambda _u: None), journal=journal
    )
    opts = MediaResizeOptions(
        roots=[folder],
        media="videos",
        video_crf=31,
        video_preset="veryfast",
        estimate_compression=0.5,
    )
    plan = TOOL.scan(opts, ctx)
    assert "about" in plan.actions[0].reason and "(estimate)" in plan.actions[0].reason
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1
    args = seen["args"]
    assert args[args.index("-crf") + 1] == "31"  # the override beats video_quality "high" (18)
    assert args[args.index("-preset") + 1] == "veryfast"
    assert seen["timeout"] == 99
    assert seen["probe_timeouts"] == [7, 7]  # once in the scan and once in the apply


def test_crf_follows_the_quality_preset_unless_overridden() -> None:
    assert resize_tool._crf(MediaResizeOptions(roots=[])) == 18
    assert resize_tool._crf(MediaResizeOptions(roots=[], video_quality="medium")) == 23
    assert resize_tool._crf(MediaResizeOptions(roots=[], video_quality="low")) == 28
    assert resize_tool._crf(MediaResizeOptions(roots=[], video_quality="low", video_crf=0)) == 0
    assert resize_tool.videos.ffmpeg_args(g.Target(2, 2), 23, False, ".mp4")[-2:] == [
        "-c:a",
        "copy",
    ]


def test_estimate_factor_changes_only_the_preview() -> None:
    default = resize_tool.videos.estimate_bytes(1000, 100, 100, 50, 50)
    assert default == int(1000 * 0.25 * 0.8)
    assert resize_tool.videos.estimate_bytes(1000, 100, 100, 50, 50, 0.4) == 100


def test_exiftool_timeout_comes_from_the_setting(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    seen: dict[str, Any] = {}
    real_write = resize_tool.images.write

    def write(*args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        real_write(*args, **kwargs)

    monkeypatch.setattr(resize_tool.images, "write", write)
    settings = Settings.model_validate({"onedrive_root": tmp_path, "exiftool_write_timeout_s": 45})
    ctx = RunContext(
        settings=settings, deps=ExternalTools(ollama_probe=lambda _u: None), journal=journal
    )
    plan = TOOL.scan(MediaResizeOptions(roots=[folder.parent]), ctx)
    assert TOOL.apply(plan, ctx).count("done") == 1
    assert seen["exiftool_timeout"] == 45


def test_protected_folders_come_from_the_settings(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    _image(locked / "a.jpg", (2560, 1440))
    settings = Settings.model_validate(
        {"onedrive_root": tmp_path, "protected_folders": [str(locked)]}
    )
    ctx = RunContext(
        settings=settings, deps=ExternalTools(ollama_probe=lambda _u: None), journal=None
    )
    plan = TOOL.scan(MediaResizeOptions(roots=[locked]), ctx)
    assert plan.actions == [] and plan.roots == []
    assert any("rotected" in n for n in plan.notes)
    open_plan = TOOL.scan(MediaResizeOptions(roots=[locked]), _ctx(tmp_path, None))
    assert len(open_plan.actions) == 1  # the default list does not protect this folder


# ----- ExifTool with non-ASCII names ------------------------------------------------------


def test_raw_metadata_copy_works_with_a_non_ascii_name(tmp_path: Path, exiftool_path: Path) -> None:
    """The RAW metadata copy runs ExifTool through a UTF-8 argument file, so any name works.

    A CR3 cannot be generated, so a JPEG with EXIF stands in for the RAW source: the ExifTool
    call is the same.
    """
    from various_tools.tools.media_resize import images as resize_images

    folder = tmp_path / "ünïcode 文件夹"
    folder.mkdir()
    source = folder / "café 写真 🙂.jpg"
    exif_obj = Image.Exif()
    exif_obj[0x0110] = "Test Model"  # Model
    Image.new("RGB", (64, 48)).save(source, format="JPEG", exif=exif_obj)
    target = folder / "ausgabe ñ.jpg"
    Image.new("RGB", (32, 24)).save(target, format="JPEG")

    resize_images._copy_raw_metadata(  # pyright: ignore[reportPrivateUsage]
        exiftool_path, source, target, g.Target(32, 24), 60
    )

    with Image.open(target) as copied:
        assert copied.getexif().get(0x0110) == "Test Model"
