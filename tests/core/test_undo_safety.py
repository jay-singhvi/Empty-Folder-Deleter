"""Undo (review fixes): never overwrite, refuse junctions and protected paths, pending rows,
Recycle Bin restore, removal of unchanged outputs, and journal concurrency and read helpers."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.fs import ProtectedPaths
from various_tools.core.journal import Journal, undo_run
from various_tools.core.plan import ActionKind, Plan, PlannedAction, apply_plan
from various_tools.core.settings import Settings


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are a Windows feature")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if made.returncode != 0 or not link.exists():
        pytest.skip("could not create a junction in this environment")


def _ctx(tmp_path: Path, journal: Journal) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


class FakeBin:
    """Stands in for the Recycle Bin: trashing writes a $I/$R pair into a tmp folder."""

    def __init__(self, folder: Path, writer: Callable[..., Path]) -> None:
        self.folder = folder
        self.writer = writer
        self.trashed: list[Path] = []

    def trash(self, path: Path | str) -> None:
        path = Path(path)
        content = None if path.is_dir() else path.read_bytes()
        key = uuid.uuid4().hex[:6].upper()
        self.writer(self.folder, key, path, datetime.now(UTC), content)
        if content is None:
            path.rmdir()
        else:
            path.unlink()
        self.trashed.append(path)

    def locate(self, _path: Path) -> Path | None:
        return self.folder


@pytest.fixture
def fake_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bin_writer: Callable[..., Path]
) -> FakeBin:
    bin_ = FakeBin(tmp_path / "fake-bin", bin_writer)
    monkeypatch.setattr(plan_module, "send2trash", bin_.trash)
    return bin_


# ---- moves ------------------------------------------------------------------------------


def test_undo_refuses_to_move_back_through_a_junction(journal: Journal, tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "orig").mkdir(parents=True)
    src = work / "orig" / "photo.jpg"
    dst = work / "moved.jpg"
    dst.write_bytes(b"x")
    run_id = journal.start_run("t", "{}")
    journal.set_action(journal.record_action(run_id, 1, "move", src, dst), "done")
    # After the run, the original folder was replaced by a junction to somewhere else.
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "orig").rmdir()
    _junction(work / "orig", outside)

    report = undo_run(journal, run_id)
    assert report.reverted == 0
    assert "junction" in report.skipped[0]
    assert dst.exists() and list(outside.iterdir()) == []


def test_undo_refuses_a_protected_original(journal: Journal, tmp_path: Path) -> None:
    src = tmp_path / "Windows" / "a.txt"
    dst = tmp_path / "b.txt"
    dst.write_text("x", encoding="utf-8")
    run_id = journal.start_run("t", "{}")
    journal.set_action(journal.record_action(run_id, 1, "move", src, dst), "done")
    rules = ProtectedPaths(folders=(str(tmp_path / "Windows"),))
    report = undo_run(journal, run_id, protection=rules)
    assert report.reverted == 0
    assert "protected" in report.skipped[0]
    assert dst.exists()


def test_undo_never_overwrites_a_file_that_appears(
    journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"
    dst.write_text("moved", encoding="utf-8")
    run_id = journal.start_run("t", "{}")
    journal.set_action(journal.record_action(run_id, 1, "move", src, dst), "done")
    from various_tools.core import journal as journal_module

    real_refusal = journal_module.destination_refusal

    def refusal_then_race(path: Path, stop: Path | None = None) -> str | None:
        answer = real_refusal(path, stop)
        src.write_text("arrived meanwhile", encoding="utf-8")
        return answer

    monkeypatch.setattr(journal_module, "destination_refusal", refusal_then_race)
    report = undo_run(journal, run_id)
    assert report.reverted == 0 and report.failed
    assert src.read_text(encoding="utf-8") == "arrived meanwhile"
    assert dst.read_text(encoding="utf-8") == "moved"


def test_rows_left_pending_by_a_crash_are_undone_when_the_move_happened(
    journal: Journal, tmp_path: Path
) -> None:
    happened_src, happened_dst = tmp_path / "a.txt", tmp_path / "moved_a.txt"
    happened_dst.write_text("a", encoding="utf-8")
    never_src, never_dst = tmp_path / "b.txt", tmp_path / "moved_b.txt"
    never_src.write_text("b", encoding="utf-8")
    run_id = journal.start_run("t", "{}")
    journal.record_action(run_id, 1, "move", happened_src, happened_dst)  # left pending
    journal.record_action(run_id, 2, "move", never_src, never_dst)  # crashed before it ran

    report = undo_run(journal, run_id)
    assert report.reverted == 1
    assert happened_src.read_text(encoding="utf-8") == "a" and not happened_dst.exists()
    assert never_src.exists() and not never_dst.exists()
    statuses = [a.status for a in journal.actions(run_id)]
    assert statuses == ["undone", "pending"]


def test_a_second_undo_is_a_no_op(journal: Journal, tmp_path: Path) -> None:
    src, dst = tmp_path / "a.txt", tmp_path / "b.txt"
    dst.write_text("x", encoding="utf-8")
    run_id = journal.start_run("t", "{}")
    journal.set_action(journal.record_action(run_id, 1, "move", src, dst), "done")
    assert undo_run(journal, run_id).reverted == 1
    # Someone moves the file back by hand; a second undo must not touch it again.
    os.rename(src, dst)
    again = undo_run(journal, run_id)
    assert again.reverted == 0 and again.skipped == () and again.failed == ()
    assert dst.exists()


# ---- Recycle Bin ----------------------------------------------------------------------------


def test_undo_restores_trashed_items_from_the_recycle_bin(
    journal: Journal, tmp_path: Path, fake_bin: FakeBin
) -> None:
    work = tmp_path / "work"
    (work / "empty").mkdir(parents=True)
    (work / "photo.jpg").write_bytes(b"pixels")
    plan = Plan(
        tool="t",
        roots=[work],
        actions=[
            PlannedAction(ActionKind.TRASH, src=work / "photo.jpg"),
            PlannedAction(ActionKind.TRASH, src=work / "empty"),
        ],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.count("done") == 2 and not (work / "photo.jpg").exists()

    report = undo_run(journal, result.run_id, locate_bin=fake_bin.locate)
    assert report.reverted == 2, report.skipped
    assert (work / "photo.jpg").read_bytes() == b"pixels"
    assert (work / "empty").is_dir()
    assert [a.status for a in journal.actions(result.run_id)] == ["undone", "undone"]
    assert list(fake_bin.folder.iterdir()) == []


def test_a_trashed_item_that_cannot_be_found_is_listed_for_manual_restore(
    journal: Journal, tmp_path: Path
) -> None:
    run_id = journal.start_run("t", "{}")
    journal.set_action(journal.record_action(run_id, 1, "trash", tmp_path / "x", None), "done")
    journal.set_action(journal.record_action(run_id, 2, "delete", tmp_path / "y", None), "done")
    report = undo_run(journal, run_id, locate_bin=lambda _p: None)
    assert report.reverted == 0
    assert len(report.skipped) == 2
    assert all("manually" in s for s in report.skipped)


# ---- write and convert outputs ----------------------------------------------------------------


def test_undo_recycles_an_unchanged_output_and_keeps_a_changed_one(
    journal: Journal, tmp_path: Path, fake_bin: FakeBin
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("a", encoding="utf-8")

    def write(action: PlannedAction) -> Path | None:
        assert action.dst is not None
        action.dst.write_text("generated", encoding="utf-8")
        return action.dst

    plan = Plan(
        tool="t",
        roots=[work],
        actions=[
            PlannedAction(ActionKind.WRITE, src=work / "a.txt", dst=work / "same.txt"),
            PlannedAction(ActionKind.WRITE, src=work / "a.txt", dst=work / "edited.txt"),
        ],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.WRITE: write})
    (work / "edited.txt").write_text("the user changed this", encoding="utf-8")

    report = undo_run(journal, result.run_id, send_to_trash=fake_bin.trash)
    assert report.reverted == 1
    assert not (work / "same.txt").exists()
    assert (work / "edited.txt").exists()
    assert any("changed" in s for s in report.skipped)
    assert fake_bin.trashed == [work / "same.txt"]


def test_an_in_place_conversion_is_undone_with_its_recycled_original(
    journal: Journal, tmp_path: Path, fake_bin: FakeBin
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    photo = work / "photo.jpg"
    photo.write_bytes(b"original with gps")

    def strip(action: PlannedAction) -> Path | None:
        staging = work / ".staging.tmp"
        staging.write_bytes(b"clean")
        plan_module.trash_extra(photo, reason="original replaced by the clean copy")
        os.rename(staging, photo)
        return photo

    plan = Plan(
        tool="t",
        roots=[work],
        actions=[PlannedAction(ActionKind.CONVERT, src=photo, dst=photo)],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: strip})
    assert result.count("done") == 1 and photo.read_bytes() == b"clean"

    report = undo_run(
        journal, result.run_id, send_to_trash=fake_bin.trash, locate_bin=fake_bin.locate
    )
    assert report.reverted == 2, (report.skipped, report.failed)
    assert photo.read_bytes() == b"original with gps"


def test_an_in_place_output_stays_when_its_original_cannot_come_back(
    journal: Journal, tmp_path: Path, fake_bin: FakeBin
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    photo = work / "photo.jpg"
    photo.write_bytes(b"original")

    def strip_untracked(action: PlannedAction) -> Path | None:
        staging = work / ".staging.tmp"
        staging.write_bytes(b"clean")
        photo.unlink()  # the original is gone without a journal row (an old-style handler)
        os.rename(staging, photo)
        return photo

    def strip_tracked(action: PlannedAction) -> Path | None:
        staging = work / ".staging2.tmp"
        staging.write_bytes(b"clean again")
        plan_module.trash_extra(photo)
        os.rename(staging, photo)
        return photo

    plan = Plan(
        tool="t", roots=[work], actions=[PlannedAction(ActionKind.CONVERT, src=photo, dst=photo)]
    )
    first = apply_plan(
        plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: strip_untracked}
    )
    report = undo_run(journal, first.run_id, send_to_trash=fake_bin.trash)
    assert report.reverted == 0 and "in place" in report.skipped[0]
    assert photo.read_bytes() == b"clean"

    second = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: strip_tracked})
    # The Recycle Bin cannot be found: nothing changes, and the path is never left empty.
    report = undo_run(
        journal, second.run_id, send_to_trash=fake_bin.trash, locate_bin=lambda _p: None
    )
    assert report.reverted == 0
    assert photo.read_bytes() == b"clean again"
    assert sorted(p.name for p in work.iterdir()) == ["photo.jpg"]


def _convert_and_recycle_original(work: Path) -> tuple[Path, Path, Plan]:
    """A heic -> jpg conversion that recycles the original, as media_convert does."""
    original = work / "photo.heic"
    output = work / "photo.jpg"
    original.write_bytes(b"heic bytes")
    plan = Plan(
        tool="t",
        roots=[work],
        actions=[PlannedAction(ActionKind.CONVERT, src=original, dst=output)],
    )
    return original, output, plan


def _converter(original: Path, output: Path) -> Callable[[PlannedAction], Path | None]:
    def convert(action: PlannedAction) -> Path | None:
        output.write_bytes(b"jpeg bytes")
        plan_module.trash_extra(original, reason="original of a verified conversion")
        return output

    return convert


def test_undo_restores_the_original_then_recycles_the_output(
    journal: Journal, tmp_path: Path, fake_bin: FakeBin
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    original, output, plan = _convert_and_recycle_original(work)
    handlers = {ActionKind.CONVERT: _converter(original, output)}
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers=handlers)
    assert not original.exists() and output.exists()

    report = undo_run(
        journal, result.run_id, send_to_trash=fake_bin.trash, locate_bin=fake_bin.locate
    )
    assert report.reverted == 2, (report.skipped, report.failed)
    assert original.read_bytes() == b"heic bytes"
    assert not output.exists()
    assert [a.status for a in journal.actions(result.run_id)] == ["undone", "undone"]


def test_the_output_stays_when_its_original_cannot_be_restored(
    journal: Journal, tmp_path: Path, fake_bin: FakeBin
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    original, output, plan = _convert_and_recycle_original(work)
    handlers = {ActionKind.CONVERT: _converter(original, output)}
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers=handlers)

    report = undo_run(
        journal,
        result.run_id,
        send_to_trash=lambda _p: pytest.fail("the output must not be recycled"),
        locate_bin=lambda _p: None,
    )
    assert report.reverted == 0
    assert output.read_bytes() == b"jpeg bytes"
    assert any("manually" in s and "Recycle Bin" in s for s in report.skipped)
    assert any("output was kept" in s for s in report.skipped)
    assert [a.status for a in journal.actions(result.run_id)] == ["done", "done"]
    # Once the bin is reachable, a second undo finishes the job.
    again = undo_run(
        journal, result.run_id, send_to_trash=fake_bin.trash, locate_bin=fake_bin.locate
    )
    assert again.reverted == 2
    assert original.read_bytes() == b"heic bytes" and not output.exists()


def test_a_download_then_move_row_is_moved_back_and_nothing_recycled(
    journal: Journal, tmp_path: Path, fake_bin: FakeBin
) -> None:
    # organize_by_date: a CONVERT handler that returns None and journals the real move.
    work = tmp_path / "work"
    work.mkdir()
    photo = work / "cloud.jpg"
    photo.write_bytes(b"downloaded")
    target = work / "2024" / "cloud.jpg"

    def download_then_move(action: PlannedAction) -> Path | None:
        with plan_module.extra_action(ActionKind.MOVE, photo, target, "by date"):
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(photo, target)
        return None

    plan = Plan(tool="t", roots=[work], actions=[PlannedAction(ActionKind.CONVERT, src=photo)])
    result = apply_plan(
        plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: download_then_move}
    )
    assert target.exists()
    report = undo_run(
        journal,
        result.run_id,
        send_to_trash=lambda _p: pytest.fail("nothing may be recycled"),
        locate_bin=fake_bin.locate,
    )
    assert report.reverted == 1 and report.skipped == () and report.failed == ()
    assert photo.read_bytes() == b"downloaded" and not target.exists()


def test_a_resize_in_place_then_move_is_moved_back_then_restored(
    journal: Journal, tmp_path: Path, fake_bin: FakeBin
) -> None:
    # organize_by_date with also_resize: CONVERT in place (recycling the original through a
    # journaled row), then a separate MOVE row for the same file.
    work = tmp_path / "work"
    work.mkdir()
    photo = work / "big.jpg"
    photo.write_bytes(b"full size original")
    target = work / "2024" / "big.jpg"
    order: list[str] = []

    def resize(action: PlannedAction) -> Path | None:
        staged = work / ".big.staged"
        staged.write_bytes(b"small")
        plan_module.trash_extra(photo, reason="replaced by the resized copy")
        os.rename(staged, photo)
        return photo

    def trash(path: Path) -> None:
        order.append(f"recycle {Path(path).suffix}")
        fake_bin.trash(path)

    def locate(path: Path) -> Path | None:
        order.append("restore" if target.exists() is False else "restore-too-early")
        return fake_bin.locate(path)

    plan = Plan(
        tool="t",
        roots=[work],
        actions=[
            PlannedAction(ActionKind.CONVERT, src=photo, dst=photo),
            PlannedAction(ActionKind.MOVE, src=photo, dst=target),
        ],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: resize})
    assert result.count("done") == 2 and target.read_bytes() == b"small"

    report = undo_run(journal, result.run_id, send_to_trash=trash, locate_bin=locate)
    assert report.reverted == 3, (report.skipped, report.failed)
    assert photo.read_bytes() == b"full size original"
    assert not target.exists()
    assert order == ["restore", "recycle .undo"]  # moved back first, then restored


def test_a_pending_output_without_a_recorded_state_is_left_alone(
    journal: Journal, tmp_path: Path
) -> None:
    out = tmp_path / "out.txt"
    out.write_text("x", encoding="utf-8")
    run_id = journal.start_run("t", "{}")
    journal.set_action(journal.record_action(run_id, 1, "write", None, out), "done")
    report = undo_run(journal, run_id, send_to_trash=lambda _p: pytest.fail("trashed"))
    assert report.reverted == 0 and out.exists()
    assert "remove it manually" in report.skipped[0]


# ---- journal ------------------------------------------------------------------------------


def test_two_journal_connections_can_write_at_the_same_time(tmp_path: Path) -> None:
    db = tmp_path / "shared.sqlite"
    gui, cli = Journal(db), Journal(db)
    try:
        a = gui.start_run("gui", "{}")
        b = cli.start_run("cli", "{}")
        for seq in range(1, 21):
            gui.record_action(a, seq, "move", tmp_path / f"g{seq}", None)
            cli.record_action(b, seq, "move", tmp_path / f"c{seq}", None)
        assert gui.action_count(b) == 20 and cli.action_count(a) == 20
        check = sqlite3.connect(db)
        try:
            mode = check.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            check.close()
        assert str(mode).lower() == "wal"
    finally:
        gui.close()
        cli.close()


def test_journal_read_helpers(journal: Journal, tmp_path: Path) -> None:
    run_id = journal.start_run("t", "{}")
    for seq in range(1, 6):
        row = journal.record_action(run_id, seq, "move", tmp_path / str(seq), None)
        journal.set_action(row, "done" if seq % 2 else "failed")
    assert journal.action_count(run_id) == 5
    assert journal.status_counts(run_id) == {"done": 3, "failed": 2}
    assert [a.seq for a in journal.actions(run_id, limit=2, offset=1)] == [2, 3]
    assert [a.seq for a in journal.actions(run_id, offset=3)] == [4, 5]
    assert len(journal.actions(run_id)) == 5
    assert journal.action_count(999) == 0 and journal.status_counts(999) == {}


def test_an_old_journal_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL,
            options_json TEXT NOT NULL, started TEXT NOT NULL, finished TEXT,
            status TEXT NOT NULL);
        CREATE TABLE actions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL,
            kind TEXT NOT NULL, src TEXT, dst TEXT, reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, error TEXT, ts TEXT NOT NULL);
        INSERT INTO runs VALUES (1, 't', '{}', '2026-01-01T00:00:00+00:00', NULL, 'done');
        INSERT INTO actions VALUES (1, 1, 1, 'move', 'a', 'b', '', 'done', NULL,
            '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()
    with Journal(db) as journal:
        old = journal.actions(1)[0]
        assert old.item_size is None and old.item_mtime_ns is None
        row = journal.record_action(1, 2, "write", None, tmp_path / "o")
        journal.set_item_stat(row, 10, 123)
        assert journal.actions(1)[1].item_size == 10
