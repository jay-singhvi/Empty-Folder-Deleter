"""MediaFilter: search, kind, month range and folder."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from various_tools.core.media.library import MediaFilter, MediaItem, filter_items


def item(path: str, taken: datetime | None, kind: str = "photo") -> MediaItem:
    return MediaItem(Path(path), kind, 1, 0.0, taken, "camera", None, None, None, None, False)


ITEMS = [
    item("C:/pics/Paris/eiffel.jpg", datetime(2023, 5, 1)),
    item("C:/pics/Rome/colosseum.JPG", datetime(2024, 7, 9)),
    item("C:/pics/Rome/trip.mp4", datetime(2024, 8, 1), "video"),
    item("C:/other/undated.jpg", None),
]


def names(flt: MediaFilter) -> list[str]:
    return [i.path.name for i in filter_items(ITEMS, flt)]


def test_empty_filter_keeps_everything() -> None:
    assert MediaFilter().is_empty
    assert len(filter_items(ITEMS, MediaFilter())) == 4


def test_search_matches_name_or_folder_any_case_all_words() -> None:
    assert names(MediaFilter(text="EIFFEL")) == ["eiffel.jpg"]
    assert names(MediaFilter(text="rome")) == ["colosseum.JPG", "trip.mp4"]
    assert names(MediaFilter(text="rome trip")) == ["trip.mp4"]
    assert names(MediaFilter(text="nothing")) == []


def test_kind() -> None:
    assert names(MediaFilter(kind="video")) == ["trip.mp4"]
    assert len(names(MediaFilter(kind="photo"))) == 3
    assert len(names(MediaFilter(kind="both"))) == 4


def test_month_range_is_inclusive_and_drops_undated() -> None:
    assert names(MediaFilter(start=(2024, 7))) == ["colosseum.JPG", "trip.mp4"]
    assert names(MediaFilter(end=(2023, 5))) == ["eiffel.jpg"]
    assert names(MediaFilter(start=(2023, 5), end=(2024, 7))) == ["eiffel.jpg", "colosseum.JPG"]
    # a reversed range is read the right way round
    assert names(MediaFilter(start=(2024, 7), end=(2023, 5))) == ["eiffel.jpg", "colosseum.JPG"]


def test_folder_keeps_items_inside_it_only() -> None:
    assert names(MediaFilter(folder=Path("C:/pics/Rome"))) == ["colosseum.JPG", "trip.mp4"]
    assert names(MediaFilter(folder=Path("C:/pics"))) == ["eiffel.jpg", "colosseum.JPG", "trip.mp4"]
    assert names(MediaFilter(folder=Path("C:/pic"))) == []  # a prefix of a name is not a parent


def test_filters_combine() -> None:
    flt = MediaFilter(text="rome", kind="photo", start=(2024, 1))
    assert names(flt) == ["colosseum.JPG"]
