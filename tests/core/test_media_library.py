"""The media library: incremental sync, cloud-only files, links, ordering and locations.

Every tree is generated under ``tmp_path``. Cloud-only files are simulated by replacing
``onedrive.get_sync_status``; reads are counted by replacing ``exif.read_exif``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from various_tools.core import onedrive
from various_tools.core.context import Cancelled, RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.media import exif, ffmpeg
from various_tools.core.media import library as library_module
from various_tools.core.media.library import (
    PHOTO_EXTS,
    VIDEO_EXTS,
    MediaItem,
    MediaLibrary,
    SyncReport,
    library_path,
)
from various_tools.core.settings import Settings

GPS_IFD = 0x8825
EXIF_IFD = 0x8769


def make_jpeg(
    path: Path,
    taken: str | None = "2024:05:06 07:08:09",
    gps: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None,
    size: tuple[int, int] = (32, 24),
) -> Path:
    """A small JPEG with an optional EXIF DateTimeOriginal and GPS position."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = Image.Exif()
    if taken is not None:
        data.get_ifd(EXIF_IFD)[0x9003] = taken
    if gps is not None:
        ifd = data.get_ifd(GPS_IFD)
        ifd[1], ifd[2] = "N", gps[0]
        ifd[3], ifd[4] = "W", gps[1]
    Image.new("RGB", size, (200, 100, 50)).save(path, "JPEG", exif=data.tobytes())
    return path


