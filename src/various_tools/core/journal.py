"""SQLite operation log. Each applied action is written before it runs, then marked done or failed.

Undo replays a run in reverse. Moves and renames are moved back. Trashed and deleted items can't
be restored by code, so they are reported for the user to restore from the Recycle Bin.

Action kinds and statuses are stored as plain strings, so this module does not depend on the
plan module.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    options_json TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    src TEXT,
    dst TEXT,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_by_run ON actions(run_id, seq);
"""

REVERSIBLE_KINDS = frozenset({"move", "rename"})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RunRecord:
    id: int
    tool: str
    options_json: str
    started: str
    finished: str | None
    status: str


@dataclass(frozen=True)
class ActionRecord:
    id: int
    run_id: int
    seq: int
    kind: str
    src: str | None
    dst: str | None
    reason: str
    status: str
    error: str | None
    ts: str


@dataclass(frozen=True)
class UndoReport:
    run_id: int
    reverted: int
    skipped: tuple[str, ...]
    failed: tuple[str, ...]

    @property
    def status(self) -> str:
        if self.reverted == 0:
            return "not undone"
        return "undone" if not self.skipped and not self.failed else "partially undone"


class Journal:
    """The run log. Writes are serialised with a lock, so worker threads can share one journal."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    def start_run(self, tool: str, options_json: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO runs (tool, options_json, started, status) "
                "VALUES (?, ?, ?, 'running')",
                (tool, options_json, _now()),
            )
            self._conn.commit()
            row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover - sqlite always returns the rowid for an INSERT
            raise RuntimeError("journal did not return a run id")
        return row_id

    def finish_run(self, run_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, finished = ? WHERE id = ?", (status, _now(), run_id)
            )
            self._conn.commit()

    def set_run_status(self, run_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
            self._conn.commit()

    def record_action(
        self,
        run_id: int,
        seq: int,
        kind: str,
        src: Path | None,
        dst: Path | None,
        reason: str = "",
        status: str = "pending",
        error: str | None = None,
    ) -> int:
        """Write an action before it runs. Returns its id."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO actions (run_id, seq, kind, src, dst, reason, status, error, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    seq,
                    kind,
                    str(src) if src is not None else None,
                    str(dst) if dst is not None else None,
                    reason,
                    status,
                    error,
                    _now(),
                ),
            )
            self._conn.commit()
            row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover
            raise RuntimeError("journal did not return an action id")
        return row_id

    def set_action(
        self,
        action_id: int,
        status: str,
        error: str | None = None,
        dst: Path | None = None,
    ) -> None:
        with self._lock:
            if dst is None:
                self._conn.execute(
                    "UPDATE actions SET status = ?, error = ?, ts = ? WHERE id = ?",
                    (status, error, _now(), action_id),
                )
            else:
                self._conn.execute(
                    "UPDATE actions SET status = ?, error = ?, dst = ?, ts = ? WHERE id = ?",
                    (status, error, str(dst), _now(), action_id),
                )
            self._conn.commit()

    def runs(self, limit: int = 50) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT id, tool, options_json, started, finished, status FROM runs "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [RunRecord(*row) for row in rows]

    def run(self, run_id: int) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT id, tool, options_json, started, finished, status FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return RunRecord(*row) if row else None

    def actions(self, run_id: int) -> list[ActionRecord]:
        rows = self._conn.execute(
            "SELECT id, run_id, seq, kind, src, dst, reason, status, error, ts FROM actions "
            "WHERE run_id = ? ORDER BY seq, id",
            (run_id,),
        ).fetchall()
        return [ActionRecord(*row) for row in rows]

    def close(self) -> None:
        with self._lock:
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


def undo_run(journal: Journal, run_id: int) -> UndoReport:
    """Reverse a run. Actions are replayed newest first.

    * ``move`` and ``rename``: moved back, if the original place is free and the moved file exists.
    * ``trash``, ``delete``, ``write`` and ``convert`` cannot be reversed here. They are listed in
      ``skipped`` so the user can restore them by hand (for example from the Recycle Bin).
    """
    reverted = 0
    skipped: list[str] = []
    failed: list[str] = []
    for action in reversed(journal.actions(run_id)):
        if action.status != "done":
            continue
        label = f"{action.kind}: {action.src or action.dst}"
        if action.kind not in REVERSIBLE_KINDS:
            skipped.append(f"{label} (not reversible by undo; restore manually)")
            continue
        if not action.src or not action.dst:
            skipped.append(f"{label} (missing paths)")
            continue
        src, dst = Path(action.src), Path(action.dst)
        if not os.path.lexists(dst):
            skipped.append(f"{label} (moved file is no longer there)")
            continue
        if os.path.lexists(src):
            skipped.append(f"{label} (original location is occupied)")
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
        except OSError as exc:
            failed.append(f"{label}: {exc}")
            continue
        journal.set_action(action.id, "undone")
        reverted += 1

    report = UndoReport(
        run_id=run_id,
        reverted=reverted,
        skipped=tuple(skipped),
        failed=tuple(failed),
    )
    if reverted:
        journal.set_run_status(run_id, report.status)
    return report
