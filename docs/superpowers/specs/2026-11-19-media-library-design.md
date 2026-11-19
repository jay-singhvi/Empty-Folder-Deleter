# Photos & videos timeline, map, and two new tools

Date: 2026-11-19. Builds on branch `simpler-tool-page` (v0.4.0). The owner said "continue", so
the calls below are the builder's, made the safer way where the owner did not decide
(AGENTS.md: "pick the safer option").

## Intent

The owner wants to browse their photos and videos: a **timeline** (by date) and an **earth
map** (by GPS location), both built once and then kept up to date **incrementally** (only new or
changed files are read; deleted files drop out). Every item opens in its default app
(photo viewer or editor, video player), and can be shown in File Explorer. The app should also
grow with useful tools found in similar apps (Czkawka, PowerToys): **Empty files** and **Fix
file extensions**. The user is a non-technical teenager, so words are plain (gui/wording.py).

## Decisions (builder's calls)

* The timeline and the map are **pages, not tools**: they never change files, so they have no
  Scan/Apply. A new sidebar section **"Photos & videos"** holds *Timeline* and *Map*.
* One **media library** (SQLite in the cache folder, `media_library.sqlite`) feeds both. It is
  a cache, so updating it is allowed (the "scan never changes the disk" exception for caches).
* **OneDrive cloud-only files are never read** (rule 3): they are listed with their modified
  date, no thumbnail, no GPS, marked "in OneDrive only". When such a file is later on the
  device, the next update reads it.
* **The map works offline by default**: a world outline drawn from Natural Earth 1:110m land
  polygons (public domain), shipped as a small JSON file. **Online map tiles (OpenStreetMap)
  are off by default** and switched on in Settings, because they send the viewed area to a
  third party. With tiles on, requests carry an app User-Agent, tiles are cached on disk, and
  the map shows "© OpenStreetMap contributors".
* New tools follow the plugin contract and AGENTS.md: Recycle Bin, preview first, journaled,
  cloud-only files never read.

## Parts

### A. Media library + Timeline (core and GUI)

`src/various_tools/core/media/library.py` (no Qt):

```python
MEDIA_KINDS = Literal["photo", "video"]
PHOTO_EXTS  # IMAGE_EXTS | RAW_EXTS from core/media/imaging
VIDEO_EXTS  # {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".webm", ".3gp", ".mts", ".m2ts"}

@dataclass(frozen=True)
class MediaItem:
    path: Path
    kind: str                # "photo" | "video"
    size: int
    mtime: float
    taken: datetime | None   # capture time, else modified time; naive local time
    date_source: str         # plain words: "camera", "video", "modified", "modified (in OneDrive only)"
    lat: float | None
    lon: float | None
    width: int | None
    height: int | None
    cloud_only: bool

@dataclass
class SyncReport:
    added: int = 0; updated: int = 0; removed: int = 0; unchanged: int = 0
    cloud_only: int = 0; errors: list[str] = field(default_factory=list)

def library_path(settings: Settings) -> Path  # resolved_cache_folder() / "media_library.sqlite"

class MediaLibrary:            # context manager; one connection
    def __init__(self, path: Path) -> None
    def sync(self, roots: Sequence[Path], ctx: RunContext) -> SyncReport
    def items(self, roots: Sequence[Path] | None = None) -> list[MediaItem]   # by taken (None last), then path
    def with_location(self, roots: Sequence[Path] | None = None) -> list[MediaItem]
    def forget(self, path: Path) -> None
```

* `sync` walks each root with `core/fs.walk` (never follows junctions or symlinks out of the
  root), compares `(size, mtime)` and the stored sync status with each row, reads only new or
  changed files, and deletes rows under the given roots whose file is gone. Rows for other
  roots are kept. It reports progress and honours cancel through `ctx`.
* Photos: `exif.read_exif` (capture time, GPS, size). Videos: `ffmpeg.creation_time` with
  `ctx.deps.path("ffprobe")` when available (missing ffprobe: modified date, one note in
  `errors`). A file that fails to read gets its modified date and an `errors` line; sync goes on.
* Cloud-only (`onedrive.get_sync_status`): never opened; see Decisions.

`src/various_tools/gui/widgets/media_grid.py`: `MediaGrid(QWidget)`, a thumbnail grid
(`QListView` icon mode) reused by the Map page.

```python
class MediaGrid(QWidget):
    countChanged = Signal(int)
    def set_items(self, items: Sequence[MediaItem]) -> None
    def items(self) -> list[MediaItem]
```

* Thumbnails load lazily (only visible ones) in a worker with `thumbs.thumbnail_jpeg`, kept in
  an in-memory LRU; cloud-only items show a cloud placeholder and are never read. The caption
  is the file name and date.
