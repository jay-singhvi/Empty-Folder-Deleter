"""When a run can be undone. One rule for the tool pages and the History page. No Qt in here.

A run can be undone when it is not already undone and at least one of its actions is a done
``move``, ``rename``, ``trash``, ``write`` or ``convert``: moves and renames are put back, trashed
items are restored from the Recycle Bin while they are still there, and written or converted files
are handled by the journal's undo. A run with nothing reversible (for example only permanent
deletes) cannot be undone.

The journal is read with one small read-only query, so this can run in a worker thread while an
apply writes to the journal.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from various_tools.core.journal import UNDOABLE_KINDS

__all__ = ["UNDOABLE_KINDS", "UndoInfo", "can_undo", "read_undo_info", "undoable_count"]

UNDONE_STATUS = "undone"


def undoable_count(done_by_kind: Mapping[str, int]) -> int:
    """How many done actions of a run undo can work on."""
    return sum(count for kind, count in done_by_kind.items() if kind in UNDOABLE_KINDS)


def can_undo(run_status: str | None, done_by_kind: Mapping[str, int]) -> bool:
    """The rule: not already undone, and at least one done action undo can work on."""
    return run_status != UNDONE_STATUS and undoable_count(done_by_kind) > 0


@dataclass(frozen=True)
class UndoInfo:
    """What the rule needs to know about one run: its status and its done actions by kind."""

    run_id: int
    status: str | None
    done_by_kind: dict[str, int] = field(default_factory=dict[str, int])

    @property
    def undoable(self) -> bool:
        return can_undo(self.status, self.done_by_kind)

    @property
    def reversible(self) -> int:
        return undoable_count(self.done_by_kind)

    @property
    def done(self) -> int:
        return sum(self.done_by_kind.values())

    def count(self, *kinds: str) -> int:
        return sum(self.done_by_kind.get(kind, 0) for kind in kinds)


def read_undo_info(journal_path: Path, run_id: int) -> UndoInfo:
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
        row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        kinds = conn.execute(
            "SELECT kind, COUNT(*) FROM actions WHERE run_id = ? AND status = 'done' GROUP BY kind",
            (run_id,),
        ).fetchall()
    except sqlite3.Error:
        return UndoInfo(run_id, None)
    finally:
        conn.close()
    status = str(row[0]) if row else None
    return UndoInfo(run_id, status, {str(kind): int(count) for kind, count in kinds})
