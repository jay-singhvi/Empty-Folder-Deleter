"""RunContext: what a tool receives from the GUI or CLI. It replaces print, input and Qt calls.

Tools use ``ctx.log`` for messages, ``ctx.report`` for progress, ``ctx.check_cancelled()`` between
steps, ``ctx.deps`` for external programs and ``ctx.settings`` for user choices.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.settings import Settings

ProgressSink = Callable[[int, int, str], None]


class Cancelled(Exception):
    """Raised by :meth:`RunContext.check_cancelled` once the user has cancelled the run."""


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

    def report(self, done: int, total: int, message: str = "") -> None:
        """Report progress. The GUI and the CLI each draw it their own way."""
        self.progress(done, total, message)

    def cancel(self) -> None:
        self.cancel_event.set()

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled("run cancelled")
