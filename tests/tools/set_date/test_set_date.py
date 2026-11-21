"""Change date: the three modes, the plan, apply and undo, cloud-only files, the library refresh.

Every file is generated under ``tmp_path``. The ExifTool tests are skipped when ExifTool is not
installed. The Recycle Bin is never touched: ``send2trash`` is replaced by a fake that moves the
file into a folder in tmp_path (the same ``$I``/``$R`` layout the real undo reads).
"""

from __future__ import annotations

import os
import shutil
import struct
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from various_tools.core import onedrive
from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.media import exif
from various_tools.core.media.library import MediaLibrary, read_media_item
from various_tools.core.plan import Plan, RunResult
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.set_date import TOOL, SetDateOptions
from various_tools.tools.set_date.tool import new_date_for, parse_new_date

EXIF_IFD = 0x8769


def jpeg(path: Path, taken: str = "2019:07:04 10:12:00") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = Image.Exif()
    data.get_ifd(EXIF_IFD)[0x9003] = taken
    Image.new("RGB", (16, 12), (1, 2, 3)).save(path, "JPEG", exif=data.tobytes())
    return path


class FakeBin(list[Path]):
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


@pytest.fixture
def fake_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBin:
    fake = FakeBin(tmp_path / "bin")
    monkeypatch.setattr(plan_module, "send2trash", fake.trash)
    return fake


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


class NoExifTool(ExternalTools):
    def exiftool(self) -> Path | None:
        return None


def ctx_for(tmp_path: Path, journal: Journal | None, *, exiftool: Path | None = None) -> RunContext:
    deps: ExternalTools
    if exiftool is None:
        deps = NoExifTool(ollama_probe=lambda _u: None)
    else:
        deps = ExternalTools(ollama_probe=lambda _u: None)
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}), deps=deps, journal=journal
    )


def run(
    tmp_path: Path, journal: Journal, opts: SetDateOptions, *, exiftool: Path | None = None
) -> tuple[Plan, RunResult]:
    ctx = ctx_for(tmp_path, journal, exiftool=exiftool)
    plan = TOOL.scan(opts, ctx)
    return plan, TOOL.apply(plan, ctx)


def scan(tmp_path: Path, opts: SetDateOptions) -> Plan:
    return TOOL.scan(opts, ctx_for(tmp_path, None))


def reasons(plan: Plan) -> list[str]:
    return [a.reason.partition(" (")[0] for a in plan.actions]


# ----- the dates ----------------------------------------------------------------------------


def test_parse_new_date_forms() -> None:
    assert parse_new_date("2021-03-04 10:12") == datetime(2021, 3, 4, 10, 12)
    assert parse_new_date("2021-03-04 10:12:30") == datetime(2021, 3, 4, 10, 12, 30)
    assert parse_new_date("2021-03-04") == datetime(2021, 3, 4, 12, 0)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_new_date("last week")


def test_month_year_clamps_the_day_and_keeps_the_time() -> None:
    opts = SetDateOptions(mode="month_year", year=2021, month=2)
    assert new_date_for(datetime(2019, 1, 31, 8, 30), opts) == datetime(2021, 2, 28, 8, 30)
    leap = SetDateOptions(mode="month_year", year=2024, month=2)
    assert new_date_for(datetime(2019, 1, 31, 8, 30), leap) == datetime(2024, 2, 29, 8, 30)
    only_month = SetDateOptions(mode="month_year", month=4)
    assert new_date_for(datetime(2019, 1, 31, 8, 30), only_month) == datetime(2019, 4, 30, 8, 30)


def test_shift_moves_by_days_hours_minutes_both_ways() -> None:
    plus = SetDateOptions(mode="shift", shift_days=1, shift_hours=2, shift_minutes=3)
    assert new_date_for(datetime(2019, 12, 31, 23, 0), plus) == datetime(2020, 1, 2, 1, 3)
    minus = SetDateOptions(mode="shift", shift_hours=-1)
    assert new_date_for(datetime(2019, 1, 1, 0, 30), minus) == datetime(2018, 12, 31, 23, 30)


def test_a_date_out_of_range_is_refused() -> None:
    with pytest.raises(ValueError, match="between"):
        new_date_for(datetime(2000, 1, 1), SetDateOptions(mode="shift", shift_days=-36500))


