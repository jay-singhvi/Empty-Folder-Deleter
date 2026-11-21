"""Build ``src/various_tools/core/media/data/places.npz`` from GeoNames.

Run by hand when the place list should be refreshed (``uv run python packaging/make_places.py``);
the result is committed. Downloads ``cities15000.zip``, ``admin1CodesASCII.txt`` and
``countryInfo.txt`` from https://download.geonames.org/export/dump/ (CC BY 4.0), keeps for each
city its name, region, country and position, and writes one compact compressed file: the
records are sorted by one-degree cell (and carry the population, so a city centre wins over a
district of it) (so the app needs no sorting), coordinates are whole
hundredths of a degree and every region and country name is stored once.

Only needs numpy. Prints the file size and the date of the source data.
"""

from __future__ import annotations

import io
import sys
import urllib.request
import zipfile
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np

BASE = "https://download.geonames.org/export/dump/"
USER_AGENT = "VariousTools-build"
OUT = Path(__file__).resolve().parents[1] / "src/various_tools/core/media/data/places.npz"
SCALE = 100  # coordinates are stored in hundredths of a degree (about 1 km)


def fetch(name: str) -> tuple[bytes, str]:
    """The bytes of one GeoNames file and its Last-Modified date as ``YYYY-MM-DD``."""
    request = urllib.request.Request(BASE + name, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed https URL
        data = response.read()
        modified = response.headers.get("Last-Modified")
    day = parsedate_to_datetime(modified).date().isoformat() if modified else ""
    return data, day or datetime.now().date().isoformat()


def blob(names: list[str]) -> np.ndarray:
    """Names joined by newlines, as bytes (a plain array, so no pickle is needed to load it)."""
    return np.frombuffer("\n".join(names).encode("utf-8"), dtype=np.uint8)


def main() -> int:
    cities_zip, date_cities = fetch("cities15000.zip")
    admin_raw, _ = fetch("admin1CodesASCII.txt")
    country_raw, _ = fetch("countryInfo.txt")

    countries: dict[str, str] = {}
    for line in country_raw.decode("utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        cols = line.split("\t")
        countries[cols[0]] = cols[4]
    regions: dict[str, str] = {}
    for line in admin_raw.decode("utf-8").splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            regions[cols[0]] = cols[1]

    with zipfile.ZipFile(io.BytesIO(cities_zip)) as archive:
        text = archive.read("cities15000.txt").decode("utf-8")

    rows: list[tuple[int, str, str, str, int, int, int]] = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 11:
            continue
        name, lat, lon, cc, admin1 = cols[1], float(cols[4]), float(cols[5]), cols[8], cols[10]
        population = int(cols[14]) if len(cols) > 14 and cols[14].isdigit() else 0
        country = countries.get(cc, cc)
        region = regions.get(f"{cc}.{admin1}", "")
        cell = (int(np.floor(lat)) + 90) * 360 + (int(np.floor(lon)) + 180)
        rows.append(
            (cell, name, region, country, round(lat * SCALE), round(lon * SCALE), population)
        )
    rows.sort()

    region_names = sorted({r[2] for r in rows})
    country_names = sorted({r[3] for r in rows})
    region_id = {n: i for i, n in enumerate(region_names)}
    country_id = {n: i for i, n in enumerate(country_names)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        OUT,
        lat=np.array([r[4] for r in rows], dtype=np.int16),
        lon=np.array([r[5] for r in rows], dtype=np.int16),
        population=np.array([min(r[6], 2**32 - 1) for r in rows], dtype=np.uint32),
        region=np.array([region_id[r[2]] for r in rows], dtype=np.uint16),
        country=np.array([country_id[r[3]] for r in rows], dtype=np.uint16),
        cities=blob([r[1] for r in rows]),
        regions=blob(region_names),
        countries=blob(country_names),
        source=blob([f"GeoNames cities15000 {date_cities}"]),
    )
    size = OUT.stat().st_size
    print(f"{len(rows)} places, {len(country_names)} countries, {size / 1024:.0f} KiB")
    print(f"source date: {date_cities}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
