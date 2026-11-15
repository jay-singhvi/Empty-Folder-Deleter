"""The "External programs" panel for Settings, and the inline "Locate..." for tool pages.

The panel shows one row for each program in ``core/deps.py`` (ffmpeg, ffprobe, ExifTool, Ollama):
whether it was found, where, which version, and how it was found. Each row can browse for the
program, test it, search again, drop the saved path and copy the install command.

Every check of a program (running it, or asking the Ollama server) runs in a worker thread, never
on the UI thread. Only ``core/deps.py`` knows where programs live: this module goes through
``TOOL_SPECS`` and the ``ExternalTools`` lookup and never searches for a program itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import shiboken6
from pydantic import ValidationError
from PySide6.QtCore import QEventLoop, Qt, Signal
from PySide6.QtGui import QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.deps import (
    DEFAULT_OLLAMA_URL,
    TOOL_SPECS,
    ToolLocation,
    ToolSource,
    install_hint,
    probe_ollama,
    probe_version,
)
from various_tools.gui.state import AppState
from various_tools.gui.workers import Worker

log = logging.getLogger(__name__)

PROGRAM_FILTER = "Programs (*.exe)"

# Plain display names for the tool ids in TOOL_SPECS. Anything not listed shows its id.
_DISPLAY_NAMES = {"exiftool": "ExifTool", "ollama": "Ollama"}

_STATUS_COLOURS = {"found": "#3a9a4a", "not working": "#d9822b", "missing": "#d04040"}

# Workers that are running. A QThread must not be garbage collected while it runs.
_RUNNING: set[Worker] = set()


def _start(
    fn: Callable[[Worker], object],
    on_success: Callable[[object], None],
    on_failure: Callable[[BaseException], None],
) -> Worker:
    worker = Worker(fn)
    _RUNNING.add(worker)
    worker.succeeded.connect(on_success)
    worker.failed.connect(on_failure)
    worker.finished.connect(lambda: _RUNNING.discard(worker))
    worker.start()
    return worker


def report_problem(parent: QWidget | None, title: str, text: str) -> None:
    """Show a warning box. A module-level hook, so tests can replace it and never block."""
    QMessageBox.warning(parent, title, text)


def display_name(tool: str) -> str:
    return _DISPLAY_NAMES.get(tool, tool)


@dataclass(frozen=True)
class CheckResult:
    """The outcome of testing one program or server."""

    ok: bool
    version: str | None
    message: str


def check_program(tool: str, path: str, timeout: float) -> CheckResult:
    """Run the program's version command. Call from a worker: it can take up to ``timeout``."""
    cleaned = path.strip().strip('"')
    name = display_name(tool)
    if not cleaned:
        return CheckResult(False, None, f"No file was chosen for {name}.")
    exe = Path(cleaned)
    try:
        is_file = exe.is_file()
    except OSError:
        is_file = False
    if not is_file:
        return CheckResult(False, None, f"There is no file at {cleaned}.")
    version = probe_version(exe, TOOL_SPECS[tool].version_args, timeout)
    if version is None:
        return CheckResult(
            False,
            None,
            f"{cleaned} did not report a version, so it does not look like a working {name}.",
        )
    return CheckResult(True, version, f"{name} works: {version}")


def check_ollama(url: str, timeout: float) -> CheckResult:
    """Ask the Ollama server for its version. Call from a worker."""
    version = probe_ollama(url, timeout)
    if version is None:
        return CheckResult(False, None, f"Ollama did not answer at {url}.")
    return CheckResult(True, version, f"Ollama {version} answered at {url}.")


def status_of(location: ToolLocation) -> str:
    """ "found", "not working" (a saved path or URL failed and nothing else worked) or "missing"."""
    if location.valid and location.source is not ToolSource.NONE:
        return "found"
    return "not working" if location.problems else "missing"


