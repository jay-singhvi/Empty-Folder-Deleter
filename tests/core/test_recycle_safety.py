"""Never delete for good by accident (final review): can_recycle, its use in every trash path,
a recycle that raised after it worked, the mtime check on restore, and roots through links or
administrative shares. The real Recycle Bin and registry are never used."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from various_tools.core import fs, recycle
from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.plan import ActionKind, Plan, PlannedAction, apply_plan
from various_tools.core.recycle import BinPolicy
from various_tools.core.recycle import can_recycle as real_can_recycle
from various_tools.core.settings import Settings

BinWriter = Callable[..., Path]
GiB = 1024**3


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def _ctx(tmp_path: Path, journal: Journal) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


def _check(
    path: Path,
    *,
    drive: int = 3,
    bin_folder: Path | None = None,
    policy: BinPolicy | None = None,
    volume: int | None = 100 * GiB,
) -> str | None:
    return real_can_recycle(
        path,
        drive_type=lambda _root: drive,
        locate_bin=lambda _p: bin_folder,
        policy=lambda _root: policy or BinPolicy(nuke_on_delete=False, max_capacity_mb=None),
        volume_size=lambda _root: volume,
    )


# ---- can_recycle --------------------------------------------------------------------------


def test_a_file_on_a_fixed_drive_with_a_bin_can_be_recycled(tmp_path: Path) -> None:
    item = tmp_path / "a.txt"
    item.write_bytes(b"x" * 10)
    assert _check(item, bin_folder=tmp_path) is None
    assert _check(item, bin_folder=tmp_path, policy=BinPolicy(False, 1)) is None


def test_a_network_path_cannot_be_recycled() -> None:
    reason = _check(Path(r"\\server\share\folder\file.txt"), bin_folder=Path("C:/"))
    assert reason is not None and "Recycle Bin" in reason


@pytest.mark.parametrize("drive", [2, 4, 5, 6, 0])  # removable, network, CD, RAM disk, unknown
def test_only_fixed_local_drives_qualify(tmp_path: Path, drive: int) -> None:
    item = tmp_path / "a.txt"
    item.write_bytes(b"x")
    reason = _check(item, drive=drive, bin_folder=tmp_path)
    assert reason is not None and "no Recycle Bin" in reason


def test_no_bin_of_this_user_on_the_drive_cannot_be_recycled(tmp_path: Path) -> None:
    item = tmp_path / "a.txt"
    item.write_bytes(b"x")
    reason = _check(item, bin_folder=None)
    assert reason is not None and "Recycle Bin" in reason


def test_a_bin_set_to_delete_immediately_refuses(tmp_path: Path) -> None:
    item = tmp_path / "a.txt"
    item.write_bytes(b"x")
    reason = _check(item, bin_folder=tmp_path, policy=BinPolicy(True, 1000))
    assert reason is not None and "deletes items immediately" in reason


def test_an_item_over_the_bin_limit_refuses(tmp_path: Path) -> None:
    item = tmp_path / "big.bin"
    with open(item, "wb") as handle:
        handle.truncate(2 * 1024 * 1024)  # 2 MB, sparse
    reason = _check(item, bin_folder=tmp_path, policy=BinPolicy(False, 1))
    assert reason is not None and "too big" in reason
    assert _check(item, bin_folder=tmp_path, policy=BinPolicy(False, 3)) is None


def test_an_unknown_limit_assumes_five_percent_of_the_volume(tmp_path: Path) -> None:
    item = tmp_path / "big.bin"
    with open(item, "wb") as handle:
        handle.truncate(6 * 1024 * 1024)
    assert _check(item, bin_folder=tmp_path, volume=100 * 1024 * 1024) is not None  # 5 MB
    assert _check(item, bin_folder=tmp_path, volume=200 * 1024 * 1024) is None  # 10 MB
    reason = _check(item, bin_folder=tmp_path, volume=None)
    assert reason is not None and "size limit" in reason


def test_an_empty_folder_counts_as_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    assert _check(folder, bin_folder=tmp_path, policy=BinPolicy(False, 0)) is not None
    assert _check(folder, bin_folder=tmp_path, policy=BinPolicy(False, 1)) is None


# ---- every trash path checks first ----------------------------------------------------------


@pytest.fixture
def no_bin(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """This drive has no Recycle Bin; send2trash must never be reached."""
    reached: list[str] = []

    def must_not_run(path: object) -> None:
        reached.append(str(path))

    monkeypatch.setattr(
        recycle,
        "can_recycle",
        lambda _p, **_k: "this drive has no Recycle Bin, so nothing was removed",
    )
    monkeypatch.setattr(plan_module, "send2trash", must_not_run)
    from various_tools.core import journal as journal_module

    monkeypatch.setattr(journal_module, "send2trash", must_not_run)
    return reached


def test_a_trash_action_where_there_is_no_bin_removes_nothing(
    tmp_path: Path, journal: Journal, no_bin: list[str]
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("only copy", encoding="utf-8")
    plan = Plan(
        tool="t", roots=[work], actions=[PlannedAction(ActionKind.TRASH, src=work / "a.txt")]
    )
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.outcomes[0].status == "skipped"
    assert "nothing was removed" in result.outcomes[0].detail
    assert (work / "a.txt").exists() and no_bin == []


def test_trash_extra_where_there_is_no_bin_raises_and_keeps_the_original(
    tmp_path: Path, journal: Journal, no_bin: list[str]
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    original = work / "photo.heic"
    original.write_bytes(b"the only copy")
    kept: list[bool] = []

    def convert(action: PlannedAction) -> Path | None:
        try:
            plan_module.trash_extra(original)
        except Exception:
            kept.append(original.exists())
            raise
        return None

    plan = Plan(tool="t", roots=[work], actions=[PlannedAction(ActionKind.CONVERT, src=original)])
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: convert})
    assert kept == [True] and original.read_bytes() == b"the only copy"
    rows = journal.actions(result.run_id)
    assert [(r.kind, r.status) for r in rows] == [("convert", "failed"), ("trash", "skipped")]
    assert "nothing was removed" in (rows[1].error or "")
    assert no_bin == []


def test_undo_does_not_recycle_an_output_where_there_is_no_bin(
    tmp_path: Path, journal: Journal, no_bin: list[str]
) -> None:
    out = tmp_path / "out.txt"
    out.write_text("x", encoding="utf-8")
    st = os.lstat(out)
    run_id = journal.start_run("t", "{}")
    row = journal.record_action(run_id, 1, "write", None, out)
    journal.set_action(row, "done")
    journal.set_item_stat(row, st.st_size, st.st_mtime_ns)
    report = undo_run(journal, run_id)
    assert report.reverted == 0 and out.exists() and no_bin == []
    assert "no Recycle Bin" in report.skipped[0]


# ---- a recycle that raised after it worked ------------------------------------------------


def test_a_recycle_that_raises_after_the_item_went_is_journaled_done(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    original = work / "photo.heic"
    original.write_bytes(b"x")

    def recycles_then_raises(path: object) -> None:
        os.remove(str(path))
        raise OSError("the shell reported an error after the operation")

    monkeypatch.setattr(plan_module, "send2trash", recycles_then_raises)

    def convert(action: PlannedAction) -> Path | None:
        plan_module.trash_extra(original)
        return None

    plan = Plan(
        tool="t",
        roots=[work],
        actions=[
            PlannedAction(ActionKind.CONVERT, src=original),
            PlannedAction(ActionKind.TRASH, src=work / "b.txt"),
        ],
    )
    (work / "b.txt").write_text("b", encoding="utf-8")
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: convert})
    rows = journal.actions(result.run_id)
    assert [(r.kind, r.status) for r in rows] == [
        ("convert", "done"),
        ("trash", "done"),
        ("trash", "done"),
    ]
    assert "reported an error" in (rows[1].error or "")
    assert "reported an error" in (rows[2].error or "")


# ---- restore checks the modification time ------------------------------------------------


def test_restore_needs_the_recorded_modification_time(
    tmp_path: Path, bin_writer: BinWriter
) -> None:
    now = datetime.now(UTC)
    bin_folder = tmp_path / "bin"
    original = tmp_path / "photo.jpg"
    item = bin_writer(bin_folder, "ABC", original, now, b"pixels")
    mtime = os.lstat(item).st_mtime_ns
    window = {
        "deleted_after": now - timedelta(minutes=2),
        "deleted_before": now + timedelta(minutes=2),
        "locate_bin": lambda _p: bin_folder,
    }
    wrong = recycle.restore(original, size=6, mtime_ns=mtime + 1_000_000, **window)  # type: ignore[arg-type]
    assert not wrong.restored and not original.exists()
    right = recycle.restore(original, size=6, mtime_ns=mtime, **window)  # type: ignore[arg-type]
    assert right.restored and original.read_bytes() == b"pixels"


def test_a_folder_is_restored_without_a_modification_time_check(
    tmp_path: Path, bin_writer: BinWriter
) -> None:
    now = datetime.now(UTC)
    bin_folder = tmp_path / "bin"
    original = tmp_path / "empty"
    bin_writer(bin_folder, "DIR", original, now, None)
    result = recycle.restore(
        original,
        deleted_after=now - timedelta(minutes=2),
        deleted_before=now + timedelta(minutes=2),
        mtime_ns=1,
        locate_bin=lambda _p: bin_folder,
    )
    assert result.restored and original.is_dir()


# ---- roots through links or administrative shares ----------------------------------------


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are a Windows feature")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if made.returncode != 0 or not link.exists():
        pytest.skip("could not create a junction in this environment")


def test_a_root_below_a_junction_into_a_protected_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = tmp_path / "Windows"
    (windows / "Temp").mkdir(parents=True)
    monkeypatch.setenv("SystemRoot", str(windows))
    _junction(tmp_path / "innocent", windows)
    refusal = fs.root_refusal(tmp_path / "innocent" / "Temp")
    assert refusal is not None and "junction" in refusal.lower()
    # Even with protected folders allowed, a link above the root is never followed.
    assert fs.root_refusal(tmp_path / "innocent" / "Temp", allow_protected=True) is not None


def test_a_root_whose_real_path_is_protected_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = tmp_path / "Windows"
    (windows / "Temp").mkdir(parents=True)
    looks_fine = tmp_path / "mapped"
    looks_fine.mkdir()
    monkeypatch.setenv("SystemRoot", str(windows))
    # A subst or mapped drive letter: the text is harmless, the real path is not.
    monkeypatch.setattr(
        fs, "real_path", lambda p: str(windows / "Temp") if fs.same_path(p, looks_fine) else str(p)
    )
    refusal = fs.root_refusal(looks_fine)
    assert refusal is not None and "Windows folder" in refusal


@pytest.mark.parametrize(
    "share",
    [
        r"\\localhost\C$\Windows\Temp",
        r"\\127.0.0.1\c$\Windows",
        r"\\?\UNC\localhost\C$\Windows\System32",
        r"\\SOMEPC\C$\Program Files\App",
    ],
)
def test_administrative_shares_are_compared_as_their_drive(share: str) -> None:
    env = {"SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files"}
    assert fs.protected_reason(Path(share), env) in ("Windows folder", "Program Files")
    assert fs.protected_reason(Path(r"\\localhost\C$"), env) == "drive root"
