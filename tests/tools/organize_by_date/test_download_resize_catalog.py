"""Organize by date: the items the owner delegated after Phase 3.6.

* the catalog no longer crashes on scan -> apply -> undo -> scan -> apply;
* cloud-only files: ``skip`` (default), ``ask`` and ``download`` (rule 3: a download happens only
  for a row the user ticked, at apply, never during the scan);
* ``also_resize``: images are resized in place through media_resize's pipeline, the original goes
  to the Recycle Bin through a journaled row, and undo restores it;
* the catalog extras: export to Excel or CSV, and the one-time import of the old master log.

OneDrive is faked by patching the tool's ``get_sync_status``, ``force_download`` and
``unpin_file``. The Recycle Bin is a temp folder. Every file is generated under ``tmp_path``.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import struct
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook, load_workbook
from PIL import Image

from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.onedrive import SyncStatus
from various_tools.core.plan import ActionKind, Plan
from various_tools.core.settings import Settings
from various_tools.tools.base import ToolExtra
from various_tools.tools.organize_by_date import TOOL, OrganizeByDateOptions
from various_tools.tools.organize_by_date import catalog as catalog_module
from various_tools.tools.organize_by_date import dates as dates_module
from various_tools.tools.organize_by_date import tool as obd
from various_tools.tools.organize_by_date.catalog import Catalog

EXIF_IFD = 0x8769
DATE_ORIGINAL = 0x9003
_EPOCH_AS_FILETIME = 116444736000000000
TAKEN = "2024:10:05 14:02:11"


# ----- helpers -------------------------------------------------------------------------------


def _jpeg(path: Path, *, size: tuple[int, int] = (64, 48), taken: str | None = TAKEN) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, (80, 120, 160))
    if taken is None:
        image.save(path, format="JPEG")
    else:
        exif = Image.Exif()
        exif[EXIF_IFD] = {DATE_ORIGINAL: taken}
        image.save(path, format="JPEG", exif=exif.tobytes())
    return path


def _ctx(tmp_path: Path, journal: Journal | None = None, **settings: Any) -> RunContext:
    values: dict[str, Any] = {"onedrive_root": tmp_path, "data_folder": tmp_path / "appdata"}
    values.update(settings)
    return RunContext(
        settings=Settings.model_validate(values),
        deps=ExternalTools(ollama_probe=lambda _u: None),
        journal=journal,
    )


def _opts(tmp_path: Path, src: Path, dest: Path, **kwargs: Any) -> OrganizeByDateOptions:
    return OrganizeByDateOptions(
        sources=[src], dest_root=dest, catalog_path=tmp_path / "catalog.sqlite", **kwargs
    )


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


class FakeBin:
    """Stands in for the Recycle Bin: trashing writes a Windows-style ``$I``/``$R`` pair."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.trashed: list[Path] = []

    def trash(self, path: Path | str) -> None:
        path = Path(path)
        self.folder.mkdir(parents=True, exist_ok=True)
        content = path.read_bytes()
        key = uuid.uuid4().hex[:6].upper()
        (self.folder / f"$R{key}{path.suffix}").write_bytes(content)
        text = str(path) + "\x00"
        when = int(datetime.now(UTC).timestamp() * 10_000_000) + _EPOCH_AS_FILETIME
        header = struct.pack("<qqqi", 2, len(content), when, len(text))
        (self.folder / f"$I{key}{path.suffix}").write_bytes(header + text.encode("utf-16-le"))
        path.unlink()
        self.trashed.append(path)

    def locate(self, _path: Path) -> Path | None:
        return self.folder


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    with Journal(tmp_path / "journal.sqlite") as opened:
        yield opened


@pytest.fixture
def src(tmp_path: Path) -> Path:
    folder = tmp_path / "Source"
    folder.mkdir()
    return folder


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    return tmp_path / "Camera Roll"


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBin:
    bin_ = FakeBin(tmp_path / "fake-bin")
    monkeypatch.setattr(plan_module, "send2trash", bin_.trash)
    return bin_


