"""Rename (plan §3.3): parity with ``legacy/rename_snapchat.py``, the presets, and the safety rules.

Every tree is generated under ``tmp_path``. The legacy script is run on a copy of the same tree,
with the working folder changed to that copy. Files are identified by their content, which is
their original name, so the two results can be compared file by file.
"""

from __future__ import annotations

import io
import os
import runpy
import subprocess
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.media import exif
from various_tools.core.plan import ActionKind, Plan, RunResult
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.rename import TOOL, RenameOptions
from various_tools.tools.rename import tool as rename_tool
from various_tools.tools.rename.naming import (
    apply_case,
    date_from_filename,
    name_problem,
    template_problem,
)
from various_tools.tools.rename.presets import PRESETS

REPO = Path(__file__).resolve().parents[3]
LEGACY = REPO / "legacy" / "rename_snapchat.py"

# (file name, modified time) in the order the legacy script would meet them, by mtime.
SNAP_FILES: list[tuple[str, tuple[int, int, int, int, int, int]]] = [
    ("Snapchat-1001.jpg", (2024, 1, 1, 12, 34, 5)),
    ("Snapchat-1002.jpg", (2024, 1, 1, 12, 34, 40)),
    ("Snapchat-1003.JPEG", (2024, 1, 1, 12, 35, 0)),
    ("Snapchat-1004.PNG", (2024, 1, 2, 9, 5, 0)),
    ("Snapchat-1005.mp4", (2024, 1, 1, 12, 34, 50)),
    ("Snapchat-1006.mov", (2024, 1, 1, 12, 30, 0)),
    ("Snapchat-1007.mkv", (2024, 1, 1, 12, 34, 10)),
    ("Snapchat-1010.JPG", (2024, 1, 1, 12, 36, 0)),
    # Not renamed by either: wrong extension, wrong pattern, other prefix, not an image or video.
    ("Snapchat-1008.gif", (2024, 1, 1, 12, 34, 20)),
    ("Snapchat-abc.jpg", (2024, 1, 1, 12, 34, 25)),
    ("Other-1009.jpg", (2024, 1, 1, 12, 34, 30)),
    ("notes.txt", (2024, 1, 1, 12, 34, 35)),
]

EXPECTED_SNAP_NAMES: dict[str, str] = {
    "Snapchat-1001.jpg": "Snapchat-20240101-12_34-001.jpg",
    "Snapchat-1007.mkv": "Snapchat-20240101-12_34-002.mkv",
    "Snapchat-1002.jpg": "Snapchat-20240101-12_34-003.jpg",
    "Snapchat-1005.mp4": "Snapchat-20240101-12_34-004.mp4",
    "Snapchat-1006.mov": "Snapchat-20240101-12_30-001.mov",
    "Snapchat-1003.JPEG": "Snapchat-20240101-12_35-001.jpeg",
    "Snapchat-1004.PNG": "Snapchat-20240102-09_05-001.png",
    "Snapchat-1010.JPG": "Snapchat-20240101-12_36-001.jpg",
    "Snapchat-1008.gif": "Snapchat-1008.gif",
    "Snapchat-abc.jpg": "Snapchat-abc.jpg",
    "Other-1009.jpg": "Other-1009.jpg",
    "notes.txt": "notes.txt",
}


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def _ctx(tmp_path: Path, journal: Journal | None) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


def _stamp(when: tuple[int, int, int, int, int, int]) -> float:
    return datetime(*when).timestamp()


