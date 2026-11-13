"""Disk usage (plan §4 Phase 7): find the largest files and folders under the chosen folders.

``scan()`` reads file names, sizes and times only. It never opens a file, so cloud-only OneDrive
placeholders are not recalled, and it never changes the disk. The plan holds:

* one ``trash`` action for each of the N largest files that pass the filters, **all unchecked**
  (``selected=False``): the person picks what to remove, and the Recycle Bin is the only way out;
* notes with the folder totals, in a fixed format a front end can parse (see
  :data:`FOLDER_SIZE_PREFIX`, :data:`ROOT_SIZE_PREFIX` and :func:`parse_size_note`).

Folders are never trash actions: the engine refuses to trash a folder that has contents.

Rules that always hold:
* the filters choose which files are counted, for the file list and for every folder total, so a
  "videos older than a year" scan shows the folders that hold the most old video;
* hidden and system files and folders are left out unless asked for;
* a cloud-only file takes no space on this device, so it is not counted and not offered for
  removal. It is reported as skipped (AGENTS.md rule 3);
* junctions and symlinks are never followed (rule 4), protected folders are refused (rule 6).
"""

from __future__ import annotations

import fnmatch
import heapq
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

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

TOOL_ID = "disk_usage"
FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
SECONDS_PER_DAY = 86400
BYTES_PER_MB = 1024 * 1024

# Plan note formats (documented in docs/tools/disk_usage.md). The path is always last, so it may
# hold spaces. Sizes are plain integers in bytes.
FOLDER_SIZE_PREFIX = "folder-size: "
ROOT_SIZE_PREFIX = "root-size: "
_SIZE_NOTE = re.compile(r"(folder-size|root-size): (\d+) (.+)", re.DOTALL)

DEFAULT_TOP_FILES = 50
DEFAULT_TOP_FOLDERS = 25

