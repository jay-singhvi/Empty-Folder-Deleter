"""Plans and the apply engine: a scan produces a Plan, and only apply() changes the disk.

Every action is journaled before it runs and marked done, failed or skipped afterwards.
Guard rules run again at apply time, so a plan saved to JSON and applied later is still checked.

Safety rules enforced here (AGENTS.md):
* rule 2: deletes go to the Recycle Bin. A permanent delete needs ``delete_mode == "permanent"``.
* rule 4: the selected root is never moved, trashed or deleted, and actions stay inside the roots.
* rule 5: every action is journaled before it runs.
* rule 6: protected paths are refused unless ``allow_protected_paths`` is set.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from send2trash import send2trash

from various_tools.core.context import Cancelled, RunContext
from various_tools.core.fs import (
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    destination_refusal,
    is_inside,
    long_path,
    protected_reason,
    unique_destination,
)
from various_tools.core.journal import Journal
from various_tools.core.jsonutil import as_dict, as_list
from various_tools.core.onedrive import get_attributes

PLAN_FORMAT = 1


class ActionKind(StrEnum):
    MOVE = "move"
    RENAME = "rename"
    TRASH = "trash"
    DELETE = "delete"
    WRITE = "write"
    CONVERT = "convert"


@dataclass
class PlannedAction:
    kind: ActionKind
    src: Path | None = None
    dst: Path | None = None
    reason: str = ""
    group_id: str | None = None
    selected: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "src": str(self.src) if self.src is not None else None,
            "dst": str(self.dst) if self.dst is not None else None,
            "reason": self.reason,
            "group_id": self.group_id,
            "selected": self.selected,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PlannedAction:
        src, dst = data.get("src"), data.get("dst")
        return cls(
            kind=ActionKind(data["kind"]),
            src=Path(src) if src else None,
            dst=Path(dst) if dst else None,
            reason=str(data.get("reason", "")),
            group_id=data.get("group_id"),
            selected=bool(data.get("selected", True)),
        )


@dataclass
class Plan:
    tool: str
    roots: list[Path]
    actions: list[PlannedAction] = field(default_factory=list[PlannedAction])
    notes: list[str] = field(default_factory=list[str])
    options_json: str = "{}"
    created: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def selected_actions(self) -> list[PlannedAction]:
        return [a for a in self.actions if a.selected]

    def select_all(self, selected: bool = True) -> int:
        """Check (or uncheck) every action. Returns how many changed.

        Some tools start a plan unselected on purpose (for example a list of the largest files),
        so selecting everything is always an explicit user choice. Apply still asks first and the
        engine still checks every guard rule.
        """
        changed = sum(1 for a in self.actions if a.selected != selected)
        for action in self.actions:
            action.selected = selected
        return changed

    def to_json(self) -> str:
        payload = {
            "format": PLAN_FORMAT,
            "tool": self.tool,
            "roots": [str(r) for r in self.roots],
            "options": json.loads(self.options_json),
            "created": self.created,
            "notes": self.notes,
            "actions": [a.to_dict() for a in self.actions],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> Plan:
        data = as_dict(json.loads(text))
        if data.get("format") != PLAN_FORMAT or "tool" not in data:
            raise ValueError("not a Various Tools plan, or an unsupported plan format")
        return cls(
            tool=str(data["tool"]),
            roots=[Path(str(r)) for r in as_list(data.get("roots"))],
            actions=[PlannedAction.from_dict(as_dict(a)) for a in as_list(data.get("actions"))],
            notes=[str(n) for n in as_list(data.get("notes"))],
            options_json=json.dumps(as_dict(data.get("options")), sort_keys=True),
            created=str(data.get("created", "")),
        )


@dataclass(frozen=True)
class ActionOutcome:
    seq: int
    kind: str
    status: str  # done | failed | skipped
    src: str | None
    dst: str | None
    detail: str = ""


@dataclass
class RunResult:
    run_id: int
    status: str  # done | partial | cancelled
    outcomes: list[ActionOutcome] = field(default_factory=list[ActionOutcome])

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)


Handler = Callable[[PlannedAction], Path | None]


def _guard(action: PlannedAction, plan: Plan, ctx: RunContext) -> str | None:
    """Why this action must not run, or None when it may. Checked again at apply time."""
    settings = ctx.settings
    kind = action.kind

    if kind in (ActionKind.MOVE, ActionKind.RENAME, ActionKind.TRASH, ActionKind.DELETE):
        if action.src is None:
            return "no source path"
        if plan.roots and not any(is_inside(action.src, root) for root in plan.roots):
            return "source is outside the selected folders"
        if any(_same(action.src, root) for root in plan.roots):
            return "the selected root folder is never changed"
        reason = protected_reason(action.src, rules=settings.protected_paths())
        if reason and not settings.allow_protected_paths:
            return f"protected path ({reason})"

    if kind in (ActionKind.TRASH, ActionKind.DELETE) and action.src is not None:
        # Trashing a folder takes its whole contents with it, so only an empty folder may go.
        refusal = _non_empty_folder_refusal(action.src)
        if refusal is not None:
            return refusal

    if kind in (ActionKind.MOVE, ActionKind.RENAME):
        if action.dst is None:
            return "no destination path"
        reason = protected_reason(action.dst, rules=settings.protected_paths())
        if reason and not settings.allow_protected_paths:
            return f"destination is a protected path ({reason})"
        if (
            kind is ActionKind.RENAME
            and action.src is not None
            and not _same(action.dst.parent, action.src.parent)
        ):
            return "a rename must stay in the same folder"
        # Rule 4 on the output side: never write through a junction or symlink. Stop at the
        # selected root that holds the destination, or walk to the drive root.
        containing = [r for r in plan.roots if is_inside(action.dst, r)]
        stop = max(containing, key=lambda r: len(str(r))) if containing else None
        refusal = destination_refusal(action.dst, stop)
        if refusal is not None:
            return refusal

    if kind is ActionKind.DELETE and settings.delete_mode != "permanent":
        return "permanent delete is off; the item would go to the Recycle Bin instead"
    return None


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _non_empty_folder_refusal(src: Path) -> str | None:
    """Why a trash or delete must not run on ``src``, or None. Lists names only, opens no file."""
    attributes = get_attributes(src) or 0
    is_folder = bool(attributes & FILE_ATTRIBUTE_DIRECTORY) and not (
        attributes & FILE_ATTRIBUTE_REPARSE_POINT
    )
    if not is_folder:
        return None
    try:
        with os.scandir(long_path(src)) as entries:
            has_entries = next(entries, None) is not None
    except OSError as exc:
        return f"could not check the folder before removing it: {exc.strerror or exc}"
    if has_entries:
        return "folder is not empty; only empty folders are removed"
    return None


def _execute(
    action: PlannedAction, final_dst: Path | None, handlers: Mapping[ActionKind, Handler]
) -> Path | None:
    """Run one action. Raises on failure. Returns the destination it produced, if any."""
    kind = action.kind
    src = action.src
    if kind in (ActionKind.MOVE, ActionKind.RENAME):
        assert src is not None and final_dst is not None
        if not os.path.lexists(src):
            raise FileNotFoundError(f"source is gone: {src}")
        final_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(final_dst))
        return final_dst
    if kind is ActionKind.TRASH:
        assert src is not None
        if not os.path.lexists(src):
            raise FileNotFoundError(f"source is gone: {src}")
        send2trash(str(src))
        return None
    if kind is ActionKind.DELETE:
        assert src is not None
        attributes = get_attributes(src) or 0
        is_folder = bool(attributes & FILE_ATTRIBUTE_DIRECTORY) and not (
            attributes & FILE_ATTRIBUTE_REPARSE_POINT
        )
        if is_folder:
            os.rmdir(src)  # only an empty folder; never a recursive delete
        else:
            os.remove(src)
        return None
    handler = handlers.get(kind)
    if handler is None:
        raise NotImplementedError(f"no handler for {kind} actions in this tool")
    return handler(action)


def apply_plan(
    plan: Plan,
    ctx: RunContext,
    handlers: Mapping[ActionKind, Handler] | None = None,
) -> RunResult:
    """Apply the selected actions in order, journaling each one first.

    ``handlers`` supply ``write`` and ``convert`` actions, which are specific to a tool.
    Raises ``ValueError`` when there is no journal, because applying without one is not allowed.
    """
    if ctx.journal is None:
        raise ValueError("apply needs a journal, so that every action can be undone")
    journal: Journal = ctx.journal
    handler_map: Mapping[ActionKind, Handler] = handlers or {}

    run_id = journal.start_run(plan.tool, plan.options_json)
    result = RunResult(run_id=run_id, status="done")
    actions = plan.selected_actions()
    total = len(actions)
    try:
        for seq, action in enumerate(actions, start=1):
            ctx.check_cancelled()
            ctx.report(seq - 1, total, f"{action.kind}: {action.src or action.dst or ''}")
            src = str(action.src) if action.src is not None else None

            refusal = _guard(action, plan, ctx)
            if refusal is not None:
                journal.record_action(
                    run_id,
                    seq,
                    str(action.kind),
                    action.src,
                    action.dst,
                    action.reason,
                    status="skipped",
                    error=refusal,
                )
                result.outcomes.append(
                    ActionOutcome(
                        seq,
                        str(action.kind),
                        "skipped",
                        src,
                        str(action.dst) if action.dst else None,
                        refusal,
                    )
                )
                continue

            final_dst = action.dst
            is_move = action.kind in (ActionKind.MOVE, ActionKind.RENAME)
            if is_move and action.dst is not None and os.path.lexists(action.dst):
                # Resolve collisions now, so the journal records where the file really went.
                final_dst = unique_destination(action.dst.parent, action.dst.name)

            action_id = journal.record_action(
                run_id, seq, str(action.kind), action.src, final_dst, action.reason
            )
            try:
                produced = _execute(action, final_dst, handler_map)
            except Exception as exc:  # one failed action must not stop the rest
                journal.set_action(action_id, "failed", error=str(exc))
                result.outcomes.append(
                    ActionOutcome(
                        seq,
                        str(action.kind),
                        "failed",
                        src,
                        str(final_dst) if final_dst else None,
                        str(exc),
                    )
                )
                continue
            landed = produced or final_dst
            journal.set_action(action_id, "done", dst=landed)
            result.outcomes.append(
                ActionOutcome(seq, str(action.kind), "done", src, str(landed) if landed else None)
            )
        ctx.report(total, total, "finished")
    except Cancelled:
        result.status = "cancelled"
    if result.status != "cancelled":
        result.status = "partial" if result.count("failed") else "done"
    journal.finish_run(run_id, result.status)
    return result
