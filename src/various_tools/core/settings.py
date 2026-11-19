"""User settings, stored as TOML in ``platformdirs.user_config_dir("Various Tools")``.

The defaults are the safe values from AGENTS.md: deletes go to the Recycle Bin, protected paths
are refused, and cloud-only files are never downloaded implicitly.

**Nothing is constant** (owner requirement, 2026-11-13). Every value that changes what the app
does is a field here (app-wide) or on a tool's options model (per tool). The value the code used
before is the default. Each field carries its hints the same way tool options do:

* ``title`` and ``description``: plain-English label and one- or two-sentence help text. The GUI
  shows them as label and tooltip, the CLI as ``--help`` text and ``config show`` notes.
* ``ge`` / ``le`` bounds on numbers (spinners and sliders), ``Literal`` for fixed choices
  (dropdowns).
* ``json_schema_extra``: ``group`` (section name), ``advanced`` (hidden behind "Advanced"),
  ``locked`` (🔒: a tuned or safety value; the GUI shows a lock and warns before it is changed),
  ``widget`` and ``depends_on`` as in plan §3.3.

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

from various_tools.core.deps import DEFAULT_OLLAMA_URL, OLLAMA_TIMEOUT_S, VERSION_TIMEOUT_S
from various_tools.core.fs import DEFAULT_PROTECTED_FOLDERS, ProtectedPaths
from various_tools.core.jsonutil import as_dict

log = logging.getLogger(__name__)

APP_NAME = "Various Tools"
SETTINGS_FILE = "settings.toml"
JOURNAL_FILE = "journal.sqlite"

# Defaults for the external-program timeouts. Core media code uses them as parameter defaults,
# and tools pass the user's value from ``ctx.settings``. They live here, not in core/media, so
# that reading settings never imports Pillow.
DEFAULT_FFPROBE_TIMEOUT_S = 30
DEFAULT_FRAME_TIMEOUT_S = 60
DEFAULT_TRANSCODE_TIMEOUT_S = 60 * 60
DEFAULT_EXIFTOOL_READ_TIMEOUT_S = 30
DEFAULT_EXIFTOOL_WRITE_TIMEOUT_S = 120
DEFAULT_OLLAMA_TAGS_TIMEOUT_S = 10
DEFAULT_OLLAMA_CHAT_TIMEOUT_S = 300
DEFAULT_HASH_CHUNK_KB = 1024
DEFAULT_MIN_PLAUSIBLE_YEAR = 1990

_LOCKED = "🔒 Locked default."


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


def _hint(group: str, *, advanced: bool = False, locked: bool = False, **extra: Any) -> Any:
    hints: dict[str, Any] = {"group": group}
    if advanced:
        hints["advanced"] = True
    if locked:
        hints["locked"] = True
    hints.update(extra)
    return hints


class Settings(BaseModel):
    """App-wide settings. Tools read these through ``ctx.settings``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ----- folders -----------------------------------------------------------
    onedrive_root: Path = Field(
        default_factory=default_onedrive_root,
        title="OneDrive folder",
        description=(
            "Your OneDrive folder. Used to suggest destinations such as Pictures\\Camera Roll. "
            "Default: %USERPROFILE%\\OneDrive, or the folder the OneDrive variable points to."
        ),
        json_schema_extra=_hint("Folders", widget="folder"),
    )
    data_folder: Path | None = Field(
        default=None,
        title="App data folder",
        description=(
            "Where the history journal (used for Undo) and the media catalog are kept. "
            "Empty uses the standard per-user app data folder."
        ),
        json_schema_extra=_hint("Folders", advanced=True, widget="folder"),
    )
    cache_folder: Path | None = Field(
        default=None,
        title="Cache folder",
        description=(
            "Where scan caches (for example image hashes) are kept. Safe to delete; scans are "
            "just slower next time. Empty uses the standard per-user cache folder."
        ),
        json_schema_extra=_hint("Folders", advanced=True, widget="folder"),
    )
    default_output_folder: str = Field(
        default="resized",
        min_length=1,
        title="Resized output folder name",
        description=(
            "Name of the folder, inside each chosen folder, that Media Resize writes new copies "
            "into when a tool does not set its own. Default: resized."
        ),
        json_schema_extra=_hint("Folders"),
    )
    duplicate_folder: str = Field(
        default="duplicate",
        min_length=1,
        title="Duplicate folder name",
        description=(
            "Name of the folder, inside each scanned folder, that the Duplicate Finder moves "
            "exact and visual copies into. Default: duplicate."
        ),
        json_schema_extra=_hint("Folders"),
    )
    review_folder: str = Field(
        default="review",
        min_length=1,
        title="Review folder name",
        description=(
            "Name of the folder, inside each scanned folder, that the Duplicate Finder moves "
            "burst shots and re-encodes into for you to check by hand. Default: review."
        ),
        json_schema_extra=_hint("Folders"),
    )
    unknown_date_folder: str = Field(
        default="Unknown Date",
        min_length=1,
        title="Unknown-date folder name",
        description=(
            "Folder that Organize by Date uses for files with no usable date. "
            "Default: Unknown Date."
        ),
        json_schema_extra=_hint("Folders"),
    )
    library_folders: list[Path] = Field(
        default_factory=list[Path],
        title="Photo and video folders",
        description=(
            "Folders the Timeline and the Map show photos and videos from. Their dates and "
            "places are kept in a small cache, so only new or changed files are read on each "
            "update. Files that are only in OneDrive are listed but never downloaded."
        ),
        json_schema_extra=_hint("Folders", widget="folders"),
    )

    # ----- safety (🔒)-------------------------------------------------------
    delete_mode: Literal["trash", "permanent"] = Field(
        default="trash",
        title="Delete mode",
        description=(
            f"{_LOCKED} trash sends deleted items to the Recycle Bin so they can be restored. "
            "permanent erases them for good and always asks for confirmation first."
        ),
        json_schema_extra=_hint("Safety", advanced=True, locked=True),
    )
    allow_protected_paths: bool = Field(
        default=False,
        title="Allow protected folders",
        description=(
            f"{_LOCKED} Off refuses to work in the protected folders listed below, drive roots and "
            "your user profile folder. Turning it on lets a mistake damage Windows or programs."
        ),
        json_schema_extra=_hint("Safety", advanced=True, locked=True),
    )
    protected_folders: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PROTECTED_FOLDERS),
        title="Protected folders",
        description=(
            f"{_LOCKED} Folders that, with everything inside them, are never used as a target. "
            "%VARIABLES% are expanded. Default: the Windows and Program Files folders. Removing "
            "one lets tools move or delete system files."
        ),
        json_schema_extra=_hint("Safety", advanced=True, locked=True, widget="folders"),
    )
    protect_drive_roots: bool = Field(
        default=True,
        title="Protect drive roots",
        description=(
            f"{_LOCKED} On refuses a whole drive (for example C:\\) as a target. Turning it off "
            "lets one click scan or change an entire disk."
        ),
        json_schema_extra=_hint("Safety", advanced=True, locked=True),
    )
    protect_user_profile: bool = Field(
        default=True,
        title="Protect the user profile folder",
        description=(
            f"{_LOCKED} On refuses C:\\Users\\<you> itself as a target (its subfolders are "
            "allowed). Turning it off lets a tool sweep your whole profile, AppData included."
        ),
        json_schema_extra=_hint("Safety", advanced=True, locked=True),
    )
    cloud_only_files: Literal["skip"] = Field(
        default="skip",
        title="OneDrive cloud-only files",
        description=(
            f"{_LOCKED} skip: files that live only in OneDrive are never read, hashed or "
            "downloaded; they are reported as skipped. Download choices are deferred until a "
            "journaled download action exists."
        ),
        json_schema_extra=_hint("Safety", advanced=True, locked=True),
    )

    # ----- performance and dates ---------------------------------------------
    workers: int = Field(
        default_factory=default_worker_count,
        ge=1,
        le=256,
        title="Worker threads",
        description=(
            "How many files are read at the same time when a tool does not set its own. "
            "Default: the number of CPU cores. Lower it to keep the PC responsive."
        ),
        json_schema_extra=_hint("Performance"),
    )
    hash_chunk_kb: int = Field(
        default=DEFAULT_HASH_CHUNK_KB,
        ge=4,
        le=65536,
        title="Hash read size (KB)",
        description=(
            "How much of a file is read at a time while computing its SHA-256 fingerprint. "
            "Affects speed and memory only, never the result. Default: 1024 KB."
        ),
        json_schema_extra=_hint("Performance", advanced=True),
    )
    min_plausible_year: int = Field(
        default=DEFAULT_MIN_PLAUSIBLE_YEAR,
        ge=1900,
        le=9999,
        title="Earliest believable year",
        description=(
            "Dates before this year (and dates in the future) are treated as wrong, for example a "
            "camera clock that was never set. Tools then try the next date source. Default: 1990."
        ),
        json_schema_extra=_hint("Dates"),
    )

    # ----- external programs -------------------------------------------------
    external_tools: dict[str, str] = Field(
        default_factory=dict,
        title="External program paths",
        description=(
            "Saved paths for ffmpeg, ffprobe and exiftool, for example "
            "exiftool = C:\\Program Files\\Exiftool\\exiftool.exe. A missing entry means "
            "auto-detect (environment variable, PATH, then known install folders)."
        ),
        json_schema_extra=_hint("External programs", widget="tool_paths"),
    )
    ollama_url: str = Field(
        default=DEFAULT_OLLAMA_URL,
        title="Ollama address",
        description=(
            "Web address of the Ollama server used for the optional AI check of borderline "
            f"duplicates. Default: {DEFAULT_OLLAMA_URL} (Ollama on this PC)."
        ),
        json_schema_extra=_hint("External programs"),
    )
    version_check_timeout_s: float = Field(
        default=VERSION_TIMEOUT_S,
        gt=0,
        le=600,
        title="Program check timeout (s)",
        description=(
            "Seconds to wait for ffmpeg, ffprobe or ExifTool to report its version when the app "
            "checks that a program works. Default: 5."
        ),
        json_schema_extra=_hint("External programs", advanced=True),
    )
    ollama_probe_timeout_s: float = Field(
        default=OLLAMA_TIMEOUT_S,
        gt=0,
        le=600,
        title="Ollama check timeout (s)",
        description=(
            "Seconds to wait for the Ollama server to answer when the app checks whether it is "
            "running. Default: 2."
        ),
        json_schema_extra=_hint("External programs", advanced=True),
    )
    ffprobe_timeout_s: float = Field(
        default=DEFAULT_FFPROBE_TIMEOUT_S,
        gt=0,
        le=3600,
        title="ffprobe timeout (s)",
        description=(
            "Seconds ffprobe may take to read one video's details before the file is skipped "
            "with a note. Default: 30."
        ),
        json_schema_extra=_hint("External programs", advanced=True),
    )
    frame_grab_timeout_s: float = Field(
        default=DEFAULT_FRAME_TIMEOUT_S,
        gt=0,
        le=3600,
        title="Frame grab timeout (s)",
        description=(
            "Seconds ffmpeg may take to extract one still frame from a video before the video "
            "is skipped. Default: 60."
        ),
        json_schema_extra=_hint("External programs", advanced=True),
    )
    transcode_timeout_s: float = Field(
        default=DEFAULT_TRANSCODE_TIMEOUT_S,
        gt=0,
        le=7 * 24 * 3600,
        title="Video encode timeout (s)",
        description=(
            "Seconds ffmpeg may take to re-encode one video before it is stopped and no output "
            "is kept. Default: 3600 (one hour)."
        ),
        json_schema_extra=_hint("External programs", advanced=True),
    )
    exiftool_read_timeout_s: float = Field(
        default=DEFAULT_EXIFTOOL_READ_TIMEOUT_S,
        gt=0,
        le=3600,
        title="ExifTool read timeout (s)",
        description=(
            "Seconds ExifTool may take to read one file's date before the next date source is "
            "tried. Default: 30."
        ),
        json_schema_extra=_hint("External programs", advanced=True),
    )
    exiftool_write_timeout_s: float = Field(
        default=DEFAULT_EXIFTOOL_WRITE_TIMEOUT_S,
        gt=0,
        le=3600,
        title="ExifTool copy timeout (s)",
        description=(
            "Seconds ExifTool may take to copy metadata into one converted file before that "
            "conversion counts as failed. Default: 120."
        ),
        json_schema_extra=_hint("External programs", advanced=True),
    )
    ollama_tags_timeout_s: float = Field(
        default=DEFAULT_OLLAMA_TAGS_TIMEOUT_S,
        gt=0,
        le=600,
        title="Ollama model list timeout (s)",
        description=(
            "Seconds to wait for Ollama to list its installed models before the AI check is "
            "turned off for the run. Default: 10."
        ),
        json_schema_extra=_hint("External programs", advanced=True),
    )
    ollama_chat_timeout_s: float = Field(
        default=DEFAULT_OLLAMA_CHAT_TIMEOUT_S,
        gt=0,
        le=3600,
        title="Ollama answer timeout (s)",
        description=(
            "Seconds to wait for the vision model to compare one pair of photos. A slow PC may "
            "need more. A pair that times out is not moved. Default: 300."
        ),
        json_schema_extra=_hint("External programs", advanced=True),
    )

    # ----- desktop app ---------------------------------------------------------
    theme: Literal["system", "light", "dark"] = Field(
        default="system",
        title="Theme",
        description=(
            "Colours of the desktop app. system follows the Windows light or dark mode; light and "
            "dark always use that look. Default: system."
        ),
        json_schema_extra=_hint("Desktop app"),
    )
    remember_last_folders: bool = Field(
        default=True,
        title="Remember last folders and options",
        description=(
            "On: each tool page opens with the folders and options you used last time on that "
            "page. Locked options, permanent delete and options that replace or remove originals "
            "always start from their defaults. Off: tool pages start empty and nothing is "
            "remembered."
        ),
        json_schema_extra=_hint("Desktop app"),
    )
    shell_integration: bool = Field(
        default=False,
        title="Folder right-click menu",
        description=(
            "On: adds 'Open with Various Tools' (Find empty folders, Find duplicates) to the "
            "right-click menu of folders in Explorer, for your Windows account only (no admin "
            "needed). Off removes it. Default: off."
        ),
        json_schema_extra=_hint("Desktop app"),
    )

    # ----- derived values ------------------------------------------------------
    def protected_paths(self) -> ProtectedPaths:
        """The protected-path rules (AGENTS.md rule 6) to pass to ``core/fs.py`` checks."""
        return ProtectedPaths(
            folders=tuple(self.protected_folders),
            drive_roots=self.protect_drive_roots,
            user_profile=self.protect_user_profile,
            allowed=self.allow_protected_paths,
        )

    def resolved_data_folder(self) -> Path:
        """``data_folder``, or the standard per-user app data folder when it is empty."""
        return self.data_folder or Path(platformdirs.user_data_dir(APP_NAME))

    def resolved_cache_folder(self) -> Path:
        """``cache_folder``, or the standard per-user cache folder when it is empty."""
        return self.cache_folder or Path(platformdirs.user_cache_dir(APP_NAME))

    def journal_path(self) -> Path:
        """The SQLite history journal that Undo and History read."""
        return self.resolved_data_folder() / JOURNAL_FILE


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
    """Write settings as TOML, through a temporary file so a crash never leaves a half file.

    Only values that differ from the defaults are written, so a later release can change a
    default (with the owner's approval) without every saved file pinning the old one.
    """
    target = path or default_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = to_toml(settings.model_dump(mode="json", exclude_defaults=True))
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
    if isinstance(value, list | tuple):
        items: list[Any] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        return "[" + ", ".join(_toml_value(item) for item in items) + "]"
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
