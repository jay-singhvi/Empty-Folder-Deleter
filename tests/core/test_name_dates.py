"""Dates read from photo and video file names (core/media/name_dates.py)."""

from __future__ import annotations

from datetime import datetime

import pytest

from various_tools.core.media.name_dates import NameDate, date_from_name

NOW = datetime(2026, 11, 22, 12, 0, 0)


def found(name: str, min_year: int = 1990) -> NameDate | None:
    return date_from_name(name, min_year=min_year, now=NOW)


@pytest.mark.parametrize(
    ("name", "when", "has_time"),
    [
        # the owner's example (Samsung camera)
        ("20220624_005115.mp4", datetime(2022, 6, 24, 0, 51, 15), True),
        ("20220624_005115(0).jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        # WhatsApp
        ("IMG-20220624-WA0001.jpg", datetime(2022, 6, 24), False),
        ("VID-20220624-WA0012.mp4", datetime(2022, 6, 24), False),
        ("PTT-20220624-WA0003.opus", datetime(2022, 6, 24), False),
        ("WhatsApp Image 2022-06-24 at 00.51.15.jpeg", datetime(2022, 6, 24, 0, 51, 15), True),
        ("WhatsApp Video 2022-06-24 at 13.05.09.mp4", datetime(2022, 6, 24, 13, 5, 9), True),
        (
            "WhatsApp Image 2022-06-24 at 1.05.09 PM.jpeg",
            datetime(2022, 6, 24, 13, 5, 9),
            True,
        ),
        (
            "WhatsApp Image 2022-06-24 at 12.05.09 AM (2).jpeg",
            datetime(2022, 6, 24, 0, 5, 9),
            True,
        ),
        # Android and other cameras
        ("IMG_20220624_005115.jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        ("VID_20220624_005115.mp4", datetime(2022, 6, 24, 0, 51, 15), True),
        ("MVIMG_20220624_005115.jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        ("PXL_20220624_005115123.jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        ("PXL_20220624_005115123.MP.jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        ("DJI_20220624005115_0001_D.JPG", datetime(2022, 6, 24, 0, 51, 15), True),
        ("Screenshot_20220624-005115.png", datetime(2022, 6, 24, 0, 51, 15), True),
        ("Screenshot_20220624-005115_WhatsApp.jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        ("signal-2022-06-24-005115.jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        ("signal-2022-06-24-005115_002.jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        # dashed
        ("2022-06-24 00.51.15.jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        ("photo_2022-06-24_00-51-15.jpg", datetime(2022, 6, 24, 0, 51, 15), True),
        ("Screenshot 2022-06-24 at 00.51.15.png", datetime(2022, 6, 24, 0, 51, 15), True),
        ("2022-06-24.jpg", datetime(2022, 6, 24), False),
        # a time that is not a real time of day: the date alone
        ("IMG_20220624_256199.jpg", datetime(2022, 6, 24), False),
    ],
)
def test_recognised_names(name: str, when: datetime, has_time: bool) -> None:
    result = found(name)
    assert result is not None
    assert (result.when, result.has_time) == (when, has_time)


@pytest.mark.parametrize(
    "name",
    [
        "IMG_1234.HEIC",  # iPhone
        "DJI_0001.JPG",  # older DJI
        "GOPR0123.MP4",
        "20221399_005115.jpg",  # month 13
        "20220231_005115.jpg",  # 31 February
        "1656031875.jpg",  # a 10-digit timestamp
        "IMG_120220624.jpg",  # 9 digits: part of a longer number
        "19700101_000000.jpg",  # a reset clock, before the earliest year
        "20991231_120000.jpg",  # in the future
        "holiday.mp4",
        "",
    ],
)
def test_names_without_a_date(name: str) -> None:
    assert found(name) is None


def test_the_earliest_year_is_respected() -> None:
    assert found("IMG-19950101-WA0001.jpg", min_year=2000) is None
    assert found("IMG-19950101-WA0001.jpg", min_year=1990) == NameDate(datetime(1995, 1, 1), False)


def test_today_is_accepted_and_tomorrow_plus_one_is_not() -> None:
    assert found("IMG-20261122-WA0001.jpg") == NameDate(datetime(2026, 11, 22), False)
    assert found("IMG-20261123-WA0001.jpg") == NameDate(datetime(2026, 11, 23), False)
    assert found("IMG-20261124-WA0001.jpg") is None


def test_the_first_date_in_the_name_wins() -> None:
    assert found("IMG_20220624_005115 copy 20230101.jpg") == NameDate(
        datetime(2022, 6, 24, 0, 51, 15), True
    )


def test_a_folder_part_is_not_read() -> None:
    assert found(r"C:\2019\IMG_1234.jpg") is None


def test_pixel_names_are_marked_as_utc() -> None:
    pixel = found("PXL_20220624_005115123.jpg")
    assert pixel is not None and pixel.utc
    samsung = found("20220624_005115.jpg")
    assert samsung is not None and not samsung.utc
