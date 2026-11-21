"""Place names in the media library: stored with each row, migrated, filled without file reads."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.core.test_media_library import make_jpeg

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.media import exif
from various_tools.core.media.library import (
    FilterIndex,
    MediaFilter,
    MediaLibrary,
    filter_items,
)
from various_tools.core.media.places import PlaceName
from various_tools.core.settings import Settings


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    settings = Settings(cache_folder=tmp_path / "cache", data_folder=tmp_path / "data")
    return RunContext(settings=settings, deps=ExternalTools(env={}))


def test_sync_stores_the_place(tmp_path: Path, ctx: RunContext) -> None:
    root = tmp_path / "pics"
    # make_jpeg writes a west longitude: 122 d 25 m W is San Francisco.
    make_jpeg(root / "sf.jpg", gps=((37.0, 46.0, 30.0), (122.0, 25.0, 12.0)))
    make_jpeg(root / "none.jpg")
    with MediaLibrary(tmp_path / "cache" / "lib.sqlite") as lib:
        lib.sync([root], ctx)
        items = {i.path.name: i for i in lib.items()}
    assert items["sf.jpg"].place == PlaceName("San Francisco", "California", "United States")
    assert items["none.jpg"].place is None


def test_old_rows_are_migrated_and_filled_without_opening_files(
    tmp_path: Path, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pics"
    make_jpeg(root / "sf.jpg", gps=((37.0, 46.0, 30.0), (122.0, 25.0, 12.0)))
    db = tmp_path / "cache" / "lib.sqlite"
    with MediaLibrary(db) as lib:
        lib.sync([root], ctx)
    # Make it look like a 0.7 library: version 1, no place columns.
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE items DROP COLUMN place_city")
    conn.execute("ALTER TABLE items DROP COLUMN place_region")
    conn.execute("ALTER TABLE items DROP COLUMN place_country")
    conn.execute("ALTER TABLE items DROP COLUMN place_done")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    opened: list[Path] = []
    monkeypatch.setattr(exif, "read_exif", lambda p: opened.append(p))  # must not be called
    with MediaLibrary(db) as lib:
        assert [i.place for i in lib.items()] == [None]  # rows kept, not yet named
        report = lib.sync([root], ctx)
        assert report.unchanged == 1
        (item,) = lib.items()
    assert opened == []
    assert item.place is not None
    assert item.place.city == "San Francisco"


def _items(tmp_path: Path, ctx: RunContext):  # type: ignore[no-untyped-def]
    root = tmp_path / "pics"
    make_jpeg(root / "sf.jpg", gps=((37.0, 46.0, 30.0), (122.0, 25.0, 12.0)))
    make_jpeg(root / "oak.jpg", gps=((37.0, 48.0, 0.0), (122.0, 16.0, 0.0)))
    make_jpeg(root / "x.jpg")
    with MediaLibrary(tmp_path / "cache" / "lib.sqlite") as lib:
        lib.sync([root], ctx)
        return lib.items()


def test_place_filter_by_country_then_region(tmp_path: Path, ctx: RunContext) -> None:
    items = _items(tmp_path, ctx)
    index = FilterIndex(items)
    for flt in (
        MediaFilter(place_country="United States"),
        MediaFilter(place_country="United States", place_region="California"),
    ):
        got = [items[i].path.name for i in index.select(flt)]
        assert got == [i.path.name for i in filter_items(items, flt)]
        assert sorted(got) == ["oak.jpg", "sf.jpg"]
    assert index.select(MediaFilter(place_country="France")) == []
    assert index.select(MediaFilter(place_country="United States", place_region="Texas")) == []


def test_duplicates_only_filter_uses_the_index(tmp_path: Path, ctx: RunContext) -> None:
    items = _items(tmp_path, ctx)
    index = FilterIndex(items)
    index.duplicates = frozenset({1})
    assert index.select(MediaFilter(duplicates_only=True)) == [1]
    assert FilterIndex.narrows(MediaFilter(), MediaFilter(duplicates_only=True))
    assert not FilterIndex.narrows(MediaFilter(duplicates_only=True), MediaFilter())
    with pytest.raises(ValueError):
        filter_items(items, MediaFilter(duplicates_only=True))
