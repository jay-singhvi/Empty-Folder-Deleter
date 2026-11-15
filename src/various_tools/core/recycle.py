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
    locate_bin: BinLocator | None = None,
) -> RestoreResult:
    """Move the recycled item that used to be at ``original`` back there, if found exactly.

    Only entries deleted within ``[deleted_after, deleted_before]`` (timezone-aware) and, when
    ``size`` is given, of that size are considered; the newest wins. Never replaces anything.
    ``locate_bin`` defaults to :func:`current_user_bin`.
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
