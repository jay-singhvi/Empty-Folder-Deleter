"""Run work off the UI thread. The UI only hears about it through Qt signals.

Two entry points:

* :class:`Worker` runs any ``fn(worker)`` on its own ``QThread``. ``fn`` may call
  ``worker.report(done, total, message)`` and should check ``worker.cancel_event`` (or call
  ``worker.check_cancelled()``) between steps. Use it for thumbnails, "Explain match", program
  checks and undo.
* :func:`tool_worker` runs ``job(ctx)`` with a :class:`~various_tools.core.context.RunContext`
  built from the app state, exactly like ``cli/runner.py: run_job()``: the journal is opened only
  when ``with_journal`` is set (apply), progress goes to the ``progress`` signal, and
  :meth:`Worker.cancel` sets the context's cancel event. The context is closed in the worker.

Signals (always delivered on the UI thread, because the receivers live there):

* ``progress(int, int, str)``: throttled to about 20 per second, the last report always arrives;
* exactly one of ``succeeded(object)`` (the return value), ``failed(object)`` (the exception) or
  ``cancelled()`` (``Cancelled`` was raised);
* then ``QThread.finished``.

Every started worker is kept in a module registry (:func:`active_workers`) until its thread has
really finished, so no ``QThread`` object is ever destroyed while its thread runs, whoever started
it and whatever happened to the widget that did. The main window uses the registry on close: it
cancels them all and waits. A worker created with ``writes=True`` changes files or the journal (an
apply or an undo); closing the window asks before stopping one of those.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import shiboken6
from PySide6.QtCore import QObject, QThread, QTimer, Signal

from various_tools.core.context import Cancelled, RunContext, make_context
from various_tools.gui.state import AppState

log = logging.getLogger(__name__)

_MIN_INTERVAL_S = 0.05

# Started workers whose thread has not finished yet. Holding them here keeps the Python wrapper
# (and so the QThread object) alive until the thread is done.
_ACTIVE: set[Worker] = set()


def active_workers() -> list[Worker]:
    """Workers that were started and whose thread has not finished."""
    return [w for w in _ACTIVE if shiboken6.isValid(w) and not w.isFinished()]


def applying_run_ids() -> set[int]:
    """Journal run ids that a live worker in this process is applying right now.

    Such a run must never be undone, however long it has been paused or waiting.
    """
    return {
        run_id for w in active_workers() if w.writes and (run_id := w.applying_run_id()) is not None
    }


def cancel_all() -> None:
    """Ask every running worker to stop at its next check."""
    for worker in list(_ACTIVE):
        worker.cancel()


class Worker(QThread):
    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()

    def __init__(
        self,
        fn: Callable[[Worker], Any],
        parent: QObject | None = None,
        *,
        writes: bool = False,
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self.writes = writes
        self.cancel_event = threading.Event()
        self.ctx: RunContext | None = None  # set by tool_worker for an apply
        self._last_emit = 0.0
        # Connected first, so it runs before anyone else hears ``finished``.
        self.finished.connect(self._release)

    def start(self, priority: QThread.Priority = QThread.Priority.InheritPriority) -> None:
        _ACTIVE.add(self)
        super().start(priority)

    def _release(self) -> None:
        # ``finished`` is queued from the worker thread, which may still be winding down: wait
        # for it so the object can be deleted safely afterwards. The reference is dropped on a
        # later turn of the event loop, after every other ``finished`` handler has run.
        self.wait()
        QTimer.singleShot(0, lambda: _ACTIVE.discard(self))

    # ----- called from the worker thread -------------------------------------------

    def report(self, done: int, total: int, message: str = "") -> None:
        now = time.monotonic()
        if done < total and now - self._last_emit < _MIN_INTERVAL_S:
            return
        self._last_emit = now
        self.progress.emit(done, total, message)

    def applying_run_id(self) -> int | None:
        """The journal run this worker is applying, once its apply has started."""
        ctx = self.ctx
        return ctx.applying_run_id if ctx is not None else None

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled("run cancelled")

    def run(self) -> None:
        try:
            result = self._fn(self)
        except Cancelled:
            self.cancelled.emit()
        except BaseException as exc:  # shown to the user by whoever listens to ``failed``
            log.exception("Background job failed")
            self.failed.emit(exc)
        else:
            self.succeeded.emit(result)

    # ----- called from the UI thread ------------------------------------------------

    def cancel(self) -> None:
        """Ask the job to stop at its next check. Safe to call more than once."""
        self.cancel_event.set()

    def start_job(self) -> Worker:
        self.start()
        return self


def tool_worker(
    state: AppState,
    job: Callable[[RunContext], Any],
    *,
    with_journal: bool,
    parent: QObject | None = None,
) -> Worker:
    """A (not yet started) worker that runs ``job(ctx)`` with a fresh run context.

    The context uses ``state.settings`` and the session's program lookup (``state.deps``). A job
    with the journal is an apply, so the worker is marked ``writes``.
    """
    settings = state.settings
    deps = state.deps

    def run(worker: Worker) -> Any:
        ctx = make_context(
            settings,
            with_journal=with_journal,
            progress=worker.report,
            cancel_event=worker.cancel_event,
        )
        ctx.deps = deps
        worker.ctx = ctx
        try:
            return job(ctx)
        finally:
            ctx.applying_run_id = None
            ctx.close()

    return Worker(run, parent, writes=with_journal)


def run_in_background(
    fn: Callable[[Worker], Any],
    *,
    parent: QObject | None = None,
    on_success: Callable[[Any], None] | None = None,
    on_failure: Callable[[BaseException], None] | None = None,
) -> Worker:
    """Start ``fn(worker)`` on a worker thread and connect the optional callbacks."""
    worker = Worker(fn, parent)
    if on_success is not None:
        worker.succeeded.connect(on_success)
    if on_failure is not None:
        worker.failed.connect(on_failure)
    worker.start()
    return worker
