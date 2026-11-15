"""Restore items from the Windows Recycle Bin, for undo (plan §3.3: "restore from the Recycle Bin
when possible").

When Windows recycles ``C:\\x\\photo.jpg`` it moves the item to
``C:\\$Recycle.Bin\\<user SID>\\$R<id>.jpg`` and writes ``$I<id>.jpg`` next to it, holding the
original path, the size and the deletion time. Two formats exist:

* version 1 (Vista to 8.1): version, size, deletion FILETIME, then a 260-character path.
* version 2 (Windows 10 and later): version, size, deletion FILETIME, path length, path.

:func:`restore` picks the newest entry whose original path equals the one asked for
(case-insensitively), whose deletion time lies in the given window and, when known, whose size
matches. It moves the ``$R`` item back with ``rename_no_replace`` and removes the ``$I`` file.
Whenever anything is not exactly as expected (no bin on that drive, a network drive, no match,
the original place taken, the item is a link or a cloud placeholder) it does nothing and says
why, and the caller lists the item for the user to restore by hand.

The bin folder lookup is injectable (``locate_bin``), so tests use a fake folder. Pure Python and
ctypes; no Qt and no printing. Reads only the small ``$I`` index files, never an item's contents.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat as stat_module
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from various_tools.core import fs

log = logging.getLogger(__name__)

BinLocator = Callable[[Path], Path | None]

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
_MAX_INDEX_BYTES = 64 * 1024
_V1_PATH_BYTES = 520
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3


@dataclass(frozen=True)
class IndexEntry:
    """What one ``$I`` file says."""

    original: str
    deleted: datetime
    size: int


@dataclass(frozen=True)
class RestoreResult:
    restored: bool
    detail: str


def _from_filetime(value: int) -> datetime | None:
    if value <= 0:
        return None
    try:
        return _FILETIME_EPOCH + timedelta(microseconds=value // 10)
    except OverflowError:
        return None


def parse_index(data: bytes) -> IndexEntry | None:
    """Read a ``$I`` file's bytes. Returns None for a damaged or unknown file."""
    if len(data) < 24 or len(data) > _MAX_INDEX_BYTES:
        return None
    version, size, filetime = struct.unpack_from("<qqq", data, 0)
    if version == 1:
        raw = data[24 : 24 + _V1_PATH_BYTES]
        if len(raw) < 2:
            return None
    elif version == 2:
        if len(data) < 28:
            return None
        (chars,) = struct.unpack_from("<i", data, 24)
        raw = data[28 : 28 + chars * 2]
        if chars <= 0 or len(raw) != chars * 2:
            return None
    else:
        return None
    try:
        text = raw.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    original = text.split("\x00", 1)[0]
    deleted = _from_filetime(filetime)
    if not original or deleted is None or size < 0:
        return None
    return IndexEntry(original=original, deleted=deleted, size=size)


def _current_user_sid() -> str | None:
    """The string SID of the user running this process, or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token_query = 0x0008
    token_user = 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, token):
        return None
    try:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, token_user, None, 0, needed)
        if needed.value == 0:
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, token_user, buffer, needed, needed):
            return None
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the SID pointer.
        sid_pointer = ctypes.c_void_p.from_buffer(buffer).value
        text = ctypes.c_wchar_p()
        if not sid_pointer or not advapi32.ConvertSidToStringSidW(sid_pointer, text):
            return None
        try:
            return str(text.value) if text.value else None
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def _drive_type(root: str) -> int:
    if sys.platform != "win32":
        return 0
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetDriveTypeW.restype = ctypes.c_uint
    return int(kernel32.GetDriveTypeW(root))


def current_user_bin(original: Path) -> Path | None:
    """The current user's Recycle Bin folder on the drive of ``original``, or None.

    Only local fixed and removable drives with a drive letter qualify; network paths, other
    volume types and missing folders give None.
    """
    drive = os.path.splitdrive(os.path.abspath(fs.strip_long_prefix(str(original))))[0]
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        return None
    root = drive + "\\"
    if _drive_type(root) not in (_DRIVE_FIXED, _DRIVE_REMOVABLE):
        return None
    sid = _current_user_sid()
    if sid is None:
        return None
    folder = Path(root) / "$Recycle.Bin" / sid
    st = fs.stat_no_follow(folder)
    if st is None or fs.stat_is_link(st) or not folder.is_dir():
        return None
    return folder


@dataclass(frozen=True)
class BinPolicy:
    """A volume's Recycle Bin settings. ``max_capacity_mb`` is None when it cannot be read."""

    nuke_on_delete: bool
    max_capacity_mb: int | None


