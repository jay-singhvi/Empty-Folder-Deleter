"""GUI test setup. Nothing here can reach the real settings, journal, registry or Recycle Bin.

* ``gui_state`` is an ``AppState`` whose settings file, data folder, cache folder and OneDrive
  root all live under ``tmp_path``.
* Trash calls go to a fake that removes the item inside the temp folder and records it.
* Every modal Qt dialog (message box, file dialog, input dialog) raises, so a test that would
  block on one fails at once instead of hanging. Pages get quiet hooks from ``dialogs``.
* Background workers that outlive a test are drained before the next one starts.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
from pytestqt.qtbot import QtBot

from various_tools.core import plan as plan_module
from various_tools.gui import app as gui_app
from various_tools.gui import history_page as history_module
from various_tools.gui.main_window import MainWindow
from various_tools.gui.shell import FakeRegistry
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.widgets import compare_view as compare_module
from various_tools.gui.widgets import external_tools as external_module
from various_tools.gui.workers import Worker

# An address that refuses connections at once, so Ollama probes never wait.
UNREACHABLE_OLLAMA = "http://127.0.0.1:9"


class Dialogs:
    """Quiet stand-ins for every dialog hook a page has. Records what was asked."""

    def __init__(self) -> None:
        self.confirms: list[tuple[str, str]] = []
        self.informs: list[tuple[str, str]] = []
        self.asked: list[tuple[str, str, str]] = []
        self.answers: list[bool] = []  # used first-in first-out; then ``default``
        self.default = True
        self.text: str | None = None
        self.export_choice: tuple[str, str] | None = None

    def confirm(self, title: str, text: str) -> bool:
        self.confirms.append((title, text))
        return self.answers.pop(0) if self.answers else self.default

    def inform(self, title: str, text: str) -> None:
        self.informs.append((title, text))

    def ask_text(self, title: str, label: str, text: str) -> str | None:
        self.asked.append((title, label, text))
        return self.text

    def choose_export(self) -> tuple[str, str] | None:
        return self.export_choice

    def attach(self, page: Any) -> None:
        """Replace the hooks the page has (tool pages have all four, others two)."""
        page.confirm = self.confirm
        page.inform = self.inform
        if isinstance(page, ToolPage):
            page.ask_text = self.ask_text
            page.choose_export = self.choose_export


def toml_value(value: Any) -> str:
    """A TOML literal for a str, bool, int, float or list of those (JSON is valid TOML here)."""
    return json.dumps(value)


@pytest.fixture(autouse=True)
def trashed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Recycle-Bin calls are recorded and carried out as a plain delete in the temp folder."""
    calls: list[str] = []

    def fake_send2trash(path: str) -> None:
        calls.append(path)
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.remove(path)

    monkeypatch.setattr(plan_module, "send2trash", fake_send2trash)
    return calls


