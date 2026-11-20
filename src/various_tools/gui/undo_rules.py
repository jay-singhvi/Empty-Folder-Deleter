"""When a run can be undone. One rule for the tool pages, the History page and the CLI. No Qt.

A run can be undone when it is not already undone, it is not still applying, and at least one of
its actions is a done ``move``, ``rename``, ``trash``, ``write`` or ``convert``: moves and renames
are put back, trashed items are restored from the Recycle Bin while they are still there, and
written or converted files are handled by the journal's undo. A run with nothing reversible (for
example only permanent deletes) cannot be undone.

**Still applying.** A run whose journal entry has no ``finished`` time, or whose status is still
``running``, is in progress: undoing it would race the apply that is still changing files. It is
refused, unless it is *stale*: nothing has been written to the journal for that run (its start, its
heartbeat or any of its actions) for :func:`stale_after` (at least :data:`STALE_AFTER`, two hours, and always
longer than the longest single step an apply may take, the transcode timeout). A live apply
journals every action before and after it runs, and while it is paused or waiting between
batches it touches the run (a heartbeat) at least once a minute, so a gap that long means the program that owned
the run stopped (it crashed or the computer was turned off), and its done actions may be undone.
A time that cannot be read counts as recent, which is the safe side.

The journal is read with one small read-only query, so this can run in a worker thread while an
apply writes to the journal.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from various_tools.core.journal import UNDOABLE_KINDS
from various_tools.core.settings import DEFAULT_TRANSCODE_TIMEOUT_S

__all__ = [
    "STALE_AFTER",
    "UNDOABLE_KINDS",
    "UndoInfo",
    "can_undo",
    "in_progress_message",
    "read_undo_info",
    "run_in_progress",
    "stale_after",
    "undoable_count",
]

UNDONE_STATUS = "undone"
RUNNING_STATUS = "running"
# No journal write for this long (and longer than the longest single step) makes an unfinished
# run stale, so it may be undone.
STALE_AFTER = timedelta(hours=2)
_STEP_MARGIN = timedelta(minutes=30)


def stale_after(longest_step_s: float = DEFAULT_TRANSCODE_TIMEOUT_S) -> timedelta:
    """How long an unfinished run must be silent before it counts as stale.

    ``longest_step_s`` is the longest one action may take without a journal write (the transcode
    timeout setting); the limit is that plus half an hour, and never less than STALE_AFTER.
    """
    return max(STALE_AFTER, timedelta(seconds=max(0.0, longest_step_s)) + _STEP_MARGIN)


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def run_in_progress(
    status: str | None,
    finished: str | None,
    last_write: str | None,
    *,
    now: datetime | None = None,
    limit: timedelta = STALE_AFTER,
) -> bool:
    """True when the run is not finished and is not stale (see the module docstring).

    ``last_write`` is the newest journal time of the run: its start or any action's time.
    """
    if finished and status != RUNNING_STATUS:
        return False
    when = _parse_time(last_write)
    if when is None:
        return True  # unknown: assume it is still running
    current = now or datetime.now(UTC)
    return current - when < limit


def in_progress_message(run_id: int, limit: timedelta = STALE_AFTER) -> str:
    hours = limit.total_seconds() / 3600
    return (
        f"Run {run_id} is still applying, so it cannot be undone yet. Wait until it has "
        f"finished. (A run that wrote nothing to the journal for {hours:g} hours counts as "
        "stopped and can then be undone.)"
    )


def undoable_count(done_by_kind: Mapping[str, int]) -> int:
    """How many done actions of a run undo can work on."""
    return sum(count for kind, count in done_by_kind.items() if kind in UNDOABLE_KINDS)


def can_undo(
    run_status: str | None, done_by_kind: Mapping[str, int], *, in_progress: bool = False
) -> bool:
    """The rule: not already undone, not still applying, and one done action undo can work on."""
    return not in_progress and run_status != UNDONE_STATUS and undoable_count(done_by_kind) > 0


@dataclass(frozen=True)
class UndoInfo:
    """What the rule needs to know about one run: its status, its done actions by kind, and
    whether it is still applying."""

    run_id: int
    status: str | None
    done_by_kind: dict[str, int] = field(default_factory=dict[str, int])
    in_progress: bool = False

    @property
    def undoable(self) -> bool:
        return can_undo(self.status, self.done_by_kind, in_progress=self.in_progress)

    @property
    def reversible(self) -> int:
        return undoable_count(self.done_by_kind)

    @property
    def done(self) -> int:
        return sum(self.done_by_kind.values())

    def count(self, *kinds: str) -> int:
        return sum(self.done_by_kind.get(kind, 0) for kind in kinds)


def read_undo_info(
    journal_path: Path,
    run_id: int,
    *,
    limit: timedelta = STALE_AFTER,
    now: datetime | None = None,
) -> UndoInfo:
    """Read a run's status and done actions by kind (read-only). Call it off the UI thread.

    A missing journal or run gives an ``UndoInfo`` with no status and nothing done.
    """
    if not journal_path.is_file():
        return UndoInfo(run_id, None)
    try:
        conn = sqlite3.connect(f"{journal_path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return UndoInfo(run_id, None)
    try:
        try:
            row = conn.execute(
                "SELECT status, finished, started, heartbeat FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        except sqlite3.OperationalError:  # a journal written before the heartbeat column
            row = conn.execute(
                "SELECT status, finished, started, NULL FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        kinds = conn.execute(
            "SELECT kind, COUNT(*) FROM actions WHERE run_id = ? AND status = 'done' GROUP BY kind",
            (run_id,),
        ).fetchall()
        newest = conn.execute("SELECT MAX(ts) FROM actions WHERE run_id = ?", (run_id,)).fetchone()
    except sqlite3.Error:
        return UndoInfo(run_id, None)
    finally:
        conn.close()
    if not row:
        return UndoInfo(run_id, None, {str(kind): int(count) for kind, count in kinds})
    status = str(row[0]) if row[0] is not None else None
    finished = str(row[1]) if row[1] else None
    times = [str(t) for t in (row[2], row[3], newest[0] if newest else None) if t]
    last_write = max(times) if times else None
    busy = run_in_progress(status, finished, last_write, now=now, limit=limit)
    return UndoInfo(run_id, status, {str(kind): int(count) for kind, count in kinds}, busy)