class FakeOneDrive:
    """Fakes OneDrive for the tool: listed files are cloud-only until ``force_download``."""

    def __init__(self, *cloud: Path, download_ok: bool = True) -> None:
        self.cloud = {os.path.normcase(str(p)) for p in cloud}
        self.download_ok = download_ok
        self.downloads: list[tuple[Path, int]] = []
        self.unpinned: list[Path] = []
        self.allow_download = False

    def status(self, path: Path) -> SyncStatus:
        if os.path.normcase(str(path)) in self.cloud:
            return SyncStatus.CLOUD_ONLY
        return SyncStatus.LOCAL

    def force_download(self, path: Path, wait_seconds: int = 30) -> tuple[bool, str]:
        assert self.allow_download, "a download happened outside an explicit apply"
        self.downloads.append((path, wait_seconds))
        if not self.download_ok:
            return False, f"attrib did not finish download within {wait_seconds}s"
        self.cloud.discard(os.path.normcase(str(path)))
        return True, "downloaded"

    def unpin(self, path: Path) -> bool:
        self.unpinned.append(path)
        return True

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(obd, "get_sync_status", self.status)
        monkeypatch.setattr(obd, "force_download", self.force_download)
        monkeypatch.setattr(obd, "unpin_file", self.unpin)


def _never_read(monkeypatch: pytest.MonkeyPatch, cloud: Path) -> None:
    real_read = dates_module.read_exif

    def guarded(path: Path) -> object:
        assert not _same(path, cloud), "a cloud-only file was read"
        return real_read(path)

    monkeypatch.setattr(dates_module, "read_exif", guarded)


# ----- item 1: the catalog never crashes a run -----------------------------------------------


def test_apply_undo_scan_apply_again_does_not_crash_on_the_catalog(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    _jpeg(src / "a.jpg")
    ctx = _ctx(tmp_path, journal)
    first = TOOL.apply(TOOL.scan(_opts(tmp_path, src, dest), ctx), ctx)
    assert first.count("done") == 1
    assert undo_run(journal, first.run_id).reverted == 1
    assert (src / "a.jpg").is_file()

    second = TOOL.apply(TOOL.scan(_opts(tmp_path, src, dest), ctx), ctx)
    assert second.status == "done" and second.count("done") == 1
    moved = dest / "2024" / "2024 - 10" / "a.jpg"
    assert moved.is_file()
    with Catalog(tmp_path / "catalog.sqlite") as catalog:
        assert catalog.status_of(moved) == "moved"
        assert catalog.status_of(src / "a.jpg") is None


def test_a_catalog_error_never_stops_the_remaining_updates(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    _jpeg(src / "a.jpg")
    _jpeg(src / "b.jpg")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, src, dest), ctx)
    real = Catalog.record_move
    calls: list[str] = []

    def flaky(self: Catalog, source: Path, target: Path) -> None:
        calls.append(source.name)
        if source.name == "a.jpg":
            raise sqlite3.OperationalError("database is locked")
        real(self, source, target)

    monkeypatch.setattr(Catalog, "record_move", flaky)
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 2
    assert calls == ["a.jpg", "b.jpg"]
    assert any("catalog" in note and "a.jpg" in note for note in plan.notes)
    with Catalog(tmp_path / "catalog.sqlite") as catalog:
        assert catalog.status_of(dest / "2024" / "2024 - 10" / "b.jpg") == "moved"


