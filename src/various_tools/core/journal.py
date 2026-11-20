"""SQLite operation log. Each applied action is written before it runs, then marked done or failed.

Undo replays a run in reverse (:func:`undo_run`):

* ``move`` and ``rename`` are moved back, never over anything, and never into a protected path
  or through a junction or symlink. A rename that only changed letter case is reversed too.
* ``trash`` is restored from the Recycle Bin when the item can be found there exactly
  (:mod:`various_tools.core.recycle`); otherwise it is listed for the user to restore by hand.
* the file a ``write`` or ``convert`` produced goes to the Recycle Bin, but only when its size
  and modification time still match what was recorded at apply time; otherwise it is listed.
  When the action also recycled originals (extra ``trash`` rows with the same ``seq``, from
  ``plan.trash_extra``), those are restored first, and the output is recycled only when every
  one of them is back; otherwise the output stays and the originals are listed for manual
  restore. An output that replaced its original at the same path is moved aside, the original
  restored, and only then is the output recycled. A handler that returned no output gets
  nothing recycled; its journaled side effects (a move, say) are undone as their own rows.
* ``delete`` (permanent) cannot be undone and is listed.

Rows left ``pending`` by a crash are offered too, when the disk shows the action happened (for a
move: the destination exists and the original place is free). Undone rows are marked
``undone``, so undoing a run twice changes nothing the second time.

The database is opened in WAL mode with a busy timeout, so the GUI and the CLI can both write to
it at the same time. Action kinds and statuses are plain strings, so this module does not depend
on the plan module.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat as stat_module
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

from send2trash import send2trash

from various_tools.core import fs, recycle
from various_tools.core.fs import (
    DEFAULT_PROTECTION,
    ProtectedPaths,
    destination_refusal,
    is_case_only_rename,
    long_path,
    protected_reason,
)

log = logging.getLogger(__name__)

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
    ts TEXT NOT NULL,
    item_size INTEGER,
    item_mtime_ns INTEGER
);
CREATE INDEX IF NOT EXISTS actions_by_run ON actions(run_id, seq);
"""

# Columns added after the first release, with their types. Older journals get them on open.
_ADDED_COLUMNS = (("item_size", "INTEGER"), ("item_mtime_ns", "INTEGER"))
_ACTION_COLUMNS = (
    "id, run_id, seq, kind, src, dst, reason, status, error, ts, item_size, item_mtime_ns"
)
BUSY_TIMEOUT_S = 30.0