@pytest.fixture(autouse=True)
def problems(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """``report_problem`` (the Locate failure box) records instead of opening a box."""
    seen: list[tuple[str, str]] = []

    def record(_parent: object, title: str, text: str) -> None:
        seen.append((title, text))

    monkeypatch.setattr(external_module, "report_problem", record)
    return seen


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a modal dialog was opened; the test must replace that hook")

    for owner, names in (
        (QMessageBox, ("question", "warning", "information", "critical")),
        (
            QFileDialog,
            ("getOpenFileName", "getSaveFileName", "getExistingDirectory", "getOpenFileNames"),
        ),
        (QInputDialog, ("getText",)),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, boom)


@pytest.fixture(autouse=True)
def _drain_workers(qtbot: QtBot) -> Iterator[None]:
    """After each test, let module-level background workers finish."""
    yield

    def idle() -> bool:
        QCoreApplication.processEvents()
        running: list[Worker] = [
            *history_module._RUNNING,  # pyright: ignore[reportPrivateUsage]
            *external_module._RUNNING,  # pyright: ignore[reportPrivateUsage]
            *compare_module._LIVE,  # pyright: ignore[reportPrivateUsage]
        ]
        return not any(w.isRunning() for w in running)

    qtbot.waitUntil(idle, timeout=15000)


@pytest.fixture
def write_settings(tmp_path: Path) -> Callable[..., Path]:
    """``write_settings(**extra)``: a settings TOML under ``tmp_path`` pointing only at it."""

    def _write(**extra: Any) -> Path:
        path = tmp_path / "cfg" / "settings.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        values: dict[str, Any] = {
            "data_folder": str(tmp_path / "data"),
            "cache_folder": str(tmp_path / "cache"),
            "onedrive_root": str(tmp_path / "onedrive"),
            "ollama_url": UNREACHABLE_OLLAMA,
            "ollama_probe_timeout_s": 0.2,
        }
        values.update(extra)
        text = "".join(f"{key} = {toml_value(value)}\n" for key, value in values.items())
        path.write_text(text, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def make_state(write_settings: Callable[..., Path]) -> Callable[..., AppState]:
    """``make_state(**extra_settings)``: an ``AppState`` loaded from a fresh temp settings file."""

    def _make(**extra: Any) -> AppState:
        return AppState.load(write_settings(**extra))

    return _make


@pytest.fixture
def gui_state(make_state: Callable[..., AppState]) -> AppState:
    return make_state()


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def dialogs() -> Dialogs:
    return Dialogs()


@pytest.fixture
def make_window(
    qtbot: QtBot, gui_state: AppState, registry: FakeRegistry, dialogs: Dialogs
) -> Callable[..., MainWindow]:
    """``make_window(state=None, tool=None, folder=None)``: a window with quiet dialogs."""

    def _make(
        state: AppState | None = None, tool: str | None = None, folder: str | None = None
    ) -> MainWindow:
        window = gui_app.make_window(
            state or gui_state, tool=tool, folder=folder, shell_backend=registry
        )
        qtbot.addWidget(window)
        dialogs.attach(window.history_page)
        dialogs.attach(window.settings_page)
        for page in window.tool_pages.values():
            dialogs.attach(page)
        return window

    return _make


@pytest.fixture
def window(make_window: Callable[..., MainWindow]) -> MainWindow:
    return make_window()


@pytest.fixture
def open_tool(window: MainWindow, dialogs: Dialogs) -> Callable[..., ToolPage]:
    """``open_tool(tool_id, folder=None)``: open the page in ``window`` with quiet dialogs."""

    def _open(tool_id: str, folder: str | None = None) -> ToolPage:
        page = window.open_tool(tool_id, folder)
        assert page is not None
        dialogs.attach(page)
        return page

    return _open


@pytest.fixture
def wait_idle(qtbot: QtBot) -> Callable[[ToolPage], None]:
    def _wait(page: ToolPage) -> None:
        qtbot.waitUntil(lambda: page.worker is None, timeout=60000)

    return _wait


@pytest.fixture
def scan(wait_idle: Callable[[ToolPage], None]) -> Callable[[ToolPage], None]:
    """Scan a page and wait until the plan is shown."""

    def _scan(page: ToolPage) -> None:
        worker = page.scan()
        assert worker is not None, "scan() did not start (busy or invalid options)"
        wait_idle(page)
        assert page.plan is not None

    return _scan


@pytest.fixture
def apply(wait_idle: Callable[[ToolPage], None]) -> Callable[[ToolPage], None]:
    """Apply a page's plan (dialogs must say yes) and wait until it finished."""

    def _apply(page: ToolPage) -> None:
        worker = page.apply()
        assert worker is not None, "apply() did not start"
        wait_idle(page)
        assert page.result is not None

    return _apply


@pytest.fixture
def undo(wait_idle: Callable[[ToolPage], None]) -> Callable[[ToolPage], None]:
    def _undo(page: ToolPage) -> None:
        worker = page.undo_last()
        assert worker is not None, "undo_last() did not start"
        wait_idle(page)

    return _undo
