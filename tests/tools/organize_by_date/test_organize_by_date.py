"""Organize by date (plan §3.6): parity with ``legacy/python-extract_file_metadata-8.py``, the date
rules, and the safety rules.

Parity: the legacy script's ``process_file`` is run, read-only, on a generated tree. Its
``DestinationPath`` and ``DateSource`` must match the tool's plan. ``run()`` is never called: it
reads and writes the owner's real master log. Its Excel and pandas imports are stubbed, because
pandas is not a project dependency. Trees are generated under ``tmp_path`` only.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.fs import protected_reason
from various_tools.core.journal import Journal, undo_run
from various_tools.core.onedrive import SyncStatus
from various_tools.core.plan import Plan, apply_plan
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.organize_by_date import TOOL, OrganizeByDateOptions
from various_tools.tools.organize_by_date import dates as dates_module
from various_tools.tools.organize_by_date import tool as obd
from various_tools.tools.organize_by_date.catalog import Catalog

REPO = Path(__file__).resolve().parents[3]
LEGACY = REPO / "tests" / "fixtures" / "legacy_reference" / "python-extract_file_metadata-8.py"
EXIF_IFD = 0x8769
DATE_ORIGINAL = 0x9003
DATE_DIGITIZED = 0x9004


# ----- helpers -------------------------------------------------------------------------------


class _Stub:
    """Stands in for pandas. Attributes and calls return stubs; nothing is read."""

    def __getattr__(self, name: str) -> _Stub:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Stub()

    def __call__(self, *args: object, **kwargs: object) -> _Stub:
        return _Stub()


def _jpeg(
    path: Path,
    *,
    original: str | None = None,
    digitized: str | None = None,
    modified: datetime | None = None,
) -> Path:
    """A small JPEG with the EXIF dates given. ``modified`` sets the file's modified time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (64, 48), (80, 120, 160))
    if original is None and digitized is None:
        image.save(path, format="JPEG")
    else:
        exif = Image.Exif()
        sub: dict[int, str] = {}
        if original is not None:
            sub[DATE_ORIGINAL] = original
        if digitized is not None:
            sub[DATE_DIGITIZED] = digitized
        exif[EXIF_IFD] = sub
        image.save(path, format="JPEG", exif=exif.tobytes())
    if modified is not None:
        stamp = modified.timestamp()
        os.utime(path, (stamp, stamp))
    return path


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _ctx(tmp_path: Path, journal: Journal | None = None, **settings: Any) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path, **settings}),
        deps=ExternalTools(ollama_probe=lambda _u: None),
        journal=journal,
    )


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    with Journal(tmp_path / "journal.sqlite") as opened:
        yield opened


def _opts(tmp_path: Path, sources: list[Path], dest: Path, **kwargs: Any) -> OrganizeByDateOptions:
    return OrganizeByDateOptions(
        sources=sources,
        dest_root=dest,
        catalog_path=tmp_path / "catalog.sqlite",
        **kwargs,
    )


def _moves(plan: Plan) -> dict[str, str]:
    """Selected moves, keyed by the source's normalised path, valued by the destination."""
    return {
        os.path.normcase(str(a.src)): str(a.dst)
        for a in plan.selected_actions()
        if a.src is not None and a.dst is not None
    }


def _snapshot(root: Path) -> dict[str, tuple[int, float]]:
    """Every file under ``root`` with its size and modified time. Used to prove nothing changed."""
    found: dict[str, tuple[int, float]] = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            found[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime)
    return found


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


@pytest.fixture
def src(tmp_path: Path) -> Path:
    folder = tmp_path / "Source"
    folder.mkdir()
    return folder


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    return tmp_path / "Camera Roll"


# ----- the date rules (legacy rules 1 and 2) -------------------------------------------------


def test_exif_date_picks_the_year_and_month_folder(tmp_path: Path, src: Path, dest: Path) -> None:
    photo = _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")
    plan = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    assert _moves(plan) == {
        os.path.normcase(str(photo)): str(dest / "2024" / "2024 - 10" / "a.jpg")
    }


