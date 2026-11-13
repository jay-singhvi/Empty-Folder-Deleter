"""The worker-thread runner: results, errors, progress and Ctrl+C."""

from __future__ import annotations

import _thread
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import typer

from various_tools.cli.runner import run_job
from various_tools.cli.state import CliState
from various_tools.core.context import RunContext

if TYPE_CHECKING:
    from tests.cli.conftest import Cli


def _state(cli: Cli) -> CliState:
    return CliState(config_path=cli.config)


def test_a_job_runs_off_the_main_thread_and_returns_its_value(cli: Cli) -> None:
    import threading

    main = threading.get_ident()

    def job(ctx: RunContext) -> int:
        assert threading.get_ident() != main
        ctx.report(1, 2, "halfway")
        return 42

    outcome = run_job(_state(cli), job, label="Testing", with_journal=False)
    assert outcome.unwrap() == 42
    assert not outcome.cancelled


def test_the_journal_is_opened_only_when_asked(cli: Cli) -> None:
    def job(ctx: RunContext) -> bool:
        return ctx.journal is not None

    assert not run_job(_state(cli), job, label="Scan", with_journal=False).unwrap()
    assert not cli.journal.exists()
    assert run_job(_state(cli), job, label="Apply", with_journal=True).unwrap()
    assert cli.journal.exists()


def test_an_exception_in_the_job_becomes_exit_code_1(
    cli: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    def job(ctx: RunContext) -> int:
        raise RuntimeError("disk on fire")

    outcome = run_job(_state(cli), job, label="Testing", with_journal=False)
    assert isinstance(outcome.error, RuntimeError)
    with pytest.raises(typer.Exit) as raised:
        outcome.unwrap()
    assert raised.value.exit_code == 1
    assert "disk on fire" in capsys.readouterr().err


def test_ctrl_c_cancels_the_job_and_exits_130(cli: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    stopped_cleanly: list[bool] = []

    def job(ctx: RunContext) -> None:
        _thread.interrupt_main()  # what Ctrl+C does: KeyboardInterrupt in the main thread
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ctx.check_cancelled()  # raises Cancelled once the main thread called ctx.cancel()
            time.sleep(0.01)
        stopped_cleanly.append(False)

    outcome = run_job(_state(cli), job, label="Testing", with_journal=False)
    assert outcome.cancelled
    assert stopped_cleanly == [], "the job saw the cancel request and stopped"
    with pytest.raises(typer.Exit) as raised:
        outcome.unwrap()
    assert raised.value.exit_code == 130
    assert "Cancelled" in capsys.readouterr().err


def test_cancel_raised_by_the_tool_counts_as_cancelled(cli: Cli) -> None:
    from various_tools.core.context import Cancelled

    def job(ctx: RunContext) -> None:
        raise Cancelled("stop")

    assert run_job(_state(cli), job, label="Testing", with_journal=False).cancelled


def test_settings_path_is_not_created_by_a_run(cli: Cli, tmp_path: Path) -> None:
    other = CliState(config_path=tmp_path / "absent.toml")
    other_cli_state = other.settings()
    assert other_cli_state.workers >= 1
    assert not (tmp_path / "absent.toml").exists()
