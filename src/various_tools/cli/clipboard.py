"""Copy the file an applied run wrote to the clipboard, when the tool's options ask for it.

Generic: any tool whose options have a boolean ``copy_to_clipboard`` that is true gets this after a
successful apply that wrote a file (a done ``write`` action). The decision and the reading live
here, without Qt, so the CLI and the GUI behave the same. The CLI puts the text on the Windows
clipboard with :func:`set_clipboard_text` (plain ``ctypes`` calls, no external program); the GUI
uses ``QGuiApplication.clipboard()``.

A file larger than :data:`MAX_CLIPBOARD_BYTES` is not copied, and the message says so.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from various_tools.core.plan import Plan, RunResult

COPY_OPTION = "copy_to_clipboard"
MAX_CLIPBOARD_BYTES = 4 * 1024 * 1024

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def wants_clipboard(plan: Plan) -> bool:
    """True when the plan's options have ``copy_to_clipboard`` set to true (exactly a bool)."""
    try:
        options = json.loads(plan.options_json)
    except ValueError:
        return False
    if not isinstance(options, dict):
        return False
    return options.get(COPY_OPTION) is True  # pyright: ignore[reportUnknownMemberType]


def written_file(result: RunResult) -> Path | None:
    """The file of the first done ``write`` action of a run, or None."""
    for outcome in result.outcomes:
        target = outcome.dst or outcome.src
        if outcome.kind == "write" and outcome.status == "done" and target:
            return Path(target)
    return None


@dataclass(frozen=True)
class ClipboardText:
    """The text to copy (None when it must not be copied) and a sentence for the user."""

    text: str | None
    message: str


def _size_text(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def read_for_clipboard(path: Path, limit: int | None = None) -> ClipboardText:
    """Read ``path`` as text for the clipboard, unless it is too large or unreadable.

    Call it off the UI thread in the GUI: it reads the whole file.
    """
    most = MAX_CLIPBOARD_BYTES if limit is None else limit
    try:
        size = path.stat().st_size
    except OSError as exc:
        return ClipboardText(None, f"Not copied to the clipboard: cannot read {path}: {exc}")
    if size > most:
        return ClipboardText(
            None,
            f"Not copied to the clipboard: {path.name} is {_size_text(size)}, more than the "
            f"{_size_text(most)} limit. Open the file instead: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ClipboardText(None, f"Not copied to the clipboard: cannot read {path}: {exc}")
    return ClipboardText(text, f"Copied {path.name} to the clipboard.")


def set_clipboard_text(text: str) -> bool:
    """Put ``text`` on the Windows clipboard. False when that did not work (or not on Windows)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    data = text.encode("utf-16-le") + b"\x00\x00"
    for _attempt in range(10):  # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True  # the clipboard owns the memory now
    finally:
        user32.CloseClipboard()