_BITBUCKET = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
_EXPLORER_POLICIES = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
# Without a recorded limit, assume the smallest share of the volume Windows gives the bin.
_DEFAULT_BIN_SHARE = 0.05


def _volume_guid(root: str) -> str | None:
    """``{guid}`` of the volume mounted at ``root`` (``C:\\``), or None."""
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    func = kernel32.GetVolumeNameForVolumeMountPointW
    func.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    func.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(64)
    if not func(root, buffer, 64):
        return None
    name = str(buffer.value)  # \\?\Volume{guid}\
    start, end = name.find("{"), name.find("}")
    return name[start : end + 1] if 0 <= start < end else None


def _read_dword(hive: int, key: str, value: str) -> int | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(hive, key) as handle:
            data, kind = winreg.QueryValueEx(handle, value)
    except OSError:
        return None
    return int(data) if kind == winreg.REG_DWORD else None


def bin_policy(root: str) -> BinPolicy:
    """The Recycle Bin settings of the volume at ``root``, read from the registry.

    ``NukeOnDelete`` and ``MaxCapacity`` (MB) come from the current user's
    ``Explorer\\BitBucket\\Volume\\{guid}`` key; the "do not move files to the Recycle Bin"
    policy (``NoRecycleFiles``, per user or per machine) also counts as delete-immediately.
    """
    if sys.platform != "win32":
        return BinPolicy(nuke_on_delete=True, max_capacity_mb=None)
    import winreg

    no_recycle = any(
        _read_dword(hive, _EXPLORER_POLICIES, "NoRecycleFiles") == 1
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    )
    guid = _volume_guid(root)
    nuke = max_mb = None
    if guid is not None:
        key = f"{_BITBUCKET}\\{guid}"
        nuke = _read_dword(winreg.HKEY_CURRENT_USER, key, "NukeOnDelete")
        max_mb = _read_dword(winreg.HKEY_CURRENT_USER, key, "MaxCapacity")
    return BinPolicy(nuke_on_delete=no_recycle or nuke == 1, max_capacity_mb=max_mb)


def _volume_size(root: str) -> int | None:
    try:
        return shutil.disk_usage(root).total
    except OSError:
        return None


def can_recycle(
    path: Path,
    *,
    drive_type: Callable[[str], int] | None = None,
    locate_bin: BinLocator | None = None,
    policy: Callable[[str], BinPolicy] | None = None,
    volume_size: Callable[[str], int | None] | None = None,
) -> str | None:
    """Why sending ``path`` to the Recycle Bin could delete it for good, or None when it is safe.

    send2trash asks Windows to recycle without confirmation and without the "delete
    permanently?" warning, so where there is no bin Windows deletes silently. Call this first.
    Safe means all of:

    * the item's real path is on a **fixed local drive** with a drive letter (no network,
      mapped, removable, optical or RAM drive; a subst or junction is resolved first);
    * the **current user's bin folder** exists on that drive (:func:`current_user_bin`);
    * the bin is **not set to delete immediately** (``NukeOnDelete``, or the
      ``NoRecycleFiles`` policy);
    * the item's size is **under the bin's size limit** (``MaxCapacity``). A folder counts as
      0 bytes, because only empty folders are ever recycled. When the limit cannot be read,
      it is taken as 5% of the volume's size, the smallest default Windows uses; when that
      cannot be read either, the item is refused.

    The lookups are injectable for tests; they default to the real ones.
    """
    drive_kind = drive_type or _drive_type
    locator = locate_bin or current_user_bin
    read_policy = policy or bin_policy
    sizer = volume_size or _volume_size
    nothing = "so nothing was removed"

    def letter(text: str) -> str | None:
        drive = os.path.splitdrive(text)[0].upper()
        return drive if len(drive) == 2 and drive[1] == ":" and drive[0].isalpha() else None

    network = f"this is a network location, which has no Recycle Bin, {nothing}"
    text = os.path.abspath(fs.strip_long_prefix(str(path)))
    drive = letter(text)
    if drive is None:  # checked before touching the disk, so a far server cannot stall this
        return network
    st = fs.stat_no_follow(path)
    if st is None:
        return f"the item is gone, {nothing}"
    try:
        real = fs.real_path(Path(text).parent)
    except OSError:
        return f"the item's real place could not be read, {nothing}"
    real_drive = letter(real)
    if real_drive is None:
        return network
    if real_drive != drive:
        return f"the item's real place is on drive {real_drive}, not {drive}; {nothing}"
    root = drive + "\\"
    if drive_kind(root) != _DRIVE_FIXED:
        return f"drive {drive} is not a fixed local drive, so it has no Recycle Bin; {nothing}"
    if locator(Path(root)) is None:
        return f"no Recycle Bin of this user was found on drive {drive}, {nothing}"
    settings = read_policy(root)
    if settings.nuke_on_delete:
        return f"the Recycle Bin on drive {drive} deletes items immediately, {nothing}"
    size = 0 if stat_module.S_ISDIR(st.st_mode) else st.st_size
    if settings.max_capacity_mb is not None:
        limit = settings.max_capacity_mb * 1024 * 1024
    else:
        total = sizer(root)
        if total is None:
            return f"the Recycle Bin size limit on drive {drive} could not be checked, {nothing}"
        limit = int(total * _DEFAULT_BIN_SHARE)
    if size >= limit:
        return f"the item is too big for the Recycle Bin on drive {drive}, {nothing}"
    return None


