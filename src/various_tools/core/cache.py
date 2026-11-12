"""Versioned SQLite cache for per-file results, keyed by path, size and modification time.

Generalised from the duplicate finder's scan cache. Each row is valid only while the file's
size and mtime are unchanged. When the cache version changes (the hashing logic changed), the
whole cache is dropped on open. Every write commits at once, so an interrupted scan can resume.

Caches are the one exception to "scan never changes the disk" (AGENTS.md).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from various_tools.core.jsonutil import as_dict


def _key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


class FileCache:
    """A small key-value cache of JSON values, one row per file."""

    def __init__(self, path: Path, *, name: str, version: int) -> None:
        self._path = path
        self._name = name
        self._version = version
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._open_schema()

    def _open_schema(self) -> None:
        create_entries = (
            "CREATE TABLE IF NOT EXISTS entries (path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
            "mtime REAL NOT NULL, value TEXT NOT NULL)"
        )
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        stored = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
        if stored.get("version") != str(self._version) or stored.get("name") != self._name:
            # A different version or cache name means the stored values cannot be trusted.
            self._conn.execute("DROP TABLE IF EXISTS entries")
            self._conn.execute(create_entries)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?), ('name', ?)",
                (str(self._version), self._name),
            )
        else:
            self._conn.execute(create_entries)
        self._conn.commit()

    def get(self, path: Path, size: int, mtime: float) -> dict[str, Any] | None:
        """The cached value, or None when missing or when the file changed since it was stored."""
        row = self._conn.execute(
            "SELECT value FROM entries WHERE path = ? AND size = ? AND mtime = ?",
            (_key(path), size, mtime),
        ).fetchone()
        if row is None:
            return None
        return as_dict(json.loads(row[0]))

    def put(self, path: Path, size: int, mtime: float, value: Mapping[str, Any]) -> None:
        """Store (or replace) a value and commit at once."""
        self._conn.execute(
            "INSERT OR REPLACE INTO entries (path, size, mtime, value) VALUES (?, ?, ?, ?)",
            (_key(path), size, mtime, json.dumps(dict(value), sort_keys=True)),
        )
        self._conn.commit()

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