# ----- scan ---------------------------------------------------------------------------------


def test_the_tool_is_registered_in_organize() -> None:
    tool = discover()["set_date"]
    assert tool.category == "Organize"


def test_exact_scan_lists_old_and_new_and_changes_nothing(tmp_path: Path) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    before = (a.read_bytes(), a.stat().st_mtime_ns)
    plan = scan(tmp_path, SetDateOptions(roots=[a.parent], new_date="2021-03-04 10:12"))
    assert reasons(plan) == ["2019-07-04 10:12 → 2021-03-04 10:12"]
    assert plan.actions[0].src == plan.actions[0].dst == a
    assert (a.read_bytes(), a.stat().st_mtime_ns) == before
    assert list(a.parent.iterdir()) == [a]


def test_month_year_scan_clamps_each_file(tmp_path: Path) -> None:
    jpeg(tmp_path / "pics" / "a.jpg", "2019:01:31 08:30:00")
    jpeg(tmp_path / "pics" / "b.jpg", "2019:01:15 09:00:00")
    opts = SetDateOptions(roots=[tmp_path / "pics"], mode="month_year", year=2021, month=2)
    assert sorted(reasons(scan(tmp_path, opts))) == [
        "2019-01-15 09:00 → 2021-02-15 09:00",
        "2019-01-31 08:30 → 2021-02-28 08:30",
    ]


def test_shift_scan_and_files_that_already_match_are_not_actions(tmp_path: Path) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg", "2019:07:04 10:12:00")
    plan = scan(tmp_path, SetDateOptions(paths=[a], mode="shift", shift_hours=-3))
    assert reasons(plan) == ["2019-07-04 10:12 → 2019-07-04 07:12"]
    same = scan(tmp_path, SetDateOptions(paths=[a], new_date="2019-07-04 10:12"))
    assert same.actions == [] and "1 already have that date" in same.notes[0]


def test_scan_without_a_choice_explains(tmp_path: Path) -> None:
    root = tmp_path / "pics"
    jpeg(root / "a.jpg")
    assert "YYYY-MM-DD" in scan(tmp_path, SetDateOptions(roots=[root])).notes[0]
    assert "month" in scan(tmp_path, SetDateOptions(roots=[root], mode="month_year")).notes[0]
    assert "days" in scan(tmp_path, SetDateOptions(roots=[root], mode="shift")).notes[0]
    assert "file or folder" in scan(tmp_path, SetDateOptions(new_date="2021-01-01")).notes[0]


def test_scan_says_when_only_the_modified_time_changes(tmp_path: Path) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    plan = scan(tmp_path, SetDateOptions(paths=[a], new_date="2021-03-04 10:12"))
    assert "ExifTool" in " ".join(plan.notes)
    assert "modified time only" in plan.actions[0].reason


def test_cloud_only_files_are_skipped_and_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    b = jpeg(tmp_path / "pics" / "b.jpg")
    real = onedrive.get_sync_status
    monkeypatch.setattr(
        onedrive,
        "get_sync_status",
        lambda p: onedrive.SyncStatus.CLOUD_ONLY if Path(p).name == "a.jpg" else real(p),
    )
    real_read = exif.read_exif

    def guarded(path: Path) -> exif.ExifInfo:
        assert path.name != "a.jpg", "a cloud-only file was opened"
        return real_read(path)

    monkeypatch.setattr(exif, "read_exif", guarded)
    plan = scan(tmp_path, SetDateOptions(roots=[a.parent], new_date="2021-03-04 10:12"))
    assert [x.src for x in plan.actions] == [b]
    assert plan.cloud_only == [a]
    assert any("cloud-only" in n for n in plan.notes)


# ----- apply and undo -----------------------------------------------------------------------