def _entries(folder: Path) -> list[tuple[Path, IndexEntry]]:
    found: list[tuple[Path, IndexEntry]] = []
    try:
        with os.scandir(fs.long_path(folder)) as it:
            names = [e.name for e in it if e.name.upper().startswith("$I")]
    except OSError as exc:
        log.warning("Could not list the Recycle Bin folder %s: %s", folder, exc)
        return found
    for name in names:
        index = folder / name
        st = fs.stat_no_follow(index)
        if st is None or fs.stat_is_link(st) or st.st_size > _MAX_INDEX_BYTES:
            continue
        attributes: int = getattr(st, "st_file_attributes", 0)
        if attributes & fs.FILE_ATTRIBUTE_REPARSE_POINT:
            continue  # not an ordinary local file; never recalled or read
        try:
            with open(fs.long_path(index), "rb") as handle:
                data = handle.read(_MAX_INDEX_BYTES + 1)
        except OSError:
            continue
        entry = parse_index(data)
        if entry is not None:
            found.append((index, entry))
    return found


def restore(
    original: Path,
    *,
    deleted_after: datetime,
    deleted_before: datetime,
    size: int | None = None,
    mtime_ns: int | None = None,
    locate_bin: BinLocator | None = None,
) -> RestoreResult:
    """Move the recycled item that used to be at ``original`` back there, if found exactly.

    Only entries deleted within ``[deleted_after, deleted_before]`` (timezone-aware) and, when
    ``size`` is given, of that size are considered. When ``mtime_ns`` is given, a file's
    ``$R`` item must also have exactly that modification time (recycling is a rename, so it
    keeps it); a folder's is not compared, because Windows may touch it while recycling, and
    only empty folders are recycled. The newest remaining entry wins. Never replaces
    anything. ``locate_bin`` defaults to :func:`current_user_bin`.
    """
    if os.path.lexists(fs.long_path(original)):
        return RestoreResult(False, "the original location is occupied")
    locator = locate_bin or current_user_bin
    folder = locator(original)
    if folder is None:
        return RestoreResult(False, "no Recycle Bin of this user was found on that drive")
    wanted = fs.comparable_path(original)
    matches = [
        (index, entry)
        for index, entry in _entries(folder)
        if fs.comparable_path(entry.original) == wanted
        and deleted_after <= entry.deleted <= deleted_before
        and (size is None or entry.size == size)
    ]
    if not matches:
        return RestoreResult(False, "no matching item in the Recycle Bin")

    def same_time(index: Path) -> bool:
        st = fs.stat_no_follow(index.with_name("$R" + index.name[2:]))
        if st is None or mtime_ns is None or stat_module.S_ISDIR(st.st_mode):
            return True  # a missing item is reported below; folders are not compared
        return st.st_mtime_ns == mtime_ns

    matches = [m for m in matches if same_time(m[0])]
    if not matches:
        return RestoreResult(False, "the item in the Recycle Bin has a different modified time")
    index, _entry = max(matches, key=lambda m: m[1].deleted)
    item = index.with_name("$R" + index.name[2:])
    st = fs.stat_no_follow(item)
    if st is None:
        return RestoreResult(False, "the recycled item is missing from the Recycle Bin")
    attributes: int = getattr(st, "st_file_attributes", 0)
    if fs.stat_is_link(st) or attributes & fs.FILE_ATTRIBUTE_REPARSE_POINT:
        return RestoreResult(False, "the recycled item is a link or a cloud placeholder")
    try:
        original.parent.mkdir(parents=True, exist_ok=True)
        fs.rename_no_replace(item, original)
    except OSError as exc:
        return RestoreResult(False, f"could not move it back: {exc}")
    try:
        os.remove(fs.long_path(index))
    except OSError as exc:  # the item is back; a stale index only shows as a ghost entry
        log.warning("Restored %s but could not remove %s: %s", original, index, exc)
    return RestoreResult(True, "restored from the Recycle Bin")
