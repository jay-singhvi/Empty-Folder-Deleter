"""The media catalog (plan §3.6): one SQLite row per file, replacing the Excel master log.

The catalog is a cache of what the scan read, so the scan may write to it (the one exception to
"scan never changes the disk", as for the hash cache). Moves change only the catalog and the
journal, through :meth:`Catalog.record_move` and :meth:`Catalog.record_failure`.

Rows are keyed by the normalised current path. A moved file's row follows it to its new place,
replacing any older row that was keyed at that place (for example after an undo and a new run).

:meth:`Catalog.export_csv` and :meth:`Catalog.export_xlsx` write a new file and never replace one.
:meth:`Catalog.import_master_log` reads the legacy ``Photo_Metadata_Master.xlsx`` (sheet
``Metadata``, the legacy column names) once, and adds only paths the catalog does not know yet.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Final, Self

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils.exceptions import InvalidFileException

from various_tools.core.fs import long_path, rename_no_replace

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

# The legacy master log (python-extract_file_metadata-8.py: MASTER_LOG_PATH, sheet "Metadata").
MASTER_SHEET: Final = "Metadata"
MASTER_REQUIRED: Final = ("FileName", "CurrentFullPath", "Action")
EXPORT_SHEET: Final = "Catalog"
EXCEL_CELL_LIMIT: Final = 32767  # Excel's longest cell text

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
    date_source: str | None = None  # the plan label of the date the last scan used


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
        rows = self._conn.execute(
            "SELECT key, status, size, mtime, date_source FROM files"
        ).fetchall()
        return {
            key: Known(status, size, mtime, source) for key, status, size, mtime, source in rows
        }

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
        """A file was moved from ``src`` to ``dst``. Its row follows it.

        A row already keyed at ``dst`` describes a file that is no longer there (the move would
        not have run over it), for example the row of an earlier run that was undone. It is
        replaced, so the key stays unique.
        """
        stamp = now_text()
        old, new = path_key(src), path_key(dst)
        with self._conn:
            if new != old:
                self._conn.execute("DELETE FROM files WHERE key = ?", (new,))
            cursor = self._conn.execute(
                "UPDATE files SET key = ?, path = ?, file_name = ?, destination = ?, "
                "status = 'moved', last_action = 'moved', error = NULL, checked = ? WHERE key = ?",
                (new, str(dst), dst.name, str(dst), stamp, old),
            )
            if cursor.rowcount == 0:
                self._insert_minimal(src, dst, "moved", None, stamp)

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

    def _all_rows(self) -> list[tuple[Any, ...]]:
        return self._conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM files ORDER BY path COLLATE NOCASE"
        ).fetchall()

    def export_csv(self, target: Path) -> int:
        """Write every row to a new CSV file, sorted by path. Returns the number of rows written.

        Never replaces a file: raises :class:`FileExistsError` when ``target`` exists.
        """
        rows = self._all_rows()
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
        data = buffer.getvalue().encode("utf-8-sig")
        _write_new_file(target, lambda handle: handle.write(data))
        return len(rows)

    def export_xlsx(self, target: Path) -> int:
        """Write every row to a new Excel workbook (sheet ``Catalog``). Returns the row count.

        Text stays text (a file name starting with ``=`` is not a formula), and characters Excel
        cannot store are dropped. Never replaces a file: raises :class:`FileExistsError`.
        """
        rows = self._all_rows()

        def write(handle: IO[bytes]) -> None:
            workbook = Workbook(write_only=True)
            sheet = workbook.create_sheet(EXPORT_SHEET)
            sheet.append(list(COLUMNS))
            for row in rows:
                sheet.append([_excel_cell(sheet, value) for value in row])
            workbook.save(handle)

        _write_new_file(target, write)
        return len(rows)

    def import_master_log(self, workbook_path: Path) -> tuple[int, int]:
        """Add the rows of the legacy master log that the catalog does not have yet.

        Rows already in the catalog are never overwritten: the catalog replaced the log, so its
        rows are the newer ones. Rows without a current path are skipped. Returns
        ``(added, skipped)``. Raises :class:`ValueError` when the file is not an Excel workbook,
        or has no ``Metadata`` sheet with the legacy columns. The caller checks that the file is
        on this device first (AGENTS.md rule 3).
        """
        name = workbook_path.name
        try:
            workbook = load_workbook(workbook_path, read_only=True, data_only=True)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, InvalidFileException) as exc:
            raise ValueError(f"{name} could not be opened as an Excel workbook ({exc}).") from exc
        try:
            if MASTER_SHEET not in workbook.sheetnames:
                raise ValueError(
                    f"{name} does not look like the old master log: it has no "
                    f"'{MASTER_SHEET}' sheet."
                )
            values = workbook[MASTER_SHEET].iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in next(values, ())]
            missing = [column for column in MASTER_REQUIRED if column not in header]
            if missing:
                raise ValueError(
                    f"{name} does not look like the old master log: the '{MASTER_SHEET}' sheet "
                    f"has no {', '.join(missing)} column."
                )
            records: list[dict[str, object]] = []
            skipped = 0
            for cells in values:
                if all(cell is None for cell in cells):
                    continue
                record = {
                    column: cells[i] if i < len(cells) else None
                    for i, column in enumerate(header)
                    if column
                }
                row = _from_master_log(record)
                if row is None:
                    skipped += 1
                else:
                    records.append(row)
        finally:
            workbook.close()
        placeholders = ", ".join(f":{column}" for column in COLUMNS)
        statement = f"INSERT OR IGNORE INTO files ({', '.join(COLUMNS)}) VALUES ({placeholders})"
        before = self._conn.total_changes
        with self._conn:
            self._conn.executemany(statement, records)
        added = self._conn.total_changes - before
        return added, skipped + len(records) - added

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


# ----- export and import helpers ---------------------------------------------------------------


def _write_new_file(target: Path, write: Callable[[IO[bytes]], object]) -> None:
    """Write a new file at ``target`` through an exclusively created ``.partial`` file.

    Never replaces anything: :class:`FileExistsError` when ``target`` exists, before or at the
    final rename. A failed write leaves no file behind.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(long_path(target)):
        raise FileExistsError(f"{target} already exists; it was not replaced")
    partial = target.parent / f".{target.name}.{uuid.uuid4().hex[:8]}.partial"
    try:
        with open(long_path(partial), "xb") as handle:
            write(handle)
        rename_no_replace(partial, target)
    finally:
        if os.path.lexists(long_path(partial)):
            os.remove(long_path(partial))


