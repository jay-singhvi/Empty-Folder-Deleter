"""RunContext: progress, cancellation, and the logger it hands to tools."""

from __future__ import annotations

import logging

import pytest

from various_tools.core.context import Cancelled, RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.settings import Settings


def _ctx() -> RunContext:
    return RunContext(settings=Settings(), deps=ExternalTools(env={}))


def test_report_forwards_to_the_progress_sink() -> None:
    seen: list[tuple[int, int, str]] = []
    ctx = _ctx()
    ctx.progress = lambda done, total, msg: seen.append((done, total, msg))
    ctx.report(1, 4, "hashing")
    assert seen == [(1, 4, "hashing")]


def test_default_progress_sink_is_silent() -> None:
    _ctx().report(0, 0)  # must not raise or print


def test_check_cancelled_raises_once_cancelled() -> None:
    ctx = _ctx()
    ctx.check_cancelled()
    ctx.cancel()
    with pytest.raises(Cancelled):
        ctx.check_cancelled()


def test_logger_is_a_standard_logger() -> None:
    ctx = _ctx()
    assert isinstance(ctx.log, logging.Logger)
    assert ctx.log.name == "various_tools.run"
