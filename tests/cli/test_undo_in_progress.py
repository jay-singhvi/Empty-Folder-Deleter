"""``various-tools undo`` refuses a run that is still applying, unless it is stale.

The journal is the test's own temp file (``cli.journal``); a run is left unfinished by hand.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from various_tools.core.journal import Journal
from various_tools.gui.undo_rules import STALE_AFTER

if TYPE_CHECKING:
    from conftest import Cli


def _unfinished_rename(cli: Cli) -> tuple[int, Path, Path]:
    src, dst = cli.work / "old.txt", cli.work / "new.txt"
    dst.write_text("renamed")
    with Journal(cli.journal) as journal:
        run_id = journal.start_run("rename", "{}")
        action = journal.record_action(run_id, 1, "rename", src, dst)
        journal.set_action(action, "done")
    return run_id, src, dst


def test_undo_refuses_a_run_that_is_still_applying(cli: Cli) -> None:
    run_id, src, dst = _unfinished_rename(cli)
    result = cli.run("undo", str(run_id), "--yes")
    assert result.exit_code == 1
    assert "still applying" in result.output
    assert dst.exists() and not src.exists()
    with Journal(cli.journal) as journal:
        run = journal.run(run_id)
        assert run is not None and run.status == "running"


def test_undo_allows_a_stale_unfinished_run(cli: Cli) -> None:
    run_id, src, _dst = _unfinished_rename(cli)
    old = (datetime.now(UTC) - STALE_AFTER - timedelta(minutes=5)).isoformat(timespec="seconds")
    with sqlite3.connect(cli.journal) as conn:
        conn.execute("UPDATE runs SET started = ? WHERE id = ?", (old, run_id))
        conn.execute("UPDATE actions SET ts = ? WHERE run_id = ?", (old, run_id))
    conn.close()
    result = cli.run("undo", str(run_id), "--yes")
    assert "still applying" not in result.output
    assert "stopped" in result.output.casefold()  # it says why it is allowed
    assert src.exists()