def _excel_cell(sheet: Any, value: object) -> object:
    """A value for the write-only sheet. Text is always stored as text, never as a formula."""
    if not isinstance(value, str):
        return value
    cell = WriteOnlyCell(sheet, value=ILLEGAL_CHARACTERS_RE.sub("", value)[:EXCEL_CELL_LIMIT])
    cell.data_type = "s"
    return cell


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: object, kind: Callable[[Any], Any]) -> Any:
    if value is None or isinstance(value, bool):
        return None
    try:
        return kind(value)
    except TypeError, ValueError:
        return None


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return _text(value)


def _checked(value: object) -> str:
    """The legacy ``LastChecked`` (local time) as UTC text, or now when it is missing."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    return now_text()


def _status_from_action(action: str) -> tuple[str, str | None]:
    """The catalog status for a legacy ``Action`` value, and the error text if it failed."""
    if action == "Moved":
        return "moved", None
    if action == "Already organized (no move needed)":
        return "organized", None
    if action.startswith("Skipped"):
        return "skipped", None
    if action.startswith("Error"):
        return "error", action
    return "imported", None


def _from_master_log(record: Mapping[str, object]) -> dict[str, object] | None:
    """One catalog row from one row of the legacy master log, or None without a path.

    ``mtime`` is left empty, so ``skip_processed`` never skips an imported row on a guessed
    timestamp: the next scan reads the file once and records it exactly.
    """
    current = _text(record.get("CurrentFullPath"))
    if current is None:
        return None
    action = _text(record.get("Action")) or ""
    status, error = _status_from_action(action)
    return {
        "key": path_key(current),
        "path": current,
        "original_path": _text(record.get("OriginalFullPath")) or current,
        "file_name": _text(record.get("FileName")) or Path(current).name,
        "size": _number(record.get("SizeBytes"), int),
        "mtime": None,
        "sync_status": _text(record.get("SyncStatus")),
        "capture_time": _iso(record.get("DateTaken")),
        "date_source": _text(record.get("DateSource")),
        "camera_make": _text(record.get("CameraMake")),
        "camera_model": _text(record.get("CameraModel")),
        "width": _number(record.get("ImageWidth"), int),
        "height": _number(record.get("ImageHeight"), int),
        "gps_lat": _number(record.get("GPSLatitude"), float),
        "gps_lon": _number(record.get("GPSLongitude"), float),
        "destination": _text(record.get("DestinationPath")),
        "status": status,
        "last_action": f"imported: {action}" if action else "imported",
        "error": error,
        "checked": _checked(record.get("LastChecked")),
    }
