"""The media library: when and where each photo and video was taken, kept up to date cheaply.

One SQLite file in the cache folder (``media_library.sqlite``) feeds the Timeline and the Map.
It is a cache, so writing it is allowed during a scan (AGENTS.md). :meth:`MediaLibrary.sync`
lists each folder with :func:`~various_tools.core.fs.walk` (which never follows junctions or
symlinks out of the folder, rule 4) and reads only files that are new or changed since the last
update, compared by size, modified time and OneDrive state. Rows of deleted files are dropped;
rows of folders that were not synced are kept.

Rule 3: a OneDrive cloud-only file is never opened. It is listed with its modified date, no
size in pixels and no GPS position. Once it is on the device, the next sync reads it.

No Qt here and no ``print``: progress and cancel go through the :class:`RunContext`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from various_tools.core import onedrive
from various_tools.core.context import RunContext
from various_tools.core.fs import FileInfo, walk
from various_tools.core.media import exif, ffmpeg
from various_tools.core.media.imaging import IMAGE_EXTS, RAW_EXTS
from various_tools.core.settings import Settings

log = logging.getLogger(__name__)

MEDIA_KINDS = Literal["photo", "video"]
PHOTO_EXTS: frozenset[str] = IMAGE_EXTS | RAW_EXTS
VIDEO_EXTS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".webm", ".3gp", ".mts", ".m2ts"}
)
MEDIA_EXTS: frozenset[str] = PHOTO_EXTS | VIDEO_EXTS

LIBRARY_FILE = "media_library.sqlite"
SCHEMA_VERSION = 1
_COMMIT_EVERY = 200

# Plain words for where a date came from (shown in the GUI).
FROM_CAMERA = "camera"
FROM_VIDEO = "video"
FROM_MODIFIED = "modified"
FROM_MODIFIED_CLOUD = "modified (in OneDrive only)"


@dataclass(frozen=True)
class MediaItem:
    path: Path
    kind: str  # "photo" | "video"
    size: int
    mtime: float
    taken: datetime | None  # capture time, else modified time; naive local time
    date_source: str  # plain words: "camera", "video", "modified", "modified (in OneDrive only)"
    lat: float | None
    lon: float | None
    width: int | None
    height: int | None
    cloud_only: bool


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    cloud_only: int = 0
    errors: list[str] = field(default_factory=list[str])


def library_path(settings: Settings) -> Path:
    """Where the library lives: the cache folder, so it is safe to delete."""
    return settings.resolved_cache_folder() / LIBRARY_FILE


def media_kind(path: Path) -> str | None:
    """``"photo"``, ``"video"`` or None, from the extension only."""
    suffix = path.suffix.lower()
    if suffix in PHOTO_EXTS:
        return "photo"
    if suffix in VIDEO_EXTS:
        return "video"
    return None


def sort_items(items: Iterable[MediaItem]) -> list[MediaItem]:
    """By date taken (items without one last), then by path."""
    return sorted(
        items, key=lambda i: (i.taken is None, i.taken or datetime.min, str(i.path).casefold())
    )


def _key(path: Path | str) -> str:
    """Comparison key: case-insensitive on Windows, normalised separators."""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _prefix(root: Path) -> str:
    key = _key(root)
    return key if key.endswith(os.sep) else key + os.sep


_COLUMNS = "key, path, kind, size, mtime, taken, date_source, lat, lon, width, height, cloud_only"


_PLACEHOLDERS = ", ".join("?" * len(_COLUMNS.split(",")))


class MediaLibrary:
    """The library file, opened once. Use as a context manager. One thread per instance."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._open_schema()

    def _open_schema(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            # An older layout cannot be trusted; it is only a cache, so it is rebuilt.
            self._conn.execute("DROP TABLE IF EXISTS items")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "key TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL, "
            "size INTEGER NOT NULL, mtime REAL NOT NULL, taken TEXT, date_source TEXT NOT NULL, "
            "lat REAL, lon REAL, width INTEGER, height INTEGER, cloud_only INTEGER NOT NULL)"
        )
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    # ----- context manager -------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ----- queries ---------------------------------------------------------------------------

    def items(self, roots: Sequence[Path] | None = None) -> list[MediaItem]:
        """Every item (or those under ``roots``), by date taken (None last), then path."""
        rows = self._conn.execute(f"SELECT {_COLUMNS} FROM items").fetchall()
        prefixes = [_prefix(r) for r in roots] if roots is not None else None
        found: list[MediaItem] = []
        for row in rows:
            if prefixes is not None and not any(str(row[0]).startswith(p) for p in prefixes):
                continue
            found.append(_item_from_row(row))
        return sort_items(found)

    def with_location(self, roots: Sequence[Path] | None = None) -> list[MediaItem]:
        """Items that have a GPS position, in the same order as :meth:`items`."""
        return [i for i in self.items(roots) if i.lat is not None and i.lon is not None]

    def forget(self, path: Path) -> None:
        """Drop one file's row (for example after the user deleted it from the app)."""
        self._conn.execute("DELETE FROM items WHERE key = ?", (_key(path),))
        self._conn.commit()

    # ----- sync ------------------------------------------------------------------------------

    def _known_under(self, root: Path) -> dict[str, tuple[int, float, bool]]:
        prefix = _prefix(root)
        rows = self._conn.execute(
            "SELECT key, size, mtime, cloud_only FROM items WHERE substr(key, 1, ?) = ?",
            (len(prefix), prefix),
        ).fetchall()
        return {str(k): (int(s), float(m), bool(c)) for k, s, m, c in rows}

    def sync(self, roots: Sequence[Path], ctx: RunContext) -> SyncReport:
        """Bring the rows under ``roots`` up to date. Reads only new or changed files.

        A folder that cannot be found (an unplugged drive) is reported in ``errors`` and its
        rows are kept. On cancel, what was read so far is kept and nothing is removed.
        """
        report = SyncReport()
        listed: list[tuple[Path, list[FileInfo]]] = []
        for root in roots:
            ctx.check_cancelled()
            if not os.path.isdir(root):
                report.errors.append(
                    f"{root}: folder not found, so its photos were kept as they were"
                )
                continue
            ctx.report(0, 0, f"Listing {root}")
            files: list[FileInfo] = []
            for info in walk(root, exts=MEDIA_EXTS):
                files.append(info)
                if len(files) % 500 == 0:
                    ctx.check_cancelled()
            listed.append((root, files))

        total = sum(len(files) for _root, files in listed)
        done = 0
        ffprobe_note = False
        try:
            for root, files in listed:
                known = self._known_under(root)
                seen: set[str] = set()
                for info in files:
                    ctx.check_cancelled()
                    key = _key(info.path)
                    seen.add(key)
                    status = onedrive.get_sync_status(info.path)
                    cloud = not onedrive.is_on_device(status)
                    old = known.get(key)
                    if old is not None and old == (info.size, info.mtime, cloud):
                        report.unchanged += 1
                    elif cloud and old is not None and old[:2] == (info.size, info.mtime):
                        # Read earlier, now "Free up space": same content, so keep what was read
                        # (date, GPS, size) and only record that it is online-only (rule 3).
                        self._conn.execute("UPDATE items SET cloud_only = 1 WHERE key = ?", (key,))
                        report.unchanged += 1
                    else:
                        item, note = _read_file(info, cloud, ctx, need_note=not ffprobe_note)
                        if note is not None:
                            if note.startswith("ffprobe"):
                                ffprobe_note = True
                            report.errors.append(note)
                        self._store(key, item)
                        if old is None:
                            report.added += 1
                        else:
                            report.updated += 1
                    if cloud:
                        report.cloud_only += 1
                    done += 1
                    ctx.report(done, total, info.path.name)
                    if done % _COMMIT_EVERY == 0:
                        self._conn.commit()
                gone = [k for k in known if k not in seen]
                self._conn.executemany("DELETE FROM items WHERE key = ?", [(k,) for k in gone])
                report.removed += len(gone)
                self._conn.commit()
        finally:
            self._conn.commit()
        return report

    def _store(self, key: str, item: MediaItem) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO items ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
            (
                key,
                str(item.path),
                item.kind,
                item.size,
                item.mtime,
                item.taken.isoformat() if item.taken is not None else None,
                item.date_source,
                item.lat,
                item.lon,
                item.width,
                item.height,
                int(item.cloud_only),
            ),
        )


