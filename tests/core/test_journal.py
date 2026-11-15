"""Journal schema, run log, and undo (AGENTS.md rule 5)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from various_tools.core.journal import Journal, undo_run


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def test_run_and_actions_are_recorded_in_order(journal: Journal, tmp_path: Path) -> None:
    run_id = journal.start_run("empty_folders", '{"roots": []}')
    first = journal.record_action(run_id, 1, "move", tmp_path / "a", tmp_path / "b", "why")
    journal.set_action(first, "done")
    journal.record_action(run_id, 2, "trash", tmp_path / "c", None, status="skipped", error="nope")
    journal.finish_run(run_id, "done")

    run = journal.run(run_id)
    assert run is not None
    assert (run.tool, run.status) == ("empty_folders", "done")
    assert run.finished is not None

    actions = journal.actions(run_id)
    assert [a.seq for a in actions] == [1, 2]
    assert actions[0].status == "done" and actions[0].reason == "why"
    assert actions[1].status == "skipped" and actions[1].error == "nope"
    assert actions[1].dst is None


def test_runs_are_listed_newest_first(journal: Journal) -> None:
    first = journal.start_run("a", "{}")
    second = journal.start_run("b", "{}")
    assert [r.id for r in journal.runs()] == [second, first]


def test_set_action_can_record_the_real_destination(journal: Journal, tmp_path: Path) -> None:
    run_id = journal.start_run("t", "{}")
    action = journal.record_action(run_id, 1, "move", tmp_path / "a", tmp_path / "wanted")
    journal.set_action(action, "done", dst=tmp_path / "wanted_dup1")
    assert journal.actions(run_id)[0].dst == str(tmp_path / "wanted_dup1")


def test_undo_moves_files_back_in_reverse_order(journal: Journal, tmp_path: Path) -> None:
    src = tmp_path / "orig" / "photo.jpg"
    dst = tmp_path / "dup" / "photo.jpg"
    src.parent.mkdir(parents=True)
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"data")

    run_id = journal.start_run("duplicates", "{}")
    action = journal.record_action(run_id, 1, "move", src, dst)
    journal.set_action(action, "done")
    journal.finish_run(run_id, "done")

    report = undo_run(journal, run_id)
    assert report.reverted == 1
    assert report.skipped == ()
    assert src.read_bytes() == b"data"
    assert not dst.exists()
    assert report.status == "undone"
    assert journal.run(run_id).status == "undone"  # type: ignore[union-attr]
    assert journal.actions(run_id)[0].status == "undone"


def test_undo_skips_when_original_place_is_taken(journal: Journal, tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    dst = tmp_path / "moved.txt"
    src.write_text("new file", encoding="utf-8")
    dst.write_text("moved", encoding="utf-8")
    run_id = journal.start_run("t", "{}")
    journal.set_action(journal.record_action(run_id, 1, "move", src, dst), "done")

    report = undo_run(journal, run_id)
    assert report.reverted == 0
    assert "occupied" in report.skipped[0]
    assert src.read_text(encoding="utf-8") == "new file"
    assert dst.exists()


def test_undo_skips_when_moved_file_is_gone(journal: Journal, tmp_path: Path) -> None:
    run_id = journal.start_run("t", "{}")
    journal.set_action(
        journal.record_action(run_id, 1, "rename", tmp_path / "a", tmp_path / "b"), "done"
    )
    report = undo_run(journal, run_id)
    assert report.reverted == 0
    assert "no longer there" in report.skipped[0]
    assert report.status == "not undone"


def test_trashed_and_deleted_items_are_reported_not_restored(
    journal: Journal, tmp_path: Path
) -> None:
    run_id = journal.start_run("t", "{}")
    journal.set_action(journal.record_action(run_id, 1, "trash", tmp_path / "x", None), "done")
    journal.set_action(journal.record_action(run_id, 2, "delete", tmp_path / "y", None), "done")
    journal.set_action(journal.record_action(run_id, 3, "trash", tmp_path / "z", None), "failed")

    report = undo_run(journal, run_id)
    assert report.reverted == 0
    assert len(report.skipped) == 2
    assert all("restore manually" in s for s in report.skipped)


def test_failed_actions_are_not_undone(journal: Journal, tmp_path: Path) -> None:
    run_id = journal.start_run("t", "{}")
    journal.set_action(
        journal.record_action(run_id, 1, "move", tmp_path / "a", tmp_path / "b"), "failed"
    )
    report = undo_run(journal, run_id)
    assert report.reverted == 0 and report.skipped == () and report.failed == ()


def test_undo_reports_os_errors_per_action(
    journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"
    dst.write_text("x", encoding="utf-8")
    run_id = journal.start_run("t", "{}")
    journal.set_action(journal.record_action(run_id, 1, "move", src, dst), "done")

    def locked(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("file in use")

    monkeypatch.setattr("various_tools.core.fs.move_no_replace", locked)
    report = undo_run(journal, run_id)
    assert report.reverted == 0
    assert "file in use" in report.failed[0]


def test_journal_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "j.sqlite"
    first = Journal(db)
    run_id = first.start_run("t", '{"x": 1}')
    first.close()
    second = Journal(db)
    try:
        run = second.run(run_id)
        assert run is not None and run.options_json == '{"x": 1}'
    finally:
        second.close()
