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
* sidecar files (``name.xmp``, ``name.aae``, ``name.thm`` and ``name.ext.xmp``, any letter case)
  follow their file as renames of their own, planned right after it and in one group, so they
  are ticked, applied and undone together: ticking a file ticks its sidecars, a sidecar cannot
  be ticked without its file, and at apply a sidecar is skipped when its file was not renamed
  and follows the name its file really got. A sidecar whose new name is taken, one the
  never-touch list covers, and a ``<stem>`` sidecar another file shares are left alone;
* ``scan()`` only reads. ``apply()`` runs the plan through the generic engine, which journals
  every rename so History can undo it.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
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
from various_tools.core.plan import (
    ActionKind,
    ActionOutcome,
    Plan,
    PlannedAction,
    RunResult,
    apply_plan,
)
from various_tools.tools.base import Category, ToolOptions
from various_tools.tools.wrong_extension.signatures import (
    FAMILIES,
    Family,
    detect,
    family_of,
    is_equivalent,
)

log = logging.getLogger(__name__)

HEAD_BYTES = 4096  # two MPEG audio frames (MP1, MP2) need more than the usual 64
DEFAULT_EXCLUDE_GLOBS = (".git", "node_modules")
TOOL_ID = "wrong_extension"
SIDECAR_EXTS = (".xmp", ".aae", ".thm")
# Facts a sidecar's action keeps, so that apply can tie it to its file (see _follow_file).
MAIN_SRC, MAIN_DST, STYLE, SUFFIX = "main_src", "main_dst", "style", "suffix"


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


class _Siblings:
    """The file names in each folder, read once, for sidecar matching (never changes anything)."""

    def __init__(self) -> None:
        self._folders: dict[str, list[str]] = {}

    def names(self, folder: Path) -> list[str]:
        key = _key(folder)
        if key not in self._folders:
            try:
                with os.scandir(long_path(folder)) as entries:
                    self._folders[key] = [
                        e.name for e in entries if e.is_file(follow_symlinks=False)
                    ]
            except OSError as exc:
                log.warning("Could not list %s: %s", folder, exc)
                self._folders[key] = []
        return self._folders[key]


def _excluded(name: str, folder: Path, root: Path, globs: Sequence[str]) -> bool:
    """True when the never-touch list covers ``folder/name``, as ``fs.walk`` would."""
    try:
        rel = (folder / name).relative_to(root).as_posix()
    except ValueError:
        rel = name
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g) for g in globs)


def _plan_sidecars(
    main: PlannedAction,
    siblings: _Siblings,
    reserved: set[str],
    notes: list[str],
    claimed: set[str],
    root: Path,
    exclude_globs: Sequence[str],
) -> list[PlannedAction]:
    """Rename actions for the sidecars of ``main``, or an empty list.

    ``<name.ext>.<sidecar ext>`` follows the file's new full name. ``<stem>.<sidecar ext>``
    follows only when the stem itself changes (a taken name got a ``_dupN`` suffix) and no other
    file in the folder shares that stem, because it may belong to that file. A sidecar the
    never-touch list covers, or another file already took, is left alone; a name is reserved only
    for a sidecar that is really planned.
    """
    assert main.src is not None and main.dst is not None
    folder = main.src.parent
    names = siblings.names(folder)
    taken = {n.casefold() for n in names}
    wanted: list[tuple[str, str, str]] = [(main.src.name, main.dst.name, "full")]
    if main.src.stem != main.dst.stem:
        stem = main.src.stem.casefold()
        shared = any(
            n != main.src.name
            and Path(n).stem.casefold() == stem
            and Path(n).suffix.casefold() not in SIDECAR_EXTS
            for n in names
        )
        if not shared:
            wanted.append((main.src.stem, main.dst.stem, "stem"))
    found: list[PlannedAction] = []
    for old_prefix, new_prefix, style in wanted:
        for ext in SIDECAR_EXTS:
            old_name = next(
                (n for n in names if n.casefold() == (old_prefix + ext).casefold()), None
            )
            if old_name is None:
                continue
            old_path = folder / old_name
            if _key(old_path) in claimed or any(
                a.src is not None and _key(a.src) == _key(old_path) for a in found
            ):
                continue
            if _excluded(old_name, folder, root, exclude_globs):
                continue
            suffix = old_name[len(old_prefix) :]
            new_name = new_prefix + suffix
            target = folder / new_name
            if new_name.casefold() in taken or _key(target) in reserved:
                notes.append(f"skipped: sidecar's new name is already taken: {old_path}")
                continue
            reserved.add(_key(target))
            found.append(
                PlannedAction(
                    kind=ActionKind.RENAME,
                    src=old_path,
                    dst=target,
                    reason=f"sidecar of {main.src.name}",
                    extra={
                        MAIN_SRC: str(main.src),
                        MAIN_DST: str(main.dst),
                        STYLE: style,
                        SUFFIX: suffix,
                    },
                )
            )
    return found


def _is_sidecar(action: PlannedAction) -> bool:
    return MAIN_SRC in action.extra


def set_many_selected(plan: Plan, actions: Sequence[PlannedAction], checked: bool) -> None:
    """Tick or untick rows. A file takes its sidecars with it, and a sidecar is ticked only
    while its file is ticked (a sidecar alone may be unticked)."""
    mains = [a for a in actions if not _is_sidecar(a)]
    sidecars = [a for a in actions if _is_sidecar(a)]
    groups: dict[str, list[PlannedAction]] = {}
    for action in plan.actions:
        if _is_sidecar(action):
            groups.setdefault(action.extra[MAIN_SRC], []).append(action)
    for main in mains:
        main.selected = checked
        for side in groups.get(str(main.src), ()):
            side.selected = checked
    if not sidecars:
        return
    selected_mains = {str(a.src) for a in plan.actions if not _is_sidecar(a) and a.selected}
    for side in sidecars:
        side.selected = checked and side.extra[MAIN_SRC] in selected_mains


def set_selected(plan: Plan, action: PlannedAction, checked: bool) -> None:
    """The one-row form of :func:`set_many_selected`."""
    set_many_selected(plan, [action], checked)


def _follow_file(
    action: PlannedAction, finished: Mapping[str, ActionOutcome]
) -> PlannedAction | str:
    """A sidecar goes only after its file was renamed, and to the name the file really got."""
    main_src = action.extra.get(MAIN_SRC)
    if main_src is None:
        return action
    main = finished.get(main_src)
    if main is None or main.status != "done" or not main.dst:
        return "skipped: its file was not renamed"
    actual = Path(main.dst)
    if _key(actual) == _key(Path(action.extra[MAIN_DST])):
        return action
    prefix = actual.stem if action.extra[STYLE] == "stem" else actual.name
    new = actual.parent / (prefix + action.extra[SUFFIX])
    if os.path.lexists(long_path(new)):
        return f"skipped: sidecar's new name is already taken: {new}"
    return replace(action, dst=new)


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
        siblings = _Siblings()
        claimed: set[str] = set()  # sidecars already planned with another file
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
                    sidecars = _plan_sidecars(
                        action, siblings, reserved, plan.notes, claimed, root, opts.exclude_globs
                    )
                    if sidecars:
                        action.group_id = f"{action.src}"
                        for side in sidecars:
                            assert side.src is not None
                            claimed.add(_key(side.src))
                            side.group_id = action.group_id
                            plan.actions.append(side)
        ctx.report(len(roots), len(roots), "Scan finished")

        found = sum(1 for a in plan.actions if not a.reason.startswith("sidecar of "))
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
        return apply_plan(plan, ctx, before_action=_follow_file)


TOOL = WrongExtension()
