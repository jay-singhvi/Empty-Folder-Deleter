"""Temp cleaner (plan §4 Phase 7): remove old files from temp folders, and list browser caches.

``scan()`` reads names, sizes, times and attributes only. It never opens a file and never changes
the disk. The plan holds one ``trash`` action (Recycle Bin, checked by default) for every file
that is old enough and matches the patterns, then, when asked, one ``trash`` action for every
folder that will be left empty, deepest first. Nothing happens until Apply.

Rules that always hold:
* the folders to clean are the ones chosen, or the user's temp folder (``TEMP``, ``TMP`` and
  ``%LOCALAPPDATA%\\Temp``) when none are chosen. :func:`default_temp_roots` works out that
  default from an environment mapping and touches no disk, so it can be tested with a fake one;
* nothing outside the chosen folders is touched. The folders themselves are never removed;
* a folder is removed only when every item in it is going too. A junction or symlink, a hidden or
  excluded file, a recent file or a cloud-only file keeps its folder, and the folders above it;
* junctions and symlinks are never followed (AGENTS.md rule 4), protected folders are refused
  (rule 6), cloud-only OneDrive files are skipped and never read (rule 3);
* browser caches are **listed, never cleaned**: browsers manage their own caches. They appear
  only as notes (see :data:`BROWSER_CACHE_PREFIX`), and a browser cache folder inside a chosen
  folder is skipped, never trashed. A file that is in use fails when applied, on its own, and
  the rest carry on.
"""

from __future__ import annotations

import fnmatch
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field

from various_tools.core.context import RunContext
from various_tools.core.fs import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    is_inside,
    long_path,
    root_refusal,
)
from various_tools.core.onedrive import is_on_device, status_from_attributes
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.tools.base import Category, ToolOptions

TOOL_ID = "temp_cleaner"
FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
SECONDS_PER_DAY = 86400
DEFAULT_MIN_AGE_DAYS = 7

# Plan note format for a browser cache (documented in docs/tools/temp_cleaner.md). The size is the
# integer number of bytes, or ``unknown`` when sizes are not measured or could not be read.
BROWSER_CACHE_PREFIX = "browser-cache (read-only): "


def _hint(group: str, *, advanced: bool = False, **extra: Any) -> Any:
    """The ``json_schema_extra`` hints the GUI and CLI read (see ``core/settings.py``)."""
    hints: dict[str, Any] = {"group": group}
    if advanced:
        hints["advanced"] = True
    hints.update(extra)
    return hints


def format_size(size: int) -> str:
    """Bytes as text with 1024-based units, as Windows shows them: ``1.5 GB``, ``340 KB``."""
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


# ----- default folders and browser caches (pure: they read an environment mapping only) ----------


def _lookup(environ: Mapping[str, str], name: str) -> str | None:
    """``environ[name]`` ignoring letter case (Windows variables are case-insensitive)."""
    wanted = name.casefold()
    for key, value in environ.items():
        if key.casefold() == wanted and value.strip():
            return value.strip()
    return None


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(path))


def default_temp_roots(environ: Mapping[str, str]) -> list[Path]:
    """The user's temp folders from ``environ``, without repeats: ``TEMP``, ``TMP`` and
    ``LOCALAPPDATA\\Temp``. Variables that are unset or not an absolute path are ignored.

    Pure function: it reads only ``environ`` and touches no disk. Whether a folder exists is
    checked later, when the scan refuses a missing or protected root.
    """
    candidates: list[str] = []
    for name in ("TEMP", "TMP"):
        value = _lookup(environ, name)
        if value:
            candidates.append(value)
    local = _lookup(environ, "LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Temp"))
    roots: list[Path] = []
    seen: set[str] = set()
    for text in candidates:
        path = Path(text)
        if not path.is_absolute() or _key(path) in seen:
            continue
        seen.add(_key(path))
        roots.append(path)
    return roots


# (browser, folder from the environment, sub-folder, patterns for the cache folders inside it)
BROWSER_CACHE_LAYOUT: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "Chrome",
        "LOCALAPPDATA",
        "Google/Chrome/User Data",
        ("*/Cache", "*/Code Cache", "*/GPUCache"),
    ),
    (
        "Edge",
        "LOCALAPPDATA",
        "Microsoft/Edge/User Data",
        ("*/Cache", "*/Code Cache", "*/GPUCache"),
    ),
    ("Firefox", "LOCALAPPDATA", "Mozilla/Firefox/Profiles", ("*/cache2",)),
)


@dataclass(frozen=True)
class BrowserCache:
    name: str
    path: Path


