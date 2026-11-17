"""CLI test setup. Every run points at a settings file under ``tmp_path`` whose ``data_folder`` is
also under ``tmp_path``, so the journal and the presets never touch the real user data."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import typer.rich_utils
from typer.testing import CliRunner, Result

from various_tools.cli.main import app
from various_tools.core import journal as journal_module
from various_tools.core import plan as plan_module
from various_tools.core import recycle as recycle_module
from various_tools.core import schedule as schedule_module


@dataclass
class Cli:
    """Runs ``various-tools`` in-process against temporary settings."""

    root: Path
    config: Path
    state: Path  # data folder: journal.sqlite and presets.json land here
    work: Path  # a folder for generated test trees

    def run(
        self,
        *args: str,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        config: bool = True,
    ) -> Result:
        runner = CliRunner()
        environment = {
            "COLUMNS": "250",
            "TERMINAL_WIDTH": "250",
            "VARIOUS_TOOLS_CONFIG": str(self.config),
            **(env or {}),
        }
        argv = ["--config", str(self.config), *args] if config else list(args)
        return runner.invoke(app, argv, input=input, env=environment)

    def write_settings(self, extra: str = "") -> None:
        # A short Ollama probe: nothing listens in tests, and a refused connection is slow here.
        folder = json.dumps(str(self.state))
        text = f"data_folder = {folder}\nollama_probe_timeout_s = 0.2\n{extra}"
        self.config.write_text(text, encoding="utf-8")

    @property
    def journal(self) -> Path:
        return self.state / "journal.sqlite"

    @property
    def presets(self) -> Path:
        return self.state / "presets.json"


@pytest.fixture(autouse=True)
def _plain_help_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typer forces a colour terminal when ``GITHUB_ACTIONS`` (or ``FORCE_COLOR``) is set, so on CI
    the help text is full of ANSI codes and ``--roots`` is split by them. Tests read plain text."""
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", None)


@pytest.fixture(autouse=True)
def _room_for_temp_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task commands built here hold pytest's temp paths (``--config``) and the venv's Python.
    On a CI runner those alone pass Task Scheduler's 261-character limit, which belongs to real
    installs, so the CLI tests lift it. ``tests/core/test_schedule.py`` tests the limit itself."""
    monkeypatch.setattr(schedule_module, "MAX_COMMAND_LENGTH", 4096)


@pytest.fixture(autouse=True)
def _no_real_recycle_bin(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Apply and undo never reach the real Recycle Bin: trashing is a plain delete inside the
    temp folder, and undo finds no bin folder to restore from."""
    calls: list[str] = []

    def fake_send2trash(path: object) -> None:
        text = str(path)
        calls.append(text)
        if os.path.isdir(text):
            os.rmdir(text)
        else:
            os.remove(text)

    monkeypatch.setattr(plan_module, "send2trash", fake_send2trash)
    monkeypatch.setattr(journal_module, "send2trash", fake_send2trash)
    monkeypatch.setattr(recycle_module, "current_user_bin", lambda _original: None)
    return calls


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    work = tmp_path / "work"
    work.mkdir()
    instance = Cli(
        root=tmp_path,
        config=tmp_path / "cfg" / "settings.toml",
        state=tmp_path / "state",
        work=work,
    )
    instance.config.parent.mkdir()
    instance.write_settings()
    return instance


@pytest.fixture
def snapshot() -> Callable[[Path], list[tuple[str, bool]]]:
    """``snapshot(folder)``: every path under ``folder`` (relative) with whether it is a
    directory, sorted. Compare before and after to prove nothing changed."""

    def _snapshot(folder: Path) -> list[tuple[str, bool]]:
        return sorted((str(p.relative_to(folder)), p.is_dir()) for p in folder.rglob("*"))

    return _snapshot


@pytest.fixture
def empty_tree(cli: Cli) -> Path:
    """``work/tree``: two empty folders (one nested), one folder holding only a junk file, and
    one folder with real content that must stay."""
    tree = cli.work / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "c").mkdir()
    (tree / "c" / "Thumbs.db").write_text("junk")
    (tree / "keep").mkdir()
    (tree / "keep" / "file.txt").write_text("data")
    return tree