def test_apply_without_exiftool_sets_the_modified_time_and_undo_restores(
    tmp_path: Path, journal: Journal, fake_trash: FakeBin
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    original, old_mtime = a.read_bytes(), a.stat().st_mtime_ns
    opts = SetDateOptions(roots=[a.parent], new_date="2021-03-04 10:12")
    plan, result = run(tmp_path, journal, opts)
    assert result.count("done") == 1, [o.detail for o in result.outcomes]
    assert a.read_bytes() == original  # nothing inside the file changed
    assert datetime.fromtimestamp(a.stat().st_mtime) == datetime(2021, 3, 4, 10, 12)
    assert len(fake_trash) == 1 and fake_trash[0].parent == a.parent  # the aside copy
    assert list(a.parent.iterdir()) == [a]
    rows = journal.actions(result.run_id)
    assert [(r.kind, r.status) for r in rows] == [("convert", "done"), ("trash", "done")]

    report = undo_run(
        journal, result.run_id, send_to_trash=fake_trash.trash, locate_bin=fake_trash.locate
    )
    assert report.reverted == 2, (report.skipped, report.failed)
    assert a.read_bytes() == original and a.stat().st_mtime_ns == old_mtime
    assert plan.actions


def test_apply_skips_a_file_that_went_cloud_only_since_the_scan(
    tmp_path: Path, journal: Journal, fake_trash: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    ctx = ctx_for(tmp_path, journal)
    plan = TOOL.scan(SetDateOptions(roots=[a.parent], new_date="2021-03-04 10:12"), ctx)
    assert len(plan.actions) == 1
    monkeypatch.setattr(onedrive, "get_sync_status", lambda _p: onedrive.SyncStatus.CLOUD_ONLY)
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1 and fake_trash == []
    assert a.exists()


def test_apply_refuses_a_file_outside_the_plan_roots(
    tmp_path: Path, journal: Journal, fake_trash: FakeBin
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    outside = jpeg(tmp_path / "other" / "b.jpg")
    ctx = ctx_for(tmp_path, journal)
    plan = TOOL.scan(SetDateOptions(roots=[a.parent], new_date="2021-03-04 10:12"), ctx)
    plan.actions[0].src = plan.actions[0].dst = outside
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0 and fake_trash == []
    assert outside.stat().st_size > 0


def test_apply_with_exiftool_writes_the_dates_and_undo_restores(
    tmp_path: Path, journal: Journal, fake_trash: FakeBin, exiftool_path: Path
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    original = a.read_bytes()
    opts = SetDateOptions(roots=[a.parent], mode="month_year", year=2021, month=3)
    plan, result = run(tmp_path, journal, opts, exiftool=exiftool_path)
    assert "modified time only" not in plan.actions[0].reason
    assert result.count("done") == 1, [o.detail for o in result.outcomes]
    assert exif.read_exif(a).capture_time == datetime(2021, 3, 4, 10, 12)
    assert datetime.fromtimestamp(a.stat().st_mtime) == datetime(2021, 3, 4, 10, 12)
    assert list(a.parent.iterdir()) == [a]

    report = undo_run(
        journal, result.run_id, send_to_trash=fake_trash.trash, locate_bin=fake_trash.locate
    )
    assert report.reverted == 2, (report.skipped, report.failed)
    assert a.read_bytes() == original


# ----- the library follows --------------------------------------------------------------------


def test_the_library_reads_the_changed_files_again(
    tmp_path: Path, journal: Journal, fake_trash: FakeBin, exiftool_path: Path
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    ctx = ctx_for(tmp_path, journal, exiftool=exiftool_path)
    with MediaLibrary(tmp_path / "lib.sqlite") as lib:
        lib.sync([a.parent], ctx)
        assert lib.items()[0].taken == datetime(2019, 7, 4, 10, 12)
        opts = SetDateOptions(paths=[a], new_date="2021-03-04 10:12")
        run(tmp_path, journal, opts, exiftool=exiftool_path)
        assert lib.refresh_paths([a], ctx) == 1
        assert [i.taken for i in lib.items()] == [datetime(2021, 3, 4, 10, 12)]
        a.unlink()
        lib.refresh_paths([a], ctx)
        assert lib.items() == []


def test_read_media_item_of_a_missing_or_foreign_file(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path, None)
    assert read_media_item(tmp_path / "gone.jpg", ctx) is None
    note = tmp_path / "note.txt"
    note.write_text("x")
    assert read_media_item(note, ctx) is None


# ----- review fixes ---------------------------------------------------------------------------


def test_applying_one_shift_plan_twice_shifts_once(
    tmp_path: Path, journal: Journal, fake_trash: FakeBin
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    opts = SetDateOptions(roots=[a.parent], mode="shift", shift_days=1)
    ctx = ctx_for(tmp_path, journal)
    plan = TOOL.scan(opts, ctx)
    TOOL.apply(plan, ctx)
    TOOL.apply(plan, ctx)
    # Without ExifTool the date read back is still the camera date, so the second apply runs;
    # it writes the planned date again and never a second shift.
    assert datetime.fromtimestamp(a.stat().st_mtime) == datetime(2019, 7, 5, 10, 12)


def test_a_file_whose_date_changed_since_the_preview_is_left_alone(
    tmp_path: Path, journal: Journal, fake_trash: FakeBin
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    ctx = ctx_for(tmp_path, journal)
    plan = TOOL.scan(SetDateOptions(roots=[a.parent], mode="shift", shift_days=1), ctx)
    jpeg(a, taken="2020:01:01 08:00:00")  # the camera date changed after the preview
    before = a.read_bytes()
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0 and result.count("failed") == 1
    assert "changed since the preview" in (result.outcomes[0].detail or "")
    assert a.read_bytes() == before and not fake_trash


def test_the_planned_dates_survive_the_plan_file(tmp_path: Path) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    plan = scan(tmp_path, SetDateOptions(roots=[a.parent], new_date="2021-03-04 10:12"))
    again = Plan.from_json(plan.to_json())
    assert again.actions[0].extra == plan.actions[0].extra
    assert plan.actions[0].extra["new_date"].startswith("2021-03-04T10:12")


def test_apply_writes_the_planned_date_not_a_recomputed_one(
    tmp_path: Path, journal: Journal, fake_trash: FakeBin
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    ctx = ctx_for(tmp_path, journal)
    plan = TOOL.scan(SetDateOptions(roots=[a.parent], mode="shift", shift_days=3), ctx)
    # The options saved with the plan are changed afterwards: the planned date still wins.
    plan.options_json = SetDateOptions(
        roots=[a.parent], mode="shift", shift_days=9
    ).model_dump_json()
    TOOL.apply(plan, ctx)
    assert datetime.fromtimestamp(a.stat().st_mtime) == datetime(2019, 7, 7, 10, 12)


def test_the_path_is_never_empty_and_undo_restores_the_original(
    tmp_path: Path, journal: Journal, fake_trash: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    original, old_mtime = a.read_bytes(), a.stat().st_mtime_ns
    seen: list[bool] = []
    real = fake_trash.trash

    def trash(path: str | os.PathLike[str]) -> None:
        seen.append(a.exists())  # the copy is already in place when the original is recycled
        assert Path(path) != a  # the aside name is recycled, not the path itself
        real(path)

    monkeypatch.setattr(plan_module, "send2trash", trash)
    _plan, result = run(tmp_path, journal, SetDateOptions(roots=[a.parent], new_date="2021-03-04"))
    assert result.count("done") == 1 and seen == [True]
    rows = journal.actions(result.run_id)
    trash_row = next(r for r in rows if r.kind == "trash")
    assert trash_row.src == str(a)  # journaled against the ORIGINAL path
    assert list(a.parent.iterdir()) == [a]
    report = undo_run(
        journal, result.run_id, send_to_trash=fake_trash.trash, locate_bin=fake_trash.locate
    )
    assert report.reverted == 2, (report.skipped, report.failed)
    assert a.read_bytes() == original and a.stat().st_mtime_ns == old_mtime


def test_a_failed_recycle_leaves_the_original_in_place(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg")
    original = a.read_bytes()

    def broken(_path: str | os.PathLike[str]) -> None:
        raise OSError("bin full")

    monkeypatch.setattr(plan_module, "send2trash", broken)
    _plan, result = run(tmp_path, journal, SetDateOptions(roots=[a.parent], new_date="2021-03-04"))
    assert result.count("failed") == 1
    assert a.read_bytes() == original
    assert list(a.parent.iterdir()) == [a]


def test_skipped_notes_follow_the_reason_colon_path_shape(tmp_path: Path) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg", taken="2099:12:31 23:00:00")
    plan = scan(tmp_path, SetDateOptions(roots=[a.parent], mode="shift", shift_days=36500))
    notes = [n for n in plan.notes if n.startswith("skipped:")]
    assert notes and notes[0].endswith(f": {a}")
    assert "range" in notes[0] or "between" in notes[0]


def test_the_tool_has_no_working_folder_field() -> None:
    assert TOOL.workspace_field is None
