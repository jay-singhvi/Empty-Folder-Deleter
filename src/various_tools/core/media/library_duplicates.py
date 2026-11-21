"""Which library photos have a duplicate, from the last Duplicate Finder result.

The Timeline never finds duplicates itself. After the Duplicate Finder has scanned the library
folders, :meth:`LibraryDuplicates.update_from_plan` keeps its groups (each group is the keeper
and the files that duplicate it, as the plan lists them) in ``library_duplicates.json`` in the
cache folder, with the plan's time and each file's size and modified time as they were then.
:meth:`LibraryDuplicates.index_for` matches those groups to the library's items. A file whose
size or modified time has changed since is ignored (the answer about it may be out of date), and
a group needs two such files in the library to count. Nothing is moved or read from the
Timeline: only ``os.stat`` is called, and only when the result is saved.

No Qt here and no ``print``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from various_tools.core.jsonutil import as_dict, as_list
from various_tools.core.media.library import MediaItem
from various_tools.core.plan import Plan

log = logging.getLogger(__name__)

DUPLICATES_FILE = "library_duplicates.json"
DUPLICATES_TOOL_ID = "duplicates"
FORMAT = 1
_MTIME_TOLERANCE = 0.002


def duplicates_path(cache_folder: Path) -> Path:
    return cache_folder / DUPLICATES_FILE


def path_key(path: Path | str) -> str:
    """Comparison key for a path (case-insensitive on Windows, normalised separators)."""
    return os.path.normcase(os.path.normpath(os.fspath(path)))


@dataclass(frozen=True)
class Member:
    path: str
    size: int
    mtime: float


@dataclass(frozen=True)
class DuplicateIndex:
    """The groups that match the library now: ``groups[n]`` lists the items of group ``n``."""

    groups: list[list[MediaItem]]
    group_of: dict[str, int]  # path_key -> group number
    saved: str = ""  # when the Duplicate Finder plan was made (ISO time), "" when none

    def __len__(self) -> int:
        return len(self.groups)

    def has(self, item: MediaItem) -> bool:
        return path_key(item.path) in self.group_of

    def copies_of(self, item: MediaItem) -> list[MediaItem]:
        """The other items of the group of ``item`` (empty when it has no duplicate)."""
        number = self.group_of.get(path_key(item.path))
        if number is None:
            return []
        me = path_key(item.path)
        return [m for m in self.groups[number] if path_key(m.path) != me]

    def keys(self) -> set[str]:
        return set(self.group_of)


EMPTY = DuplicateIndex([], {})


class LibraryDuplicates:
    """The saved duplicate groups. Light: ``load`` and ``save`` read and write one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.saved = ""
        self.groups: list[list[Member]] = []
        self.load()

    # ----- file ------------------------------------------------------------------------------

    def load(self) -> None:
        self.saved, self.groups = "", []
        try:
            data = as_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except OSError, ValueError:
            return
        if data.get("format") != FORMAT:
            return
        self.saved = str(data.get("saved", ""))
        for raw in as_list(data.get("groups")):
            members: list[Member] = []
            for entry in as_list(raw):
                d = as_dict(entry)
                try:
                    members.append(Member(str(d["path"]), int(d["size"]), float(d["mtime"])))
                except KeyError, TypeError, ValueError:
                    continue
            if len(members) >= 2:
                self.groups.append(members)

    def save(self) -> None:
        payload: dict[str, Any] = {
            "format": FORMAT,
            "saved": self.saved,
            "groups": [
                [{"path": m.path, "size": m.size, "mtime": m.mtime} for m in group]
                for group in self.groups
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:  # a cache: failing to write it only loses the badges
            log.warning("Could not save %s: %s", self.path, exc)

    # ----- from a plan -----------------------------------------------------------------------

    def update_from_plan(self, plan: Plan) -> int:
        """Keep the duplicate groups of a Duplicate Finder plan. Returns how many groups.

        Other tools' plans are ignored (returns 0 and changes nothing). Each group is its
        keeper (the action's ``group_id``) and the files the plan moves or deletes. Size and
        modified time are read with ``os.stat``; a missing file leaves its group.
        """
        if plan.tool != DUPLICATES_TOOL_ID:
            return 0
        by_group: dict[str, list[str]] = {}
        for action in plan.actions:
            if action.group_id is None or action.src is None:
                continue
            members = by_group.setdefault(action.group_id, [action.group_id])
            members.append(str(action.src))
        groups: list[list[Member]] = []
        for paths in by_group.values():
            seen: set[str] = set()
            group: list[Member] = []
            for p in paths:
                key = path_key(p)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                group.append(Member(p, st.st_size, st.st_mtime))
            if len(group) >= 2:
                groups.append(group)
        self.groups = groups
        self.saved = plan.created
        self.save()
        return len(groups)

    # ----- matching the library --------------------------------------------------------------

    def index_for(self, find: Callable[[str], MediaItem | None]) -> DuplicateIndex:
        """Match the groups to library items. ``find`` gives the item for a path key (or None).

        A member counts when the library has it and its size and modified time are what they
        were when the result was saved.
        """
        groups: list[list[MediaItem]] = []
        group_of: dict[str, int] = {}
        for members in self.groups:
            kept: list[MediaItem] = []
            for m in members:
                item = find(path_key(m.path))
                if item is None or item.size != m.size:
                    continue
                if abs(item.mtime - m.mtime) > _MTIME_TOLERANCE:
                    continue
                kept.append(item)
            if len(kept) < 2:
                continue
            for item in kept:
                group_of[path_key(item.path)] = len(groups)
            groups.append(kept)
        return DuplicateIndex(groups, group_of, self.saved)
