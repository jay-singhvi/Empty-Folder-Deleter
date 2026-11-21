"""Paced apply that waits for OneDrive to upload each batch."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from various_tools.core import onedrive
from various_tools.core import plan as plan_module
from various_tools.core.context import Pace, RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.plan import ActionKind, Plan, PlannedAction, apply_plan
from various_tools.core.settings import Settings


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def _ctx(tmp_path: Path, journal: Journal, pace: Pace | None) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
        pace=pace,
    )


def _plan(tmp_path: Path, count: int) -> Plan:
    root = tmp_path / "work"
    root.mkdir()
    actions = []
    for i in range(count):
        (root / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        actions.append(
            PlannedAction(ActionKind.MOVE, src=root / f"f{i}.txt", dst=root / "out" / f"f{i}.txt")
        )
    return Plan(tool="t", roots=[root], actions=actions)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(plan_module, "_sleep", calls.append)
    return calls


def _pace(**kw: object) -> Pace:
    return Pace(2, 0.0, wait_for_upload=True, **kw)  # type: ignore[arg-type]


def test_pace_defaults_and_validation() -> None:
    p = Pace(1, 1)
    assert p.wait_for_upload is False and p.upload_timeout_s == 1800
    with pytest.raises(ValueError):
        Pace(1, 1, wait_for_upload=True, upload_timeout_s=0)


def test_waits_until_every_destination_reports_in_sync(
    tmp_path: Path, journal: Journal, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    f0_checks = 0

    def pending(path: Path) -> bool | None:
        nonlocal f0_checks
        if path.name != "f0.txt":
            return False
        f0_checks += 1
        return f0_checks <= 2  # still uploading on the first two polls

    monkeypatch.setattr(onedrive, "upload_pending", pending)
    ctx = _ctx(tmp_path, journal, _pace())
    messages: list[str] = []
    ctx.progress = lambda _d, _t, m: messages.append(m)
    result = apply_plan(_plan(tmp_path, 3), ctx)
    assert result.status == "done" and result.count("done") == 3
    assert any("Waiting for OneDrive to upload 1 of 2 files" in m for m in messages)
    assert f0_checks == 3
    assert result.notes == []
    # Two poll intervals (5 s each, in 0.25 s steps) passed.
    assert sum(sleeps) == pytest.approx(10.0)


def test_only_the_batchs_own_files_are_checked(
    tmp_path: Path, journal: Journal, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def pending(path: Path) -> bool | None:
        seen.append(path.name)
        return False

    monkeypatch.setattr(onedrive, "upload_pending", pending)
    apply_plan(_plan(tmp_path, 4), _ctx(tmp_path, journal, _pace()))
    assert seen == ["f0.txt", "f1.txt"]  # the first batch only; no check after the last batch


def test_none_counts_as_uploaded(
    tmp_path: Path, journal: Journal, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onedrive, "upload_pending", lambda _p: None)
    result = apply_plan(_plan(tmp_path, 3), _ctx(tmp_path, journal, _pace()))
    assert result.status == "done" and result.notes == []
    assert sleeps == []


def test_timeout_continues_and_notes_it(
    tmp_path: Path, journal: Journal, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onedrive, "upload_pending", lambda _p: True)
    ctx = _ctx(tmp_path, journal, _pace(upload_timeout_s=10.0))
    result = apply_plan(_plan(tmp_path, 3), ctx)
    assert result.status == "done" and result.count("done") == 3
    assert "2 of 2" in result.notes[0] and "batch 1" in result.notes[0]
    assert "did not upload anything for 10 seconds" in result.notes[1]
    assert sum(sleeps) == pytest.approx(10.0)


def test_without_the_flag_nothing_is_checked(
    tmp_path: Path, journal: Journal, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_p: Path) -> bool:
        raise AssertionError("must not check")

    monkeypatch.setattr(onedrive, "upload_pending", boom)
    apply_plan(_plan(tmp_path, 3), _ctx(tmp_path, journal, Pace(2, 0.0)))


def test_cancel_during_the_upload_wait(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onedrive, "upload_pending", lambda _p: True)
    ctx = _ctx(tmp_path, journal, _pace())
    monkeypatch.setattr(plan_module, "_sleep", lambda _s: ctx.cancel())
    result = apply_plan(_plan(tmp_path, 4), ctx)
    assert result.status == "cancelled" and result.count("done") == 2


def test_skip_ends_the_upload_wait_and_the_pause(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onedrive, "upload_pending", lambda _p: True)
    ctx = _ctx(tmp_path, journal, Pace(2, 600.0, wait_for_upload=True))
    calls: list[float] = []

    def sleep(step: float) -> None:
        calls.append(step)
        ctx.skip_wait_event.set()

    monkeypatch.setattr(plan_module, "_sleep", sleep)
    result = apply_plan(_plan(tmp_path, 3), ctx)
    assert result.status == "done" and result.count("done") == 3
    assert len(calls) == 1 and result.notes == []


def test_pause_holds_the_upload_wait_without_using_its_time(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, journal, _pace(upload_timeout_s=1.0))
    polls = 0

    def pending(_p: Path) -> bool:
        nonlocal polls
        polls += 1
        if polls == 1:
            ctx.pause_event.set()  # paused while the wait is on
        return polls < 4

    monkeypatch.setattr(onedrive, "upload_pending", pending)
    messages: list[str] = []
    ctx.progress = lambda _d, _t, m: messages.append(m)
    count = 0

    def sleep(_s: float) -> None:
        nonlocal count
        count += 1
        if count == 5:
            ctx.pause_event.clear()

    monkeypatch.setattr(plan_module, "_sleep", sleep)
    result = apply_plan(_plan(tmp_path, 3), ctx)
    assert result.status == "done"
    assert any("Paused" in m for m in messages)
    assert polls >= 2


def test_the_heartbeat_keeps_running_while_waiting(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onedrive, "upload_pending", lambda _p: True)
    clock = {"t": 0.0}
    monkeypatch.setattr(plan_module, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        plan_module, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + 20 * s)
    )
    touches: list[int] = []
    monkeypatch.setattr(journal, "touch_run", touches.append)
    apply_plan(_plan(tmp_path, 3), _ctx(tmp_path, journal, _pace(upload_timeout_s=300.0)))
    assert len(touches) >= 3


def test_after_one_full_timeout_with_no_progress_later_batches_are_not_waited_for(
    tmp_path: Path, journal: Journal, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def stuck(path: Path) -> bool | None:
        calls.append(path.name)
        return True  # OneDrive never uploads anything (paused, or not running)

    monkeypatch.setattr(onedrive, "upload_pending", stuck)
    ctx = _ctx(tmp_path, journal, _pace(upload_timeout_s=10.0))
    result = apply_plan(_plan(tmp_path, 6), ctx)
    assert result.status == "done" and result.count("done") == 6
    assert sum(sleeps) == pytest.approx(10.0)  # only the first batch waited the full timeout
    assert set(calls) == {"f0.txt", "f1.txt"}  # the second batch was never polled
    assert any("did not upload anything" in n and "continuing" in n for n in result.notes)


def test_progress_during_a_wait_keeps_later_waits_going(
    tmp_path: Path, journal: Journal, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    f1_checks = 0

    def pending(path: Path) -> bool | None:
        nonlocal f1_checks
        if path.name == "f1.txt":
            f1_checks += 1
            return f1_checks <= 1  # uploads during the first wait
        return path.name in {"f2.txt", "f3.txt"}  # the second batch never uploads

    monkeypatch.setattr(onedrive, "upload_pending", pending)
    result = apply_plan(_plan(tmp_path, 6), _ctx(tmp_path, journal, _pace(upload_timeout_s=10.0)))
    gave_up = [n for n in result.notes if "did not upload anything" in n]
    assert len(gave_up) == 1  # only after the second batch's full timeout without progress
    assert sum(sleeps) == pytest.approx(10.0 + 5.0)
