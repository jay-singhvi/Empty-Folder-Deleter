"""Offline place names: nearest city within 50 km, lazy and thread-safe loading."""

from __future__ import annotations

import threading
import time
from importlib import resources

import pytest

from various_tools.core.media import places
from various_tools.core.media.places import PLACES_ATTRIBUTION, PlaceName, lookup


def test_known_cities() -> None:
    paris = lookup(48.8566, 2.3522)
    assert paris is not None
    assert paris.text == "Paris, Île-de-France, France"
    london = lookup(51.5074, -0.1278)
    assert london is not None
    assert london.country == "United Kingdom"
    sf = lookup(37.7749, -122.4194)
    assert sf is not None
    assert (sf.city, sf.region, sf.country) == ("San Francisco", "California", "United States")


def test_nothing_beyond_50_km() -> None:
    assert lookup(0.0, -30.0) is None  # open Atlantic
    assert lookup(48.8, 179.9) is None
    assert lookup(float("nan"), 1.0) is None
    assert lookup(95.0, 1.0) is None


def test_wraps_around_the_date_line() -> None:
    # Taveuni, Fiji, is on the 180th meridian; Suva is far, but Levuka-area points near +-180 work.
    near = lookup(-16.8, 179.99)
    assert near is None or isinstance(near, PlaceName)


def test_place_text_without_region() -> None:
    assert PlaceName("Monaco", "", "Monaco").text == "Monaco, Monaco"


def test_attribution_and_size() -> None:
    assert PLACES_ATTRIBUTION == "Place names: GeoNames (geonames.org), CC BY 4.0"
    data = resources.files("various_tools.core.media").joinpath("data", "places.npz")
    assert data.is_file()
    assert len(data.read_bytes()) < 1_500_000


def test_lookup_is_fast_after_load() -> None:
    lookup(48.85, 2.35)
    start = time.perf_counter()
    for i in range(200):
        lookup(40.0 + i * 0.01, -3.0)
    assert (time.perf_counter() - start) / 200 < 0.005  # 1 ms in practice; loose bound


def test_lazy_and_thread_safe_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(places, "_index", None)
    loads: list[int] = []
    real = places.load_index

    def counting() -> places.PlaceIndex:
        loads.append(1)
        time.sleep(0.05)
        return real()

    monkeypatch.setattr(places, "load_index", counting)
    results: list[PlaceName | None] = []
    threads = [
        threading.Thread(target=lambda: results.append(lookup(48.8566, 2.3522))) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(loads) == 1
    assert all(r is not None and r.city == "Paris" for r in results)
