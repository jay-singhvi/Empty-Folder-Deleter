"""Empty Folders (plan §3.1): find folders that will end up empty, and remove them safely.

One bottom-up pass decides everything. A folder is removed when, after its own subfolders are
removed, it holds nothing but junk files (``desktop.ini``, ``Thumbs.db``, ``.DS_Store`` by default).
The junk files are removed first, then the folder. Actions are ordered deepest first.

Rules that differ from the original Tkinter app (plan §2.1 fixes):
* the selected root is never a target, even when it is empty;
* junctions and symlinks are never followed, and they count as content;
* an excluded entry (``.git``, ``node_modules``) counts as content, so its parent stays;
* hidden and system folders are kept by default, and they keep their parents too;
* removal goes to the Recycle Bin unless the user picks permanent delete.
"""

from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from pydantic import Field

from various_tools.core.context import RunContext
from various_tools.core.fs import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    is_inside,
    long_path,
    protected_reason,
)
from various_tools.core.plan import (
    ActionKind,
    ActionOutcome,
    Plan,
    PlannedAction,
    RunResult,
    apply_plan,
)
from various_tools.tools.base import ToolOptions

FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
SECONDS_PER_DAY = 86400

DEFAULT_TREAT_AS_EMPTY = ("desktop.ini", "Thumbs.db", ".DS_Store")
DEFAULT_EXCLUDE_GLOBS = (".git", "node_modules")
JUNK_REASON = "junk file in a folder that will be empty"


class EmptyFoldersOptions(ToolOptions):
    roots: list[Path] = Field(
        default_factory=list,
        json_schema_extra={"widget": "folders", "group": "Folders"},
    )
    treat_as_empty: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TREAT_AS_EMPTY),
        json_schema_extra={"widget": "glob_list", "group": "Rules"},
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS),
        json_schema_extra={"widget": "glob_list", "group": "Rules", "advanced": True},
    )
    min_age_days: int = Field(
        default=0,
        ge=0,
        json_schema_extra={"group": "Rules", "advanced": True},
    )
    skip_hidden_system: bool = Field(
        default=True,
        json_schema_extra={"group": "Rules", "advanced": True},
    )
    delete_mode: Literal["trash", "permanent"] = Field(
        default="trash",
        json_schema_extra={"group": "Delete"},
    )


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


