"""Fix File Extensions: find files whose content does not match their extension, and rename them.

The first 64 bytes of each file are read and compared with known signatures (see
``signatures.py``). A file is renamed only when its content is recognised and its extension is
neither the right one nor an equivalent one (``.jpeg`` for JPEG, ``.docx`` for a ZIP container).
Content that is not recognised is left alone, and so are files with no extension or an extension
this tool does not check.

Safety:
* cloud-only OneDrive files are never opened. They are skipped and listed in the plan notes;
* junctions and symlinks are never followed (``fs.walk``), and the root is never renamed;
* the new name never replaces anything: ``unique_destination`` picks a free name, and names that
  other planned renames will take are counted as taken;
* ``scan()`` only reads. ``apply()`` runs the plan through the generic engine, which journals
  every rename so History can undo it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import Field

from various_tools.core import onedrive
from various_tools.core.context import RunContext
from various_tools.core.fs import (
    FileInfo,
    is_inside,
    long_path,
    root_refusal,
    unique_destination,
    walk,
)
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.tools.base import Category, ToolOptions
from various_tools.tools.wrong_extension.signatures import (
    FAMILIES,
    Family,
    detect,
    family_of,
    is_equivalent,
)

log = logging.getLogger(__name__)

HEAD_BYTES = 64
DEFAULT_EXCLUDE_GLOBS = (".git", "node_modules")
TOOL_ID = "wrong_extension"


class WrongExtensionOptions(ToolOptions):
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
    families: list[Family] = Field(
        default_factory=lambda: list(FAMILIES),
        title="Kinds of files to check",
        description=(
            "Which kinds of files to check, by their current extension: photos, videos, audio, "
            "documents (PDF and Office files) and archives. Default: all of them. Files with "
            "other extensions, or none, are never checked."
        ),
        json_schema_extra={"group": "Rules"},
    )


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _usable_roots(options: WrongExtensionOptions, ctx: RunContext, notes: list[str]) -> list[Path]:
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


def _read_head(path: Path) -> bytes | None:
    try:
        with open(long_path(path), "rb") as handle:  # noqa: PTH123 - open() is what is guarded
            return handle.read(HEAD_BYTES)
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return None


def _free_name(folder: Path, name: str, reserved: set[str]) -> Path:
    """A name that is free on disk and not already taken by another planned rename."""
    candidate = unique_destination(folder, name)
    counter = 1
    stem, suffix = Path(name).stem, Path(name).suffix
    while _key(candidate) in reserved:
        counter += 1
        candidate = unique_destination(folder, f"{stem}_dup{counter}{suffix}")
    reserved.add(_key(candidate))
    return candidate


class WrongExtension:
    id = TOOL_ID
    name = "Fix File Extensions"
    # Plain words for the GUI (gui/wording.py).
    scan_label = "Look for wrong extensions"
    found_nouns = ("file to rename", "files to rename")
    description = (
        "Find files whose content does not match their extension, such as a PNG named .jpg, "
        "and rename them to the right extension."
    )
    category: Category = "Organize"
    Options = WrongExtensionOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, WrongExtensionOptions):
            raise TypeError("WrongExtension.scan needs WrongExtensionOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        roots = _usable_roots(opts, ctx, plan.notes)
        plan.roots = roots
        if not roots:
            plan.notes.append("No folders to scan.")
            return plan

        wanted_families = set(opts.families)
        reserved: set[str] = set()
        checked = unreadable = 0
        for index, root in enumerate(roots, start=1):
            ctx.report(index - 1, len(roots), f"Scanning {root}")
            for info in walk(root, exclude_globs=opts.exclude_globs):
                ctx.check_cancelled()
                if family_of(info.path.suffix) not in wanted_families:
                    continue
                if not self._on_device(info, plan):
                    continue
                head = _read_head(info.path)
                if head is None:
                    unreadable += 1
                    continue
                checked += 1
                action = self._plan_rename(info.path, head, reserved)
                if action is not None:
                    plan.actions.append(action)
        ctx.report(len(roots), len(roots), "Scan finished")

        found = len(plan.actions)
        plan.notes.insert(
            0,
            f"Found {found} file(s) with the wrong extension among {checked} checked."
            if found
            else f"No wrong extensions found among {checked} checked file(s).",
        )
        if plan.cloud_only:
            plan.notes.append(
                f"skipped: cloud-only, not read: {len(plan.cloud_only)} file(s) "
                "(download them first to check them)"
            )
        if unreadable:
            plan.notes.append(f"Could not read {unreadable} file(s); left alone.")
        ctx.log.info("wrong_extension scan: %d to rename, %d checked", found, checked)
        return plan

    @staticmethod
    def _on_device(info: FileInfo, plan: Plan) -> bool:
        """False for a cloud-only file, which is recorded and never opened (AGENTS.md rule 3)."""
        if onedrive.is_on_device(onedrive.get_sync_status(info.path)):
            return True
        if info.path not in plan.cloud_only:
            plan.cloud_only.append(info.path)
        return False

    @staticmethod
    def _plan_rename(path: Path, head: bytes, reserved: set[str]) -> PlannedAction | None:
        wanted = detect(head)
        current = path.suffix
        if wanted is None or is_equivalent(current, wanted):
            return None
        dst = _free_name(path.parent, path.stem + wanted, reserved)
        return PlannedAction(
            kind=ActionKind.RENAME,
            src=path,
            dst=dst,
            reason=f"content is {wanted[1:].upper()}, but the name ends in {current}",
        )

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        return apply_plan(plan, ctx)


TOOL = WrongExtension()
