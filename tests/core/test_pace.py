"""Paced apply: batches, waits, pause, skip the wait, cancel, one journal run, undo."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from various_tools.core import onedrive
from various_tools.core import plan as plan_module
from various_tools.core.context import Pace, RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
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


def test_pace_validates() -> None:
    with pytest.raises(ValueError):
        Pace(0, 1)
    with pytest.raises(ValueError):
        Pace(1, -1)


def test_waits_after_each_full_batch_but_not_after_the_last(
    tmp_path: Path, journal: Journal, sleeps: list[float]
) -> None:
    ctx = _ctx(tmp_path, journal, Pace(2, 1.0))
    messages: list[str] = []
    ctx.progress = lambda _d, _t, m: messages.append(m)
    result = apply_plan(_plan(tmp_path, 5), ctx)
    assert result.status == "done" and result.count("done") == 5
    # Two waits (after 2 and after 4), each four steps of 0.25 s.
    assert sleeps == [0.25] * 8
    assert any("Batch 1 of 3 done. Next batch in 0:01." in m for m in messages)
    assert any("Batch 2 of 3 done." in m for m in messages)
    assert not any("Batch 3 of 3" in m for m in messages)


def test_exact_multiple_has_no_trailing_wait(
    tmp_path: Path, journal: Journal, sleeps: list[float]
) -> None:
    apply_plan(_plan(tmp_path, 4), _ctx(tmp_path, journal, Pace(2, 0.5)))
    assert sleeps == [0.25] * 2  # only between the two batches


def test_no_pace_means_no_waiting(tmp_path: Path, journal: Journal, sleeps: list[float]) -> None:
    apply_plan(_plan(tmp_path, 3), _ctx(tmp_path, journal, None))
    assert sleeps == []


def test_skipped_actions_do_not_count_towards_a_batch(
    tmp_path: Path, journal: Journal, sleeps: list[float]
) -> None:
    plan = _plan(tmp_path, 3)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    # Two refused actions (outside the roots) first, then three real ones, batch of 3.
    refused = [
        PlannedAction(ActionKind.MOVE, src=outside, dst=tmp_path / "x1.txt"),
        PlannedAction(ActionKind.MOVE, src=outside, dst=tmp_path / "x2.txt"),
    ]
    plan.actions[:0] = refused
    result = apply_plan(plan, _ctx(tmp_path, journal, Pace(3, 1.0)))
    assert result.count("skipped") == 2 and result.count("done") == 3
    assert sleeps == []  # the three real ones are one batch, nothing remains


def test_cancel_during_a_wait_ends_the_run_as_cancelled(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, journal, Pace(1, 60.0))
    seen: list[float] = []

    def sleep(step: float) -> None:
        seen.append(step)
        ctx.cancel()

    monkeypatch.setattr(plan_module, "_sleep", sleep)
    plan = _plan(tmp_path, 3)
    result = apply_plan(plan, ctx)
    assert result.status == "cancelled"
    assert result.count("done") == 1 and len(seen) == 1
    assert journal.run(result.run_id).status == "cancelled"  # type: ignore[union-attr]


def test_skip_wait_cuts_the_wait_short(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, journal, Pace(1, 60.0))
    calls: list[float] = []

    def sleep(step: float) -> None:
        calls.append(step)
        ctx.skip_wait_event.set()

    monkeypatch.setattr(plan_module, "_sleep", sleep)
    result = apply_plan(_plan(tmp_path, 3), ctx)
    assert result.status == "done" and result.count("done") == 3
    assert len(calls) == 2  # one step per wait, then skipped
    assert not ctx.skip_wait_event.is_set()


def _resume_after(ctx: RunContext, polls: int) -> Callable[[float], None]:
    count = 0

    def sleep(_step: float) -> None:
        nonlocal count
        count += 1
        if count >= polls:
            ctx.pause_event.clear()

    return sleep


def test_pause_holds_between_actions_until_resumed(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, journal, None)
    ctx.pause_event.set()
    messages: list[str] = []
    ctx.progress = lambda _d, _t, m: messages.append(m)
    monkeypatch.setattr(plan_module, "_sleep", _resume_after(ctx, 3))
    result = apply_plan(_plan(tmp_path, 2), ctx)
    assert result.status == "done" and result.count("done") == 2
    assert messages.count("Paused.") == 3


def test_cancel_while_paused_stops(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, journal, None)
    ctx.pause_event.set()
    monkeypatch.setattr(plan_module, "_sleep", lambda _s: ctx.cancel())
    result = apply_plan(_plan(tmp_path, 2), ctx)
    assert result.status == "cancelled" and result.count("done") == 0


def test_a_paced_run_is_one_journal_run_and_undoes_in_one_go(
    tmp_path: Path, journal: Journal, sleeps: list[float]
) -> None:
    plan = _plan(tmp_path, 5)
    result = apply_plan(plan, _ctx(tmp_path, journal, Pace(2, 0.25)))
    assert len(journal.actions(result.run_id)) == 5
    report = undo_run(journal, result.run_id)
    assert report.reverted == 5 and report.status == "undone"
    for i in range(5):
        assert (tmp_path / "work" / f"f{i}.txt").exists()


def test_is_inside_onedrive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "OneDrive"
    assert not onedrive.is_inside_onedrive(root / "a.txt")
    monkeypatch.setenv("OneDriveCommercial", str(root))
    assert onedrive.is_inside_onedrive(root / "a" / "b.txt")
    assert onedrive.is_inside_onedrive(root)
    assert not onedrive.is_inside_onedrive(tmp_path / "OneDrive2" / "a.txt")
    assert not onedrive.is_inside_onedrive(tmp_path / "other.txt")


def _fake_clock(monkeypatch: pytest.MonkeyPatch, ctx: RunContext, polls: int) -> list[float]:
    """A clock that moves 5 s per sleep; the pause is lifted after ``polls`` sleeps."""
    now = [0.0]
    count = [0]

    def sleep(_step: float) -> None:
        now[0] += 5.0
        count[0] += 1
        if count[0] >= polls:
            ctx.pause_event.clear()

    monkeypatch.setattr(plan_module, "_sleep", sleep)
    monkeypatch.setattr(plan_module, "_monotonic", lambda: now[0])
    return now


def test_a_paused_apply_touches_its_journal_run(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[int] = []
    real = journal.touch_run
    monkeypatch.setattr(journal, "touch_run", lambda rid: (touched.append(rid), real(rid)))
    ctx = _ctx(tmp_path, journal, None)
    ctx.pause_event.set()
    _fake_clock(monkeypatch, ctx, polls=20)  # 100 s of pause
    result = apply_plan(_plan(tmp_path, 1), ctx)
    assert result.status == "done"
    assert len(touched) >= 3 and set(touched) == {result.run_id}


def test_an_apply_waiting_between_batches_touches_its_journal_run(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[int] = []
    real = journal.touch_run
    monkeypatch.setattr(journal, "touch_run", lambda rid: (touched.append(rid), real(rid)))
    ctx = _ctx(tmp_path, journal, Pace(1, 100.0))  # waits 100 s, the clock moves 5 s per step
    _fake_clock(monkeypatch, ctx, polls=10**9)
    apply_plan(_plan(tmp_path, 2), ctx)
    assert len(touched) >= 3


def test_the_context_knows_the_run_it_is_applying(tmp_path: Path, journal: Journal) -> None:
    ctx = _ctx(tmp_path, journal, None)
    seen: list[int | None] = []
    ctx.progress = lambda _d, _t, _m: seen.append(ctx.applying_run_id)
    result = apply_plan(_plan(tmp_path, 1), ctx)
    assert seen and set(seen) == {result.run_id}
    assert ctx.applying_run_id is None


def test_a_skip_asked_earlier_does_not_cut_a_later_wait_short(
    tmp_path: Path, journal: Journal, sleeps: list[float]
) -> None:
    ctx = _ctx(tmp_path, journal, Pace(1, 1.0))
    ctx.skip_wait_event.set()  # stale: set while not waiting
    apply_plan(_plan(tmp_path, 2), ctx)
    assert sleeps == [0.25] * 4  # the full wait ran


def test_a_pause_inside_the_wait_says_it_is_waiting(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, journal, Pace(1, 1.0))
    messages: list[str] = []
    ctx.progress = lambda _d, _t, m: messages.append(m)
    orig = ctx.progress

    def progress(d: int, t: int, m: str) -> None:
        orig(d, t, m)
        if m.startswith("Batch 1"):
            ctx.pause_event.set()

    ctx.progress = progress
    monkeypatch.setattr(plan_module, "_sleep", _resume_after(ctx, 2))
    apply_plan(_plan(tmp_path, 2), ctx)
    assert plan_module.WAIT_PAUSED_MESSAGE in messages
    assert "Paused." not in messages


def _swap_when_starting(ctx: RunContext, index: int, new: Pace | None) -> None:
    """Replace ``ctx.pace`` just before action ``index`` (0-based) is reported, as the GUI does."""
    count = 0

    def progress(_done: int, _total: int, message: str) -> None:
        nonlocal count
        if message.startswith("move:"):
            if count == index:
                ctx.pace = new
            count += 1

    ctx.progress = progress


def test_a_pace_swapped_mid_run_to_a_larger_batch_counts_from_the_last_wait(
    tmp_path: Path, journal: Journal, sleeps: list[float]
) -> None:
    ctx = _ctx(tmp_path, journal, Pace(1, 0.25))
    _swap_when_starting(ctx, 2, Pace(3, 0.5))  # waits after 1 and 2 (old pace), then after 5
    result = apply_plan(_plan(tmp_path, 7), ctx)
    assert result.status == "done" and result.count("done") == 7
    # one step after action 1, one after action 2 (old pace), two steps (0.5 s) after action 5
    assert sleeps == [0.25] * 4


def test_a_pace_swapped_to_a_smaller_batch_waits_at_once_when_enough_ran(
    tmp_path: Path, journal: Journal, sleeps: list[float]
) -> None:
    ctx = _ctx(tmp_path, journal, Pace(10, 0.25))
    _swap_when_starting(ctx, 3, Pace(2, 0.25))  # 3 ran since the start, so wait before the 4th
    apply_plan(_plan(tmp_path, 5), ctx)
    # wait before action 4 (3 done >= 2), then 1 more ran: no second wait
    assert sleeps == [0.25]


def test_none_then_a_pace_starts_waiting(
    tmp_path: Path, journal: Journal, sleeps: list[float]
) -> None:
    ctx = _ctx(tmp_path, journal, None)
    _swap_when_starting(ctx, 2, Pace(2, 0.25))
    apply_plan(_plan(tmp_path, 6), ctx)
    # no waits for 2 actions, then the 3rd sees 2 since the start: one wait, then 2 more, wait
    assert sleeps == [0.25, 0.25]


def test_a_pace_then_none_stops_waiting(
    tmp_path: Path, journal: Journal, sleeps: list[float]
) -> None:
    ctx = _ctx(tmp_path, journal, Pace(2, 0.25))
    _swap_when_starting(ctx, 3, None)
    apply_plan(_plan(tmp_path, 8), ctx)
    assert sleeps == [0.25]  # only the wait after the first two