def find_browser_caches(environ: Mapping[str, str]) -> list[BrowserCache]:
    """The Chrome, Edge and Firefox cache folders that exist under the folders in ``environ``.

    Lists folder names only (one ``glob`` per pattern); no file is opened and nothing is changed.
    Sorted by name, then path.
    """
    found: list[BrowserCache] = []
    for browser, variable, sub, patterns in BROWSER_CACHE_LAYOUT:
        base = _lookup(environ, variable)
        if base is None or not Path(base).is_absolute():
            continue
        data = Path(base) / sub
        for pattern in patterns:
            try:
                matches = sorted(data.glob(pattern))
            except OSError:
                continue
            for match in matches:
                if match.is_dir() and not match.is_symlink():
                    name = f"{browser} ({match.parent.name}) {match.name}"
                    found.append(BrowserCache(name=name, path=match))
    return sorted(found, key=lambda c: (c.name.casefold(), _key(c.path)))


def parse_browser_cache_note(note: str) -> tuple[str, Path, int | None] | None:
    """Read a ``browser-cache (read-only):`` plan note as ``(name, path, bytes or None)``."""
    if not note.startswith(BROWSER_CACHE_PREFIX):
        return None
    name, sep, rest = note[len(BROWSER_CACHE_PREFIX) :].partition(" | ")
    path_text, sep2, size_text = rest.rpartition(" | ")
    if not sep or not sep2 or not name or not path_text:
        return None
    size = int(size_text) if size_text.isdigit() else None
    return name, Path(path_text), size


def _folder_size(folder: Path, ctx: RunContext) -> int | None:
    """Total size of the files under ``folder`` from stat data alone, or ``None`` when the folder
    cannot be read. Junctions and symlinks are not followed and no file is opened."""
    total = 0
    stack = [folder]
    first = True
    while stack:
        ctx.check_cancelled()
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as iterator:
                for entry in iterator:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attributes = getattr(st, "st_file_attributes", 0)
                    if entry.is_symlink() or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(current / entry.name)
                    else:
                        total += st.st_size
        except OSError:
            if first:
                return None
        first = False
    return total


# ----- options -----------------------------------------------------------------------------------


class TempCleanerOptions(ToolOptions):
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to clean",
        description=(
            "The folders to remove old files from. Leave empty to use your temp folder (the "
            "TEMP and TMP folders and %LOCALAPPDATA%\\Temp). A drive root or a protected folder "
            "is refused. The folders themselves are never removed."
        ),
        json_schema_extra=_hint("Folders", widget="folders"),
    )
    recursive: bool = Field(
        default=True,
        title="Include sub-folders",
        description=(
            "Also clean the sub-folders of each folder. When off, only files directly in the "
            "folder are removed and no folder is removed. Default: on."
        ),
        json_schema_extra=_hint("Folders"),
    )
    min_age_days: int = Field(
        default=DEFAULT_MIN_AGE_DAYS,
        ge=0,
        le=36500,
        title="Older than (days)",
        description=(
            "Only files last modified more than this many days ago are removed, so files that "
            "programs are using right now are left alone. 0 removes files of any age, including "
            "recent ones. Default: 7."
        ),
        json_schema_extra=_hint("Rules"),
    )
    include_patterns: list[str] = Field(
        default_factory=lambda: ["*"],
        min_length=1,
        title="Only these files",
        description=(
            "File names or relative paths (patterns allowed, for example *.tmp or *.log) that "
            "may be removed. Matched without regard to letter case. Default: * (every file)."
        ),
        json_schema_extra=_hint("Rules", widget="glob_list"),
    )
    exclude_patterns: list[str] = Field(
        default_factory=list,
        title="Never remove",
        description=(
            "File or folder names, or relative paths (patterns allowed), that are always kept. "
            "A kept folder stays with everything in it. Matched without regard to letter case. "
            "Default: none."
        ),
        json_schema_extra=_hint("Rules", widget="glob_list"),
    )
    skip_hidden_system: bool = Field(
        default=True,
        title="Keep hidden and system files",
        description=(
            "Files and folders that Windows marks hidden or system are kept, and so are the "
            "folders that hold them. On by default, because programs create them."
        ),
        json_schema_extra=_hint("Rules", advanced=True),
    )
    remove_empty_folders: bool = Field(
        default=True,
        title="Remove folders left empty",
        description=(
            "Also remove a folder when every item in it is being removed, or it is already "
            "empty, and it has not changed for the number of days above. The Recycle Bin gets "
            "them deepest first, after their files. Default: on."
        ),
        json_schema_extra=_hint("Rules"),
    )
    list_browser_caches: bool = Field(
        default=True,
        title="List browser caches",
        description=(
            "Add a note for each Chrome, Edge and Firefox cache folder found, so you can see how "
            "big they are. They are only listed: browsers manage their own caches, so nothing "
            "in them is ever removed here. Default: on."
        ),
        json_schema_extra=_hint("Browser caches"),
    )
    measure_browser_caches: bool = Field(
        default=True,
        title="Measure browser cache sizes",
        description=(
            "Add up the size of each listed browser cache from file sizes only (no file is "
            "opened). A big cache can take a few seconds. When off, the size shows as unknown. "
            "Default: on."
        ),
        json_schema_extra=_hint(
            "Browser caches", advanced=True, depends_on="list_browser_caches=true"
        ),
    )