def test_digitized_date_is_used_when_original_is_missing(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    _jpeg(src / "d.jpg", digitized="2019:08:01 09:00:00")
    plan = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    (action,) = plan.selected_actions()
    assert action.dst == dest / "2019" / "2019 - 08" / "d.jpg"


def test_modified_date_is_the_fallback_when_it_is_plausible(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    _jpeg(src / "m.jpg", modified=_when("2015-03-09 12:00:00"))
    plan = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    (action,) = plan.selected_actions()
    assert action.dst == dest / "2015" / "2015 - 03" / "m.jpg"
    assert action.reason.startswith("Date Modified (fallback)")


def test_implausible_modified_date_goes_to_the_unknown_folder(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    _jpeg(src / "old.jpg", modified=_when("1975-06-01 10:00:00"))
    plan = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    (action,) = plan.selected_actions()
    assert action.dst == dest / "Unknown Date" / "old.jpg"
    assert action.reason.startswith("Date Modified (implausible - unknown date)")


def test_implausible_exif_falls_through_to_the_modified_date(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    _jpeg(src / "zero.jpg", original="1970:01:01 00:00:00", modified=_when("2021-02-03 08:00:00"))
    plan = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    (action,) = plan.selected_actions()
    assert action.dst == dest / "2021" / "2021 - 02" / "zero.jpg"


def test_priority_without_modified_sends_undated_photos_to_unknown(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    _jpeg(src / "nodate.jpg", modified=_when("2015-03-09 12:00:00"))
    opts = _opts(tmp_path, [src], dest, date_priority=["exif"])
    (action,) = TOOL.scan(opts, _ctx(tmp_path)).selected_actions()
    assert action.dst == dest / "Unknown Date" / "nodate.jpg"


def test_priority_order_is_honoured(tmp_path: Path, src: Path, dest: Path) -> None:
    _jpeg(src / "p.jpg", original="2024:10:05 14:02:11", modified=_when("2016-07-04 09:00:00"))
    opts = _opts(tmp_path, [src], dest, date_priority=["modified"])
    (action,) = TOOL.scan(opts, _ctx(tmp_path)).selected_actions()
    assert action.dst == dest / "2016" / "2016 - 07" / "p.jpg"


def test_duplicate_priority_sources_are_refused() -> None:
    with pytest.raises(ValidationError):
        OrganizeByDateOptions(sources=[], date_priority=["exif", "exif"])


def test_video_creation_time_is_the_capture_date(
    tmp_path: Path, src: Path, dest: Path, make_video: Callable[..., Path]
) -> None:
    if ExternalTools(ollama_probe=lambda _u: None).path("ffprobe") is None:
        pytest.skip("ffprobe is not installed")
    make_video("clip.mp4", creation_time="2023-05-06T12:00:00Z")
    shutil.move(str(tmp_path / "clip.mp4"), str(src / "clip.mp4"))
    expected = datetime.fromisoformat("2023-05-06T12:00:00+00:00").astimezone().replace(tzinfo=None)
    plan = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    (action,) = plan.selected_actions()
    assert action.dst == dest / str(expected.year) / f"{expected.year} - {expected.month:02d}" / (
        "clip.mp4"
    )
    assert action.reason.startswith("Date Taken (video metadata)")


# ----- the date in the file name (0.9) ------------------------------------------------------


def test_the_date_in_the_name_beats_a_later_exif_date(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    _jpeg(src / "IMG_20220624_005115.jpg", original="2025:03:14 10:00:00")
    (action,) = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path)).selected_actions()
    assert action.dst == dest / "2022" / "2022 - 06" / "IMG_20220624_005115.jpg"
    assert action.reason.startswith("Date in the file name")


def test_a_pixel_name_in_utc_gives_way_to_an_exif_date_that_agrees(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    """Pixel names hold UTC: 01:30 on 1 July UTC is 30 June in the evening in the US."""
    _jpeg(src / "PXL_20220701_013000123.jpg", original="2022:06:30 18:30:00")
    _jpeg(src / "PXL_20220701_013000999.jpg", original="2025:03:01 10:00:00")
    plan = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    by_name = {Path(a.src).name: a for a in plan.selected_actions() if a.src}
    agrees = by_name["PXL_20220701_013000123.jpg"]
    assert agrees.dst == dest / "2022" / "2022 - 06" / "PXL_20220701_013000123.jpg"
    assert agrees.reason.startswith("Date Taken (EXIF)")
    edited = by_name["PXL_20220701_013000999.jpg"]
    assert edited.dst == dest / "2022" / "2022 - 07" / "PXL_20220701_013000999.jpg"


def test_whatsapp_names_are_dated_by_their_name(tmp_path: Path, src: Path, dest: Path) -> None:
    _jpeg(src / "IMG-20190801-WA0003.jpg", modified=_when("2024-01-01 12:00:00"))
    (action,) = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path)).selected_actions()
    assert action.dst == dest / "2019" / "2019 - 08" / "IMG-20190801-WA0003.jpg"


def test_a_name_without_a_date_uses_the_metadata(tmp_path: Path, src: Path, dest: Path) -> None:
    _jpeg(src / "IMG_1234.jpg", original="2024:10:05 14:02:11")
    (action,) = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path)).selected_actions()
    assert action.dst == dest / "2024" / "2024 - 10" / "IMG_1234.jpg"
    assert action.reason.startswith("Date Taken (EXIF)")


def test_name_date_first_off_keeps_the_metadata_order(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    _jpeg(src / "IMG_20220624_005115.jpg", original="2025:03:14 10:00:00")
    opts = _opts(tmp_path, [src], dest, name_date_first=False)
    (action,) = TOOL.scan(opts, _ctx(tmp_path)).selected_actions()
    assert action.dst == dest / "2025" / "2025 - 03" / "IMG_20220624_005115.jpg"


def test_the_owners_video_stays_in_its_month(
    tmp_path: Path, dest: Path, make_video: Callable[..., Path]
) -> None:
    """``2022\2022 - 06\20220624_005115.mp4`` whose creation time says March 2025 stays put."""
    if ExternalTools(ollama_probe=lambda _u: None).path("ffprobe") is None:
        pytest.skip("ffprobe is not installed")
    make_video("clip.mp4", creation_time="2025-03-10T12:00:00Z")
    home = dest / "2022" / "2022 - 06"
    home.mkdir(parents=True)
    shutil.move(str(tmp_path / "clip.mp4"), str(home / "20220624_005115.mp4"))
    plan = TOOL.scan(_opts(tmp_path, [dest], dest, recursive=True), _ctx(tmp_path))
    assert plan.selected_actions() == []
    assert any("1 already in place" in note for note in plan.notes)
    old_rules = _opts(tmp_path, [dest], dest, recursive=True, name_date_first=False)
    (wrong,) = TOOL.scan(old_rules, _ctx(tmp_path)).selected_actions()
    assert wrong.dst is not None and "2025" in wrong.dst.parts


def test_skip_processed_redates_files_moved_by_an_older_rule(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    _jpeg(src / "IMG_20220624_005115.jpg", original="2025:03:14 10:00:00")
    _jpeg(src / "IMG_1234.jpg", original="2024:10:05 14:02:11")
    ctx = _ctx(tmp_path, journal)
    old = _opts(tmp_path, [src], dest, name_date_first=False)
    TOOL.apply(TOOL.scan(old, ctx), ctx)
    misplaced = dest / "2025" / "2025 - 03" / "IMG_20220624_005115.jpg"
    assert misplaced.is_file()

    again = _opts(tmp_path, [dest], dest, recursive=True, skip_processed=True)
    plan = TOOL.scan(again, _ctx(tmp_path))
    assert _moves(plan) == {
        os.path.normcase(str(misplaced)): str(dest / "2022" / "2022 - 06" / misplaced.name)
    }
    assert any("skipped as already processed" in note for note in plan.notes)  # IMG_1234
    TOOL.apply(plan, ctx)
    redone = TOOL.scan(again, _ctx(tmp_path))
    assert redone.selected_actions() == []


# ----- cloud-only files (rule 3) and other safety rules --------------------------------------


def test_cloud_only_file_is_never_read_or_moved(
    tmp_path: Path, src: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _jpeg(src / "cloud.jpg", original="2024:10:05 14:02:11")
    _jpeg(src / "local.jpg", original="2024:10:05 14:02:11")
    real_read = dates_module.read_exif

    def fake_status(path: Path) -> SyncStatus:
        same = _same(path, cloud)
        return SyncStatus.CLOUD_ONLY if same else SyncStatus.LOCAL

    def guarded_read(path: Path) -> object:
        assert not _same(path, cloud), "a cloud-only file was read"
        return real_read(path)

    monkeypatch.setattr(obd, "get_sync_status", fake_status)
    monkeypatch.setattr(dates_module, "read_exif", guarded_read)
    plan = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))

    selected = [a.src.name for a in plan.selected_actions() if a.src]  # type: ignore[union-attr]
    assert selected == ["local.jpg"]
    skipped = [a for a in plan.actions if a.src is not None and _same(a.src, cloud)]
    assert len(skipped) == 1
    assert skipped[0].selected is False
    assert skipped[0].dst is None
    assert "cloud-only" in skipped[0].reason
    assert cloud.exists()


def test_cloud_only_row_is_not_executable_and_apply_does_not_fail(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _jpeg(src / "cloud.jpg", original="2024:10:05 14:02:11")
    monkeypatch.setattr(
        obd,
        "get_sync_status",
        lambda path: SyncStatus.CLOUD_ONLY if _same(path, cloud) else SyncStatus.LOCAL,
    )
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, [src], dest), ctx)
    assert plan.selected_actions() == []

    result = TOOL.apply(plan, ctx)
    assert result.outcomes == []
    assert cloud.exists() and not dest.exists()

    # Even if someone ticks the row by hand, the tool unticks it again at apply time.
    row = next(a for a in plan.actions if a.src is not None and _same(a.src, cloud))
    row.selected = True
    result = TOOL.apply(plan, ctx)
    assert result.outcomes == [] and cloud.exists()

    # And the engine alone refuses a move without a destination: skipped, never failed.
    row.selected = True
    result = apply_plan(plan, ctx)
    assert [o.status for o in result.outcomes] == ["skipped"]
    assert "no destination" in result.outcomes[0].detail
    assert result.count("failed") == 0
    assert cloud.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_destination_that_is_a_junction_is_refused_and_deeper_ones_fail_at_apply(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    outside = tmp_path / "Outside"
    outside.mkdir()
    dest.mkdir(parents=True)
    _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")

    # A junction inside the destination: the scan plans the move, the engine refuses it.
    inner = dest / "2024"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(inner), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip(f"could not create a junction: {made.stderr or made.stdout}")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, [src], dest), ctx)
    assert len(plan.selected_actions()) == 1
    result = TOOL.apply(plan, ctx)
    assert [o.status for o in result.outcomes] == ["skipped"]
    assert "junction" in result.outcomes[0].detail
    assert (src / "a.jpg").exists() and list(outside.iterdir()) == []

    # The destination folder itself is a junction: the scan refuses it.
    linked = tmp_path / "LinkedDest"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip(f"could not create a junction: {made.stderr or made.stdout}")
    refused = TOOL.scan(_opts(tmp_path, [src], linked), ctx)
    assert refused.actions == []
    assert any("junction" in note for note in refused.notes)


