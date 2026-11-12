"""User settings, stored as TOML in ``platformdirs.user_config_dir("Various Tools")``.

The defaults are the safe values from AGENTS.md: deletes go to the Recycle Bin, protected paths
are refused, and cloud-only files are never downloaded implicitly.

Plain pydantic models are used, not pydantic-settings. Environment overrides for external
programs already live in ``core/deps.py``, and a second ``VARIOUS_TOOLS_`` prefix would collide.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, Field

from various_tools.core.deps import DEFAULT_OLLAMA_URL
from various_tools.core.jsonutil import as_dict

log = logging.getLogger(__name__)

APP_NAME = "Various Tools"
SETTINGS_FILE = "settings.toml"


def default_onedrive_root(env: Mapping[str, str] | None = None) -> Path:
    """The OneDrive root, resolved the same way metadata-8 does it.

    ``%USERPROFILE%\\OneDrive`` if that folder exists, otherwise the ``OneDrive`` variable, which
    is what a relocated or business OneDrive sets. Nothing is hardcoded to one account.
    """
    environ = os.environ if env is None else env
    user_profile = Path(environ.get("USERPROFILE", str(Path.home())))
    personal = user_profile / "OneDrive"
    if personal.is_dir():
        return personal
    return Path(environ.get("OneDrive", str(personal)))


def default_worker_count() -> int:
    return max(1, os.cpu_count() or 1)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    onedrive_root: Path = Field(default_factory=default_onedrive_root)
    default_output_folder: str = "resized"
    duplicate_folder: str = "duplicate"
    review_folder: str = "review"
    unknown_date_folder: str = "Unknown Date"
    workers: int = Field(default_factory=default_worker_count, ge=1)
    # 🔒 Trash is the default. Permanent delete is an advanced setting that asks for confirmation.
    delete_mode: Literal["trash", "permanent"] = "trash"
    # 🔒 Protected paths are refused unless the user turns this on.
    allow_protected_paths: bool = False
    ollama_url: str = DEFAULT_OLLAMA_URL
    min_plausible_year: int = Field(default=1990, ge=1900)
    # Saved paths for external programs, e.g. {"exiftool": "C:\\...\\exiftool.exe"}.
    external_tools: dict[str, str] = Field(default_factory=dict)


def default_settings_path() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME)) / SETTINGS_FILE


def load_settings(path: Path | None = None) -> Settings:
    """Read settings from TOML. A missing file gives the defaults.

    A file that cannot be parsed is logged and the defaults are used. The broken file is left
    in place so the user can fix it.
    """
    target = path or default_settings_path()
    if not target.is_file():
        return Settings()
    try:
        with target.open("rb") as handle:
            data = tomllib.load(handle)
        return Settings.model_validate(data)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        log.warning("Ignoring settings file %s: %s", target, exc)
        return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Write settings as TOML, through a temporary file so a crash never leaves a half file."""
    target = path or default_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = to_toml(settings.model_dump(mode="json"))
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    # JSON string escaping is valid TOML basic-string escaping for the values used here.
    return json.dumps(str(value), ensure_ascii=False)


def _is_table(value: object) -> bool:
    return isinstance(value, dict)


def to_toml(data: Mapping[str, Any]) -> str:
    """Serialise a flat mapping plus one level of tables. Enough for :class:`Settings`."""
    lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    for key, value in data.items():
        if value is None:
            continue
        if _is_table(value):
            tables.append((key, as_dict(value)))
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    for name, table in tables:
        lines.append("")
        lines.append(f"[{name}]")
        for key, value in table.items():
            if value is not None:
                lines.append(f"{json.dumps(key)} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"