def test_record_move_replaces_a_stale_row_at_the_destination(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    with Catalog(tmp_path / "c.sqlite") as catalog:
        catalog.write_scan(
            [
                {
                    "key": obd.path_key(p),
                    "path": str(p),
                    "file_name": p.name,
                    "status": "planned",
                    "checked": "x",
                }
                for p in (a, b)
            ]
        )
        catalog.record_move(a, b)
        assert catalog.status_of(b) == "moved"
        assert catalog.status_of(a) is None


# ----- item 2: cloud-only files ---------------------------------------------------------------


def test_skip_is_the_default_and_lists_the_file_in_plan_cloud_only(
    tmp_path: Path, src: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _jpeg(src / "cloud.jpg")
    drive = FakeOneDrive(cloud)
    drive.install(monkeypatch)
    _never_read(monkeypatch, cloud)
    assert OrganizeByDateOptions().cloud_only == "skip"
    plan = TOOL.scan(_opts(tmp_path, src, dest), _ctx(tmp_path))
    assert [_same(p, cloud) for p in plan.cloud_only] == [True]
    (row,) = plan.actions
    assert row.kind is ActionKind.MOVE and row.dst is None and row.selected is False


def test_ask_lists_an_unticked_download_row_and_nothing_is_downloaded_unticked(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _jpeg(src / "cloud.jpg")
    drive = FakeOneDrive(cloud)
    drive.install(monkeypatch)
    _never_read(monkeypatch, cloud)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, src, dest, cloud_only="ask"), ctx)
    (row,) = plan.actions
    assert row.kind is ActionKind.CONVERT and row.dst is None
    assert row.selected is False
    assert "downloaded" in row.reason
    assert plan.cloud_only == []

    drive.allow_download = True
    result = TOOL.apply(plan, ctx)
    assert result.outcomes == []
    assert drive.downloads == [] and cloud.exists()


def test_download_ticked_row_downloads_moves_unpins_and_undo_moves_it_back(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _jpeg(src / "cloud.jpg")
    drive = FakeOneDrive(cloud)
    drive.install(monkeypatch)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(
        _opts(tmp_path, src, dest, cloud_only="download", download_wait_seconds=12), ctx
    )
    (row,) = plan.actions
    assert row.selected is True  # "download" selects the rows; the option is locked

    drive.allow_download = True
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1, result.outcomes
    moved = dest / "2024" / "2024 - 10" / "cloud.jpg"
    assert moved.is_file() and not cloud.exists()
    assert [(p.name, wait) for p, wait in drive.downloads] == [("cloud.jpg", 12)]
    assert [_same(p, moved) for p in drive.unpinned] == [True]

    rows = journal.actions(result.run_id)
    assert [(r.kind, r.status) for r in rows] == [("convert", "done"), ("move", "done")]
    assert rows[1].dst is not None and _same(rows[1].dst, moved)
    with Catalog(tmp_path / "catalog.sqlite") as catalog:
        assert catalog.status_of(moved) == "moved"

    report = undo_run(journal, result.run_id)
    assert report.reverted == 1, (report.skipped, report.failed)
    assert cloud.is_file() and not moved.exists()


def test_keep_pinned_leaves_the_pin(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _jpeg(src / "cloud.jpg")
    drive = FakeOneDrive(cloud)
    drive.install(monkeypatch)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, src, dest, cloud_only="download", keep_pinned=True), ctx)
    drive.allow_download = True
    assert TOOL.apply(plan, ctx).count("done") == 1
    assert drive.unpinned == []


def test_a_failed_download_is_a_failed_row_and_the_file_stays(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _jpeg(src / "cloud.jpg")
    drive = FakeOneDrive(cloud, download_ok=False)
    drive.install(monkeypatch)
    _never_read(monkeypatch, cloud)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, src, dest, cloud_only="ask"), ctx)
    plan.actions[0].selected = True  # ticking the row is the consent
    drive.allow_download = True
    result = TOOL.apply(plan, ctx)
    assert [o.status for o in result.outcomes] == ["failed"]
    assert "download" in result.outcomes[0].detail and "within" in result.outcomes[0].detail
    assert cloud.exists() and not dest.exists()
    assert drive.unpinned == []
    with Catalog(tmp_path / "catalog.sqlite") as catalog:
        assert catalog.status_of(cloud) == "error"


def test_a_download_row_is_refused_when_the_options_say_skip(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _jpeg(src / "cloud.jpg")
    drive = FakeOneDrive(cloud)
    drive.install(monkeypatch)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, src, dest, cloud_only="ask"), ctx)
    # A saved plan edited by hand: the options now say "skip", but the row is ticked.
    edited = Plan.from_json(plan.to_json())
    edited.options_json = _opts(tmp_path, src, dest).model_dump_json()
    edited.actions[0].selected = True
    drive.allow_download = True
    result = TOOL.apply(edited, ctx)
    assert [o.status for o in result.outcomes] == ["failed"]
    assert drive.downloads == [] and cloud.exists()


def test_download_name_avoids_names_other_rows_will_take(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _jpeg(src / "a" / "IMG.jpg")
    _jpeg(src / "b" / "IMG.jpg")
    drive = FakeOneDrive(cloud)
    drive.install(monkeypatch)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, src, dest, cloud_only="download", recursive=True), ctx)
    drive.allow_download = True
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 2
    folder = dest / "2024" / "2024 - 10"
    assert sorted(p.name for p in folder.iterdir()) == ["IMG.jpg", "IMG_dup1.jpg"]


def test_cloud_options_carry_full_hints() -> None:
    fields = OrganizeByDateOptions.model_fields
    cloud = fields["cloud_only"].json_schema_extra
    assert isinstance(cloud, dict) and cloud.get("advanced") and cloud.get("locked")
    assert "download" in (fields["cloud_only"].description or "")
    assert OrganizeByDateOptions().download_wait_seconds == 30
    assert OrganizeByDateOptions().keep_pinned is False


# ----- item 3: also_resize ---------------------------------------------------------------------


