"""Show a results row's files in File Explorer, or open them in their default app.

:func:`reveal_targets` is pure apart from existence checks, which read attributes only, so a
cloud-only OneDrive file is never downloaded by building the menu or by "Show in File
Explorer". Opening a cloud-only file does download it, so the caller asks first
(:func:`needs_download`, AGENTS.md rule 3).

The Explorer executable is resolved by :func:`various_tools.core.deps.system_tool`, the only
place that names executables.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from various_tools.core import onedrive
from various_tools.core.deps import system_tool
from various_tools.core.plan import ActionKind, PlannedAction

log = logging.getLogger(__name__)

SHOW = "Show in File Explorer"


@dataclass(frozen=True)
class RevealTarget:
    """One "show" entry. ``path`` is what Explorer shows: ``original`` itself, or, when that
    does not exist yet (a destination), its nearest existing parent (``is_destination``)."""

    label: str
    path: Path
    exists: bool
    is_destination: bool = False
    original: Path | None = None

    @property
    def own_path(self) -> Path:
        """The path the row is about (what Copy path copies)."""
        return self.original if self.original is not None else self.path


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _nearest_existing(path: Path) -> Path:
    """``path`` if it exists, else its nearest existing parent (or ``path`` if none does)."""
    current = path
    while not _exists(current):
        if current.parent == current:
            return path
        current = current.parent
    return current


def _target(label: str, path: Path, *, destination: bool = False) -> RevealTarget:
    shown = _nearest_existing(path) if destination else path
    replaced = shown != path
    return RevealTarget(label, shown, _exists(shown), replaced, path)


def reveal_targets(
    action: PlannedAction | None, node_path: Path | None, is_duplicates: bool
) -> list[RevealTarget]:
    """The "show" entries for one row: one per place the row is about."""
    if action is None:
        return [_target(SHOW, node_path)] if node_path is not None else []
    src, dst = action.src, action.dst
    if is_duplicates and src is not None and action.group_id:
        targets = [
            _target("Show this copy", src),
            _target("Show the kept copy", Path(action.group_id)),
        ]
        if dst is not None:  # moved to the duplicate or review folder
            targets.append(_target("Show where it will go", dst, destination=True))
        return targets
    moves = action.kind in (ActionKind.MOVE, ActionKind.RENAME)
    if moves and src is not None and dst is not None:
        return [
            _target("Show where it is now", src),
            _target("Show where it will go", dst, destination=True),
        ]
    place = src if src is not None else dst if dst is not None else node_path
    if place is None:
        return []
    return [_target(SHOW, place, destination=src is None)]


def needs_download(path: Path) -> bool:
    """True when opening ``path`` would make OneDrive download it. Reads attributes only."""
    if not onedrive.is_regular_file_no_hydrate(path):
        return False
    return not onedrive.is_on_device(onedrive.get_sync_status(path))


def show_in_explorer(path: Path) -> bool:
    """Open File Explorer with ``path`` selected (a drive root is opened instead).

    Explorer splits its arguments at commas, and ``subprocess`` quotes only around spaces, so
    the command line is built here with the path in double quotes (a Windows path cannot
    contain ``"``).
    """
    explorer = system_tool("explorer")
    select = "" if path.parent == path else "/select,"
    command = f'"{explorer}" {select}"{path}"'
    try:
        subprocess.Popen(command)  # noqa: S603 - fixed program, a quoted path
    except OSError as exc:
        log.warning("Could not open File Explorer for %s: %s", path, exc)
        return False
    return True


def open_path(path: Path) -> bool:
    """Open a file in its default app, or a folder in File Explorer."""
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
