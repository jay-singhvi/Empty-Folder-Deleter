"""What every GUI page shares: the settings, the tools, remembered options and the
external-program lookup. No Qt here, so it is easy to test.

Tests build an :class:`AppState` with a settings file under ``tmp_path`` whose ``data_folder`` is
also under ``tmp_path``, so nothing touches the real user settings or journal.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from various_tools.core.context import make_deps
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.settings import (
    Settings,
    default_settings_path,
    load_settings,
    save_settings,
)
from various_tools.tools.base import Tool, discover

log = logging.getLogger(__name__)

GUI_STATE_FILE = "gui_state.json"
GUI_STATE_FORMAT = 1
LAST_OPTIONS_KEY = "last_options"
OLD_LAST_FOLDERS_KEY = "last_folders"
SHOW_ALL_KEY = "show_all_settings"
WORKSPACE_KEY = "workspace"
RECENT_WORKSPACES_KEY = "recent_workspaces"
MAX_RECENT_WORKSPACES = 8

SettingsListener = Callable[["Settings"], None]
ShowAllListener = Callable[[bool], None]
WorkspaceListener = Callable[[list[Path]], None]


@dataclass
class AppState:
    """Shared, mutable app state. Pages read ``state.settings`` and call :meth:`save_settings`.

    ``deps`` is the session's external-program lookup (resolved once, cached). It is rebuilt when
    the settings are saved, and :meth:`redetect` clears its cache.
    """

    settings_path: Path
    settings: Settings
    tools: dict[str, Tool]
    deps: ExternalTools = field(init=False)
    _listeners: list[SettingsListener] = field(default_factory=list[SettingsListener])
    _show_all_listeners: list[ShowAllListener] = field(default_factory=list[ShowAllListener])
    _workspace_listeners: list[WorkspaceListener] = field(default_factory=list[WorkspaceListener])

    def __post_init__(self) -> None:
        self.deps = make_deps(self.settings)

    @classmethod
    def load(
        cls, settings_path: Path | None = None, tools: Mapping[str, Tool] | None = None
    ) -> AppState:
        path = settings_path or default_settings_path()
        return cls(
            settings_path=path,
            settings=load_settings(path),
            tools=dict(tools) if tools is not None else discover(),
        )

    # ----- settings --------------------------------------------------------------

    def on_settings_changed(self, listener: SettingsListener) -> None:
        """Call ``listener(new_settings)`` after every :meth:`save_settings`."""
        self._listeners.append(listener)

    def save_settings(self, new: Settings) -> None:
        """Write ``new`` to the settings file, make it current and tell the listeners."""
        save_settings(new, self.settings_path)
        self.settings = new
        self.deps = make_deps(new)
        for listener in list(self._listeners):
            listener(new)

    def update_settings(self, **changes: Any) -> Settings:
        """Validate ``changes`` on top of the current settings and save them."""
        data = self.settings.model_dump()
        data.update(changes)
        new = Settings.model_validate(data)
        self.save_settings(new)
        return new

    def redetect(self) -> None:
        """Forget cached program locations so the next lookup searches again."""
        self.deps.redetect()

    # ----- history ---------------------------------------------------------------

    def open_journal(self) -> Journal | None:
        """The history journal, or None when nothing was applied yet (no file is created)."""
        path = self.settings.journal_path()
        return Journal(path) if path.is_file() else None

    # ----- remembered options ----------------------------------------------------

    def _gui_state_path(self) -> Path:
        return self.settings.resolved_data_folder() / GUI_STATE_FILE

    def _read_gui_state(self) -> dict[str, Any]:
        path = self._gui_state_path()
        if not path.is_file():
            return {}
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable GUI state file %s: %s", path, exc)
            return {}
        if not isinstance(data, dict) or data.get("format") != GUI_STATE_FORMAT:  # pyright: ignore[reportUnknownMemberType]
            return {}
        return data  # pyright: ignore[reportUnknownVariableType]

    def last_options(self, tool_id: str) -> dict[str, Any]:
        """Option values used last time on this tool's page (empty when off or unknown).

        Version 0.2 stored only the folders, under ``last_folders``; those are still read.
        """
        if not self.settings.remember_last_folders:
            return {}
        state = self._read_gui_state()
        for key in (LAST_OPTIONS_KEY, OLD_LAST_FOLDERS_KEY):
            tools: Any = state.get(key, {})
            if not isinstance(tools, dict):
                continue
            values: Any = tools.get(tool_id)  # pyright: ignore[reportUnknownMemberType]
            if isinstance(values, dict):
                return dict(values)  # pyright: ignore[reportUnknownArgumentType]
        return {}

    def remember_options(self, tool_id: str, values: Mapping[str, Any]) -> None:
        """Store option values for this tool. Does nothing when the setting is off.

        The caller decides which options may be remembered (see ``tool_page.rememberable_fields``).
        """
        if not self.settings.remember_last_folders:
            return
        data = self._read_gui_state()
        data["format"] = GUI_STATE_FORMAT
        tools: Any = data.setdefault(LAST_OPTIONS_KEY, {})
        if not isinstance(tools, dict):
            tools = {}
            data[LAST_OPTIONS_KEY] = tools
        tools[tool_id] = json.loads(json.dumps(dict(values), default=str))
        self._write_gui_state(data)

    # ----- the "Show all settings" switch ----------------------------------------

    def show_all_settings(self) -> bool:
        """Whether every page shows all its settings and controls (off by default).

        Kept in the GUI state file even when remembering options is switched off: it is a
        display preference, not an option value.
        """
        return self._read_gui_state().get(SHOW_ALL_KEY) is True

    def set_show_all_settings(self, on: bool) -> None:
        """Store the switch and tell every listener."""
        data = self._read_gui_state()
        data["format"] = GUI_STATE_FORMAT
        data[SHOW_ALL_KEY] = bool(on)
        self._write_gui_state(data)
        for listener in list(self._show_all_listeners):
            listener(bool(on))

    def on_show_all_changed(self, listener: ShowAllListener) -> None:
        """Call ``listener(on)`` after every :meth:`set_show_all_settings`."""
        self._show_all_listeners.append(listener)

    # ----- the working folder ----------------------------------------------------

    def _stored_paths(self, key: str) -> list[Path]:
        raw: Any = self._read_gui_state().get(key, [])
        if not isinstance(raw, list):
            return []
        return [Path(item) for item in raw if isinstance(item, str) and item]  # pyright: ignore[reportUnknownVariableType]

    def workspace(self) -> list[Path]:
        """The working folder: one or more folders every tool looks in by default (may be empty).

        GUI state, kept even when "Remember last folders and options" is off.
        """
        return self._stored_paths(WORKSPACE_KEY)

    def recent_workspaces(self) -> list[list[Path]]:
        """The last working folders, newest first (at most 8 entries, each one or more folders)."""
        raw: Any = self._read_gui_state().get(RECENT_WORKSPACES_KEY, [])
        if not isinstance(raw, list):
            return []
        recents: list[list[Path]] = []
        for entry in raw:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(entry, list):
                paths = [Path(p) for p in entry if isinstance(p, str) and p]  # pyright: ignore[reportUnknownVariableType]
                if paths:
                    recents.append(paths)
        return recents[:MAX_RECENT_WORKSPACES]

    def set_workspace(self, paths: Sequence[Path | str]) -> None:
        """Choose the working folder. Listeners are told only when it really changed."""
        new: list[Path] = []
        for item in paths:
            path = Path(item)
            if str(path) and path not in new:
                new.append(path)
        old = self.workspace()
        data = self._read_gui_state()
        data["format"] = GUI_STATE_FORMAT
        data[WORKSPACE_KEY] = [str(p) for p in new]
        if new:
            recents = [r for r in self.recent_workspaces() if r != new]
            recents.insert(0, new)
            data[RECENT_WORKSPACES_KEY] = [
                [str(p) for p in entry] for entry in recents[:MAX_RECENT_WORKSPACES]
            ]
        self._write_gui_state(data)
        if new != old:
            for listener in list(self._workspace_listeners):
                listener(list(new))

    def on_workspace_changed(self, listener: WorkspaceListener) -> None:
        """Call ``listener(paths)`` after the working folder changed."""
        self._workspace_listeners.append(listener)

    def _write_gui_state(self, data: dict[str, Any]) -> None:
        """Write the GUI state file atomically; a failure is logged, never raised."""
        path = self._gui_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(tmp_name, path)
        except OSError as exc:
            log.warning("Could not save the GUI state in %s: %s", path, exc)