TypeCategory = Literal[
    "any", "pictures", "videos", "music", "documents", "archives", "installers", "disk_images"
]
CATEGORY_LABELS: dict[str, str] = {
    "pictures": "picture",
    "videos": "video",
    "music": "music",
    "documents": "document",
    "archives": "archive",
    "installers": "installer",
    "disk_images": "disk image",
}
# What each category means. A file type that is in none of them is reported as "other".
CATEGORY_EXTENSIONS: dict[str, frozenset[str]] = {
    "pictures": frozenset(
        {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
        | {".svg", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}
    ),
    "videos": frozenset({".mp4", ".mov", ".mkv", ".avi", ".wmv", ".m4v", ".webm", ".mpg", ".mpeg"}),
    "music": frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus"}),
    "documents": frozenset(
        {".pdf", ".doc", ".docx", ".odt", ".txt", ".rtf", ".xls", ".xlsx", ".ods", ".csv"}
        | {".ppt", ".pptx", ".odp", ".md"}
    ),
    "archives": frozenset({".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".cab"}),
    "installers": frozenset({".exe", ".msi", ".msix", ".appx", ".dmg", ".pkg"}),
    "disk_images": frozenset({".iso", ".img", ".vhd", ".vhdx", ".vmdk", ".wim"}),
}


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
            if unit == "bytes":
                return f"{int(value)} bytes"
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def parse_size_note(note: str) -> tuple[str, int, Path] | None:
    """Read a ``folder-size:`` or ``root-size:`` plan note as ``(kind, bytes, path)``.

    ``kind`` is ``"folder-size"`` or ``"root-size"``. Any other note gives ``None``.
    """
    match = _SIZE_NOTE.fullmatch(note)
    if match is None:
        return None
    return match.group(1), int(match.group(2)), Path(match.group(3))


def _normalise_extensions(values: list[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        ext = raw.strip().lower()
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        if len(ext) < 2 or any(ch in ext[1:] for ch in '.\\/:*?"<>| \t'):
            raise ValueError(f"{raw!r} is not a file extension such as .mp4")
        if ext not in result:
            result.append(ext)
    return result


class DiskUsageOptions(ToolOptions):
    # ----- where -----------------------------------------------------------------------
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to measure",
        description=(
            "The folders to look in. A drive root or a protected folder (Windows, Program Files, "
            "your profile folder) is refused. Nothing is changed by the scan."
        ),
        json_schema_extra=_hint("Folders", widget="folders"),
    )
    recursive: bool = Field(
        default=True,
        title="Include sub-folders",
        description=(
            "Look inside the sub-folders too. When off, only files directly in each chosen folder "
            "are listed and no folder totals are made. Default: on."
        ),
        json_schema_extra=_hint("Folders"),
    )
    # ----- how many ---------------------------------------------------------------------
    top_files: int = Field(
        default=DEFAULT_TOP_FILES,
        ge=1,
        le=10000,
        title="Largest files to list",
        description=(
            "How many of the largest files are offered for removal (all unchecked). Default: 50."
        ),
        json_schema_extra=_hint("Results"),
    )
    top_folders: int = Field(
        default=DEFAULT_TOP_FOLDERS,
        ge=0,
        le=10000,
        title="Largest folders to report",
        description=(
            "How many folders get a size line in the plan notes. A folder's size includes "
            "everything inside it, so a parent is larger than its children. 0 turns the folder "
            "report off. Default: 25."
        ),
        json_schema_extra=_hint("Results"),
    )
    # ----- filters ----------------------------------------------------------------------
    min_size_mb: float = Field(
        default=0.0,
        ge=0.0,
        le=10_000_000.0,
        title="Minimum file size (MB)",
        description=(
            "Ignore files smaller than this many megabytes (1 MB is 1,048,576 bytes). They are "
            "left out of the file list and of every folder total. 0 keeps every file that is not "
            "empty. Default: 0."
        ),
        json_schema_extra=_hint("Filters"),
    )
    category: TypeCategory = Field(
        default="any",
        title="File kind",
        description=(
            "Only count files of one kind: pictures, videos, music, documents, archives, "
            "installers or disk images. 'any' counts every kind. File types listed below are "
            "counted as well. Default: any."
        ),
        json_schema_extra=_hint("Filters"),
    )
    file_types: list[str] = Field(
        default_factory=list,
        title="File types",
        description=(
            "Only count files with these extensions, each with a leading dot (for example .mp4). "
            "Added to the file kind above. Empty means no extension filter. Default: empty."
        ),
        json_schema_extra=_hint("Filters", widget="ext_list"),
    )
    older_than_days: int = Field(
        default=0,
        ge=0,
        le=36500,
        title="Not changed for (days)",
        description=(
            "Only count files last modified at least this many days ago. 0 counts files of any "
            "age. Default: 0."
        ),
        json_schema_extra=_hint("Filters"),
    )
    newer_than_days: int = Field(
        default=0,
        ge=0,
        le=36500,
        title="Changed within (days)",
        description=(
            "Only count files modified within this many days. 0 counts files of any age. Must be "
            "larger than 'Not changed for' when both are set. Default: 0."
        ),
        json_schema_extra=_hint("Filters", advanced=True),
    )
    exclude_globs: list[str] = Field(
        default_factory=list,
        title="Never count",
        description=(
            "Names or relative paths (patterns allowed, for example node_modules or *.vhdx) that "
            "are skipped, with everything inside a skipped folder. Matched without regard to "
            "letter case. Default: none."
        ),
        json_schema_extra=_hint("Filters", advanced=True, widget="glob_list"),
    )
    include_hidden: bool = Field(
        default=False,
        title="Include hidden and system files",
        description=(
            "Also count files and folders that Windows marks hidden or system (such as "
            "pagefile.sys). Off by default, because these are rarely safe to remove."
        ),
        json_schema_extra=_hint("Filters", advanced=True),
    )

    @field_validator("file_types")
    @classmethod
    def _check_types(cls, value: list[str]) -> list[str]:
        return _normalise_extensions(value)

    @model_validator(mode="after")
    def _check_age_range(self) -> DiskUsageOptions:
        if self.older_than_days and 0 < self.newer_than_days <= self.older_than_days:
            raise ValueError(
                "'Changed within' must be more days than 'Not changed for', "
                "or no file can match both."
            )
        return self


@dataclass
class _Folder:
    path: Path
    parent: int
    total: int = 0


@dataclass
class _Filter:
    """The file filters, resolved once per scan."""

    min_bytes: int
    exts: frozenset[str] | None
    oldest_mtime: float | None  # keep files modified at or before this
    newest_mtime: float | None  # keep files modified at or after this
    excludes: tuple[str, ...]
    include_hidden: bool

    def excluded(self, name: str, rel_posix: str) -> bool:
        return any(
            fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(rel_posix, glob)
            for glob in self.excludes
        )

    def accepts(self, suffix: str, size: int, mtime: float) -> bool:
        if size <= 0 or size < self.min_bytes:
            return False
        if self.exts is not None and suffix not in self.exts:
            return False
        if self.oldest_mtime is not None and mtime > self.oldest_mtime:
            return False
        return self.newest_mtime is None or mtime >= self.newest_mtime


@dataclass
class _Totals:
    files: int = 0
    bytes: int = 0
    folders: int = 0
    hidden: int = 0
    cloud_only: int = 0
    cloud_only_bytes: int = 0
    unreadable: int = 0


@dataclass
class _Scan:
    ctx: RunContext
    top_files: int
    flt: _Filter
    recursive: bool
    notes: list[str]
    totals: _Totals = field(default_factory=_Totals)
    # Min-heap of (size, -order, mtime, path): the smallest file is dropped first, and among equal
    # sizes the one found later. ``order`` makes the result the same on every run.
    heap: list[tuple[int, int, float, Path]] = field(
        default_factory=list[tuple[int, int, float, Path]]
    )
    order: int = 0
    folder_sizes: list[tuple[int, Path]] = field(default_factory=list[tuple[int, Path]])
    root_sizes: list[tuple[int, Path]] = field(default_factory=list[tuple[int, Path]])

    def keep_file(self, size: int, mtime: float, path: Path) -> None:
        self.order += 1
        item = (size, -self.order, mtime, path)
        if len(self.heap) < self.top_files:
            heapq.heappush(self.heap, item)
        elif item > self.heap[0]:
            heapq.heapreplace(self.heap, item)

    def scan_root(self, root: Path) -> None:
        folders: list[_Folder] = []
        stack: list[tuple[Path, int]] = [(root, -1)]
        while stack:
            self.ctx.check_cancelled()
            folder, parent = stack.pop()
            index = len(folders)
            folders.append(_Folder(folder, parent))
            try:
                with os.scandir(long_path(folder)) as iterator:
                    entries = sorted(iterator, key=lambda e: e.name.casefold())
            except OSError as exc:
                self.totals.unreadable += 1
                self.notes.append(f"Could not read {folder}; skipped ({exc.strerror or exc})")
                continue
            children: list[tuple[Path, int]] = []
            for entry in entries:
                path = folder / entry.name
                rel_posix = path.relative_to(root).as_posix()
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    self.totals.unreadable += 1
                    continue
                attributes = _attributes(path, st)
                if entry.is_symlink() or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    continue  # rule 4: never followed, and never counted
                if self.flt.excluded(entry.name, rel_posix):
                    continue
                if not self.flt.include_hidden and attributes & (
                    FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
                ):
                    self.totals.hidden += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if self.recursive:
                        children.append((path, index))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not self.flt.accepts(path.suffix.lower(), st.st_size, st.st_mtime):
                    continue
                if not is_on_device(status_from_attributes(attributes)):
                    self.totals.cloud_only += 1
                    self.totals.cloud_only_bytes += st.st_size
                    continue
                self.totals.files += 1
                self.totals.bytes += st.st_size
                folders[index].total += st.st_size
                self.keep_file(st.st_size, st.st_mtime, path)
            stack.extend(reversed(children))

        # Children were found after their parents, so going backwards adds each total upwards.
        for index in range(len(folders) - 1, 0, -1):
            folder_record = folders[index]
            folders[folder_record.parent].total += folder_record.total
        self.totals.folders += len(folders) - 1
        self.root_sizes.append((folders[0].total, root))
        self.folder_sizes.extend((f.total, f.path) for f in folders[1:] if f.total > 0)


def _attributes(path: Path, st: os.stat_result) -> int:
    """Windows attributes from a stat result already in hand. Opens nothing."""
    return getattr(st, "st_file_attributes", 0)


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _usable_roots(options: DiskUsageOptions, ctx: RunContext, notes: list[str]) -> list[Path]:
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


def _extensions(options: DiskUsageOptions) -> frozenset[str] | None:
    exts: set[str] = set(options.file_types)
    if options.category != "any":
        exts |= CATEGORY_EXTENSIONS[options.category]
    return frozenset(exts) if exts else None


def _kind_of(suffix: str) -> str:
    for name, exts in CATEGORY_EXTENSIONS.items():
        if suffix in exts:
            return CATEGORY_LABELS[name]
    return "other"


def _age_text(now: float, mtime: float) -> str:
    days = max(0, int((now - mtime) // SECONDS_PER_DAY))
    if days == 0:
        return "modified today"
    return f"modified {days} day{'' if days == 1 else 's'} ago"


def _describe_filters(options: DiskUsageOptions) -> str:
    parts: list[str] = []
    if options.min_size_mb:
        parts.append(f"at least {options.min_size_mb:g} MB")
    kinds = [options.category] if options.category != "any" else []
    kinds.extend(options.file_types)
    if kinds:
        parts.append("of type " + ", ".join(kinds))
    if options.older_than_days:
        parts.append(f"not changed for {options.older_than_days} day(s)")
    if options.newer_than_days:
        parts.append(f"changed within {options.newer_than_days} day(s)")
    return "; ".join(parts)


class DiskUsage:
    id = TOOL_ID
    name = "Disk Usage"
    description = (
        "Find the largest files and folders under the chosen folders, filtered by size, type "
        "and age, and send the files you pick to the Recycle Bin."
    )
    category: Category = "Clean up"
    Options = DiskUsageOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, DiskUsageOptions):
            raise TypeError("DiskUsage.scan needs DiskUsageOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        roots = _usable_roots(opts, ctx, plan.notes)
        plan.roots = roots
        if not roots:
            plan.notes.append("No folders to scan.")
            return plan

        now = time.time()
        flt = _Filter(
            min_bytes=int(opts.min_size_mb * BYTES_PER_MB),
            exts=_extensions(opts),
            oldest_mtime=(
                now - opts.older_than_days * SECONDS_PER_DAY if opts.older_than_days else None
            ),
            newest_mtime=(
                now - opts.newer_than_days * SECONDS_PER_DAY if opts.newer_than_days else None
            ),
            excludes=tuple(opts.exclude_globs),
            include_hidden=opts.include_hidden,
        )
        notes: list[str] = []
        scan = _Scan(
            ctx=ctx, top_files=opts.top_files, flt=flt, recursive=opts.recursive, notes=notes
        )
        for index, root in enumerate(roots, start=1):
            ctx.report(index - 1, len(roots), f"Measuring {root}")
            scan.scan_root(root)
        ctx.report(len(roots), len(roots), "Scan finished")

        largest = sorted(scan.heap, key=lambda item: (-item[0], -item[1]))
        for size, _order, mtime, path in largest:
            kind = _kind_of(path.suffix.lower())
            reason = f"{format_size(size)}, {kind} ({path.suffix.lower() or 'no extension'})"
            plan.actions.append(
                PlannedAction(
                    kind=ActionKind.TRASH,
                    src=path,
                    reason=f"{reason}, {_age_text(now, mtime)}",
                    selected=False,
                )
            )

        totals = scan.totals
        summary = (
            f"Disk usage: {totals.files} file(s), {format_size(totals.bytes)} counted in "
            f"{totals.folders} folder(s) under {len(roots)} folder(s). "
            f"Listing the {len(plan.actions)} largest file(s), none checked."
        )
        filters = _describe_filters(opts)
        if filters:
            summary += f" Counting only files {filters}."
        plan.notes.insert(0, summary)
        for size, root in sorted(scan.root_sizes, key=lambda item: (-item[0], _key(item[1]))):
            plan.notes.append(f"{ROOT_SIZE_PREFIX}{size} {root}")
        if opts.top_folders and opts.recursive:
            biggest = sorted(scan.folder_sizes, key=lambda item: (-item[0], _key(item[1])))
            for size, folder in biggest[: opts.top_folders]:
                plan.notes.append(f"{FOLDER_SIZE_PREFIX}{size} {folder}")
        if totals.cloud_only:
            plan.notes.append(
                f"skipped: cloud-only: {totals.cloud_only} file(s) "
                f"({format_size(totals.cloud_only_bytes)}) not counted; "
                "they take no space on this device and are never read"
            )
        if totals.hidden:
            plan.notes.append(
                f"{totals.hidden} hidden or system item(s) left out "
                "(turn on 'Include hidden and system files' to count them)"
            )
        plan.notes.extend(notes)
        ctx.log.info(
            "disk_usage scan: %d file(s), %d byte(s), %d folder(s)",
            totals.files,
            totals.bytes,
            totals.folders,
        )
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        return apply_plan(plan, ctx)


TOOL = DiskUsage()
