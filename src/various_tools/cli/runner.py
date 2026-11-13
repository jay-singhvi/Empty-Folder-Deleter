"""Run a scan, apply or other job on a worker thread, with a progress bar and Ctrl+C cancel.

This is the programmatic recipe the GUI mirrors: build a context with
:func:`~various_tools.core.context.make_context` (journal only for apply), call
``tool.scan(opts, ctx)`` or ``tool.apply(plan, ctx)`` off the main thread, feed ``ctx.progress`` to
the display, and call ``ctx.cancel()`` to stop. :func:`run_job` does exactly that for the CLI.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import typer
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

from various_tools.cli.state import EXIT_CANCELLED, EXIT_FAILED, CliState, err, fail, note
from various_tools.core.context import Cancelled, RunContext, make_context


@dataclass
class JobOutcome[T]:
    """How a job ended: its value, or the exception it raised, or that it was cancelled."""

    value: T | None = None
    error: BaseException | None = None
    cancelled: bool = False

    def unwrap(self) -> T:
        """The value, or exit: 130 when cancelled, 1 when the job failed."""
        if self.cancelled:
            note("Cancelled.")
            raise typer.Exit(EXIT_CANCELLED)
        if self.error is not None:
            fail(f"{type(self.error).__name__}: {self.error}", EXIT_FAILED)
        assert self.value is not None
        return self.value


class _Feed:
    """The latest progress report from the worker, readable from the main thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: tuple[int, int, str] = (0, 0, "")

    def __call__(self, done: int, total: int, message: str) -> None:
        with self._lock:
            self._state = (done, total, message)

    def snapshot(self) -> tuple[int, int, str]:
        with self._lock:
            return self._state


@contextmanager
def _progress_display(label: str, enabled: bool):
    """Yield ``update(done, total, message)``. Draws a bar on standard error when it is a
    terminal; otherwise does nothing, so logs and pipes stay clean."""
    if not enabled:
        yield lambda done, total, message: None  # pyright: ignore[reportUnknownLambdaType]
        return
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", markup=False),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err(),
        transient=True,
    )
    with progress:
        task: TaskID = progress.add_task(label, total=None)

        def update(done: int, total: int, message: str) -> None:
            text = f"{label}: {message}" if message else label
            progress.update(
                task, description=text[:70], completed=done, total=total if total > 0 else None
            )

        yield update


def run_job[T](
    state: CliState,
    job: Callable[[RunContext], T],
    *,
    label: str,
    with_journal: bool,
    show_progress: bool = True,
) -> JobOutcome[T]:
    """Run ``job(ctx)`` on a worker thread and wait for it on the main thread.

    The main thread draws progress and watches for Ctrl+C. On Ctrl+C it calls ``ctx.cancel()``,
    waits for the worker to stop at its next check and returns an outcome with ``cancelled`` set.
    A second Ctrl+C gives up waiting and exits at once with code 130.
    """
    feed = _Feed()
    cancel_event = threading.Event()
    ctx = make_context(
        state.settings(),
        one_off=state.one_off,
        with_journal=with_journal,
        progress=feed,
        cancel_event=cancel_event,
    )
    outcome: JobOutcome[T] = JobOutcome()

    def target() -> None:
        try:
            outcome.value = job(ctx)
        except Cancelled:
            outcome.cancelled = True
        except BaseException as exc:  # reported to the user by JobOutcome.unwrap
            outcome.error = exc

    worker = threading.Thread(target=target, name="various-tools-worker", daemon=True)
    forced = False
    try:
        worker.start()
        display: Any = _progress_display(label, show_progress and err().is_terminal)
        with display as update:
            while worker.is_alive():
                worker.join(0.05)
                update(*feed.snapshot())
    except KeyboardInterrupt:
        ctx.cancel()
        outcome.cancelled = True
        note("Cancelling: waiting for the current step to finish (Ctrl+C again to quit now).")
        try:
            while worker.is_alive():
                worker.join(0.1)
        except KeyboardInterrupt:
            forced = True
    finally:
        if not forced:
            ctx.close()
    if forced:
        note("Stopped without waiting. Check History: the run may be incomplete.")
        raise typer.Exit(EXIT_CANCELLED) from None
    return outcome
