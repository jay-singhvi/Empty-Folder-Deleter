"""RunContext: what a tool receives from the GUI or CLI. It replaces print, input and Qt calls.

Tools use ``ctx.log`` for messages, ``ctx.report`` for progress, ``ctx.check_cancelled()`` between
steps, ``ctx.deps`` for external programs and ``ctx.settings`` for user choices.

Front ends build one with :func:`make_context`, which wires the user's settings into the
external-program lookup and opens the history journal. Cancel from any thread with
``ctx.cancel()``; the tool raises :class:`Cancelled` at its next ``check_cancelled()``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.settings import Settings

ProgressSink = Callable[[int, int, str], None]


class Cancelled(Exception):
    """Raised by :meth:`RunContext.check_cancelled` once the user has cancelled the run."""


@dataclass(frozen=True)
class Pace:
    """Apply in batches: after every ``batch_size`` executed actions wait ``pause_s`` seconds.

    It keeps a sync client such as OneDrive from being flooded by thousands of changes at once.
    """

    batch_size: int
    pause_s: float

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError("batch size must be at least 1")
        if self.pause_s < 0:
            raise ValueError("pause must not be negative")


def _no_progress(done: int, total: int, message: str) -> None:
    return None


@dataclass
class RunContext:
    settings: Settings
    deps: ExternalTools
    journal: Journal | None = None
    progress: ProgressSink = _no_progress
    cancel_event: threading.Event = field(default_factory=threading.Event)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("various_tools.run"))
    # Paced apply: ``pace`` None means all at once. ``pause_event`` set means "paused" (checked
    # between actions); ``skip_wait_event`` set cuts the wait between two batches short.
    pace: Pace | None = None
    pause_event: threading.Event = field(default_factory=threading.Event)
    skip_wait_event: threading.Event = field(default_factory=threading.Event)

    def report(self, done: int, total: int, message: str = "") -> None:
        """Report progress. The GUI and the CLI each draw it their own way."""
        self.progress(done, total, message)

    def cancel(self) -> None:
        self.cancel_event.set()

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled("run cancelled")

    def close(self) -> None:
        """Close the journal, if one was opened. Safe to call twice."""
        if self.journal is not None:
            self.journal.close()
            self.journal = None


def make_deps(settings: Settings, one_off: Mapping[str, str | None] | None = None) -> ExternalTools:
    """External-program lookup that honours the user's saved paths and timeouts.

    ``one_off`` holds paths (or, for ``ollama``, a URL) given for one run only, such as the CLI's
    ``--exiftool`` flag. They win over the saved paths and are never written to settings.
    """
    # The saved ``ollama_url`` is not an override here: tools pass it to ``resolve_ollama``.
    overrides: dict[str, str | None] = dict(settings.external_tools)
    for name, value in (one_off or {}).items():
        if value:
            overrides[name] = value
    return ExternalTools(
        overrides,
        version_timeout=settings.version_check_timeout_s,
        ollama_timeout=settings.ollama_probe_timeout_s,
    )


def make_context(
    settings: Settings,
    *,
    one_off: Mapping[str, str | None] | None = None,
    with_journal: bool = True,
    progress: ProgressSink | None = None,
    cancel_event: threading.Event | None = None,
) -> RunContext:
    """A ready-to-use context. Opens the journal at ``settings.journal_path()`` unless
    ``with_journal`` is False (a scan needs none). Call ``ctx.close()`` when done."""
    return RunContext(
        settings=settings,
        deps=make_deps(settings, one_off),
        journal=Journal(settings.journal_path()) if with_journal else None,
        progress=progress or _no_progress,
        cancel_event=cancel_event or threading.Event(),
    )
