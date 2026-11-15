"""Plans and the apply engine: a scan produces a Plan, and only apply() changes the disk.

Every action is journaled before it runs and marked done, failed or skipped afterwards.
Guard rules run again at apply time, so a plan saved to JSON and applied later is still checked.

Safety rules enforced here (AGENTS.md):
* rule 2: deletes go to the Recycle Bin. A permanent delete needs ``delete_mode == "permanent"``.
* rule 4: the selected root is never moved, trashed or deleted, and actions stay inside the roots.
  A plan without roots changes nothing. Each root is checked again with ``root_refusal`` at
  apply time, and a source below a junction or symlink inside its root is refused.
* rule 5: every action is journaled before it runs.
* rule 6: protected paths are refused unless ``allow_protected_paths`` is set. ``write`` and
  ``convert`` actions get the protected-path and junction checks on ``src``/``dst`` from the
  engine too, so a new handler cannot forget them.

Moves never replace anything: :func:`~various_tools.core.fs.move_no_replace` renames on one
volume, copies through a ``.partial`` file to another (refusing cloud-only files and folders),
and fails rather than overwrite a destination that appears at the last moment.

**Extra journal rows from a handler.** A ``write`` or ``convert`` handler sometimes has a side
effect of its own, such as recycling the original after a verified conversion. Call
:func:`trash_extra` (or, for any other kind, the :func:`extra_action` context manager) from
inside the handler. The side effect becomes its own journal row, with the same ``seq`` as the
action that caused it: written as pending before it happens, then marked done or failed. Undo
handles it like any other row, so a recycled original is restored from the Recycle Bin. Both
raise :class:`RuntimeError` when called outside :func:`apply_plan`, and :class:`ActionRefused`
when the engine's guard rules refuse the side effect (it is then journaled as skipped).

For ``trash`` rows (before the item goes) and for the output of done ``write``/``convert`` rows
(after it is written) the engine records the file's size and ``mtime_ns`` in the journal. Undo
uses them to find the item in the Recycle Bin, and to recycle an output only if it is unchanged.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from send2trash import send2trash

from various_tools.core import fs, recycle
from various_tools.core.context import Cancelled, RunContext
from various_tools.core.fs import (
    destination_refusal,
    is_case_only_rename,
    is_inside,
    long_path,
    protected_reason,
    reparse_ancestor,
    root_refusal,
    same_path,
    stat_is_link,
    unique_destination,
)
from various_tools.core.journal import Journal
from various_tools.core.jsonutil import as_dict, as_list

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
    # Files the scan skipped because they are cloud-only (AGENTS.md rule 3), for the optional
    # "download these N files" action. Saved as "cloud_only" in the plan JSON.
    cloud_only: list[Path] = field(default_factory=list[Path])

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
            "cloud_only": [str(p) for p in self.cloud_only],
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
            cloud_only=[Path(str(p)) for p in as_list(data.get("cloud_only")) if p],
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

_SOURCE_CHANGING = frozenset(
    {ActionKind.MOVE, ActionKind.RENAME, ActionKind.TRASH, ActionKind.DELETE}
)
_OUTPUT_KINDS = frozenset({ActionKind.WRITE, ActionKind.CONVERT})


class ActionRefused(RuntimeError):
    """Raised by :func:`extra_action` when the guard rules refuse the side effect."""


@dataclass
class _Running:
    """What the engine knows while one action runs; read by :func:`extra_action`."""

    plan: Plan
    ctx: RunContext
    journal: Journal
    run_id: int
    seq: int
    refused_roots: Mapping[str, str]


_RUNNING: ContextVar[_Running | None] = ContextVar("various_tools_running_action", default=None)


def _refused_roots(plan: Plan, ctx: RunContext) -> dict[str, str]:
    """``root_refusal`` for every plan root, checked again at apply time. Keyed by root."""
    rules = ctx.settings.protected_paths()
    refused: dict[str, str] = {}
    for root in plan.roots:
        problem = root_refusal(root, rules)
        if problem is not None:
            refused[str(root)] = problem
    return refused


def _deepest_root(path: Path, roots: list[Path]) -> Path | None:
    containing = [r for r in roots if is_inside(path, r)]
    return max(containing, key=lambda r: len(str(r))) if containing else None


def _guard(
    action: PlannedAction,
    plan: Plan,
    ctx: RunContext,
    refused_roots: Mapping[str, str] | None = None,
) -> str | None:
    """Why this action must not run, or None when it may. Checked again at apply time."""
    settings = ctx.settings
    rules = settings.protected_paths()
    kind = action.kind
    refused = refused_roots if refused_roots is not None else _refused_roots(plan, ctx)

    for path in (action.src, action.dst):
        if path is None:
            continue
        for root in plan.roots:
            problem = refused.get(str(root))
            if problem is not None and is_inside(path, root):
                return f"the selected folder is refused now: {problem}"

    if kind in _SOURCE_CHANGING:
        if action.src is None:
            return "no source path"
        if not plan.roots:
            return "the plan has no selected folders, so nothing in it may be changed"
        root = _deepest_root(action.src, plan.roots)
        if root is None:
            return "source is outside the selected folders"
        if any(same_path(action.src, r) for r in plan.roots):
            return "the selected root folder is never changed"
        reason = protected_reason(action.src, rules=rules)
        if reason and not settings.allow_protected_paths:
            return f"protected path ({reason})"
        link = reparse_ancestor(action.src.parent, root)
        if link is not None:
            return f"source is inside a junction or symlink, not followed: {link}"

    if kind in (ActionKind.TRASH, ActionKind.DELETE) and action.src is not None:
        # Trashing a folder takes its whole contents with it, so only an empty folder may go.
        refusal = _non_empty_folder_refusal(action.src)
        if refusal is not None:
            return refusal

    if (
        kind is ActionKind.TRASH
        and action.src is not None
        and os.path.lexists(long_path(action.src))
    ):
        # Rule 2: never where Windows would delete it for good instead (no bin, too big...).
        refusal = _recycle_refusal(action.src)
        if refusal is not None:
            return refusal

    if kind in (ActionKind.MOVE, ActionKind.RENAME):
        if action.dst is None:
            return "no destination path"
        if (
            kind is ActionKind.RENAME
            and action.src is not None
            and not same_path(action.dst.parent, action.src.parent)
        ):
            return "a rename must stay in the same folder"

    if kind in _OUTPUT_KINDS and action.src is not None:
        reason = protected_reason(action.src, rules=rules)
        if reason and not settings.allow_protected_paths:
            return f"protected path ({reason})"

    if action.dst is not None and kind not in (ActionKind.TRASH, ActionKind.DELETE):
        reason = protected_reason(action.dst, rules=rules)
        if reason and not settings.allow_protected_paths:
            return f"destination is a protected path ({reason})"
        # Rule 4 on the output side: never write through a junction or symlink. Stop at the
        # selected root that holds the destination, or walk to the drive root.
        refusal = destination_refusal(action.dst, _deepest_root(action.dst, plan.roots))
        if refusal is not None:
            return refusal

    if kind is ActionKind.DELETE and settings.delete_mode != "permanent":
        return "permanent delete is off; the item would go to the Recycle Bin instead"
    return None


def _non_empty_folder_refusal(src: Path) -> str | None:
    """Why a trash or delete must not run on ``src``, or None. Lists names only, opens no file.

    A folder must be empty, whatever reparse tag it carries (a OneDrive folder is a cloud
    placeholder). Only a real link (junction or symlink, see ``fs.stat_is_link``) is exempt,
    because removing it removes the link and not what it points to.
    """
    st = fs.stat_no_follow(src)
    if st is None or stat_is_link(st):
        return None
    attributes: int = getattr(st, "st_file_attributes", 0)
    if not (stat_module.S_ISDIR(st.st_mode) or attributes & fs.FILE_ATTRIBUTE_DIRECTORY):
        return None
    try:
        with os.scandir(long_path(src)) as entries:
            has_entries = next(entries, None) is not None
    except OSError as exc:
        return f"could not check the folder before removing it: {exc.strerror or exc}"
    if has_entries:
        return "folder is not empty; only empty folders are removed"
    return None


def _file_stat(path: Path | None) -> tuple[int, int] | None:
    """``(size, mtime_ns)`` of a regular file that is not a link, else None. Opens nothing."""
    if path is None:
        return None
    st = fs.stat_no_follow(path)
    if st is None or stat_is_link(st) or not stat_module.S_ISREG(st.st_mode):
        return None
    return st.st_size, st.st_mtime_ns


class NotRecyclable(OSError):
    """Raised before anything is removed when the Recycle Bin would delete the item for good."""


class _RecycledDespiteError(Exception):
    """send2trash raised, but the item is gone: it was recycled. Carries the error text."""


def _recycle_refusal(path: Path) -> str | None:
    """Why ``path`` must not be sent to the Recycle Bin (see ``recycle.can_recycle``)."""
    return recycle.can_recycle(path)


def _send_to_trash(path: Path) -> None:
    """Recycle ``path``, only where Windows really keeps it (rule 2). send2trash recycles with
    no confirmation and no "delete permanently?" warning, so without this check a drive with
    no Recycle Bin would delete the item for good, silently."""
    refusal = _recycle_refusal(path)
    if refusal is not None:
        raise NotRecyclable(refusal)
    try:
        send2trash(str(path))
    except Exception as exc:
        if os.path.lexists(long_path(path)):
            raise
        raise _RecycledDespiteError(str(exc) or type(exc).__name__) from exc


def _execute(
    action: PlannedAction, final_dst: Path | None, handlers: Mapping[ActionKind, Handler]
) -> Path | None:
    """Run one action. Raises on failure. Returns the destination it produced, if any."""
    kind = action.kind
    src = action.src
    if kind in (ActionKind.MOVE, ActionKind.RENAME):
        assert src is not None and final_dst is not None
        if not os.path.lexists(long_path(src)):
            raise FileNotFoundError(f"source is gone: {src}")
        final_dst.parent.mkdir(parents=True, exist_ok=True)
        fs.move_no_replace(src, final_dst)
        return final_dst
    if kind is ActionKind.TRASH:
        assert src is not None
        if not os.path.lexists(long_path(src)):
            raise FileNotFoundError(f"source is gone: {src}")
        _send_to_trash(src)
        return None
    if kind is ActionKind.DELETE:
        assert src is not None
        st = fs.stat_no_follow(src)
        if st is None:
            raise FileNotFoundError(f"source is gone: {src}")
        if stat_module.S_ISDIR(st.st_mode) and not stat_is_link(st):
            os.rmdir(long_path(src))  # only an empty folder; never a recursive delete
        else:
            os.remove(long_path(src))
        return None
    handler = handlers.get(kind)
    if handler is None:
        raise NotImplementedError(f"no handler for {kind} actions in this tool")
    return handler(action)


@contextmanager
def extra_action(
    kind: ActionKind, src: Path | None, dst: Path | None = None, reason: str = ""
) -> Generator[None]:
    """Journal a side effect of the running ``write``/``convert`` action as its own row.

    Use inside a handler, around the code that performs the side effect::

        with extra_action(ActionKind.TRASH, original, reason="replaced by the output"):
            ...  # do it

    The guard rules run first (for a ``trash``, that includes the check that the drive's
    Recycle Bin would really keep the item); a refusal is journaled as skipped and raises
    :class:`ActionRefused`, before anything is removed. Otherwise the row is written as
    pending, then marked done when the block finishes, or failed (and the error re-raised) when
    it raises. A recycle that raised although the item is gone did happen: it is marked done,
    with the error kept, and nothing is raised. For a ``trash`` row the size and ``mtime_ns``
    of ``src`` are recorded first. Raises :class:`RuntimeError` outside :func:`apply_plan`.
    """
    running = _RUNNING.get()
    if running is None:
        raise RuntimeError("extra_action can only be used by a handler while a plan is applied")
    journal, run_id, seq = running.journal, running.run_id, running.seq
    extra = PlannedAction(kind, src=src, dst=dst, reason=reason)
    refusal = _guard(extra, running.plan, running.ctx, running.refused_roots)
    if refusal is not None:
        journal.record_action(
            run_id, seq, str(kind), src, dst, reason, status="skipped", error=refusal
        )
        raise ActionRefused(f"{kind} of {src or dst} refused: {refusal}")
    row = journal.record_action(run_id, seq, str(kind), src, dst, reason)
    if kind is ActionKind.TRASH:
        before = _file_stat(src)
        if before is not None:
            journal.set_item_stat(row, *before)
    try:
        yield
    except _RecycledDespiteError as exc:
        journal.set_action(row, "done", error=f"recycled, but the Recycle Bin reported: {exc}")
        running.ctx.log.warning("%s was recycled, but the Recycle Bin reported: %s", src, exc)
        return
    except BaseException as exc:
        journal.set_action(row, "failed", error=str(exc) or type(exc).__name__)
        raise
    journal.set_action(row, "done")


def trash_extra(path: Path, reason: str = "") -> None:
    """Send ``path`` to the Recycle Bin as its own journaled ``trash`` row (see
    :func:`extra_action`), so undo can restore it. For use inside a handler only."""
    with extra_action(ActionKind.TRASH, path, None, reason):
        if not os.path.lexists(long_path(path)):
            raise FileNotFoundError(f"source is gone: {path}")
        _send_to_trash(path)


def _final_destination(action: PlannedAction) -> Path | None:
    """Where a move or rename really goes: collisions are resolved now, so the journal records
    where the file went. A rename that only changes letter case keeps its name."""
    dst = action.dst
    if action.kind not in (ActionKind.MOVE, ActionKind.RENAME) or dst is None:
        return dst
    if not os.path.lexists(long_path(dst)):
        return dst
    if action.src is not None and is_case_only_rename(action.src, dst):
        return dst
    return unique_destination(dst.parent, dst.name)


def apply_plan(
    plan: Plan,
    ctx: RunContext,
    handlers: Mapping[ActionKind, Handler] | None = None,
) -> RunResult:
    """Apply the selected actions in order, journaling each one first.

    ``handlers`` supply ``write`` and ``convert`` actions, which are specific to a tool. A
    handler may journal side effects of its own with :func:`trash_extra` or
    :func:`extra_action`. Raises ``ValueError`` when there is no journal, because applying
    without one is not allowed.
    """
    if ctx.journal is None:
        raise ValueError("apply needs a journal, so that every action can be undone")
    journal: Journal = ctx.journal
    handler_map: Mapping[ActionKind, Handler] = handlers or {}

    run_id = journal.start_run(plan.tool, plan.options_json)
    result = RunResult(run_id=run_id, status="done")
    actions = plan.selected_actions()
    total = len(actions)
    refused_roots = _refused_roots(plan, ctx)
    try:
        for seq, action in enumerate(actions, start=1):
            ctx.check_cancelled()
            ctx.report(seq - 1, total, f"{action.kind}: {action.src or action.dst or ''}")
            src = str(action.src) if action.src is not None else None

            refusal = _guard(action, plan, ctx, refused_roots)
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

            final_dst = _final_destination(action)
            action_id = journal.record_action(
                run_id, seq, str(action.kind), action.src, final_dst, action.reason
            )
            if action.kind is ActionKind.TRASH:
                before = _file_stat(action.src)
                if before is not None:
                    journal.set_item_stat(action_id, *before)
            token = _RUNNING.set(_Running(plan, ctx, journal, run_id, seq, refused_roots))
            try:
                produced = _execute(action, final_dst, handler_map)
            except _RecycledDespiteError as exc:  # it did go to the Recycle Bin: done
                detail = f"recycled, but the Recycle Bin reported: {exc}"
                journal.set_action(action_id, "done", error=detail)
                result.outcomes.append(
                    ActionOutcome(seq, str(action.kind), "done", src, None, detail)
                )
                continue
            except Exception as exc:  # one failed action must not stop the rest
                journal.set_action(action_id, "failed", error=str(exc) or type(exc).__name__)
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
            finally:
                _RUNNING.reset(token)
            landed = produced or final_dst
            journal.set_action(action_id, "done", dst=landed)
            if action.kind in _OUTPUT_KINDS:
                after = _file_stat(landed)
                if after is not None:
                    journal.set_item_stat(action_id, *after)
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
