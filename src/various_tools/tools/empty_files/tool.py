"""Empty Files: find files of 0 bytes and send them to the Recycle Bin.

The size comes from the directory listing, so no file is ever opened (which also means cloud-only
OneDrive files are never read). Files that are empty on purpose (``__init__.py``, ``.gitkeep`` and
so on) are kept by name. The selected root is never a target; junctions and symlinks are never
followed (``fs.walk``). Removal is always a ``trash`` action, which the engine journals so that
History can undo it.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

from pydantic import Field

from various_tools.core.context import RunContext
from various_tools.core.fs import is_inside, root_refusal, stat_no_follow, walk
from various_tools.core.plan import (
    ActionKind,
    ActionOutcome,
    Plan,
    PlannedAction,
    RunResult,
    apply_plan,
)
from various_tools.tools.base import Category, ToolOptions

FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
SECONDS_PER_DAY = 86400

DEFAULT_EXCLUDE_GLOBS = (".git", "node_modules")
DEFAULT_KEEP_NAMES = ("__init__.py", ".gitkeep", ".keep", "py.typed", ".nojekyll")


class EmptyFilesOptions(ToolOptions):
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to scan",
        description="The folders to look in, including the folders inside them.",
        json_schema_extra={"widget": "folders", "group": "Folders"},
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS),
        title="Never touch",
        description=(
            "Names or relative paths (patterns allowed) that are left alone, with everything "
            "inside them. Default: .git, node_modules."
        ),
        json_schema_extra={"widget": "glob_list", "group": "Rules", "advanced": True},
    )
    keep_names: list[str] = Field(
        default_factory=lambda: list(DEFAULT_KEEP_NAMES),
        title="Empty on purpose",
        description=(
            "Exact file names that are kept even when empty, because programs expect them to "
            "be empty. Matched without regard to letter case. "
            "Default: __init__.py, .gitkeep, .keep, py.typed, .nojekyll."
        ),
        json_schema_extra={"widget": "glob_list", "group": "Rules"},
    )
    min_age_days: int = Field(
        default=0,
        ge=0,
        le=36500,
        title="Minimum age (days)",
        description=(
            "Files changed more recently than this many days ago are kept. 0 means any age "
            "may be removed."
        ),
        json_schema_extra={"group": "Rules"},
    )
    skip_hidden_system: bool = Field(
        default=True,
        title="Keep hidden and system files",
        description=(
            "Files marked hidden or system by Windows are kept. On by default, because they "
            "are often created by programs."
        ),
        json_schema_extra={"group": "Rules", "advanced": True},
    )


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _usable_roots(options: EmptyFilesOptions, ctx: RunContext, notes: list[str]) -> list[Path]:
    """Folders that may be scanned. Duplicates and folders inside another root drop out."""
    candidates: list[Path] = []
    for raw in options.roots:
        path = Path(raw)
        refusal = root_refusal(path, ctx.settings.protected_paths())
        if refusal is not None:
            notes.append(refusal)
            continue
        if any(_key(path) == _key(other) for other in candidates):
            continue
        candidates.append(path)
    return [
        path
        for path in candidates
        if not any(_key(path) != _key(other) and is_inside(path, other) for other in candidates)
    ]


class EmptyFiles:
    id = "empty_files"
    name = "Empty Files"
    # Plain words for the GUI (gui/wording.py).
    scan_label = "Look for empty files"
    found_nouns = ("empty file", "empty files")
    description = "Find files of 0 bytes and send them to the Recycle Bin."
    category: Category = "Clean up"
    Options = EmptyFilesOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, EmptyFilesOptions):
            raise TypeError("EmptyFiles.scan needs EmptyFilesOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        roots = _usable_roots(opts, ctx, plan.notes)
        plan.roots = roots
        if not roots:
            plan.notes.append("No folders to scan.")
            return plan

        keep = frozenset(name.casefold() for name in opts.keep_names)
        cutoff = time.time() - opts.min_age_days * SECONDS_PER_DAY if opts.min_age_days else None
        hidden_mask = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
        for index, root in enumerate(roots, start=1):
            ctx.report(index - 1, len(roots), f"Scanning {root}")
            for info in walk(root, exclude_globs=opts.exclude_globs):
                ctx.check_cancelled()
                if info.size != 0 or info.path.name.casefold() in keep:
                    continue
                if opts.skip_hidden_system and info.attributes & hidden_mask:
                    continue
                if cutoff is not None and info.mtime > cutoff:
                    continue
                plan.actions.append(
                    PlannedAction(kind=ActionKind.TRASH, src=info.path, reason="empty file")
                )
        ctx.report(len(roots), len(roots), "Scan finished")

        found = len(plan.actions)
        plan.notes.insert(
            0, f"Found {found} empty file(s) to remove." if found else "No empty files found."
        )
        ctx.log.info("empty_files scan: %d file(s)", found)
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        # A plan can be applied long after the scan: a file that has been written to since is
        # no longer empty and must not be trashed. Attributes only, so nothing is opened.
        kept: list[PlannedAction] = []
        changed: list[ActionOutcome] = []
        for action in plan.actions:
            if action.selected and action.src is not None and _no_longer_empty(action.src):
                kept.append(replace(action, selected=False))
                changed.append(
                    ActionOutcome(0, str(action.kind), "skipped", str(action.src), None, CHANGED)
                )
            else:
                kept.append(action)
        result = apply_plan(replace(plan, actions=kept), ctx)
        journal = ctx.journal
        seq = len(result.outcomes)
        for outcome in changed:
            seq += 1
            if journal is not None:
                journal.record_action(
                    result.run_id,
                    seq,
                    outcome.kind,
                    Path(outcome.src) if outcome.src else None,
                    None,
                    "checked again at apply",
                    status="skipped",
                    error=outcome.detail,
                )
            result.outcomes.append(replace(outcome, seq=seq))
        return result


CHANGED = "changed since the scan: no longer empty"


def _no_longer_empty(path: Path) -> bool:
    st = stat_no_follow(path)
    return st is not None and st.st_size != 0


TOOL = EmptyFiles()