def test_also_resize_resizes_in_place_then_moves_and_undo_restores_the_original(
    tmp_path: Path,
    src: Path,
    dest: Path,
    journal: Journal,
    fake_bin: FakeBin,
) -> None:
    photo = _jpeg(src / "big.jpg", size=(4032, 3024))
    original = photo.read_bytes()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, src, dest, also_resize=True), ctx)
    assert photo.read_bytes() == original  # the scan changes nothing
    kinds = [
        (a.kind, _same(a.src, photo), a.dst is not None and _same(a.dst, photo))
        for a in plan.actions
        if a.src is not None
    ]
    assert kinds == [(ActionKind.CONVERT, True, True), (ActionKind.MOVE, True, False)]
    assert "4032x3024 -> 1440x1080" in plan.actions[0].reason

    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 2, result.outcomes
    moved = dest / "2024" / "2024 - 10" / "big.jpg"
    with Image.open(moved) as out:
        assert out.size == (1440, 1080)  # the legacy shorter-side rule, 1080 px by default
    assert [p.name for p in fake_bin.trashed] == ["big.jpg"]
    assert [r.kind for r in journal.actions(result.run_id)] == ["convert", "trash", "move"]

    report = undo_run(
        journal, result.run_id, send_to_trash=fake_bin.trash, locate_bin=fake_bin.locate
    )
    assert report.reverted == 3, (report.skipped, report.failed)
    assert photo.read_bytes() == original
    assert not moved.exists()


def test_also_resize_never_enlarges_and_videos_are_only_moved(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    _jpeg(src / "small.jpg", size=(800, 600))
    clip = src / "clip.mp4"
    clip.write_bytes(b"not really a video")
    os.utime(clip, (datetime(2020, 1, 2).timestamp(), datetime(2020, 1, 2).timestamp()))
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, src, dest, also_resize=True), ctx)
    assert {a.kind for a in plan.actions} == {ActionKind.MOVE}
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 2
    with Image.open(dest / "2024" / "2024 - 10" / "small.jpg") as out:
        assert out.size == (800, 600)


def test_unticking_the_move_also_drops_its_resize(
    tmp_path: Path, src: Path, dest: Path, journal: Journal, fake_bin: FakeBin
) -> None:
    photo = _jpeg(src / "big.jpg", size=(4032, 3024))
    original = photo.read_bytes()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(tmp_path, src, dest, also_resize=True), ctx)
    for action in plan.actions:
        if action.kind is ActionKind.MOVE:
            action.selected = False
    result = TOOL.apply(plan, ctx)
    assert result.outcomes == []
    assert photo.read_bytes() == original and fake_bin.trashed == []
    assert any("resize" in note and "big.jpg" in note for note in plan.notes)


def test_also_resize_options_default_to_the_legacy_rule_and_media_resize() -> None:
    opts = OrganizeByDateOptions()
    assert opts.also_resize is False
    assert opts.resize_mode == "shorter_side"
    assert opts.resize_px == 1080
    assert opts.resize_jpeg_quality == 85


# ----- item 6: the catalog extras --------------------------------------------------------------


def _extra(extra_id: str) -> ToolExtra:
    return next(e for e in TOOL.extras if e.id == extra_id)


def test_the_tool_offers_the_two_catalog_extras() -> None:
    assert [(e.id, e.path_kind) for e in TOOL.extras] == [
        ("catalog-export", "save_file"),
        ("catalog-import", "open_file"),
    ]
    assert "*.xlsx" in _extra("catalog-export").file_filter
    assert "*.csv" in _extra("catalog-export").file_filter


def _catalog_with_rows(tmp_path: Path, src: Path, dest: Path, journal: Journal) -> RunContext:
    _jpeg(src / "a.jpg")
    _jpeg(src / "=HYPERLINK(1).jpg")
    ctx = _ctx(tmp_path, journal)
    opts = OrganizeByDateOptions(sources=[src], dest_root=dest)  # the catalog from the settings
    TOOL.apply(TOOL.scan(opts, ctx), ctx)
    return ctx


def test_catalog_export_to_excel_and_csv(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    ctx = _catalog_with_rows(tmp_path, src, dest, journal)
    out = tmp_path / "out" / "catalog.xlsx"
    message = _extra("catalog-export").run(out, ctx)
    assert "2" in message and out.is_file()
    sheet = load_workbook(out).active
    assert sheet is not None
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0][:4] == ("key", "path", "original_path", "file_name")
    names = sorted(str(r[3]) for r in rows[1:])
    assert names == ["=HYPERLINK(1).jpg", "a.jpg"]
    # A name starting with "=" stays text: no cell of the export is a formula.
    assert all(cell.data_type != "f" for row in sheet.iter_rows() for cell in row)

    out_csv = tmp_path / "out" / "catalog.csv"
    _extra("catalog-export").run(out_csv, ctx)
    with out_csv.open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.reader(handle))) == 3
    assert sorted(p.name for p in out.parent.iterdir()) == ["catalog.csv", "catalog.xlsx"]


