"""Offline place names: the nearest city to a GPS position.

The list is built from GeoNames ``cities15000`` (cities over 15,000 people) by
``packaging/make_places.py`` and ships as ``data/places.npz``. It is loaded on first use (under
a lock, so any thread may call :func:`lookup`) and never needs the network. Records are sorted by
one-degree cell; a lookup looks at the cells around the point and returns the nearest city within
:data:`MAX_KM`, or None (open sea, remote areas).

No Qt here and no ``print``.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from importlib import resources

import numpy as np
from numpy.typing import NDArray

PLACES_ATTRIBUTION = "Place names: GeoNames (geonames.org), CC BY 4.0"
MAX_KM = 50.0
_EARTH_KM = 6371.0088
_SCALE = 100  # the file stores hundredths of a degree
_CELLS = 180 * 360
_CLOSE_KM = 10.0


@dataclass(frozen=True)
class PlaceName:
    city: str
    region: str  # may be empty
    country: str

    @property
    def text(self) -> str:
        """For example "Paris, Île-de-France, France" (the region is left out when unknown)."""
        return ", ".join(part for part in (self.city, self.region, self.country) if part)


class PlaceIndex:
    """The loaded list and the nearest-city search over it."""

    def __init__(
        self,
        lat: NDArray[np.int16],
        lon: NDArray[np.int16],
        region: NDArray[np.uint16],
        country: NDArray[np.uint16],
        population: NDArray[np.uint32],
        cities: list[str],
        regions: list[str],
        countries: list[str],
        source: str = "",
    ) -> None:
        lat_deg = lat.astype(np.float64) / _SCALE
        lon_deg = lon.astype(np.float64) / _SCALE
        cell = (np.floor(lat_deg).astype(np.int64) + 90) * 360 + (
            np.floor(lon_deg).astype(np.int64) + 180
        )
        order = np.argsort(cell, kind="stable")  # the packaged file is sorted already
        self.lat_rad = np.radians(lat_deg[order])
        self.lon_rad = np.radians(lon_deg[order])
        self.cos_lat = np.cos(self.lat_rad)
        self.region = region[order]
        self.country = country[order]
        self.population = population[order]
        self.cities: list[str] = [cities[i] for i in order.tolist()]
        self.regions = regions
        self.countries = countries
        self.source = source
        self._starts = np.searchsorted(cell[order], np.arange(_CELLS + 1))

    def __len__(self) -> int:
        return len(self.cities)

    def nearest(self, lat: float, lon: float, max_km: float = MAX_KM) -> PlaceName | None:
        if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180:
            return None
        dlat = max_km / 111.0 + 0.02
        cos = math.cos(math.radians(lat))
        wide = cos < 0.05 or max_km / (111.0 * cos) >= 180
        dlon = 180.0 if wide else max_km / (111.0 * cos) + 0.02
        lat_cells = range(
            max(0, math.floor(lat - dlat) + 90), min(179, math.floor(lat + dlat) + 90) + 1
        )
        if dlon >= 180:
            lon_cells = list(range(360))
        else:
            first, last = math.floor(lon - dlon), math.floor(lon + dlon)
            lon_cells = sorted({(c + 180) % 360 for c in range(first, last + 1)})
        pieces: list[NDArray[np.int64]] = []
        for row in lat_cells:
            base = row * 360
            for col in lon_cells:
                a, b = int(self._starts[base + col]), int(self._starts[base + col + 1])
                if b > a:
                    pieces.append(np.arange(a, b))
        if not pieces:
            return None
        idx = np.concatenate(pieces)
        p1, l1 = math.radians(lat), math.radians(lon)
        a = (
            np.sin((self.lat_rad[idx] - p1) / 2) ** 2
            + math.cos(p1) * self.cos_lat[idx] * np.sin((self.lon_rad[idx] - l1) / 2) ** 2
        )
        km = 2 * _EARTH_KM * np.arcsin(np.sqrt(np.minimum(1.0, a)))
        nearest = float(km.min())
        if nearest > max_km:
            return None
        # A district of a big city can be a little nearer than the city itself: among the places
        # about as near as the nearest one, the largest wins.
        close = np.flatnonzero((km <= nearest + _CLOSE_KM) & (km <= max_km))
        best = int(close[np.argmax(self.population[idx[close]])])
        i = int(idx[best])
        return PlaceName(
            self.cities[i], self.regions[int(self.region[i])], self.countries[int(self.country[i])]
        )


def _names(blob: NDArray[np.uint8]) -> list[str]:
    return blob.tobytes().decode("utf-8").split("\n")


def load_index() -> PlaceIndex:
    """Read the packaged list (a few hundred KB)."""
    resource = resources.files("various_tools.core.media").joinpath("data", "places.npz")
    with resources.as_file(resource) as path, np.load(path, allow_pickle=False) as data:
        return PlaceIndex(
            data["lat"],
            data["lon"],
            data["region"],
            data["country"],
            data["population"],
            _names(data["cities"]),
            _names(data["regions"]),
            _names(data["countries"]),
            data["source"].tobytes().decode("utf-8"),
        )


_lock = threading.Lock()
_index: PlaceIndex | None = None


def get_index() -> PlaceIndex:
    """The shared index, loaded on first use (safe to call from several threads)."""
    global _index
    index = _index
    if index is None:
        with _lock:
            if _index is None:
                _index = load_index()
            index = _index
    return index


def lookup(lat: float, lon: float) -> PlaceName | None:
    """The nearest city within 50 km of this position, else None."""
    return get_index().nearest(lat, lon)