def _read_file(
    info: FileInfo, cloud: bool, ctx: RunContext, *, need_note: bool
) -> tuple[MediaItem, str | None]:
    """The item for one file and an optional note for ``errors``. Never raises for a file."""
    base = MediaItem(
        info.path, media_kind(info.path) or "photo", info.size, info.mtime,
        _modified(info.mtime), FROM_MODIFIED, None, None, None, None, cloud,
    )  # fmt: skip
    if cloud:  # rule 3: never opened
        return replace(base, date_source=FROM_MODIFIED_CLOUD), None
    try:
        if base.kind == "video":
            return _read_video(base, ctx, need_note=need_note)
        return _read_photo(base, ctx), None
    except onedrive.CloudOnlyFileError:
        # It went cloud-only between the listing and the read: treat it as such.
        return replace(base, cloud_only=True, date_source=FROM_MODIFIED_CLOUD), None
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the update
        log.warning("Could not read %s: %s", info.path, exc)
        return base, f"{info.path}: could not read it ({exc}); used its modified date"


def _read_photo(base: MediaItem, ctx: RunContext) -> MediaItem:
    settings = ctx.settings
    data = exif.read_exif(base.path)
    taken = data.capture_time
    if taken is None and base.path.suffix.lower() in RAW_EXTS:
        exiftool = ctx.deps.path("exiftool")
        if exiftool is not None:
            taken = exif.exiftool_capture_time(
                exiftool, base.path, settings.exiftool_read_timeout_s
            )
    lat, lon = data.gps if data.gps is not None else (None, None)
    item = replace(base, lat=lat, lon=lon, width=data.width, height=data.height)
    if exif.is_plausible_date(taken, min_year=settings.min_plausible_year):
        return replace(item, taken=taken, date_source=FROM_CAMERA)
    return item