class ToolRow(QFrame):
    """One program: status, location, version, source, buttons and the install command."""

    def __init__(self, tool: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tool = tool
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self.title_label = QLabel(f"<b>{display_name(tool)}</b>")
        self.status_label = QLabel("Checking...")
        self.source_label = QLabel("")
        self.version_label = QLabel("")
        self.version_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.location_edit = QLineEdit()
        self.location_edit.setReadOnly(True)
        self.location_edit.setPlaceholderText("Not found")
        self.message_label = QLabel("")
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.browse_button = QPushButton("Browse...")
        self.test_button = QPushButton("Test")
        self.detect_button = QPushButton("Auto-detect")
        self.reset_button = QPushButton("Reset to automatic")
        self.browse_button.setToolTip(f"Choose the {display_name(tool)} program yourself.")
        self.test_button.setToolTip("Check that it runs and show its version.")
        self.detect_button.setToolTip("Forget what was found and search again.")
        self.reset_button.setToolTip("Remove the saved path and go back to finding it by itself.")
        # Ollama is a server: an address field (the "Ollama address" setting) instead of Browse.
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(DEFAULT_OLLAMA_URL)
        self.url_edit.setToolTip("The Ollama server address (the same setting as in the form).")
        self.save_url_button = QPushButton("Save address")
        self.save_url_button.setToolTip("Save this address, then ask the server for its version.")
        is_ollama = tool == "ollama"
        self.url_edit.setVisible(is_ollama)
        self.save_url_button.setVisible(is_ollama)
        if is_ollama:
            self.browse_button.setVisible(False)
            self.browse_button.setToolTip("")
            self.location_edit.setVisible(False)
            self.test_button.setToolTip("Ask the Ollama server at the address in the field.")
            self.reset_button.setToolTip(f"Go back to the default address, {DEFAULT_OLLAMA_URL}.")

        self.hint_edit = QLineEdit(install_hint(tool))
        self.hint_edit.setReadOnly(True)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Copy the install command to the clipboard.")
        self.copy_button.clicked.connect(self.copy_hint)

        head = QHBoxLayout()
        head.addWidget(self.title_label)
        head.addWidget(self.status_label)
        head.addStretch(1)
        head.addWidget(self.source_label)
        buttons = QHBoxLayout()
        for button in (
            self.browse_button,
            self.test_button,
            self.detect_button,
            self.reset_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        hint = QHBoxLayout()
        hint.addWidget(QLabel("Install:"))
        hint.addWidget(self.hint_edit, 1)
        hint.addWidget(self.copy_button)

        address = QHBoxLayout()
        address.addWidget(self.url_edit, 1)
        address.addWidget(self.save_url_button)

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(self.location_edit)
        layout.addLayout(address)
        layout.addWidget(self.version_label)
        layout.addWidget(self.message_label)
        layout.addLayout(buttons)
        layout.addLayout(hint)

    def copy_hint(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.hint_edit.text())

    def set_location(self, location: ToolLocation, shown: str, *, has_override: bool) -> None:
        """Fill the row. ``shown`` is the path (or, for Ollama, the address) to display."""
        status = status_of(location)
        self.status_label.setText(f'<span style="color:{_STATUS_COLOURS[status]}">{status}</span>')
        self.location_edit.setText(shown)
        self.location_edit.setCursorPosition(0)
        if self.tool == "ollama" and not self.url_edit.isModified():
            self.url_edit.setText(shown)
        self.version_label.setText(f"Version: {location.version}" if location.version else "")
        self.source_label.setText(f"Found by: {location.source}" if status == "found" else "")
        self.message_label.setText("; ".join(location.problems))
        self.reset_button.setEnabled(has_override)

    def show_message(self, text: str) -> None:
        self.message_label.setText(text)


class ExternalToolsPanel(QGroupBox):
    """Status, path, version and source of every external program, with the buttons to fix them.

    The panel looks again each time it is shown, and after every change it makes itself. The
    Ollama row edits the "Ollama address" setting (``ollama_url``, the same one the Settings form
    shows) and emits ``settingsSaved`` so the form can show the new value.
    """

    settingsSaved = Signal()

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__("External programs", parent)
        self._state = state
        self._refreshing = False
        self.rows: dict[str, ToolRow] = {}
        layout = QVBoxLayout(self)
        for tool in TOOL_SPECS:
            row = ToolRow(tool, self)
            row.reset_button.setEnabled(self.has_override(tool))
            self.rows[tool] = row
            layout.addWidget(row)
            row.browse_button.clicked.connect(lambda _=False, t=tool: self.browse(t))
            row.test_button.clicked.connect(lambda _=False, t=tool: self.test(t))
            row.detect_button.clicked.connect(lambda _=False: self.auto_detect())
            row.reset_button.clicked.connect(lambda _=False, t=tool: self.reset(t))
        ollama = self.rows.get("ollama")
        if ollama is not None:
            ollama.url_edit.setText(state.settings.ollama_url)
            ollama.save_url_button.clicked.connect(
                lambda _=False, r=ollama: self.set_ollama_url(r.url_edit.text())
            )
            ollama.url_edit.returnPressed.connect(
                lambda r=ollama: self.set_ollama_url(r.url_edit.text())
            )

    def has_override(self, tool: str) -> bool:
        """Whether Reset has anything to undo for this row."""
        settings = self._state.settings
        if tool == "ollama":
            return settings.ollama_url != DEFAULT_OLLAMA_URL or tool in settings.external_tools
        return tool in settings.external_tools

    # ----- looking up ------------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if not self._refreshing:
            self.refresh()

    def refresh(self) -> Worker:
        """Resolve all four programs in a worker, then fill the rows."""
        deps = self._state.deps
        url = self._state.settings.ollama_url
        self._refreshing = True

        def job(_worker: Worker) -> dict[str, ToolLocation]:
            found = {tool: deps.resolve(tool) for tool in TOOL_SPECS if tool != "ollama"}
            found["ollama"] = deps.resolve_ollama(url)
            return {tool: found[tool] for tool in TOOL_SPECS}

        return _start(job, self._fill, self._refresh_failed)

    def _fill(self, result: object) -> None:
        self._refreshing = False
        if not shiboken6.isValid(self) or not isinstance(result, dict):
            return
        settings = self._state.settings
        for tool, location in result.items():  # pyright: ignore[reportUnknownVariableType]
            row = self.rows.get(tool)  # pyright: ignore[reportUnknownArgumentType]
            if row is None or not isinstance(location, ToolLocation):
                continue
            if tool == "ollama":
                shown = settings.ollama_url  # the setting the field edits
            else:
                saved = settings.external_tools.get(tool, "")
                shown = str(location.path) if location.valid and location.path else saved
            row.set_location(location, shown, has_override=self.has_override(tool))

    def _refresh_failed(self, exc: BaseException) -> None:
        self._refreshing = False
        if shiboken6.isValid(self):
            for row in self.rows.values():
                row.show_message(f"Could not check the programs: {exc}")

    # ----- buttons ---------------------------------------------------------------------------

    def browse(self, tool: str) -> Worker | None:
        """Pick the program in a file dialog, then check and save it (see :meth:`set_program`)."""
        current = self._state.settings.external_tools.get(tool, "")
        start = str(Path(current).parent) if current else ""
        chosen, _filter = QFileDialog.getOpenFileName(
            self, f"Locate {display_name(tool)}", start, PROGRAM_FILTER
        )
        return self.set_program(tool, chosen) if chosen else None

    def set_program(self, tool: str, path: str) -> Worker:
        """Check that ``path`` is a working program, and save it as the override only if so."""
        row = self.rows[tool]
        row.show_message("Checking...")
        timeout = self._state.settings.version_check_timeout_s
        return _start(
            lambda _worker: check_program(tool, path, timeout),
            lambda result: self._program_checked(tool, path, result),
            lambda exc: self._failed(tool, exc),
        )

    def _program_checked(self, tool: str, path: str, result: object) -> None:
        if not shiboken6.isValid(self) or not isinstance(result, CheckResult):
            return
        row = self.rows[tool]
        if not result.ok:
            row.show_message(f"Not saved. {result.message}")
            return
        if not self._save_override(tool, path.strip().strip('"'), row):
            return
        self._state.redetect()
        self.refresh()

    def _save_override(self, tool: str, path: str | None, row: ToolRow) -> bool:
        overrides = dict(self._state.settings.external_tools)
        if path is None:
            overrides.pop(tool, None)
        else:
            overrides[tool] = path
        return self._save(row, external_tools=overrides)

    def test(self, tool: str) -> Worker:
        """Re-check the current program (or the saved Ollama address) and show the result."""
        row = self.rows[tool]
        settings = self._state.settings
        deps = self._state.deps
        row.show_message("Testing...")

        url = row.url_edit.text().strip() or settings.ollama_url

        def job(_worker: Worker) -> CheckResult:
            if tool == "ollama":
                return check_ollama(url, settings.ollama_probe_timeout_s)
            location = deps.resolve(tool)
            if not location.valid or location.path is None:
                return CheckResult(False, None, f"{display_name(tool)} was not found.")
            return check_program(tool, str(location.path), settings.version_check_timeout_s)

        return _start(job, lambda r: self._tested(tool, r), lambda exc: self._failed(tool, exc))

    def _tested(self, tool: str, result: object) -> None:
        if shiboken6.isValid(self) and isinstance(result, CheckResult):
            self.rows[tool].show_message(result.message)

    def _failed(self, tool: str, exc: BaseException) -> None:
        if shiboken6.isValid(self):
            self.rows[tool].show_message(f"The check failed: {exc}")

    def auto_detect(self) -> Worker:
        """Forget cached locations and search again."""
        self._state.redetect()
        return self.refresh()

    def reset(self, tool: str) -> Worker | None:
        """Remove the saved path so the program is found automatically again.

        For Ollama: back to the default address, and any saved address override is dropped.
        """
        row = self.rows[tool]
        if not self.has_override(tool):
            return None
        if tool == "ollama":
            overrides = dict(self._state.settings.external_tools)
            overrides.pop(tool, None)
            if not self._save(row, ollama_url=DEFAULT_OLLAMA_URL, external_tools=overrides):
                return None
            row.url_edit.setText(DEFAULT_OLLAMA_URL)
            row.url_edit.setModified(False)
            row.show_message(f"Back to the default address, {DEFAULT_OLLAMA_URL}.")
        elif not self._save_override(tool, None, row):
            return None
        self._state.redetect()
        return self.refresh()

    def set_ollama_url(self, url: str) -> Worker | None:
        """Save the Ollama address (the ``ollama_url`` setting), then ask the server in a worker.

        An address that does not answer is still saved (the server may just be off); the row
        says so. Only an address that is not a web address is refused.
        """
        row = self.rows["ollama"]
        cleaned = url.strip().rstrip("/")
        if not cleaned.lower().startswith(("http://", "https://")):
            row.show_message("Not saved: the address must start with http:// or https://.")
            return None
        if not self._save(row, ollama_url=cleaned):
            return None
        row.url_edit.setText(cleaned)
        row.url_edit.setModified(False)
        row.reset_button.setEnabled(self.has_override("ollama"))
        row.show_message("Saved. Asking the server...")
        self._state.redetect()
        timeout = self._state.settings.ollama_probe_timeout_s
        return _start(
            lambda _worker: check_ollama(cleaned, timeout),
            lambda result: self._ollama_checked(result),
            lambda exc: self._failed("ollama", exc),
        )

    def _ollama_checked(self, result: object) -> None:
        if not shiboken6.isValid(self) or not isinstance(result, CheckResult):
            return
        row = self.rows["ollama"]
        if result.ok:
            row.show_message(f"Saved. {result.message}")
        else:
            row.show_message(f"Saved, but {result.message} Start Ollama, or check the address.")
        self.refresh()

    def _save(self, row: ToolRow, **changes: object) -> bool:
        try:
            self._state.update_settings(**changes)
        except (ValidationError, OSError) as exc:
            row.show_message(f"Could not save the setting: {exc}")
            return False
        self.settingsSaved.emit()
        return True


def locate_program(parent: QWidget, state: AppState, tool: str, *, path: str | None = None) -> bool:
    """Let the user point at a missing program (the inline "Locate..." on a tool page).

    Asks for the file with a dialog unless ``path`` is given (tests do that), checks that it works
    in a worker while a small modal box waits, and saves it as the override only when it does.
    Returns True when it was saved. Otherwise the reason is shown through :func:`report_problem`.
    """
    name = display_name(tool)
    if path is None:
        path, _filter = QFileDialog.getOpenFileName(parent, f"Locate {name}", "", PROGRAM_FILTER)
        if not path:
            return False

    timeout = state.settings.version_check_timeout_s
    outcome: list[CheckResult] = []

    def job(_worker: Worker) -> CheckResult:
        result = check_program(tool, path or "", timeout)
        outcome.append(result)
        return result

    worker = Worker(job)
    _RUNNING.add(worker)
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    worker.finished.connect(lambda: _RUNNING.discard(worker))
    dialog = QProgressDialog(f"Checking {name}...", "", 0, 0, parent)
    dialog.setCancelButton(None)  # pyright: ignore[reportArgumentType]
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(400)
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        worker.start()
        if not worker.isFinished():
            loop.exec()
        worker.wait()
    finally:
        QApplication.restoreOverrideCursor()
        dialog.close()
        dialog.deleteLater()

    result = outcome[0] if outcome else CheckResult(False, None, f"The check of {name} failed.")
    if not result.ok:
        report_problem(parent, f"Locate {name}", f"Not saved. {result.message}")
        return False
    overrides = {**state.settings.external_tools, tool: path.strip().strip('"')}
    try:
        state.update_settings(external_tools=overrides)
    except (ValidationError, OSError) as exc:
        report_problem(parent, f"Locate {name}", f"Could not save the setting: {exc}")
        return False
    state.redetect()
    return True
