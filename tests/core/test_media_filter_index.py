"""FilterIndex: the filter on precomputed strings, same answers as filter_items, fast at 200,000."""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from various_tools.core.media.library import (
    FilterIndex,
    MediaFilter,
    MediaItem,
    filter_items,
    sort_items,
)


def make_items(count: int) -> list[MediaItem]:
    start = datetime(2018, 1, 1)
    rng = random.Random(3)
    items: list[MediaItem] = []
    for i in range(count):
        taken = None if i % 97 == 0 else start + timedelta(minutes=i * 3)
        items.append(
            MediaItem(
                Path(f"C:/Pictures/{2018 + i % 6}/Album {i % 500}/IMG_{i:06d}.jpg"),
                "video" if rng.random() < 0.15 else "photo",
                1, 1.0, taken, "camera", None, None, None, None, False,
            )
        )  # fmt: skip
    return sort_items(items)


FILTERS = [
    MediaFilter(),
    MediaFilter(text="album 12"),
    MediaFilter(text="IMG_0001 ALBUM"),
    MediaFilter(kind="video"),
    MediaFilter(kind="photo", start=(2019, 3), end=(2020, 6)),
    MediaFilter(start=(2021, 1)),
    MediaFilter(end=(2018, 6)),
    MediaFilter(start=(2020, 6), end=(2019, 3)),  # reversed: swapped
    MediaFilter(folder=Path("C:/Pictures/2019")),
    MediaFilter(folder=Path("C:/Pictures/2019"), text="album 4", kind="video"),
    MediaFilter(text="nothing like this"),
]


@pytest.mark.parametrize("flt", FILTERS)
def test_index_gives_the_same_items_as_filter_items(flt: MediaFilter) -> None:
    items = make_items(3000)
    index = FilterIndex(items)
    assert [items[i] for i in index.select(flt)] == filter_items(items, flt)


def test_narrowing_from_a_previous_answer_gives_the_same_items() -> None:
    items = make_items(3000)
    index = FilterIndex(items)
    first = index.select(MediaFilter(text="alb"))
    second = MediaFilter(text="alb 12")
    assert index.select(second, among=first) == index.select(second)


def test_narrows_only_when_the_new_filter_asks_for_a_subset() -> None:
    f = MediaFilter(text="alb", kind="video")
    assert FilterIndex.narrows(f, MediaFilter(text="alb 12", kind="video"))
    assert FilterIndex.narrows(f, f)
    assert not FilterIndex.narrows(f, MediaFilter(text="al", kind="video"))
    assert not FilterIndex.narrows(f, MediaFilter(text="alb", kind="both"))
    assert not FilterIndex.narrows(f, MediaFilter(text="alb", kind="video", start=(2020, 1)))
    assert FilterIndex.narrows(MediaFilter(), MediaFilter(text="x", start=(2020, 1)))


def test_a_search_over_200_000_items_takes_well_under_a_second() -> None:
    items = make_items(200_000)
    index = FilterIndex(items)
    worst = 0.0
    for flt in (
        MediaFilter(text="album 12"),
        MediaFilter(text="img_00"),
        MediaFilter(kind="video", start=(2020, 1), end=(2021, 12)),
        MediaFilter(folder=Path("C:/Pictures/2020"), text="album"),
    ):
        t0 = time.perf_counter()
        index.select(flt)
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.25, f"a filter over 200,000 items took {worst * 1000:.0f} ms"