* Double-click opens the item in its default app (`gui/reveal.open_path`). Right-click:
  *Open*, *Edit* (Windows "edit" verb through `os.startfile(path, "edit")`, photos only,
  disabled when it fails), *Show in File Explorer* (`gui/reveal.show_in_explorer`), *Copy
  path*. Hooks are replaceable in tests, like `PlanTable.reveal`.

`src/various_tools/gui/timeline_page.py`: `TimelinePage(QWidget)`.

* Step text in plain words: "1 Choose your photo folders" (a folder list; stored in a new
  `Settings.library_folders: list[Path] = []` with title and description), "2 Update the
  timeline" (button; runs `MediaLibrary.sync` in a `tool_worker`, cancellable), then the
  timeline: left a tree *Year > Month (count)*, right a `MediaGrid` of that month. A summary
  line: "1,234 photos and 56 videos. Updated: 12 new, 3 removed."
* On open, it shows what the library already has (no update needed to browse).
* `TimelinePage(state: AppState)`, attribute names: `folders`, `update_button`, `months`
  (`QTreeWidget`), `grid` (`MediaGrid`), `status_label`, `update_library() -> Worker | None` (not `update`, which is Qt's repaint).

### B. World map widget

`src/various_tools/gui/widgets/world_map.py` and `src/various_tools/gui/data/world_land.json`.

* Pure functions (unit-tested, no Qt): `lonlat_to_world(lon, lat) -> (x, y)` in Web
  Mercator 0..1, `world_to_lonlat`, `tile_url(template, z, x, y) -> str`, and
  `cluster(points, zoom, cell_px) -> list[Cluster]` (`Cluster(lon, lat, members: list[object])`).
* `WorldMap(QWidget)`: `set_points(points: Sequence[tuple[float, float, object]])` (lat, lon,
  payload), drag to pan, wheel and double-click to zoom (zoom 1..18), clusters drawn as
  circles with counts, signal `clusterClicked(list)` with the payloads. Land polygons drawn
  from the JSON (lon/lat rings) in both themes. `set_tile_source(url_template: str | None,
  cache_dir: Path | None, user_agent: str)`: when set, fetch raster tiles with
  `QNetworkAccessManager` (User-Agent header), cache to `cache_dir/z/x/y.png`, draw them under
  the clusters, and draw the attribution "© OpenStreetMap contributors". `None` means offline.
* The JSON is built once from Natural Earth `ne_110m_land` (public domain), with coordinates
  rounded to 0.1°; record the source and licence in `src/various_tools/gui/data/README.md`.
  Make sure the data file is included in the wheel and the PyInstaller spec.

### C. Two new tools

Both are normal plugins (`tools/<id>/`, entry point in `pyproject.toml`, `docs/tools/<id>.md`,
`scan_label` and `found_nouns`, options with title and description per AGENTS.md).

* **`empty_files`**, "Empty Files", category "Clean up": files of 0 bytes, planned as TRASH.
  Options: folders, never touch (default `.git, node_modules`), keep names (default
  `__init__.py, .gitkeep, .keep, py.typed, .nojekyll`; these are empty on purpose), minimum
  age in days (default 0), skip hidden and system files (default on). The size comes from the
  directory listing; nothing is opened. Words: "Look for empty files", ("empty file", "empty
  files").
* **`wrong_extension`**, "Fix File Extensions", category "Organize": finds files whose content
  does not match their extension (for example a PNG named `.jpg`, a HEIC named `.jpg`, a
  ZIP-based `.docx` named `.zip` is fine) by reading the first 64 bytes, and plans a RENAME to
  the right extension (`unique_destination` when taken). Signatures: jpg, png, gif, webp, bmp,
  tiff, heic/heif, avif, pdf, zip (and docx/xlsx/pptx as zip), mp4/mov/m4v (ftyp brands), mkv/
  webm, avi, wav, mp3, flac, ogg, 7z, rar, gz. Unknown content: leave alone. Cloud-only files
  are skipped and reported ("skipped: cloud-only"). Options: folders, never touch, which
  families to check (photos, videos, audio, documents, archives; default all). Words: "Look
  for wrong extensions", ("file to rename", "files to rename").

### Integration (done by the lead after A, B, C)

Settings `map_online_tiles: bool = False`, `map_tile_url` (default
`https://tile.openstreetmap.org/{z}/{x}/{y}.png`); `MapPage` (WorldMap + MediaGrid of the
clicked cluster); sidebar section "Photos & videos"; CHANGELOG, README, screenshots, version
0.5.0; full checks.

## Non-goals

No face recognition, no albums, no editing inside the app, no reverse geocoding (place names),
no upload anywhere.