def test_scan_changes_nothing_on_disk(tmp_path: Path, src: Path, dest: Path) -> None:
    _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")
    _jpeg(src / "b.jpg", modified=_when("1975-01-01 00:00:00"))
    before = _snapshot(src)
    TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    assert _snapshot(src) == before
    assert not dest.exists()


def test_apply_moves_each_file_and_undo_puts_it_back(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, [src], dest), ctx)
    result = TOOL.apply(plan, ctx)
    assert result.status == "done"
    assert result.count("done") == 1
    assert (dest / "2024" / "2024 - 10" / "a.jpg").is_file()
    assert not (src / "a.jpg").exists()

    report = undo_run(journal, result.run_id)
    assert report.reverted == 1
    assert (src / "a.jpg").is_file()
    assert not (dest / "2024" / "2024 - 10" / "a.jpg").exists()


def test_name_clashes_get_numbered_names_inside_the_batch_and_on_disk(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    _jpeg(src / "one" / "IMG.jpg", original="2024:10:05 14:02:11")
    _jpeg(src / "two" / "IMG.jpg", original="2024:10:05 15:00:00")
    existing = _jpeg(dest / "2024" / "2024 - 10" / "IMG.jpg", original="2024:10:01 10:00:00")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, [src], dest, recursive=True), ctx)
    destinations = sorted(Path(a.dst).name for a in plan.selected_actions() if a.dst)
    assert destinations == ["IMG_dup1.jpg", "IMG_dup2.jpg"]

    TOOL.apply(plan, ctx)
    folder = dest / "2024" / "2024 - 10"
    assert sorted(p.name for p in folder.iterdir()) == ["IMG.jpg", "IMG_dup1.jpg", "IMG_dup2.jpg"]
    assert existing.exists()