def test_catalog_export_never_overwrites_and_needs_a_known_type(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    ctx = _catalog_with_rows(tmp_path, src, dest, journal)
    taken = tmp_path / "taken.csv"
    taken.write_text("keep me", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        _extra("catalog-export").run(taken, ctx)
    assert taken.read_text(encoding="utf-8") == "keep me"
    with pytest.raises(ValueError, match="xlsx"):
        _extra("catalog-export").run(tmp_path / "catalog.txt", ctx)
    with Catalog(tmp_path / "direct.sqlite") as catalog, pytest.raises(FileExistsError):
        catalog.export_csv(taken)
    assert taken.read_text(encoding="utf-8") == "keep me"


def _master_log(path: Path, rows: list[dict[str, Any]], sheet: str = "Metadata") -> Path:
    """A workbook shaped like the legacy Photo_Metadata_Master.xlsx."""
    columns = [
        "FileName", "Extension", "FolderPath", "CurrentFullPath", "OriginalFullPath",
        "SizeBytes", "SizeMB", "CreatedDate", "DateModified", "SyncStatus", "DateTaken",
        "CameraMake", "CameraModel", "ImageWidth", "ImageHeight", "GPSLatitude", "GPSLongitude",
        "DateUsed", "DateSource", "DestinationPath", "ConvertedFromFileName", "Action",
        "LastChecked", "ResizeAction", "ResizedWidth", "ResizedHeight",
    ]  # fmt: skip
    workbook = Workbook()
    ws: Any = workbook.active
    ws.title = sheet
    ws.append(columns)
    for row in rows:
        ws.append([row.get(column) for column in columns])
    workbook.save(path)
    return path


def test_catalog_import_adds_old_rows_and_keeps_newer_catalog_rows(
    tmp_path: Path, src: Path, dest: Path, journal: Journal
) -> None:
    ctx = _catalog_with_rows(tmp_path, src, dest, journal)
    kept = dest / "2024" / "2024 - 10" / "a.jpg"
    old = tmp_path / "Old" / "x.jpg"
    log = _master_log(
        tmp_path / "Photo_Metadata_Master.xlsx",
        [
            {
                "FileName": "x.jpg", "CurrentFullPath": str(old), "OriginalFullPath": str(old),
                "SizeBytes": 1234, "DateTaken": datetime(2019, 8, 1, 9, 0),
                "CameraMake": "Canon", "DateSource": "Date Taken (EXIF)", "Action": "Moved",
                "DestinationPath": str(old), "LastChecked": datetime(2025, 1, 1, 10, 0),
            },
            {
                "FileName": "a.jpg", "CurrentFullPath": str(kept), "Action": "Error: locked",
                "LastChecked": datetime(2025, 1, 1, 10, 0),
            },
            {"FileName": "nopath.jpg", "Action": "Moved"},
        ],
    )  # fmt: skip
    message = _extra("catalog-import").run(log, ctx)
    assert "1 added" in message and "2 skipped" in message
    with Catalog(ctx.settings.resolved_data_folder() / obd.CATALOG_FILE) as catalog:
        assert catalog.status_of(old) == "moved"
        assert catalog.status_of(kept) == "moved"  # the newer catalog row is not overwritten
    assert _extra("catalog-import").run(log, ctx).startswith("Imported 0")


def test_catalog_import_refuses_a_workbook_that_is_not_the_master_log(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    other = _master_log(tmp_path / "other.xlsx", [], sheet="Budget")
    with pytest.raises(ValueError, match="Metadata"):
        _extra("catalog-import").run(other, ctx)
    workbook = Workbook()
    ws: Any = workbook.active
    ws.title = "Metadata"
    ws.append(["Name", "Amount"])
    workbook.save(tmp_path / "wrong-columns.xlsx")
    with pytest.raises(ValueError, match="CurrentFullPath"):
        _extra("catalog-import").run(tmp_path / "wrong-columns.xlsx", ctx)
    garbage = tmp_path / "garbage.xlsx"
    garbage.write_bytes(b"not a workbook")
    with pytest.raises(ValueError):
        _extra("catalog-import").run(garbage, ctx)


def test_catalog_import_never_reads_a_cloud_only_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _master_log(tmp_path / "Photo_Metadata_Master.xlsx", [])
    monkeypatch.setattr(obd, "get_sync_status", lambda _p: SyncStatus.CLOUD_ONLY)

    def must_not_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cloud-only workbook was opened")

    monkeypatch.setattr(catalog_module, "load_workbook", must_not_open)
    with pytest.raises(ValueError, match="cloud-only"):
        _extra("catalog-import").run(log, _ctx(tmp_path))
