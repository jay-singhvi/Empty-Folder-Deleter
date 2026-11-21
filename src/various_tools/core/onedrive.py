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


_ONEDRIVE_ENV = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def is_inside_onedrive(path: Path | str) -> bool:
    """True when ``path`` is at or under a OneDrive folder named by the environment.

    Reads only the ``OneDrive``, ``OneDriveConsumer`` and ``OneDriveCommercial`` variables and
    compares names; it never touches the disk.
    """
    key = os.path.normcase(os.path.abspath(str(path)))
    for name in _ONEDRIVE_ENV:
        root = os.environ.get(name)
        if not root:
            continue
        base = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
        if key == base or key.startswith(base + os.sep):
            return True
    return False


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


# ----- upload state ------------------------------------------------------------------------------

FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write, delete
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
CF_PLACEHOLDER_INFO_BASIC = 0
CF_IN_SYNC_STATE_IN_SYNC = 1
HRESULT_NOT_A_CLOUD_FILE = 0x80070178  # HRESULT_FROM_WIN32(ERROR_NOT_A_CLOUD_FILE)
_INVALID_HANDLE = ctypes.c_void_p(-1).value

NOT_A_PLACEHOLDER = "not-a-placeholder"


class _PlaceholderBasicInfo(ctypes.Structure):
    """``CF_PLACEHOLDER_BASIC_INFO`` (cfapi.h): PinState, InSyncState, then the file ids."""

    _fields_ = (
        ("PinState", ctypes.c_int32),
        ("InSyncState", ctypes.c_int32),
        ("FileId", ctypes.c_int64),
        ("SyncRootFileId", ctypes.c_int64),
        ("FileIdentityLength", ctypes.c_uint32),
        ("FileIdentity", ctypes.c_ubyte * 1),
    )


_INFO_BUFFER_BYTES = ctypes.sizeof(_PlaceholderBasicInfo) + 512  # room for the file identity


def _query_in_sync_state(path: Path) -> int | str | None:
    """The placeholder's ``InSyncState``, :data:`NOT_A_PLACEHOLDER`, or ``None`` (cannot tell).

    The file is opened with ``FILE_READ_ATTRIBUTES`` only and without following the reparse
    point, so nothing is ever recalled or downloaded.
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        cldapi = ctypes.WinDLL("cldapi")
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        get_info = cldapi.CfGetPlaceholderInfo
        get_info.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_info.restype = ctypes.c_int32
    except OSError, AttributeError:
        return None
    handle = create_file(
        long_path(path),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE:
        return None
    try:
        buffer = ctypes.create_string_buffer(_INFO_BUFFER_BYTES)
        returned = ctypes.c_uint32(0)
        hresult = int(
            get_info(handle, CF_PLACEHOLDER_INFO_BASIC, buffer, _INFO_BUFFER_BYTES, returned)
        )
        if (hresult & 0xFFFFFFFF) == HRESULT_NOT_A_CLOUD_FILE:
            return NOT_A_PLACEHOLDER
        if hresult < 0:
            return None
        info = _PlaceholderBasicInfo.from_buffer(buffer)
        return int(info.InSyncState)
    finally:
        close_handle(handle)


def upload_pending(path: Path) -> bool | None:
    """True while a file inside a OneDrive folder is not yet in sync with the cloud.

    Read with the Cloud Files API on a handle opened for attributes only, so it never downloads
    anything. A file that is not a placeholder yet, inside a OneDrive folder, counts as waiting
    to be uploaded. ``None`` when it cannot tell: not a OneDrive folder, not Windows, or the
    Cloud Files API is missing or refuses.
    """
    if not is_inside_onedrive(path):
        return None
    state = _query_in_sync_state(path)
    if state is None:
        return None
    if state == NOT_A_PLACEHOLDER:
        return True
    return state != CF_IN_SYNC_STATE_IN_SYNC
