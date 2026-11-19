# GUI data files

## world_land.json

Land outlines for the offline world map (`widgets/world_map.py`).

* Source: Natural Earth, `ne_110m_land` (1:110m physical vectors), GeoJSON copy at
  https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson
* Licence: public domain (https://www.naturalearthdata.com/about/terms-of-use/).
* Made by: reading the GeoJSON as plain JSON, keeping the outer ring of every polygon, rounding
  each coordinate to 0.1 degree, dropping repeated points, and writing compact JSON. The result
  is a list of rings; each ring is a list of `[lon, lat]` pairs. The raw download is not kept in
  the repository.
