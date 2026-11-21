# Place data

## places.npz

Offline place names for the media library (`core/media/places.py`).

* Source: GeoNames, `cities15000.zip` (cities over 15,000 people), `admin1CodesASCII.txt` and
  `countryInfo.txt` from https://download.geonames.org/export/dump/ (downloaded 2026-11-21).
* Licence: Creative Commons Attribution 4.0 (https://creativecommons.org/licenses/by/4.0/).
  Credit: "Place names: GeoNames (geonames.org), CC BY 4.0" (`PLACES_ATTRIBUTION`).
* Made by: `packaging/make_places.py`. It keeps each city's name, region, country, position
  (hundredths of a degree) and population, sorted by one-degree cell, and writes a compressed
  numpy archive. The raw downloads are not kept in the repository.