@dataclass
class _Scan:
    """State shared across one scan. Lists are appended to in bottom-up order."""

    ctx: RunContext
    options: EmptyFoldersOptions
    kind: ActionKind
    junk: frozenset[str]
    cutoff: float | None
    root: Path = field(default_factory=Path)
    actions: list[PlannedAction] = field(default_factory=list[PlannedAction])
    # Each action with the folder it belongs to, so groups can be assigned at the end.
    owners: list[Path] = field(default_factory=list[Path])
    removed: list[Path] = field(default_factory=list[Path])
    notes: list[str] = field(default_factory=list[str])

    def excluded(self, name: str, rel_posix: str) -> bool:
        return any(
            fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(rel_posix, glob)
            for glob in self.options.exclude_globs
        )

    def may_remove(self, folder: Path, stat: os.stat_result) -> bool:
        attributes = getattr(stat, "st_file_attributes", 0)
        if self.options.skip_hidden_system and attributes & (
            FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
        ):
            self.notes.append(f"Hidden or system folder kept: {folder}")
            return False
        return self.cutoff is None or stat.st_mtime <= self.cutoff

    def add(self, path: Path, owner: Path, reason: str) -> None:
        self.actions.append(PlannedAction(kind=self.kind, src=path, reason=reason))
        self.owners.append(owner)

    def plan_folder(self, folder: Path, stat: os.stat_result, *, is_root: bool = False) -> bool:
        """Plan the removal of ``folder``. Returns True when it will be removed.

        Children are planned first, so the actions come out deepest first. The root is walked
        for its children but is never returned as removable.
        """
        self.ctx.check_cancelled()
        try:
            with os.scandir(long_path(folder)) as iterator:
                entries = list(iterator)
        except OSError as exc:
            self.notes.append(f"Could not read {folder}; left in place ({exc.strerror or exc})")
            return False

        blocked = False
        subfolders: list[tuple[Path, os.stat_result]] = []
        junk: list[Path] = []
        for entry in entries:
            path = folder / entry.name
            if self.excluded(entry.name, path.relative_to(self.root).as_posix()):
                blocked = True
                continue
            try:
                child_stat = entry.stat(follow_symlinks=False)
            except OSError:
                blocked = True
                continue
            if entry.is_symlink() or (
                getattr(child_stat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
            ):
                # Never followed. It counts as content, so the folder above it stays.
                blocked = True
                continue
            if entry.is_dir(follow_symlinks=False):
                subfolders.append((path, child_stat))
            elif entry.is_file(follow_symlinks=False) and entry.name.casefold() in self.junk:
                junk.append(path)
            else:
                blocked = True

        for path, child_stat in subfolders:
            if not self.plan_folder(path, child_stat):
                blocked = True

        if is_root:
            return False
        if blocked or not self.may_remove(folder, stat):
            return False

        for junk_file in junk:
            self.add(junk_file, folder, f"{JUNK_REASON}: {junk_file.name}")
        if junk:
            names = ", ".join(sorted(p.name for p in junk))
            reason = f"empty except for {names}"
        else:
            reason = "empty folder"
        self.add(folder, folder, reason)
        self.removed.append(folder)
        return True


def _usable_roots(options: EmptyFoldersOptions, ctx: RunContext, notes: list[str]) -> list[Path]:
    """Selected folders that can be scanned. Duplicates and folders inside another root drop out."""
    candidates: list[Path] = []
    for raw in options.roots:
        path = Path(raw)
        attributes = _attributes(path)
        if not path.is_dir():
            notes.append(f"Not a folder, skipped: {path}")
            continue
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            notes.append(f"Junction or symlink, not followed: {path}")
            continue
        reason = protected_reason(path)
        if reason and not ctx.settings.allow_protected_paths:
            notes.append(f"Protected folder refused ({reason}): {path}")
            continue
        if any(_key(path) == _key(other) for other in candidates):
            continue
        candidates.append(path)
    return [
        path
        for path in candidates
        if not any(_key(path) != _key(other) and is_inside(path, other) for other in candidates)
    ]


def _attributes(path: Path) -> int:
    try:
        return getattr(os.stat(long_path(path), follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return 0


def _assign_groups(scan: _Scan) -> None:
    """Group each action under the top-most folder of its empty tree, for the plan view."""
    removed = {_key(folder) for folder in scan.removed}
    top_of: dict[str, str] = {}
    for folder in scan.removed:
        top = folder
        while _key(top.parent) in removed:
            top = top.parent
        top_of[_key(folder)] = str(top)
    for action, owner in zip(scan.actions, scan.owners, strict=True):
        action.group_id = top_of.get(_key(owner))


class EmptyFolders:
    id = "empty_folders"
    name = "Empty Folder Deleter"
    description = (
        "Find folders that will be empty after their empty subfolders are removed, and "
        "remove them to the Recycle Bin."
    )
    category = "Clean up"
    Options = EmptyFoldersOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, EmptyFoldersOptions):
            raise TypeError("EmptyFolders.scan needs EmptyFoldersOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        roots = _usable_roots(opts, ctx, plan.notes)
        plan.roots = roots
        if not roots:
            plan.notes.append("No folders to scan.")
            return plan

        scan = _Scan(
            ctx=ctx,
            options=opts,
            kind=ActionKind.TRASH if opts.delete_mode == "trash" else ActionKind.DELETE,
            junk=frozenset(name.casefold() for name in opts.treat_as_empty),
            cutoff=(
                time.time() - opts.min_age_days * SECONDS_PER_DAY if opts.min_age_days else None
            ),
            notes=plan.notes,
        )
        for index, root in enumerate(roots, start=1):
            ctx.report(index - 1, len(roots), f"Scanning {root}")
            scan.root = root
            root_stat = os.stat(long_path(root))
            scan.plan_folder(root, root_stat, is_root=True)
        ctx.report(len(roots), len(roots), "Scan finished")

        _assign_groups(scan)
        plan.actions = scan.actions
        folders = len(scan.removed)
        junk_files = len(scan.actions) - folders
        plan.notes.insert(
            0,
            f"Found {folders} empty folder(s) and {junk_files} junk file(s) to remove."
            if folders
            else "No empty folders found.",
        )
        ctx.log.info("empty_folders scan: %d folder(s), %d junk file(s)", folders, junk_files)
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        checked, refused = _recheck(plan)
        result = apply_plan(checked, ctx)
        _journal_refused(ctx, result, refused)
        return result


def _is_junk_action(action: PlannedAction) -> bool:
    return action.reason.startswith(JUNK_REASON)


def set_folder_selected(plan: Plan, folder: Path, selected: bool) -> None:
    """Check or uncheck the removal of one folder, with its junk files.

    Unchecking also unchecks the removal of every folder above it, with the junk files inside
    those folders, because those folders would no longer be empty. Checking a folder never checks
    its parents.
    """
    target = _key(folder)
    unchecked_parents: set[str] = set()
    for action in plan.actions:
        if action.src is None or _is_junk_action(action):
            continue
        src = _key(action.src)
        if src == target:
            action.selected = selected
        elif not selected and is_inside(folder, action.src):
            action.selected = False
            unchecked_parents.add(src)
    for action in plan.actions:
        if action.src is None or not _is_junk_action(action):
            continue
        owner = _key(action.src.parent)
        if owner == target:
            action.selected = selected
        elif owner in unchecked_parents:
            action.selected = False


def _will_be_empty(
    folder: Path, junk: frozenset[str], removable: frozenset[str], memo: dict[str, bool]
) -> bool:
    """True when ``folder`` holds only junk files and subfolders that are themselves going."""
    key = _key(folder)
    if key in memo:
        return memo[key]
    memo[key] = False  # a guard against loops; replaced below
    ok = True
    try:
        with os.scandir(long_path(folder)) as iterator:
            for entry in iterator:
                path = folder / entry.name
                try:
                    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                except OSError:
                    ok = False
                    break
                if entry.is_symlink() or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    ok = False
                elif entry.is_dir(follow_symlinks=False):
                    ok = _key(path) in removable and _will_be_empty(path, junk, removable, memo)
                else:
                    ok = entry.is_file(follow_symlinks=False) and entry.name.casefold() in junk
                if not ok:
                    break
    except OSError:
        ok = False
    memo[key] = ok
    return ok


def _recheck(plan: Plan) -> tuple[Plan, list[ActionOutcome]]:
    """Unselect any junk-file removal whose folder has gained real content since the scan.

    A junk file is only trashed when its folder will really end up empty, so a file that was
    added after the scan is never left behind in a folder that lost its ``Thumbs.db``. Returns
    the plan to run and the outcomes of the rows that were refused.
    """
    options = EmptyFoldersOptions.model_validate_json(plan.options_json)
    junk = frozenset(name.casefold() for name in options.treat_as_empty)
    removable = frozenset(
        _key(a.src)
        for a in plan.actions
        if a.selected and a.src is not None and not _is_junk_action(a)
    )
    memo: dict[str, bool] = {}
    kept: list[PlannedAction] = []
    refused: list[ActionOutcome] = []
    for action in plan.actions:
        if (
            action.selected
            and action.src is not None
            and _is_junk_action(action)
            and not _will_be_empty(action.src.parent, junk, removable, memo)
        ):
            kept.append(replace(action, selected=False))
            refused.append(
                ActionOutcome(
                    0,
                    str(action.kind),
                    "skipped",
                    str(action.src),
                    None,
                    "its folder is no longer empty, so the file was kept",
                )
            )
            continue
        kept.append(action)
    return replace(plan, actions=kept), refused


def _journal_refused(ctx: RunContext, result: RunResult, refused: list[ActionOutcome]) -> None:
    """Record the rows refused before the engine ran, as skipped, like the engine's own refusals."""
    journal = ctx.journal
    seq = len(result.outcomes)
    for outcome in refused:
        seq += 1
        if journal is not None:
            journal.record_action(
                result.run_id,
                seq,
                outcome.kind,
                Path(outcome.src) if outcome.src else None,
                None,
                JUNK_REASON,
                status="skipped",
                error=outcome.detail,
            )
        result.outcomes.append(replace(outcome, seq=seq))


TOOL = EmptyFolders()