def test_file_already_in_its_place_is_not_moved(
    tmp_path: Path, dest: Path, journal: Journal
) -> None:
    placed = _jpeg(dest / "2024" / "2024 - 10" / "a.jpg", original="2024:10:05 14:02:11")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, [dest], dest, recursive=True), ctx)
    assert plan.selected_actions() == []
    assert any("1 already in place" in note for note in plan.notes)
    assert placed.exists()


def test_dot_files_and_non_media_are_ignored_and_subfolders_need_recursive(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    _jpeg(src / "sub" / "c.jpg", original="2024:10:05 14:02:11")
    _jpeg(src / ".hidden.jpg", original="2024:10:05 14:02:11")
    (src / "notes.txt").write_text("not media", encoding="utf-8")
    flat = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    assert flat.selected_actions() == []

    deep = TOOL.scan(_opts(tmp_path, [src], dest, recursive=True), _ctx(tmp_path))
    assert [a.src.name for a in deep.selected_actions() if a.src] == ["c.jpg"]  # type: ignore[union-attr]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_junction_out_of_the_selected_folder_is_not_followed(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    outside = tmp_path / "Outside"
    _jpeg(outside / "x.jpg", original="2024:10:05 14:02:11")
    link = src / "link"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip(f"could not create a junction: {made.stderr or made.stdout}")
    plan = TOOL.scan(_opts(tmp_path, [src], dest, recursive=True), _ctx(tmp_path))
    assert not any(a.src is not None and a.src.name == "x.jpg" for a in plan.actions)


@pytest.mark.skipif(sys.platform != "win32", reason="needs a Windows protected folder")
def test_protected_destination_is_refused(tmp_path: Path, src: Path) -> None:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    if protected_reason(windows) is None:
        pytest.skip("the Windows folder is not listed as protected here")
    _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")
    plan = TOOL.scan(_opts(tmp_path, [src], windows), _ctx(tmp_path))
    assert plan.selected_actions() == []
    assert any("protected" in note for note in plan.notes)


def test_file_that_turns_cloud_only_before_apply_is_not_moved(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, [src], dest), ctx)
    monkeypatch.setattr(
        obd,
        "get_sync_status",
        lambda path: SyncStatus.CLOUD_ONLY if _same(path, photo) else SyncStatus.LOCAL,
    )
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0
    assert photo.exists()
    assert any("became cloud-only" in note for note in plan.notes)