# ----- scan --------------------------------------------------------------------------------------


@dataclass
class _Totals:
    files: int = 0
    bytes: int = 0
    folders: int = 0
    cloud_only: int = 0
    hidden: int = 0
    recent: int = 0
    unreadable: int = 0
    cache_skipped: int = 0


@dataclass
class _Scan:
    ctx: RunContext
    opts: TempCleanerOptions
    cutoff: float
    now: float
    pruned: frozenset[str]
    plan: Plan
    totals: _Totals = field(default_factory=_Totals)
    root: Path = field(default_factory=Path)

    def excluded(self, name: str, rel_posix: str) -> bool:
        return any(
            fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel_posix, g)
            for g in self.opts.exclude_patterns
        )

    def included(self, name: str, rel_posix: str) -> bool:
        return any(
            fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel_posix, g)
            for g in self.opts.include_patterns
        )

    def group_of(self, path: Path) -> str | None:
        """The first folder under the root that holds ``path``, for the plan view."""
        parts = path.relative_to(self.root).parts
        return str(self.root / parts[0]) if len(parts) > 1 else None

    def plan_folder(self, folder: Path, st: os.stat_result | None) -> bool:
        """Plan the clean-up of ``folder``. Returns True when the folder itself will be removed.

        Subfolders are planned first, so their actions come before the folder's own files, and a
        folder's action comes last: deepest first. The root is walked but never removable.
        """
        self.ctx.check_cancelled()
        try:
            with os.scandir(long_path(folder)) as iterator:
                entries = sorted(iterator, key=lambda e: e.name.casefold())
        except OSError as exc:
            self.totals.unreadable += 1
            self.plan.notes.append(
                f"Could not read {folder}; left in place ({exc.strerror or exc})"
            )
            return False

        blocked = False
        files: list[PlannedAction] = []
        subfolders: list[tuple[Path, os.stat_result]] = []
        for entry in entries:
            path = folder / entry.name
            rel_posix = path.relative_to(self.root).as_posix()
            try:
                child = entry.stat(follow_symlinks=False)
            except OSError:
                blocked = True
                continue
            attributes = _attributes(path, child)
            if entry.is_symlink() or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                blocked = True  # rule 4: never followed, and it counts as content
                continue
            if _key(path) in self.pruned:
                self.totals.cache_skipped += 1
                blocked = True
                continue
            if self.excluded(entry.name, rel_posix):
                blocked = True
                continue
            if self.opts.skip_hidden_system and attributes & (
                FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
            ):
                self.totals.hidden += 1
                blocked = True
                continue
            if entry.is_dir(follow_symlinks=False):
                if self.opts.recursive:
                    subfolders.append((path, child))
                else:
                    blocked = True
                continue
            if not entry.is_file(follow_symlinks=False):
                blocked = True
                continue
            if not self.included(entry.name, rel_posix):
                blocked = True
                continue
            if not is_on_device(status_from_attributes(attributes)):
                self.totals.cloud_only += 1
                self.plan.notes.append(f"skipped: cloud-only: {path}")
                blocked = True
                continue
            if child.st_mtime > self.cutoff:
                self.totals.recent += 1
                blocked = True
                continue
            files.append(self.file_action(path, child))
            self.totals.files += 1
            self.totals.bytes += child.st_size

        emptied_folders = 0
        for path, child in subfolders:
            if self.plan_folder(path, child):
                emptied_folders += 1
            else:
                blocked = True
        self.plan.actions.extend(files)

        if st is None or blocked or not self.opts.remove_empty_folders:
            return False
        if st.st_mtime > self.cutoff:
            return False  # changed recently: kept until it has been quiet for the age limit
        self.plan.actions.append(
            PlannedAction(
                kind=ActionKind.TRASH,
                src=folder,
                reason="folder left empty by this clean-up"
                if files or emptied_folders
                else "empty folder",
                group_id=self.group_of(folder),
            )
        )
        self.totals.folders += 1
        return True

    def file_action(self, path: Path, st: os.stat_result) -> PlannedAction:
        days = max(0, int((self.now - st.st_mtime) // SECONDS_PER_DAY))
        age = (
            "modified today" if days == 0 else f"modified {days} day{'' if days == 1 else 's'} ago"
        )
        return PlannedAction(
            kind=ActionKind.TRASH,
            src=path,
            reason=f"{format_size(st.st_size)}, {age}",
            group_id=self.group_of(path),
        )


def _attributes(path: Path, st: os.stat_result) -> int:
    """Windows attributes from a stat result already in hand. Opens nothing."""
    return getattr(st, "st_file_attributes", 0)


def _usable_roots(roots: list[Path], ctx: RunContext, notes: list[str]) -> list[Path]:
    """Folders that may be cleaned. Duplicates and folders inside another root drop out."""
    candidates: list[Path] = []
    for raw in roots:
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


class TempCleaner:
    id = TOOL_ID
    name = "Temp Cleaner"
    description = (
        "Remove old files from your temp folder (or folders you choose) to the Recycle Bin, "
        "and list browser caches without touching them."
    )
    category: Category = "Clean up"
    Options = TempCleanerOptions

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        # ``None`` means the real environment, read at scan time. Tests pass a fake mapping.
        self._environ = environ

    def environ(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, TempCleanerOptions):
            raise TypeError("TempCleaner.scan needs TempCleanerOptions")
        environ = self.environ()
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        chosen = list(opts.roots)
        if not chosen:
            chosen = default_temp_roots(environ)
            if chosen:
                plan.notes.append(
                    "No folder chosen, so the temp folder is used: "
                    + ", ".join(str(p) for p in chosen)
                )
            else:
                plan.notes.append(
                    "No folder chosen and no TEMP, TMP or LOCALAPPDATA folder is set."
                )
        caches = find_browser_caches(environ)
        roots: list[Path] = []
        for root in _usable_roots(chosen, ctx, plan.notes):
            if any(is_inside(root, cache.path) for cache in caches):
                plan.notes.append(f"Browser caches are listed, never cleaned; refused: {root}")
            else:
                roots.append(root)
        plan.roots = roots
        if not roots:
            plan.notes.append("No folders to clean.")
        else:
            now = time.time()
            scan = _Scan(
                ctx=ctx,
                opts=opts,
                cutoff=now - opts.min_age_days * SECONDS_PER_DAY,
                now=now,
                pruned=frozenset(_key(c.path) for c in caches),
                plan=plan,
            )
            for index, root in enumerate(roots, start=1):
                ctx.report(index - 1, len(roots), f"Scanning {root}")
                scan.root = root
                scan.plan_folder(root, None)
            ctx.report(len(roots), len(roots), "Scan finished")
            totals = scan.totals
            parts = [
                f"Temp cleaner: {totals.files} file(s) ({format_size(totals.bytes)}) not changed "
                f"for {opts.min_age_days} day(s) or more",
            ]
            if opts.remove_empty_folders:
                parts.append(f"{totals.folders} empty folder(s)")
            if totals.recent:
                parts.append(f"{totals.recent} too recent, kept")
            if totals.hidden:
                parts.append(f"{totals.hidden} hidden or system, kept")
            if totals.cloud_only:
                parts.append(f"{totals.cloud_only} cloud-only, skipped and not read")
            if totals.cache_skipped:
                parts.append(f"{totals.cache_skipped} browser cache folder(s) skipped")
            plan.notes.insert(0, "; ".join(parts) + ". Everything goes to the Recycle Bin.")
            ctx.log.info(
                "temp_cleaner scan: %d file(s), %d folder(s)", totals.files, totals.folders
            )

        if opts.list_browser_caches:
            self._list_caches(plan, caches, opts, ctx)
        return plan

    def _list_caches(
        self, plan: Plan, caches: list[BrowserCache], opts: TempCleanerOptions, ctx: RunContext
    ) -> None:
        """Browser caches as read-only notes. Never an action, whatever the roots are."""
        if not caches:
            plan.notes.append("No browser cache folders found (read-only listing).")
            return
        for index, cache in enumerate(caches, start=1):
            ctx.report(index - 1, len(caches), f"Measuring {cache.name}")
            size = _folder_size(cache.path, ctx) if opts.measure_browser_caches else None
            text = "unknown" if size is None else str(size)
            plan.notes.append(f"{BROWSER_CACHE_PREFIX}{cache.name} | {cache.path} | {text}")

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        return apply_plan(plan, ctx)


TOOL = TempCleaner()