def bump(path: Path, seconds: float = 10.0) -> None:
    """Change the file's modified time so the library sees it as changed."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + seconds))


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[RunContext]:
    settings = Settings(cache_folder=tmp_path / "cache", data_folder=tmp_path / "data")
    context = RunContext(settings=settings, deps=ExternalTools(env={}))
    yield context
    context.close()


@pytest.fixture
def lib(tmp_path: Path) -> Iterator[MediaLibrary]:
    with MediaLibrary(tmp_path / "cache" / "media_library.sqlite") as library:
        yield library


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every path ``read_exif`` is called with, in order. The real function still runs."""
    seen: list[Path] = []
    real = exif.read_exif

    def counting(path: Path) -> exif.ExifInfo:
        seen.append(path)
        return real(path)

    monkeypatch.setattr(exif, "read_exif", counting)
    return seen


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Names (file names) reported as cloud-only. Everything else is local."""
    names: set[str] = set()
    real = onedrive.get_sync_status

    def status(path: Path) -> onedrive.SyncStatus:
        if Path(path).name in names:
            return onedrive.SyncStatus.CLOUD_ONLY
        return real(path)

    monkeypatch.setattr(onedrive, "get_sync_status", status)
    return names


def no_ffprobe(ctx: RunContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctx.deps, "path", lambda _name: None)


# ----- basics ----------------------------------------------------------------------------------


def test_extensions_and_library_path(tmp_path: Path) -> None:
    assert {".jpg", ".heic", ".cr3"} <= PHOTO_EXTS
    assert {".mp4", ".mov", ".mkv", ".m2ts"} <= VIDEO_EXTS
    assert not PHOTO_EXTS & VIDEO_EXTS
    settings = Settings(cache_folder=tmp_path / "c")
    assert library_path(settings) == tmp_path / "c" / "media_library.sqlite"


def test_first_sync_reads_photos_with_date_gps_and_size(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext
) -> None:
    root = tmp_path / "pics"
    make_jpeg(root / "a.jpg", gps=((37.0, 46.0, 30.0), (122.0, 25.0, 12.0)), size=(40, 30))
    make_jpeg(root / "sub" / "b.jpg", taken=None)
    (root / "notes.txt").write_text("x", encoding="utf-8")

    report = lib.sync([root], ctx)

    assert isinstance(report, SyncReport)
    assert (report.added, report.updated, report.removed, report.unchanged) == (2, 0, 0, 0)
    items = {item.path.name: item for item in lib.items()}
    assert set(items) == {"a.jpg", "b.jpg"}
    a = items["a.jpg"]
    assert isinstance(a, MediaItem)
    assert a.kind == "photo"
    assert a.taken == datetime(2024, 5, 6, 7, 8, 9)
    assert a.date_source == "camera"
    assert a.lat == pytest.approx(37.775, abs=1e-3)
    assert a.lon == pytest.approx(-122.42, abs=1e-3)
    assert (a.width, a.height) == (40, 30)
    assert a.cloud_only is False
    assert a.size == (root / "a.jpg").stat().st_size
    b = items["b.jpg"]
    assert b.date_source == "modified"
    assert b.taken == datetime.fromtimestamp((root / "sub" / "b.jpg").stat().st_mtime)
    assert b.lat is None and b.lon is None


def test_second_sync_with_no_changes_reads_nothing(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, reads: list[Path]
) -> None:
    root = tmp_path / "pics"
    make_jpeg(root / "a.jpg")
    make_jpeg(root / "b.jpg")
    lib.sync([root], ctx)
    assert len(reads) == 2
    reads.clear()

    report = lib.sync([root], ctx)

    assert reads == []
    assert (report.added, report.updated, report.removed, report.unchanged) == (0, 0, 0, 2)


def test_changed_file_is_read_again(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, reads: list[Path]
) -> None:
    root = tmp_path / "pics"
    make_jpeg(root / "a.jpg")
    changed = make_jpeg(root / "b.jpg")
    lib.sync([root], ctx)
    reads.clear()

    make_jpeg(changed, taken="2020:01:02 03:04:05")
    bump(changed)
    report = lib.sync([root], ctx)

    assert reads == [changed]
    assert report.updated == 1 and report.unchanged == 1
    item = next(i for i in lib.items() if i.path.name == "b.jpg")
    assert item.taken == datetime(2020, 1, 2, 3, 4, 5)


def test_deleted_file_row_is_removed_and_other_roots_are_kept(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext
) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    gone = make_jpeg(one / "gone.jpg")
    make_jpeg(one / "stays.jpg")
    make_jpeg(two / "other.jpg")
    lib.sync([one, two], ctx)

    gone.unlink()
    report = lib.sync([one], ctx)

    assert report.removed == 1
    assert sorted(i.path.name for i in lib.items()) == ["other.jpg", "stays.jpg"]
    assert [i.path.name for i in lib.items([two])] == ["other.jpg"]
    assert [i.path.name for i in lib.items([one])] == ["stays.jpg"]


def test_a_root_that_is_missing_keeps_its_rows(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext
) -> None:
    root = tmp_path / "drive"
    make_jpeg(root / "a.jpg")
    lib.sync([root], ctx)
    renamed = tmp_path / "unplugged"
    root.rename(renamed)

    report = lib.sync([root], ctx)

    assert report.removed == 0
    assert report.errors and "drive" in report.errors[0]
    assert len(lib.items([root])) == 1


def test_a_sibling_folder_with_a_common_prefix_is_not_under_the_root(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext
) -> None:
    make_jpeg(tmp_path / "pics" / "a.jpg")
    make_jpeg(tmp_path / "pics2" / "b.jpg")
    lib.sync([tmp_path / "pics", tmp_path / "pics2"], ctx)
    assert [i.path.name for i in lib.items([tmp_path / "pics"])] == ["a.jpg"]


def test_forget_removes_one_row(tmp_path: Path, lib: MediaLibrary, ctx: RunContext) -> None:
    root = tmp_path / "pics"
    a = make_jpeg(root / "a.jpg")
    make_jpeg(root / "b.jpg")
    lib.sync([root], ctx)
    lib.forget(a)
    assert [i.path.name for i in lib.items()] == ["b.jpg"]


def test_rows_survive_reopening(tmp_path: Path, ctx: RunContext) -> None:
    db = tmp_path / "lib.sqlite"
    make_jpeg(tmp_path / "pics" / "a.jpg")
    with MediaLibrary(db) as first:
        first.sync([tmp_path / "pics"], ctx)
    with MediaLibrary(db) as second:
        assert [i.path.name for i in second.items()] == ["a.jpg"]


# ----- OneDrive cloud-only (rule 3) ---------------------------------------------------------


def test_cloud_only_file_is_never_read_and_listed_with_modified_date(
    tmp_path: Path,
    lib: MediaLibrary,
    ctx: RunContext,
    reads: list[Path],
    cloud: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "pics"
    remote = make_jpeg(root / "remote.jpg", gps=((1.0, 0.0, 0.0), (2.0, 0.0, 0.0)))
    make_jpeg(root / "local.jpg")
    cloud.add("remote.jpg")
    opened: list[str] = []
    real_open = Image.open

    def spy_open(fp: object, *args: object, **kwargs: object) -> Image.Image:
        opened.append(str(fp))
        return real_open(fp, *args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(Image, "open", spy_open)

    report = lib.sync([root], ctx)

    assert remote not in reads
    assert not any("remote" in name for name in opened)
    assert report.cloud_only == 1
    item = next(i for i in lib.items() if i.path.name == "remote.jpg")
    assert item.cloud_only is True
    assert item.date_source == "modified (in OneDrive only)"
    assert item.taken == datetime.fromtimestamp(remote.stat().st_mtime)
    assert item.lat is None and item.lon is None and item.width is None


def test_cloud_only_video_is_not_probed(
    tmp_path: Path,
    lib: MediaLibrary,
    ctx: RunContext,
    cloud: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vids"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"not really a video")
    cloud.add("clip.mp4")
    monkeypatch.setattr(ctx.deps, "path", lambda _name: Path("C:/fake/ffprobe.exe"))
    probed: list[Path] = []
    monkeypatch.setattr(
        ffmpeg, "probe_video_info", lambda _p, path, **_k: probed.append(path) or ffmpeg.VideoInfo()
    )

    lib.sync([root], ctx)

    assert probed == []
    assert lib.items()[0].cloud_only is True


def test_cloud_only_file_that_becomes_local_is_read_on_next_sync(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, reads: list[Path], cloud: set[str]
) -> None:
    root = tmp_path / "pics"
    remote = make_jpeg(root / "remote.jpg")
    cloud.add("remote.jpg")
    lib.sync([root], ctx)
    assert reads == []

    cloud.clear()  # OneDrive downloaded it; size and mtime are unchanged
    report = lib.sync([root], ctx)

    assert reads == [remote]
    assert report.updated == 1
    item = lib.items()[0]
    assert item.cloud_only is False
    assert item.date_source == "camera"
    assert item.taken == datetime(2024, 5, 6, 7, 8, 9)


# ----- links (rule 4) ---------------------------------------------------------------------------


def _make_dir_link(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError, NotImplementedError:
        pass
    if os.name == "nt":
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    return False


def test_links_inside_the_root_are_not_followed(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext
) -> None:
    outside = tmp_path / "outside"
    make_jpeg(outside / "secret.jpg")
    root = tmp_path / "pics"
    make_jpeg(root / "mine.jpg")
    if not _make_dir_link(root / "link", outside):
        pytest.skip("cannot create a symlink or junction here")

    lib.sync([root], ctx)

    assert [i.path.name for i in lib.items()] == ["mine.jpg"]


# ----- videos and errors ------------------------------------------------------------------------


def test_video_uses_ffprobe_creation_time(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vids"
    root.mkdir()
    (root / "clip.mov").write_bytes(b"x" * 10)
    probe = Path("C:/fake/ffprobe.exe")
    monkeypatch.setattr(ctx.deps, "path", lambda name: probe if name == "ffprobe" else None)
    calls: list[tuple[Path, Path]] = []

    def fake_probe(ffprobe: Path, path: Path, **_kw: object) -> ffmpeg.VideoInfo:
        calls.append((ffprobe, path))
        return ffmpeg.VideoInfo(creation_time=datetime(2023, 8, 9, 10, 11, 12))

    monkeypatch.setattr(ffmpeg, "probe_video_info", fake_probe)

    lib.sync([root], ctx)

    assert calls == [(probe, root / "clip.mov")]
    item = lib.items()[0]
    assert item.kind == "video"
    assert item.taken == datetime(2023, 8, 9, 10, 11, 12)
    assert item.date_source == "video"


def test_missing_ffprobe_gives_modified_date_and_one_note(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vids"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"x")
    (root / "b.mp4").write_bytes(b"y")
    no_ffprobe(ctx, monkeypatch)

    report = lib.sync([root], ctx)

    assert len(report.errors) == 1
    assert "ffprobe" in report.errors[0]
    assert {i.date_source for i in lib.items()} == {"modified"}


def test_a_file_that_fails_to_read_gets_modified_date_and_sync_goes_on(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pics"
    make_jpeg(root / "bad.jpg")
    make_jpeg(root / "good.jpg")
    real = exif.read_exif

    def failing(path: Path) -> exif.ExifInfo:
        if path.name == "bad.jpg":
            raise OSError("broken disk sector")
        return real(path)

    monkeypatch.setattr(exif, "read_exif", failing)

    report = lib.sync([root], ctx)

    assert len(report.errors) == 1 and "bad.jpg" in report.errors[0]
    by_name = {i.path.name: i for i in lib.items()}
    assert by_name["bad.jpg"].date_source == "modified"
    assert by_name["good.jpg"].date_source == "camera"


def test_implausible_camera_date_falls_back_to_modified(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext
) -> None:
    make_jpeg(tmp_path / "pics" / "reset.jpg", taken="1970:01:01 00:00:00")
    lib.sync([tmp_path / "pics"], ctx)
    assert lib.items()[0].date_source == "modified"


def test_progress_is_reported_and_cancel_is_honoured(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext
) -> None:
    root = tmp_path / "pics"
    for n in range(3):
        make_jpeg(root / f"{n}.jpg")
    seen: list[tuple[int, int]] = []

    def progress(done: int, total: int, _message: str) -> None:
        seen.append((done, total))
        if done == 1:
            ctx.cancel()

    ctx.progress = progress
    with pytest.raises(Cancelled):
        lib.sync([root], ctx)
    assert seen and seen[-1][1] == 3
    # what was read before the cancel is kept; nothing was removed
    assert 1 <= len(lib.items()) < 3


# ----- queries ----------------------------------------------------------------------------------


def test_items_are_ordered_by_taken_then_path(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext
) -> None:
    root = tmp_path / "pics"
    make_jpeg(root / "c.jpg", taken="2022:01:01 00:00:00")
    make_jpeg(root / "b.jpg", taken="2021:06:01 00:00:00")
    make_jpeg(root / "a.jpg", taken="2022:01:01 00:00:00")
    make_jpeg(root / "z.jpg", taken="2021:06:01 00:00:00.5")
    lib.sync([root], ctx)
    assert [i.path.name for i in lib.items()] == ["b.jpg", "z.jpg", "a.jpg", "c.jpg"]


def test_items_without_a_date_come_last() -> None:
    def item(name: str, taken: datetime | None) -> MediaItem:
        return MediaItem(
            Path(name), "photo", 1, 0.0, taken, "camera", None, None, None, None, False
        )

    ordered = library_module.sort_items(
        [item("b", None), item("a", None), item("c", datetime(2020, 1, 1))]
    )
    assert [i.path.name for i in ordered] == ["c", "a", "b"]


def test_with_location_returns_only_items_with_gps(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext
) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    make_jpeg(one / "here.jpg", gps=((10.0, 0.0, 0.0), (20.0, 0.0, 0.0)))
    make_jpeg(one / "nowhere.jpg")
    make_jpeg(two / "there.jpg", gps=((30.0, 0.0, 0.0), (40.0, 0.0, 0.0)))
    lib.sync([one, two], ctx)

    assert sorted(i.path.name for i in lib.with_location()) == ["here.jpg", "there.jpg"]
    located = lib.with_location([one])
    assert [i.path.name for i in located] == ["here.jpg"]
    assert located[0].lat == pytest.approx(10.0)
    assert located[0].lon == pytest.approx(-20.0)


def test_file_that_goes_cloud_only_after_being_read_keeps_its_date_and_gps(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, reads: list[Path], cloud: set[str]
) -> None:
    root = tmp_path / "pics"
    make_jpeg(root / "a.jpg", gps=((37.0, 46.0, 30.0), (122.0, 25.0, 12.0)), size=(40, 30))
    lib.sync([root], ctx)
    assert len(reads) == 1

    cloud.add("a.jpg")  # "Free up space": same size and mtime, now online-only
    report = lib.sync([root], ctx)

    assert len(reads) == 1  # not read again (rule 3)
    assert report.cloud_only == 1
    item = lib.items()[0]
    assert item.cloud_only is True
    assert item.date_source == "camera"
    assert item.taken == datetime(2024, 5, 6, 7, 8, 9)
    assert item.lat == pytest.approx(37.775, abs=1e-3)
    assert (item.width, item.height) == (40, 30)
    assert len(lib.with_location()) == 1


# ----- nested roots (part 1, item 1) --------------------------------------------------------


def test_a_folder_inside_another_chosen_folder_is_read_and_counted_once(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, reads: list[Path]
) -> None:
    outer = tmp_path / "pics"
    inner = outer / "2024" / "trip"
    make_jpeg(inner / "a.jpg")
    make_jpeg(outer / "b.jpg")

    report = lib.sync([outer, inner, outer], ctx)

    assert report.added == 2
    assert sorted(p.name for p in reads) == ["a.jpg", "b.jpg"]
    assert len(lib.items([outer, inner])) == 2
    assert library_module.fold_roots([inner, outer]) == [outer]
    assert library_module.fold_roots([outer, tmp_path / "pics2"]) == [outer, tmp_path / "pics2"]


# ----- folders that cannot be listed (item 2) -------------------------------------------------


def test_a_subfolder_that_cannot_be_listed_keeps_its_rows(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pics"
    make_jpeg(root / "ok" / "a.jpg")
    make_jpeg(root / "locked" / "b.jpg")
    make_jpeg(root / "locked" / "deeper" / "c.jpg")
    lib.sync([root], ctx)
    assert len(lib.items()) == 3

    real_scandir = os.scandir

    def scandir(path: str) -> object:
        if Path(str(path).removeprefix("\\?\\")).name == "locked":
            raise PermissionError(13, "Access is denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    report = lib.sync([root], ctx)

    assert len(lib.items()) == 3
    assert report.removed == 0
    assert any("locked" in note and "could not be listed" in note for note in report.errors)

    monkeypatch.undo()
    (root / "locked" / "b.jpg").unlink()
    report = lib.sync([root], ctx)
    assert report.removed == 1
    assert sorted(i.path.name for i in lib.items()) == ["a.jpg", "c.jpg"]


# ----- RAW and video details (item 3) ---------------------------------------------------------


def test_raw_files_are_read_in_batches_with_gps_and_size(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "raws"
    root.mkdir()
    for n in range(7):
        (root / f"IMG_{n}.CR3").write_bytes(b"raw")
    exiftool = Path("C:/fake/exiftool.exe")
    monkeypatch.setattr(ctx.deps, "path", lambda name: exiftool if name == "exiftool" else None)
    monkeypatch.setattr(exif, "EXIFTOOL_BATCH_SIZE", 3)
    monkeypatch.setattr(library_module.exif, "EXIFTOOL_BATCH_SIZE", 3)
    calls: list[list[Path]] = []

    def fake(tool: Path, paths: list[Path], timeout: float, **_kw: object) -> dict[Path, object]:
        assert tool == exiftool
        calls.append(list(paths))
        return {
            p: exif.RawInfo(datetime(2022, 3, 4, 5, 6, 7), (48.85, 2.35), 6000, 4000)
            for p in paths
            if p.name != "IMG_6.CR3"
        }

    monkeypatch.setattr(exif, "exiftool_raw_info", fake)

    report = lib.sync([root], ctx)

    assert [len(c) for c in calls] == [3, 3, 1]
    assert report.added == 7
    items = {i.path.name: i for i in lib.items()}
    first = items["IMG_0.CR3"]
    assert (first.lat, first.lon, first.width, first.height) == (48.85, 2.35, 6000, 4000)
    assert first.taken == datetime(2022, 3, 4, 5, 6, 7)
    assert first.date_source == "camera"
    assert items["IMG_6.CR3"].lat is None and items["IMG_6.CR3"].date_source == "modified"


def test_cloud_only_raw_is_not_given_to_exiftool(
    tmp_path: Path,
    lib: MediaLibrary,
    ctx: RunContext,
    cloud: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "raws"
    root.mkdir()
    (root / "far.CR3").write_bytes(b"raw")
    cloud.add("far.CR3")
    monkeypatch.setattr(ctx.deps, "path", lambda _n: Path("C:/fake/tool.exe"))
    seen: list[Path] = []
    monkeypatch.setattr(
        exif, "exiftool_raw_info", lambda _t, paths, *_a, **_k: seen.extend(paths) or {}
    )

    lib.sync([root], ctx)

    assert seen == []
    assert lib.items()[0].cloud_only


def test_video_gets_place_and_size_from_ffprobe(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vids"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"x")
    monkeypatch.setattr(
        ctx.deps, "path", lambda n: Path("C:/fake/ffprobe") if n == "ffprobe" else None
    )
    monkeypatch.setattr(
        ffmpeg,
        "probe_video_info",
        lambda *_a, **_k: ffmpeg.VideoInfo(
            datetime(2023, 1, 2, 3, 4, 5), (37.422, -122.084), 1080, 1920
        ),
    )

    lib.sync([root], ctx)

    item = lib.items()[0]
    assert (item.lat, item.lon, item.width, item.height) == (37.422, -122.084, 1080, 1920)
    assert lib.with_location() == [item]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+37.4220-122.0840/", (37.422, -122.084)),
        ("+37.4220-122.0840+012.300/", (37.422, -122.084)),
        ("-33.8688+151.2093/", (-33.8688, 151.2093)),
        ("+3725.32-12205.04/", (37.422, -122.084)),
        ("+372519.2-1220502.4/", (37.422, -122.084)),
        ("+95.0-122.0/", None),
        ("nonsense", None),
        ("", None),
    ],
)
def test_iso6709_parsing(text: str, expected: tuple[float, float] | None) -> None:
    found = ffmpeg.parse_iso6709(text)
    if expected is None:
        assert found is None
    else:
        assert found == pytest.approx(expected, abs=1e-3)


def test_queued_raw_files_are_read_and_stored_before_a_cancel_is_honoured(
    tmp_path: Path, lib: MediaLibrary, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "raws"
    root.mkdir()
    for n in range(3):
        (root / f"IMG_{n}.CR3").write_bytes(b"raw")
    monkeypatch.setattr(
        ctx.deps, "path", lambda n: Path("C:/fake/exiftool.exe") if n == "exiftool" else None
    )

    def fake(_tool: Path, paths: list[Path], _timeout: float, **_kw: object) -> dict[Path, object]:
        return {p: exif.RawInfo(datetime(2022, 3, 4, 5, 6, 7), None, 10, 10) for p in paths}

    monkeypatch.setattr(exif, "exiftool_raw_info", fake)

    def progress(done: int, _total: int, _message: str) -> None:
        if done == 2:
            ctx.cancel()

    ctx.progress = progress
    with pytest.raises(Cancelled):
        lib.sync([root], ctx)
    stored = {i.path.name: i for i in lib.items()}
    assert sorted(stored) == ["IMG_0.CR3", "IMG_1.CR3"]  # queued, then read, then kept
    assert all(i.taken == datetime(2022, 3, 4, 5, 6, 7) for i in stored.values())