# Kinds that undo moves back. Undo also restores ``trash`` rows and recycles unchanged
# ``write``/``convert`` outputs when it can: see UNDOABLE_KINDS.
REVERSIBLE_KINDS = frozenset({"move", "rename"})
UNDOABLE_KINDS = frozenset({"move", "rename", "trash", "write", "convert"})
# How far the Recycle Bin's deletion time may be from the journaled time of a trash row.
RECYCLE_SLACK = timedelta(minutes=2)


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
    # For a trash row: the trashed file before it went. For write/convert: the output after it
    # was written. None when unknown (a folder, or a journal written before these existed).
    item_size: int | None = None
    item_mtime_ns: int | None = None


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
    """The run log. Calls are serialised with a lock, so worker threads can share one journal.

    Several ``Journal`` objects (the GUI and the CLI, for example) may use the same file at once.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, check_same_thread=False)
        self._conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        present = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(actions)")}
        for name, kind in _ADDED_COLUMNS:
            if name not in present:
                self._conn.execute(f"ALTER TABLE actions ADD COLUMN {name} {kind}")
        run_columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(runs)")}
        if "heartbeat" not in run_columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN heartbeat TEXT")

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

    def touch_run(self, run_id: int) -> None:
        """Record that the run is still alive (an apply that is paused or waiting between
        batches writes no actions). The undo rule's staleness reads this time."""
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET heartbeat = ? WHERE id = ? AND finished IS NULL",
                (_now(), run_id),
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

    def set_item_stat(self, action_id: int, size: int, mtime_ns: int) -> None:
        """Record the size and modification time of the item an action concerns (see
        :class:`ActionRecord`). Undo uses them to prove an item is the one it touched."""
        with self._lock:
            self._conn.execute(
                "UPDATE actions SET item_size = ?, item_mtime_ns = ? WHERE id = ?",
                (size, mtime_ns, action_id),
            )
            self._conn.commit()

    def runs(self, limit: int = 50) -> list[RunRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tool, options_json, started, finished, status FROM runs "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [RunRecord(*row) for row in rows]

    def run(self, run_id: int) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, tool, options_json, started, finished, status FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return RunRecord(*row) if row else None

    def actions(self, run_id: int, limit: int | None = None, offset: int = 0) -> list[ActionRecord]:
        """The actions of a run in order. ``limit``/``offset`` page through a long run."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_ACTION_COLUMNS} FROM actions WHERE run_id = ? "
                "ORDER BY seq, id LIMIT ? OFFSET ?",
                (run_id, -1 if limit is None else max(0, limit), max(0, offset)),
            ).fetchall()
        return [ActionRecord(*row) for row in rows]

    def action_count(self, run_id: int) -> int:
        """How many actions a run has, without loading them."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM actions WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def status_counts(self, run_id: int) -> dict[str, int]:
        """How many actions of a run have each status, e.g. ``{"done": 3, "failed": 1}``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM actions WHERE run_id = ? GROUP BY status",
                (run_id,),
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

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


def _parse_ts(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class _Undo:
    """One undo pass over a run. Collects the report lines as it goes."""

    def __init__(
        self,
        journal: Journal,
        rows: list[ActionRecord],
        protection: ProtectedPaths,
        send_to_trash: Callable[[Path], None],
        locate_bin: recycle.BinLocator | None,
    ) -> None:
        self.journal = journal
        self.rows = rows
        self.handled: set[int] = set()
        self.protection = protection
        self.send_to_trash = send_to_trash
        self.locate_bin = locate_bin
        self.started = datetime.now(UTC)
        self.reverted = 0
        self.skipped: list[str] = []
        self.failed: list[str] = []

    def refusal(self, path: Path) -> str | None:
        """Why undo must not put anything at ``path`` (or take it from there), or None."""
        reason = protected_reason(path, rules=self.protection)
        if reason and not self.protection.allowed:
            return f"protected path ({reason})"
        return destination_refusal(path)

    def done(self, action: ActionRecord) -> None:
        self.journal.set_action(action.id, "undone")
        self.reverted += 1

    def run(self, action: ActionRecord) -> None:
        if action.status not in ("done", "pending") or action.id in self.handled:
            return
        pending = action.status == "pending"
        label = f"{action.kind}: {action.src or action.dst}"
        if action.kind in REVERSIBLE_KINDS:
            self.move_back(action, label, pending)
        elif action.kind == "trash":
            self.restore(action, label, pending)
        elif action.kind in ("write", "convert"):
            self.recycle_output(action, label, pending)
        elif not pending:
            self.skipped.append(f"{label} (not reversible by undo; restore manually)")

    def move_back(self, action: ActionRecord, label: str, pending: bool) -> None:
        if not action.src or not action.dst:
            if not pending:
                self.skipped.append(f"{label} (missing paths)")
            return
        src, dst = Path(action.src), Path(action.dst)
        moved = os.path.lexists(long_path(dst))
        case_only = moved and is_case_only_rename(dst, src)
        occupied = os.path.lexists(long_path(src)) and not case_only
        if pending and not (moved and not occupied):
            # Left pending by a crash: undone only when the disk shows the move happened.
            if moved and occupied:
                self.skipped.append(f"{label} (interrupted; both places exist, check by hand)")
            return
        if not moved:
            self.skipped.append(f"{label} (moved file is no longer there)")
            return
        if occupied:
            self.skipped.append(f"{label} (original location is occupied)")
            return
        refusal = self.refusal(src)
        if refusal is not None:
            self.skipped.append(f"{label} ({refusal})")
            return
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            fs.move_no_replace(dst, src)
        except OSError as exc:
            self.failed.append(f"{label}: {exc}")
            return
        self.done(action)

    def restore(self, action: ActionRecord, label: str, pending: bool) -> bool:
        """Restore a trashed item. True when it is back (or was never trashed)."""
        if not action.src:
            if not pending:
                self.skipped.append(f"{label} (missing paths)")
            return False
        src = Path(action.src)
        if pending and os.path.lexists(long_path(src)):
            return True  # the crash came before it was trashed
        problem = self.restore_from_bin(action, src)
        if problem is not None:
            self.skipped.append(f"{label} ({problem}; restore manually from the Recycle Bin)")
            return False
        self.done(action)
        return True

    def extra_trash_rows(self, action: ActionRecord) -> list[ActionRecord]:
        """The originals a write/convert action recycled as its own journaled rows."""
        return [
            row
            for row in self.rows
            if row.seq == action.seq
            and row.id != action.id
            and row.kind == "trash"
            and row.status in ("done", "pending")
            and row.id not in self.handled
        ]

    def restore_from_bin(self, action: ActionRecord, src: Path) -> str | None:
        """Put a trashed item back at ``src``. Returns why not, or None when it is back."""
        refusal = self.refusal(src)
        if refusal is not None:
            return refusal
        when = _parse_ts(action.ts)
        if when is None:
            return "no usable time in the journal"
        result = recycle.restore(
            src,
            deleted_after=when - RECYCLE_SLACK,
            deleted_before=min(when + RECYCLE_SLACK, self.started),
            size=action.item_size,
            mtime_ns=action.item_mtime_ns,
            locate_bin=self.locate_bin,
        )
        return None if result.restored else result.detail

    def recycle_output(self, action: ActionRecord, label: str, pending: bool) -> None:
        """Recycle the output of a write/convert action, and bring back the originals it
        recycled (its extra ``trash`` rows). The output is recycled only when every one of
        those originals is back, so undo never leaves the user with neither file."""
        dst = Path(action.dst) if action.dst else None
        in_place = dst is not None and bool(action.src) and fs.same_path(str(action.src), dst)
        extras = self.extra_trash_rows(action)
        # Originals at other paths come back first; a restore never replaces anything. The
        # one an in-place output replaced comes back in undo_in_place, once the path is free.
        beside = [
            row
            for row in extras
            if not (in_place and dst is not None and row.src and fs.same_path(row.src, dst))
        ]
        all_back = True
        for row in beside:
            self.handled.add(row.id)
            if not self.restore(row, f"{row.kind}: {row.src}", row.status == "pending"):
                all_back = False

        if dst is None:
            # The handler produced no file (its side effects, if any, are rows of their own).
            if not pending and not extras and not self.other_rows(action):
                self.skipped.append(f"{label} (no output recorded; nothing to remove)")
            return
        if not os.path.lexists(long_path(dst)):
            if not pending:
                self.skipped.append(f"{label} (the output is no longer there)")
            return
        if not all_back:
            self.skipped.append(
                f"{label} (the output was kept, because its original could not be restored "
                "from the Recycle Bin; restore the original manually, then remove the output "
                "if you do not want it)"
            )
            return
        manual = "remove it manually if you do not want it"
        if pending or action.item_size is None or action.item_mtime_ns is None:
            self.skipped.append(f"{label} (output state was not recorded; {manual})")
            return
        st = fs.stat_no_follow(dst)
        unchanged = (
            st is not None
            and not fs.stat_is_link(st)
            and stat_module.S_ISREG(st.st_mode)
            and st.st_size == action.item_size
            and st.st_mtime_ns == action.item_mtime_ns
        )
        if not unchanged:
            self.skipped.append(f"{label} (the output changed after the run; {manual})")
            return
        refusal = self.refusal(dst) or recycle.can_recycle(dst)
        if refusal is not None:
            self.skipped.append(f"{label} ({refusal}; {manual})")
            return
        if in_place:
            self.undo_in_place(action, label, dst)
            return
        try:
            self.send_to_trash(dst)
        except Exception as exc:  # the Recycle Bin can fail for many reasons; report it
            self.failed.append(f"{label}: could not send the output to the Recycle Bin: {exc}")
            return
        self.done(action)

    def other_rows(self, action: ActionRecord) -> list[ActionRecord]:
        """Any other rows the same action journaled (for example a move done by a handler)."""
        return [row for row in self.rows if row.seq == action.seq and row.id != action.id]

    def undo_in_place(self, action: ActionRecord, label: str, dst: Path) -> None:
        """The output replaced its original at the same path. Recycle the output only when the
        original comes back from the Recycle Bin, so the path is never left empty."""
        original = next(
            (
                row
                for row in self.rows
                if row.seq == action.seq
                and row.id != action.id
                and row.kind == "trash"
                and row.status in ("done", "pending")
                and row.src
                and fs.same_path(row.src, dst)
            ),
            None,
        )
        if original is None:
            self.skipped.append(
                f"{label} (it replaced the original in place, and the original was not "
                "journaled; restore the original manually from the Recycle Bin)"
            )
            return
        aside = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:8]}.undo")
        try:
            fs.rename_no_replace(dst, aside)
        except OSError as exc:
            self.failed.append(f"{label}: could not move the output aside: {exc}")
            return
        problem = self.restore_from_bin(original, dst)
        if problem is not None:
            try:
                fs.rename_no_replace(aside, dst)
            except OSError as exc:
                self.failed.append(f"{label}: the output is at {aside}; rename it back ({exc})")
                return
            self.skipped.append(
                f"{label} (the original could not be restored: {problem}; nothing was changed; "
                "restore the original manually from the Recycle Bin)"
            )
            return
        self.handled.add(original.id)
        self.done(original)
        try:
            self.send_to_trash(aside)
        except Exception as exc:  # the original is back; the replaced copy stays aside
            self.failed.append(
                f"{label}: the original is back, but its replacement could not be sent to the "
                f"Recycle Bin and is at {aside}: {exc}"
            )
            return
        self.done(action)


def _default_trash(path: Path) -> None:
    """Recycle ``path``, only where the Recycle Bin really keeps it (see recycle.can_recycle)."""
    refusal = recycle.can_recycle(path)
    if refusal is not None:
        raise OSError(refusal)
    send2trash(str(path))


def undo_run(
    journal: Journal,
    run_id: int,
    *,
    protection: ProtectedPaths | None = None,
    send_to_trash: Callable[[Path], None] | None = None,
    locate_bin: recycle.BinLocator | None = None,
) -> UndoReport:
    """Reverse a run, newest action first (see the module docstring for each kind).

    ``protection`` is the protected-path rules (pass ``settings.protected_paths()``; the default
    list is used otherwise). ``send_to_trash`` and ``locate_bin`` replace the Recycle Bin, for
    tests. Within one ``seq``, the action itself is undone before the side effects it journaled
    (for example, an in-place conversion's output is recycled before its original is restored).
    """
    rows = journal.actions(run_id)
    undo = _Undo(
        journal,
        rows,
        protection or DEFAULT_PROTECTION,
        send_to_trash or _default_trash,
        locate_bin,
    )
    for action in sorted(rows, key=lambda a: (-a.seq, a.id)):
        undo.run(action)

    report = UndoReport(
        run_id=run_id,
        reverted=undo.reverted,
        skipped=tuple(undo.skipped),
        failed=tuple(undo.failed),
    )
    if undo.reverted:
        journal.set_run_status(run_id, report.status)
    log.info("undo of run %d: %s", run_id, report.status)
    return report
