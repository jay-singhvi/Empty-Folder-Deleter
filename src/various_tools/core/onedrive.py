"""OneDrive placeholder state, ported from the metadata-8 script.

🔒 Rule (AGENTS.md rule 3): a cloud-only file must never be opened, read or hashed implicitly.
Every step that reads file contents calls :func:`ensure_on_device` first. Downloading a file
(:func:`force_download`) is only for an explicit user action.

Status is read from file attributes, which Windows caches. Opening a handle on a placeholder can
block while OneDrive recalls it, so these checks never open the file.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from enum import StrEnum
from pathlib import Path

from various_tools.core.deps import system_tool
from various_tools.core.fs import long_path

log = logging.getLogger(__name__)

FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

_CLOUD_ONLY_MASK = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


class SyncStatus(StrEnum):
    LOCAL = "local"
    PINNED = "pinned"
    CLOUD_ONLY = "cloud_only"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            SyncStatus.LOCAL: "Local",
            SyncStatus.PINNED: "Always available (pinned)",
            SyncStatus.CLOUD_ONLY: "Cloud-only (not downloaded)",
            SyncStatus.UNKNOWN: "Unknown",
        }[self]


class CloudOnlyFileError(RuntimeError):
    """Raised when a step would read a file whose contents are not on this device."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"skipped: cloud-only: {path}")
        self.path = path


def status_from_attributes(attributes: int) -> SyncStatus:
    """Map Windows file attributes to a sync status. Pure function, no I/O."""
    if attributes & _CLOUD_ONLY_MASK:
        return SyncStatus.CLOUD_ONLY
    if attributes & FILE_ATTRIBUTE_PINNED:
        return SyncStatus.PINNED
    return SyncStatus.LOCAL


def is_on_device(status: SyncStatus) -> bool:
    """True when the file's contents can be read without downloading anything."""
    return status in (SyncStatus.LOCAL, SyncStatus.PINNED)


def get_attributes(path: Path) -> int | None:
    """The attributes of ``path`` without opening it, or ``None`` when it does not exist.

    Outside Windows there are no placeholders, so an existing path reports ``0``.
    """
    if sys.platform != "win32":
        return 0 if os.path.lexists(path) else None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attrs = kernel32.GetFileAttributesW
    get_attrs.argtypes = [ctypes.c_wchar_p]
    get_attrs.restype = ctypes.c_uint32
    result = int(get_attrs(long_path(path)))
    if result == 0xFFFFFFFF:
        return None
    return result


def get_sync_status(path: Path) -> SyncStatus:
    """Sync status of ``path``, read from attributes. Never downloads."""
    attributes = get_attributes(path)
    if attributes is None:
        return SyncStatus.UNKNOWN
    return status_from_attributes(attributes)


def ensure_on_device(path: Path) -> None:
    """Raise :class:`CloudOnlyFileError` unless ``path`` can be read without a download."""
    if not is_on_device(get_sync_status(path)):
        raise CloudOnlyFileError(path)


def is_regular_file_no_hydrate(path: Path) -> bool:
    """True when ``path`` exists and is not a directory. Reads attributes only."""
    attributes = get_attributes(path)
    if attributes is None:
        return False
    return not (attributes & FILE_ATTRIBUTE_DIRECTORY)


def _attrib_command() -> str:
    """Windows' own ``attrib`` tool. The executable name is resolved in ``core/deps.py``."""
    return system_tool("attrib")


def unpin_file(path: Path) -> bool:
    """Clear the 'always keep on this device' pin (``attrib -P +U``). Returns success."""
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            [_attrib_command(), "-P", "+U", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not unpin %s: %s", path, exc)
        return False
    return result.returncode == 0


def force_download(path: Path, wait_seconds: int = 30) -> tuple[bool, str]:
    """Make OneDrive download a cloud-only file. Call only on an explicit user request.

    First pin and hydrate the file with ``attrib +P -U``, then wait up to ``wait_seconds`` for
    it to become local. If that fails, read the bytes, which also triggers the recall.
    Returns ``(ok, detail)``.
    """
    if get_attributes(path) is None:
        return False, "file no longer exists at logged path"

    attrib_detail: str | None = None
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [_attrib_command(), "+P", "-U", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                attrib_detail = f"attrib failed: {detail or f'exit code {result.returncode}'}"
            else:
                deadline = time.monotonic() + wait_seconds
                while time.monotonic() < deadline:
                    if is_on_device(get_sync_status(path)):
                        return True, "downloaded"
                    time.sleep(1)
                attrib_detail = f"attrib did not finish download within {wait_seconds}s"
        except subprocess.TimeoutExpired:
            attrib_detail = "attrib timed out"
        except (OSError, subprocess.SubprocessError) as exc:
            attrib_detail = f"attrib failed: {exc}"

    try:
        with path.open("rb") as handle:
            while handle.read(1024 * 1024):
                pass
    except OSError as exc:
        read_detail = f"read failed: {exc}"
        if attrib_detail:
            return False, f"{attrib_detail}; {read_detail}"
        return False, read_detail
    return True, "downloaded"