def _write(
    path: Path, content: str, when: tuple[int, int, int, int, int, int] | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if when is not None:
        os.utime(path, (_stamp(when), _stamp(when)))
    return path


def _make_snap_tree(root: Path) -> Path:
    for name, when in SNAP_FILES:
        _write(root / name, name, when)
    return root


def _by_content(folder: Path) -> dict[str, str]:
    """Map each file's content (its original name) to the name it has now."""
    return {p.read_text(encoding="utf-8"): p.name for p in folder.iterdir() if p.is_file()}


def _run_legacy(folder: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the legacy script with ``folder`` as its working folder. Its output is returned."""
    monkeypatch.chdir(folder)
    out = io.StringIO()
    with redirect_stdout(out):
        runpy.run_path(str(LEGACY), run_name="__main__")
    return out.getvalue()


def _run_ours(tmp_path: Path, journal: Journal, opts: RenameOptions) -> tuple[Plan, RunResult]:
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(opts, ctx)
    return plan, TOOL.apply(plan, ctx)


# ---- parity with the legacy script -------------------------------------------------------


def test_snapchat_preset_matches_legacy_script_exactly(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_dir = _make_snap_tree(tmp_path / "legacy")
    ours_dir = _make_snap_tree(tmp_path / "ours")

    legacy_output = _run_legacy(legacy_dir, monkeypatch)
    _run_ours(tmp_path, journal, RenameOptions(roots=[ours_dir], preset="snapchat"))

    legacy_names = _by_content(legacy_dir)
    ours_names = _by_content(ours_dir)
    assert ours_names == legacy_names
    assert ours_names == {name: EXPECTED_SNAP_NAMES[name] for name in EXPECTED_SNAP_NAMES}
    assert "Found 8 Snapchat files to rename" in legacy_output


def test_snapchat_preset_numbers_per_minute_in_time_order(tmp_path: Path, journal: Journal) -> None:
    """Four files share 12:34. They are numbered by modified time, not by name."""
    root = _make_snap_tree(tmp_path / "shots")
    plan, _ = _run_ours(tmp_path, journal, RenameOptions(roots=[root], preset="snapchat"))
    renamed = {a.src.name: a.dst.name for a in plan.actions if a.src and a.dst}
    minute = {n: v for n, v in renamed.items() if "12_34" in v}
    assert [
        minute[n]
        for n in (
            "Snapchat-1001.jpg",
            "Snapchat-1007.mkv",
            "Snapchat-1002.jpg",
            "Snapchat-1005.mp4",
        )
    ] == [
        "Snapchat-20240101-12_34-001.jpg",
        "Snapchat-20240101-12_34-002.mkv",
        "Snapchat-20240101-12_34-003.jpg",
        "Snapchat-20240101-12_34-004.mp4",
    ]


# ---- scan, apply, undo ------------------------------------------------------------------


def test_scan_changes_nothing_on_disk(tmp_path: Path) -> None:
    root = _make_snap_tree(tmp_path / "shots")
    before = sorted(p.name for p in root.iterdir())
    plan = TOOL.scan(RenameOptions(roots=[root], preset="snapchat"), _ctx(tmp_path, None))
    assert sorted(p.name for p in root.iterdir()) == before
    assert plan.actions and all(a.kind is ActionKind.RENAME for a in plan.actions)
    assert plan.notes[0].startswith("Snapchat export: 8 file(s) match. 8 will be renamed")


def test_apply_then_undo_restores_every_name(tmp_path: Path, journal: Journal) -> None:
    root = _make_snap_tree(tmp_path / "shots")
    before = sorted(p.name for p in root.iterdir())
    _, result = _run_ours(tmp_path, journal, RenameOptions(roots=[root], preset="snapchat"))
    assert result.status == "done" and result.count("done") == 8

    report = undo_run(journal, result.run_id)
    assert report.reverted == 8
    assert sorted(p.name for p in root.iterdir()) == before


def test_saved_plan_can_be_applied_later(tmp_path: Path, journal: Journal) -> None:
    root = _make_snap_tree(tmp_path / "shots")
    ctx = _ctx(tmp_path, journal)
    saved = Plan.from_json(TOOL.scan(RenameOptions(roots=[root], preset="snapchat"), ctx).to_json())
    result = TOOL.apply(saved, ctx)
    assert result.count("done") == 8
    assert "Snapchat-20240101-12_34-001.jpg" in {p.name for p in root.iterdir()}


# ---- collisions and the apply-time re-check ---------------------------------------------


def test_existing_file_with_the_target_name_is_never_overwritten(
    tmp_path: Path, journal: Journal
) -> None:
    root = _make_snap_tree(tmp_path / "shots")
    _write(root / "Snapchat-20240101-12_34-001.jpg", "keep me")
    plan, result = _run_ours(tmp_path, journal, RenameOptions(roots=[root], preset="snapchat"))

    blocked = [a for a in plan.actions if not a.selected]
    assert [a.src.name for a in blocked if a.src] == ["Snapchat-1001.jpg"]
    assert "a file with this name already exists" in blocked[0].reason
    assert (root / "Snapchat-20240101-12_34-001.jpg").read_text(encoding="utf-8") == "keep me"
    assert (root / "Snapchat-1001.jpg").exists()
    assert result.count("done") == 7


def test_names_claimed_by_the_batch_are_not_renamed_twice(tmp_path: Path, journal: Journal) -> None:
    """Without a counter, two files on one day want the same name: only the first is selected."""
    root = tmp_path / "pics"
    _write(root / "IMG_1.jpg", "first", (2024, 3, 3, 10, 0, 0))
    _write(root / "IMG_2.jpg", "second", (2024, 3, 3, 11, 0, 0))
    opts = RenameOptions(roots=[root], match="IMG_*.jpg", template="Pic-{date:%Y%m%d}{ext}")
    plan, result = _run_ours(tmp_path, journal, opts)

    assert [a.selected for a in plan.actions] == [True, False]
    assert "another file in this batch takes this name" in plan.actions[1].reason
    assert {p.name for p in root.iterdir()} == {"Pic-20240303.jpg", "IMG_2.jpg"}
    assert result.count("done") == 1


def test_apply_checks_again_and_never_renames_onto_a_file_that_appeared(
    tmp_path: Path, journal: Journal
) -> None:
    root = _make_snap_tree(tmp_path / "shots")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(RenameOptions(roots=[root], preset="snapchat"), ctx)
    _write(root / "Snapchat-20240101-12_34-001.jpg", "appeared later")

    result = TOOL.apply(plan, ctx)
    assert (root / "Snapchat-20240101-12_34-001.jpg").read_text(
        encoding="utf-8"
    ) == "appeared later"
    skipped = [o for o in result.outcomes if o.status == "skipped"]
    assert [o.src for o in skipped] == [str(root / "Snapchat-1001.jpg")]
    assert result.count("done") == 7
    assert not any("_dup" in p.name for p in root.iterdir())


def test_a_plan_edited_to_select_a_clashing_row_still_does_not_clash(
    tmp_path: Path, journal: Journal
) -> None:
    root = tmp_path / "pics"
    _write(root / "IMG_1.jpg", "first", (2024, 3, 3, 10, 0, 0))
    _write(root / "IMG_2.jpg", "second", (2024, 3, 3, 11, 0, 0))
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(
        RenameOptions(roots=[root], match="IMG_*.jpg", template="Pic-{date:%Y%m%d}{ext}"), ctx
    )
    plan.actions[1].selected = True  # the user ticks the blocked row by hand

    result = TOOL.apply(plan, ctx)
    assert {p.name for p in root.iterdir()} == {"Pic-20240303.jpg", "IMG_2.jpg"}
    assert any(o.status == "skipped" for o in result.outcomes)
    assert not any("_dup" in p.name for p in root.iterdir())


def test_letter_case_only_change_is_listed_but_not_renamed(tmp_path: Path) -> None:
    root = tmp_path / "pics"
    _write(root / "IMG_1.JPG", "x", (2024, 3, 3, 10, 0, 0))
    plan = TOOL.scan(
        RenameOptions(roots=[root], match="IMG_*.JPG", template="{name}{ext}", case="lower"),
        _ctx(tmp_path, None),
    )
    assert len(plan.actions) == 1 and not plan.actions[0].selected
    assert "only the letter case changes" in plan.actions[0].reason


# ---- dates ------------------------------------------------------------------------------


def test_filename_dates_drive_the_screenshot_and_whatsapp_presets(
    tmp_path: Path, journal: Journal, make_image: Callable[..., Path]
) -> None:
    root = tmp_path / "phone"
    make_image("phone/Screenshot_20240101_123456.png")
    make_image("phone/Screenshot_20240101_123500.png")
    make_image("phone/IMG-20240102-WA0001.jpg")
    make_image("phone/IMG-20240102-WA0002.jpg")
    _write(root / "IMG-20241345-WA0009.jpg", "bad month")  # not a date: skipped, not renamed

    plan, result = _run_ours(tmp_path, journal, RenameOptions(roots=[root], preset="screenshot"))
    assert result.count("done") == 2
    assert {p.name for p in root.iterdir()} == {
        "Screenshot-20240101-12_34-001.png",
        "Screenshot-20240101-12_35-001.png",
        "IMG-20240102-WA0001.jpg",
        "IMG-20240102-WA0002.jpg",
        "IMG-20241345-WA0009.jpg",
    }

    plan2, result2 = _run_ours(tmp_path, journal, RenameOptions(roots=[root], preset="whatsapp"))
    assert result2.count("done") == 2
    assert {p.name for p in root.iterdir()} == {
        "Screenshot-20240101-12_34-001.png",
        "Screenshot-20240101-12_35-001.png",
        "WhatsApp-20240102-001.jpg",
        "WhatsApp-20240102-002.jpg",
        "IMG-20241345-WA0009.jpg",
    }
    assert any("no date in the file name" in n for n in plan2.notes)


def test_capture_date_comes_from_exif(tmp_path: Path, journal: Journal) -> None:
    root = tmp_path / "camera"
    root.mkdir()
    image = Image.new("RGB", (20, 20), (1, 2, 3))
    exif_obj = Image.Exif()
    exif_obj.get_ifd(exif.EXIF_IFD)[exif.TAG_DATETIME_ORIGINAL] = "2019:05:06 07:08:09"
    image.save(root / "IMG_001.jpg", format="JPEG", exif=exif_obj)
    _write(root / "IMG_002.jpg", "no exif")  # no capture date: skipped

    opts = RenameOptions(
        roots=[root],
        match="IMG_*.jpg",
        template="Photo-{date:%Y%m%d-%H_%M}-{counter:03}{ext}",
        date_source="capture",
    )
    plan, result = _run_ours(tmp_path, journal, opts)
    assert result.count("done") == 1
    assert {p.name for p in root.iterdir()} == {"Photo-20190506-07_08-001.jpg", "IMG_002.jpg"}
    assert any("no capture date" in n and "IMG_002.jpg" in n for n in plan.notes)


def test_video_capture_falls_back_to_ffprobe(
    tmp_path: Path, fake_exe: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "clips"
    _write(root / "VID_1.mp4", "not a real video")
    ffprobe = fake_exe(tmp_path / "tools", "ffprobe", output="ffprobe 7.1")
    deps = ExternalTools(
        overrides={"ffprobe": str(ffprobe)},
        env={},
        version_probe=lambda _path, _args: "ffprobe 7.1",
        ollama_probe=lambda _url: None,
    )
    ctx = RunContext(settings=Settings.model_validate({"onedrive_root": tmp_path}), deps=deps)
    monkeypatch.setattr(
        rename_tool.ffmpeg,
        "creation_time",
        lambda _ffprobe, _path: datetime(2019, 5, 6, 7, 8, 9),
    )
    plan = TOOL.scan(
        RenameOptions(
            roots=[root],
            match="VID_*.mp4",
            template="Clip-{date:%Y%m%d-%H_%M}-{counter:03}{ext}",
            date_source="capture",
        ),
        ctx,
    )
    assert [a.dst.name for a in plan.actions if a.dst] == ["Clip-20190506-07_08-001.mp4"]


def test_created_date_uses_the_file_creation_time(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    path = _write(root / "scan_1.png", "x", (2024, 3, 3, 10, 0, 0))
    stat = os.stat(path)
    created = datetime.fromtimestamp(getattr(stat, "st_birthtime", stat.st_ctime))
    plan = TOOL.scan(
        RenameOptions(
            roots=[root],
            match="scan_*.png",
            template="Scan-{date:%Y%m%d}-{counter:03}{ext}",
            date_source="created",
        ),
        _ctx(tmp_path, None),
    )
    assert plan.actions[0].dst is not None
    assert plan.actions[0].dst.name == f"Scan-{created:%Y%m%d}-001.png"


def test_cloud_only_capture_is_never_read(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "camera"
    _write(root / "IMG_1.jpg", "cloud")
    monkeypatch.setattr(rename_tool, "_cloud_only", lambda info: True)

    def must_not_read(info: object, ctx: object) -> None:
        raise AssertionError("a cloud-only file was read")

    monkeypatch.setattr(rename_tool, "_capture", must_not_read)
    plan, result = _run_ours(
        tmp_path,
        journal,
        RenameOptions(
            roots=[root], match="IMG_*.jpg", template="P-{date:%Y%m%d}{ext}", date_source="capture"
        ),
    )
    assert result.count("done") == 0
    assert (root / "IMG_1.jpg").exists()
    assert any("cloud-only, not read" in n for n in plan.notes)


def test_cloud_only_file_can_still_be_renamed_by_modified_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming changes only the name. No contents are read, so the cloud-only rule is not hit."""
    root = tmp_path / "shots"
    _write(root / "Snapchat-5.jpg", "cloud", (2024, 1, 1, 12, 34, 5))
    monkeypatch.setattr(rename_tool, "_cloud_only", lambda info: True)
    plan = TOOL.scan(RenameOptions(roots=[root], preset="snapchat"), _ctx(tmp_path, None))
    assert [a.selected for a in plan.actions] == [True]


# ---- options, templates and names -------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "fragment"),
    [
        ({"roots": []}, "Choose a preset"),
        ({"roots": [Path("x")], "preset": "custom"}, "Choose a preset"),
        ({"roots": [Path("x")], "match": "IMG_*"}, "Choose a preset, or give a name template"),
        (
            {"roots": [Path("x")], "match": "(", "match_mode": "regex", "template": "a{ext}"},
            "not a valid regular expression",
        ),
        (
            {"roots": [Path("x")], "match": "IMG_*", "template": "{counter:03}"},
            "must include {ext}",
        ),
        ({"roots": [Path("x")], "match": "IMG_*", "template": "{foo}{ext}"}, "unknown field"),
        (
            {"roots": [Path("x")], "match": "IMG_*", "template": "{counter:s}{ext}"},
            "cannot be used",
        ),
        ({"roots": [Path("x")], "match": "IMG_*", "template": "{ext"}, "not valid"),
    ],
)
def test_unusable_options_give_a_note_and_no_actions(
    tmp_path: Path, options: dict[str, object], fragment: str
) -> None:
    plan = TOOL.scan(RenameOptions.model_validate(options), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any(fragment in note for note in plan.notes), plan.notes


def test_unknown_option_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RenameOptions.model_validate({"roots": [], "bogus": 1})


def test_names_windows_cannot_hold_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "pics"
    _write(root / "IMG_1.jpg", "x", (2024, 3, 3, 10, 0, 0))
    _write(root / "IMG_2.jpg", "y", (2024, 3, 3, 10, 0, 1))
    opts = RenameOptions(roots=[root], match="IMG_*.jpg", template="Bad:{counter:03}{ext}")
    plan = TOOL.scan(opts, _ctx(tmp_path, None))
    assert plan.actions == []
    assert sum("contains a character that Windows does not allow" in n for n in plan.notes) == 2


def test_scope_counters_and_recursion(tmp_path: Path, journal: Journal) -> None:
    root = tmp_path / "trip"
    _write(root / "a_1.jpg", "a1", (2024, 5, 5, 9, 0, 0))
    _write(root / "a_2.jpg", "a2", (2024, 5, 6, 9, 0, 0))
    _write(root / "sub" / "b_1.jpg", "b1", (2024, 5, 5, 9, 0, 30))
    flat = TOOL.scan(
        RenameOptions(
            roots=[root],
            match="*_?.jpg",
            template="T-{date:%Y%m%d}-{counter:02}{ext}",
            counter_scope="per-day",
        ),
        _ctx(tmp_path, None),
    )
    assert {a.src.name: a.dst.name for a in flat.actions if a.src and a.dst} == {
        "a_1.jpg": "T-20240505-01.jpg",
        "a_2.jpg": "T-20240506-01.jpg",
    }

    deep = TOOL.scan(
        RenameOptions(
            roots=[root],
            recursive=True,
            match="*_?.jpg",
            template="T-{date:%Y%m%d}-{counter:02}{ext}",
            counter_scope="global",
        ),
        _ctx(tmp_path, None),
    )
    assert sorted(a.dst.name for a in deep.actions if a.dst) == [
        "T-20240505-01.jpg",
        "T-20240505-02.jpg",
        "T-20240506-03.jpg",
    ]


def test_overlapping_roots_do_not_list_a_file_twice(tmp_path: Path) -> None:
    root = tmp_path / "trip"
    _write(root / "sub" / "x_1.jpg", "x", (2024, 5, 5, 9, 0, 0))
    plan = TOOL.scan(
        RenameOptions(
            roots=[root, root / "sub"],
            recursive=True,
            match="x_*.jpg",
            template="X-{date:%Y%m%d}{ext}",
        ),
        _ctx(tmp_path, None),
    )
    assert len(plan.actions) == 1


def test_protected_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile"
    _write(profile / "Snapchat-1.jpg", "x", (2024, 1, 1, 12, 0, 0))
    monkeypatch.setenv("USERPROFILE", str(profile))
    plan = TOOL.scan(RenameOptions(roots=[profile], preset="snapchat"), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("user profile folder" in n for n in plan.notes)
    assert (profile / "Snapchat-1.jpg").exists()


def test_links_out_of_the_root_are_not_followed(tmp_path: Path, journal: Journal) -> None:
    root = tmp_path / "root"
    outside = _write(tmp_path / "outside" / "Snapchat-9.jpg", "outside", (2024, 1, 1, 12, 0, 0))
    root.mkdir()
    # A directory symlink needs admin rights or developer mode on Windows. A junction does not,
    # and it is the link type the plan worries about most.
    try:
        os.symlink(outside.parent, root / "link", target_is_directory=True)
    except OSError:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside.parent)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("could not create a symlink or a junction here")
    plan, result = _run_ours(
        tmp_path, journal, RenameOptions(roots=[root], preset="snapchat", recursive=True)
    )
    assert plan.actions == []
    assert outside.exists() and outside.read_text(encoding="utf-8") == "outside"
    assert result.outcomes == []


# ---- helpers, registry and the undo path -----------------------------------------------


def test_naming_helpers() -> None:
    assert date_from_filename("Screenshot_20240101_123456.png") == (
        datetime(2024, 1, 1, 12, 34, 56),
        True,
    )
    assert date_from_filename("IMG-20240102-WA0001.jpg") == (datetime(2024, 1, 2), False)
    assert date_from_filename("photo.jpg") is None
    assert date_from_filename("IMG-19000101.jpg") is None  # before min_year
    assert apply_case(".JPG", "lower") == ".jpg"
    assert apply_case(".jpg", "upper") == ".JPG"
    assert apply_case(".Jpg", "keep") == ".Jpg"
    assert (
        template_problem("{date:%Y}")
        == "The name template must include {ext}, so that the file keeps its type."
    )
    assert name_problem("CON.jpg") == "is a reserved Windows name"
    assert name_problem("a.") == "ends with a space or a dot"
    assert name_problem("ok.jpg") is None


def test_presets_are_defined_for_the_three_named_exports() -> None:
    assert set(PRESETS) == {"snapchat", "whatsapp", "screenshot"}
    assert PRESETS["snapchat"].template == "Snapchat-{date:%Y%m%d}-{time:%H_%M}-{counter:03}{ext}"


def test_tool_is_discovered_through_the_registry() -> None:
    tools = discover()
    assert "rename" in tools
    assert tools["rename"] is TOOL
    assert TOOL.Options is RenameOptions


def test_undo_sees_renames_as_reversible(tmp_path: Path, journal: Journal) -> None:
    root = tmp_path / "shots"
    _write(root / "Snapchat-1.jpg", "x", (2024, 1, 1, 12, 0, 0))
    _, result = _run_ours(tmp_path, journal, RenameOptions(roots=[root], preset="snapchat"))
    assert result.count("done") == 1
    assert undo_run(journal, result.run_id).reverted == 1
    assert (root / "Snapchat-1.jpg").exists()
