"""Undo is refused while a run is still applying, unless the run is stale (undo_rules).

The journal is a temp file. Timestamps are set with plain SQL so no test has to wait.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from various_tools.core.journal import Journal
from various_tools.gui.undo_rules import (
    STALE_AFTER,
    UndoInfo,
    read_undo_info,
    run_in_progress,
    stale_after,
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def test_a_finished_run_is_never_in_progress() -> None:
    assert not run_in_progress("done", _iso(NOW), _iso(NOW), now=NOW)
    assert not run_in_progress("partial", _iso(NOW), None, now=NOW)


def test_an_unfinished_run_with_a_recent_journal_write_is_in_progress() -> None:
    recent = _iso(NOW - timedelta(minutes=5))
    assert run_in_progress("running", None, recent, now=NOW)
    # A finished time without a final status, or a final status without a finished time, both
    # count as not finished.
    assert run_in_progress("running", recent, recent, now=NOW)
    assert run_in_progress("done", None, recent, now=NOW)


def test_an_unfinished_run_with_no_journal_write_for_a_long_time_is_stale() -> None:
    old = _iso(NOW - STALE_AFTER - timedelta(minutes=1))
    assert not run_in_progress("running", None, old, now=NOW)
    # An unreadable time is treated as recent (the safe side).
    assert run_in_progress("running", None, "not a time", now=NOW)
    assert run_in_progress("running", None, None, now=NOW)


def test_the_stale_limit_outlasts_the_longest_single_step() -> None:
    assert stale_after(60 * 60) == STALE_AFTER
    assert stale_after(10 * 60 * 60) > timedelta(hours=10)


def test_undo_info_of_an_in_progress_run_is_not_undoable() -> None:
    info = UndoInfo(1, "running", {"move": 2}, in_progress=True)
    assert not info.undoable
    assert UndoInfo(1, "done", {"move": 2}).undoable


def _journal_with_unfinished_run(path: Path) -> int:
    with Journal(path) as journal:
        run_id = journal.start_run("rename", "{}")
        action = journal.record_action(run_id, 1, "rename", Path("a"), Path("b"))
        journal.set_action(action, "done")
    return run_id


def _age_run(path: Path, run_id: int, by: timedelta) -> None:
    old = _iso(datetime.now(UTC) - by)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE runs SET started = ? WHERE id = ?", (old, run_id))
        conn.execute("UPDATE actions SET ts = ? WHERE run_id = ?", (old, run_id))
    conn.close()


def test_read_undo_info_marks_a_running_run_in_progress(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    run_id = _journal_with_unfinished_run(path)
    info = read_undo_info(path, run_id)
    assert info.in_progress and not info.undoable
    _age_run(path, run_id, STALE_AFTER + timedelta(minutes=5))
    stale = read_undo_info(path, run_id)
    assert not stale.in_progress and stale.undoable


def test_read_undo_info_of_a_finished_run(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    run_id = _journal_with_unfinished_run(path)
    with Journal(path) as journal:
        journal.finish_run(run_id, "done")
    info = read_undo_info(path, run_id)
    assert not info.in_progress and info.undoable