# ----- resume, retry and the catalog ---------------------------------------------------------


def test_skip_processed_skips_files_that_an_earlier_run_moved(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")
    ctx = _ctx(tmp_path, journal)
    TOOL.apply(TOOL.scan(_opts(tmp_path, [src], dest), ctx), ctx)

    resumed = TOOL.scan(
        _opts(tmp_path, [dest], dest, recursive=True, skip_processed=True), _ctx(tmp_path)
    )
    assert resumed.selected_actions() == []
    assert any("skipped as already processed" in note for note in resumed.notes)

    rechecked = TOOL.scan(_opts(tmp_path, [dest], dest, recursive=True), _ctx(tmp_path))
    assert rechecked.selected_actions() == []
    assert any("1 already in place" in note for note in rechecked.notes)


def test_errors_only_retries_only_the_files_that_failed(
    tmp_path: Path, src: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _jpeg(src / "good.jpg", original="2024:10:05 14:02:11")
    bad = _jpeg(src / "bad.jpg", original="2024:10:05 14:02:11")
    real_read = dates_module.read_capture_date

    def flaky(path: Path, **kwargs: Any) -> Any:
        if _same(path, bad):
            raise RuntimeError("disk hiccup")
        return real_read(path, **kwargs)

    monkeypatch.setattr(obd.dates, "read_capture_date", flaky)
    first = TOOL.scan(_opts(tmp_path, [src], dest), _ctx(tmp_path))
    assert any("disk hiccup" in note for note in first.notes)
    assert {a.src.name for a in first.selected_actions() if a.src} == {"good.jpg"}

    monkeypatch.setattr(obd.dates, "read_capture_date", real_read)
    retry = TOOL.scan(_opts(tmp_path, [src], dest, errors_only=True), _ctx(tmp_path))
    assert [a.src.name for a in retry.selected_actions() if a.src] == ["bad.jpg"]

    fresh = OrganizeByDateOptions(
        sources=[src], dest_root=dest, errors_only=True, catalog_path=tmp_path / "fresh.sqlite"
    )
    nothing = TOOL.scan(fresh, _ctx(tmp_path))
    assert any("No errored files" in note for note in nothing.notes)


def test_workers_do_not_change_the_plan(tmp_path: Path, src: Path, dest: Path) -> None:
    for index in range(6):
        _jpeg(src / f"p{index}.jpg", original=f"2024:10:0{index % 5 + 1} 10:00:00")

    def plan_for(workers: int) -> list[tuple[str, str]]:
        opts = _opts(tmp_path, [src], dest, workers=workers)
        plan = TOOL.scan(opts, _ctx(tmp_path))
        return [(str(a.src), str(a.dst)) for a in plan.actions]

    assert plan_for(4) == plan_for(1)


def test_catalog_records_each_file_and_exports_to_csv(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")
    _jpeg(src / "b.jpg", modified=_when("1975-01-01 00:00:00"))
    ctx = _ctx(tmp_path, journal)
    TOOL.apply(TOOL.scan(_opts(tmp_path, [src], dest), ctx), ctx)

    with Catalog(tmp_path / "catalog.sqlite") as catalog:
        assert catalog.status_of(dest / "2024" / "2024 - 10" / "a.jpg") == "moved"
        assert catalog.status_of(dest / "Unknown Date" / "b.jpg") == "moved"
        out = tmp_path / "export" / "catalog.csv"
        assert catalog.export_csv(out) == 2
    lines = out.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].startswith("key,path,original_path,file_name")
    assert len(lines) == 3


def test_saved_plan_applies_later(tmp_path: Path, src: Path, dest: Path, journal: Journal) -> None:
    _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")
    ctx = _ctx(tmp_path, journal)
    saved = TOOL.scan(_opts(tmp_path, [src], dest), ctx).to_json()
    result = TOOL.apply(Plan.from_json(saved), ctx)
    assert result.count("done") == 1
    assert (dest / "2024" / "2024 - 10" / "a.jpg").is_file()


def test_bad_options_are_reported_and_nothing_is_scanned(
    tmp_path: Path, src: Path, dest: Path
) -> None:
    _jpeg(src / "a.jpg", original="2024:10:05 14:02:11")
    cases = {
        "Choose at least one folder": _opts(tmp_path, [], dest),
        "folder template must use only": _opts(tmp_path, [src], dest, folder_template="{day}/x"),
        "inside the destination": _opts(tmp_path, [src], dest, folder_template="../escape"),
        "plain folder name": _opts(tmp_path, [src], dest, unknown_folder="a/b"),
    }
    for expected, opts in cases.items():
        plan = TOOL.scan(opts, _ctx(tmp_path))
        assert plan.selected_actions() == [], expected
        assert any(expected in note for note in plan.notes), expected
    assert not dest.exists()


def test_tool_is_discovered_through_the_registry() -> None:
    assert "organize_by_date" in discover()


# ----- parity with the legacy script ---------------------------------------------------------


def _load_legacy(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the legacy script without running it. Only pandas is stubbed."""
    monkeypatch.setitem(sys.modules, "pandas", _Stub())
    spec = importlib.util.spec_from_file_location("legacy_metadata8", LEGACY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        spec.loader.exec_module(module)
    return module


def test_plan_matches_the_legacy_script(
    tmp_path: Path,
    src: Path,
    dest: Path,
    journal: Journal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same tree gives the same destinations and date sources in both programs.

    Files: EXIF Date Taken, digitized date only, modified-date fallback, implausible modified date,
    implausible EXIF date, and (when ffprobe is on PATH) a video's creation time.
    """
    legacy = _load_legacy(monkeypatch)
    deps = ExternalTools(ollama_probe=lambda _u: None)
    ffmpeg, ffprobe = deps.path("ffmpeg"), deps.path("ffprobe")
    if ffprobe is not None:
        # The legacy script calls "ffprobe" by name, so its folder goes on PATH for this test only.
        monkeypatch.setenv("PATH", f"{ffprobe.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    _jpeg(src / "exif_2024.jpg", original="2024:10:05 14:02:11")
    _jpeg(src / "digitized_2019.jpg", digitized="2019:08:01 09:00:00")
    _jpeg(src / "no_exif_2015.jpg", modified=_when("2015-03-09 12:00:00"))
    _jpeg(src / "no_exif_1975.jpg", modified=_when("1975-06-01 10:00:00"))
    _jpeg(
        src / "zero_exif_2021.jpg",
        original="1970:01:01 00:00:00",
        modified=_when("2021-02-03 08:00:00"),
    )
    (src / "notes.txt").write_text("not media", encoding="utf-8")
    if ffmpeg is not None and ffprobe is not None:
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
                "-metadata",
                "creation_time=2023-05-06T12:00:00Z",
                str(src / "clip_2023.mp4"),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )

    legacy.DEST_ROOT = dest  # the legacy script's destination, pointed at the temp tree
    legacy_rows = {}
    for path in sorted(src.iterdir()):
        if path.suffix.lower() in legacy.PROCESSABLE_EXTS:
            legacy_rows[path.name] = legacy.process_file(path)
    assert len(legacy_rows) >= 5

    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, [src], dest), ctx)
    by_name = {Path(a.src).name: a for a in plan.selected_actions() if a.src}
    assert set(by_name) == {name for name, row in legacy_rows.items() if row["Action"] == "Pending"}

    for name, row in legacy_rows.items():
        assert row["Action"] == "Pending", name
        action = by_name[name]
        assert _same(action.dst, row["DestinationPath"]), name  # type: ignore[arg-type]
        assert action.reason.startswith(row["DateSource"]), name

    result = TOOL.apply(plan, ctx)
    assert result.count("done") == len(legacy_rows)
    for row in legacy_rows.values():
        assert Path(row["DestinationPath"]).is_file()
