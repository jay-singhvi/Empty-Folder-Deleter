"""What every GUI page shares: the settings, the tools, presets, remembered folders and the
external-program lookup. No Qt here, so it is easy to test.

Tests build an :class:`AppState` with a settings file under ``tmp_path`` whose ``data_folder`` is
also under ``tmp_path``, so nothing touches the real user settings, journal or presets.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from various_tools.core.context import make_deps
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.presets import PresetStore
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

SettingsListener = Callable[["Settings"], None]


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

    # ----- presets and history ---------------------------------------------------

    def presets(self) -> PresetStore:
        return PresetStore.for_settings(self.settings)

    def open_journal(self) -> Journal | None:
        """The history journal, or None when nothing was applied yet (no file is created)."""
        path = self.settings.journal_path()
        return Journal(path) if path.is_file() else None

    # ----- remembered folders ----------------------------------------------------

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

    def last_folders(self, tool_id: str) -> dict[str, Any]:
        """Folder field values used last time on this tool's page (empty when off or unknown)."""
        if not self.settings.remember_last_folders:
            return {}
        folders: Any = self._read_gui_state().get("last_folders", {})
        if not isinstance(folders, dict):
            return {}
        values: Any = folders.get(tool_id, {})  # pyright: ignore[reportUnknownMemberType]
        return dict(values) if isinstance(values, dict) else {}  # pyright: ignore[reportUnknownArgumentType]

    def remember_folders(self, tool_id: str, values: Mapping[str, Any]) -> None:
        """Store folder field values for this tool. Does nothing when the setting is off."""
        if not self.settings.remember_last_folders:
            return
        data = self._read_gui_state()
        data["format"] = GUI_STATE_FORMAT
        folders: Any = data.setdefault("last_folders", {})
        if not isinstance(folders, dict):
            folders = {}
            data["last_folders"] = folders
        folders[tool_id] = json.loads(json.dumps(dict(values), default=str))
        path = self._gui_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(tmp_name, path)
        except OSError as exc:
            log.warning("Could not remember folders in %s: %s", path, exc)
