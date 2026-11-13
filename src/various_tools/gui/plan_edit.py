"""Small edits to a plan that the GUI makes before the user applies it. No Qt in here.

Duplicate plans group their actions by keeper: ``group_id`` is ``str(keeper path)`` and each
reason ends with ``keeps <keeper name>``. :func:`change_keeper` swaps which file of a group is
kept. It only edits the plan in memory; nothing on disk is touched.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from various_tools.core.plan import Plan, PlannedAction

_KEEPS = re.compile(r"(?:^|; )keeps [^\n]*\Z")


def human_size(size: int | None) -> str:
    """A short size such as ``1.2 MB``. An unknown size is an empty string."""
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"  # unreachable; keeps the type checker content


def group_ids(plan: Plan) -> list[str]:
    """Distinct group ids in the order they first appear."""
    seen: dict[str, None] = {}
    for action in plan.actions:
        if action.group_id is not None:
            seen.setdefault(action.group_id, None)
    return list(seen)


def group_actions(plan: Plan, group_id: str) -> list[PlannedAction]:
    """The actions of one group, in plan order."""
    return [a for a in plan.actions if a.group_id == group_id]


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _rewrite_reason(reason: str, old_name: str, new_name: str) -> str:
    """Point the trailing ``keeps <name>`` of a reason at ``new_name``."""
    suffix = f"keeps {old_name}"
    if reason.endswith(suffix):
        return f"{reason[: -len(suffix)]}keeps {new_name}"
    if _KEEPS.search(reason):
        return _KEEPS.sub(
            lambda m: ("; " if m.group(0).startswith("; ") else "") + f"keeps {new_name}", reason
        )
    return f"{reason}; keeps {new_name}" if reason else f"keeps {new_name}"


def change_keeper(plan: Plan, group_id: str, new_keeper: Path) -> str:
    """Make ``new_keeper`` the kept file of a duplicate group and return the new group id.

    ``new_keeper`` must be the source of one of the group's actions, otherwise ``ValueError``.
    The old keeper takes over that action (same kind, same selection state). A move destination
    keeps its folder and takes the old keeper's file name. Every action of the group gets the new
    group id and a reason ending in ``keeps <new name>``. The plan is changed in place.
    """
    actions = group_actions(plan, group_id)
    chosen = next((a for a in actions if a.src is not None and _same_path(a.src, new_keeper)), None)
    if chosen is None:
        raise ValueError(f"{new_keeper} is not part of this group")
    old_keeper = Path(group_id)
    new_id = str(chosen.src)
    chosen.src = old_keeper
    if chosen.dst is not None:
        chosen.dst = chosen.dst.parent / old_keeper.name
    for action in actions:
        action.group_id = new_id
        action.reason = _rewrite_reason(action.reason, old_keeper.name, Path(new_id).name)
    return new_id
