"""The media catalog (plan §3.6): one SQLite row per file, replacing the Excel master log.

The catalog is a cache of what the scan read, so the scan may write to it (the one exception to
"scan never changes the disk", as for the hash cache). Moves change only the catalog and the
journal, through :meth:`Catalog.record_move` and :meth:`Catalog.record_failure`.

Rows are keyed by the normalised current path. A moved file's row follows it to its new place.
"""

from __future__ import annotations

import csv
import os
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

COLUMNS: Final = (
    "key",
    "path",
    "original_path",
    "file_name",
    "size",
    "mtime",
    "sync_status",
    "capture_time",
    "date_source",
    "camera_make",
    "camera_model",
    "width",
    "height",
    "gps_lat",
    "gps_lon",
    "destination",
    "status",
    "last_action",
    "error",
    "checked",
)

# Statuses that count as finished, so ``skip_processed`` may skip the file.
DONE_STATUSES: Final = frozenset({"moved", "organized"})

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS files (
    key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    original_path TEXT,
    file_name TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    sync_status TEXT,
    capture_time TEXT,
    date_source TEXT,
    camera_make TEXT,
    camera_model TEXT,
    width INTEGER,
    height INTEGER,
    gps_lat REAL,
    gps_lon REAL,
    destination TEXT,
    status TEXT NOT NULL,
    last_action TEXT,
    error TEXT,
    checked TEXT NOT NULL
);
"""


def path_key(path: Path | str) -> str:
    """The key used for matching: absolute, normalised and case-insensitive on Windows."""
    return os.path.normcase(os.path.abspath(str(path)))


@dataclass(frozen=True)
class Known:
    """What the catalog already knows about a file's current path."""

    status: str
    size: int | None
    mtime: float | None


def now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Catalog:
    """The SQLite catalog. Open it with ``with Catalog(path) as catalog:``."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(SCHEMA)

    def snapshot(self) -> dict[str, Known]:
        """Every known path, keyed by :func:`path_key`. Opens no media file."""
        rows = self._conn.execute("SELECT key, status, size, mtime FROM files").fetchall()
        return {key: Known(status, size, mtime) for key, status, size, mtime in rows}

    def write_scan(self, rows: Iterable[Mapping[str, object]]) -> None:
        """Insert or replace one row per file, in one transaction. The caller sets the keys."""
        placeholders = ", ".join(f":{column}" for column in COLUMNS)
        names = ", ".join(COLUMNS)
        statement = f"INSERT OR REPLACE INTO files ({names}) VALUES ({placeholders})"
        complete = [{column: row.get(column) for column in COLUMNS} for row in rows]
        if not complete:
            return
        with self._conn:
            self._conn.executemany(statement, complete)

    def record_move(self, src: Path, dst: Path) -> None:
        """A file was moved from ``src`` to ``dst``. Its row follows it."""
        stamp = now_text()
        cursor = self._conn.execute(
            "UPDATE files SET key = ?, path = ?, file_name = ?, destination = ?, status = 'moved', "
            "last_action = 'moved', error = NULL, checked = ? WHERE key = ?",
            (path_key(dst), str(dst), dst.name, str(dst), stamp, path_key(src)),
        )
        if cursor.rowcount == 0:
            self._insert_minimal(src, dst, "moved", None, stamp)
        self._conn.commit()

    def record_failure(self, src: Path, error: str) -> None:
        """A move from ``src`` failed. The row stays at ``src`` and records the error."""
        stamp = now_text()
        cursor = self._conn.execute(
            "UPDATE files SET status = 'error', last_action = 'failed', error = ?, checked = ? "
            "WHERE key = ?",
            (error, stamp, path_key(src)),
        )
        if cursor.rowcount == 0:
            self._insert_minimal(src, None, "error", error, stamp)
        self._conn.commit()

    def _insert_minimal(
        self, src: Path, dst: Path | None, status: str, error: str | None, stamp: str
    ) -> None:
        current = dst or src
        self._conn.execute(
            "INSERT OR REPLACE INTO files (key, path, original_path, file_name, status, "
            "last_action, error, destination, checked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                path_key(current),
                str(current),
                str(src),
                current.name,
                status,
                "moved" if status == "moved" else "failed",
                error,
                str(dst) if dst else None,
                stamp,
            ),
        )

    def status_of(self, path: Path) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM files WHERE key = ?", (path_key(path),)
        ).fetchone()
        return str(row[0]) if row else None

    def export_csv(self, target: Path) -> int:
        """Write every row to a CSV file, sorted by path. Returns the number of rows written."""
        rows = self._conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM files ORDER BY path COLLATE NOCASE"
        ).fetchall()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(COLUMNS)
            writer.writerows(rows)
        return len(rows)

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