def _read_video(
    base: MediaItem, ctx: RunContext, *, need_note: bool
) -> tuple[MediaItem, str | None]:
    ffprobe = ctx.deps.path("ffprobe")
    if ffprobe is None:
        note = (
            "ffprobe was not found, so videos use their modified date. Install ffmpeg to see "
            "when they were filmed."
        )
        return base, note if need_note else None
    taken = ffmpeg.creation_time(ffprobe, base.path, timeout=ctx.settings.ffprobe_timeout_s)
    if exif.is_plausible_date(taken, min_year=ctx.settings.min_plausible_year):
        return replace(base, taken=taken, date_source=FROM_VIDEO), None
    return base, None


def _modified(mtime: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(mtime)
    except OverflowError, OSError, ValueError:
        return None


def _item_from_row(row: tuple[object, ...]) -> MediaItem:
    (_key_, path, kind, size, mtime, taken, source, lat, lon, width, height, cloud) = row
    return MediaItem(
        path=Path(str(path)),
        kind=str(kind),
        size=int(size),  # pyright: ignore[reportArgumentType]
        mtime=float(mtime),  # pyright: ignore[reportArgumentType]
        taken=datetime.fromisoformat(str(taken)) if taken else None,
        date_source=str(source),
        lat=float(lat) if lat is not None else None,  # pyright: ignore[reportArgumentType]
        lon=float(lon) if lon is not None else None,  # pyright: ignore[reportArgumentType]
        width=int(width) if width is not None else None,  # pyright: ignore[reportArgumentType]
        height=int(height) if height is not None else None,  # pyright: ignore[reportArgumentType]
        cloud_only=bool(cloud),
    )
