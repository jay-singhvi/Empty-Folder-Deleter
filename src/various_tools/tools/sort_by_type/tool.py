"""Sort by type (plan §4 Phase 7): move files into sub-folders chosen by simple name rules.

A rule is one line, ``<pattern>[|<pattern>...]=<folder>``, for example ``*.pdf=Documents/PDF`` or
``*.jpg|*.png=Pictures``. Patterns match the file *name* (not the path) without regard to letter
case; the first rule that matches wins. The folder is a relative path inside the folder being
sorted: never absolute, never with ``..``, and made only of characters Windows allows.

``scan()`` reads names and attributes only. It never opens a file and never changes the disk.
Every file is a ``move`` action, so the journal records it and History can undo it.

Rules that always hold:
* destinations are inside the selected folder, and a folder reached through a junction or symlink
  is never written to (AGENTS.md rule 4). The selected folder itself is never moved;
* an existing file is never replaced. A name that is taken (on disk, or by another file in the
  same plan) gets ``_dup1``, ``_dup2`` ... or the file is left alone, as the option says;
* with sub-folders on, the destination folders are never entered, so a sorted file is not sorted
  again and a second run finds nothing to do;
* cloud-only OneDrive files are skipped, not moved. A move on one drive is a rename that reads
  nothing, but the move falls back to copy-and-delete when the rename fails, and that would
  download the file. Skipping is the safer choice (AGENTS.md rule 3).
"""

from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from various_tools.core.context import RunContext
from various_tools.core.fs import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    destination_refusal,
    is_inside,
    long_path,
    root_refusal,
)
from various_tools.core.onedrive import is_on_device, status_from_attributes
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.tools.base import Category, ToolOptions

TOOL_ID = "sort_by_type"
FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
SECONDS_PER_DAY = 86400
MAX_NOTES_PER_KIND = 10

DEFAULT_RULES: tuple[str, ...] = (
    "*.pdf=Documents/PDF",
    "*.doc|*.docx|*.odt|*.txt|*.rtf|*.md=Documents",
    "*.xls|*.xlsx|*.ods|*.csv=Spreadsheets",
    "*.ppt|*.pptx|*.odp|*.key=Presentations",
    "*.cr2|*.cr3|*.nef|*.arw|*.dng|*.raf|*.orf|*.rw2=Pictures/RAW",
    "*.jpg|*.jpeg|*.png|*.gif|*.webp|*.bmp|*.heic|*.heif|*.tif|*.tiff|*.svg=Pictures",
    "*.mp4|*.mov|*.mkv|*.avi|*.wmv|*.m4v|*.webm|*.mpg|*.mpeg=Videos",
    "*.mp3|*.wav|*.flac|*.m4a|*.aac|*.ogg|*.wma|*.opus=Music",
    "*.zip|*.7z|*.rar|*.tar|*.gz|*.bz2|*.xz=Archives",
    "*.exe|*.msi|*.msix|*.appx=Installers",
    "*.iso|*.img=Disk images",
)
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    ".git",
    "node_modules",
    "*.crdownload",
    "*.part",
    "*.partial",
    "*.tmp",
)
DEFAULT_CLASH_SUFFIX = "_dup"

_BAD_FOLDER_CHARS = '<>:"|?*'
_FORBIDDEN_NAME_CHARS = _BAD_FOLDER_CHARS + "\\/"
_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


def _hint(group: str, *, advanced: bool = False, **extra: Any) -> Any:
    """The ``json_schema_extra`` hints the GUI and CLI read (see ``core/settings.py``)."""
    hints: dict[str, Any] = {"group": group}
    if advanced:
        hints["advanced"] = True
    hints.update(extra)
    return hints


# ----- rules -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One parsed rule line. ``folder`` holds the destination as relative path parts."""

    text: str
    patterns: tuple[str, ...]
    folder: tuple[str, ...]

    @property
    def folder_text(self) -> str:
        return "/".join(self.folder)

    def matches(self, name: str) -> str | None:
        """The pattern that matches ``name`` (ignoring letter case), or ``None``."""
        lowered = name.casefold()
        for pattern in self.patterns:
            if fnmatch.fnmatchcase(lowered, pattern.casefold()):
                return pattern
        return None


def parse_folder(text: str) -> tuple[str, ...]:
    """A destination folder as path parts. Raises ``ValueError`` when it is not a safe, relative,
    Windows-legal folder path."""
    cleaned = text.strip().replace("\\", "/").strip("/")
    if not cleaned or text.strip().startswith(("/", "\\")):
        raise ValueError("the folder must be a relative path such as Documents/PDF")
    parts = tuple(part.strip() for part in cleaned.split("/"))
    for part in parts:
        if not part:
            raise ValueError(f"{text!r} has an empty folder name in it")
        if part in (".", ".."):
            raise ValueError(f"{text!r}: '.' and '..' are not allowed in a folder path")
        if any(ch in _BAD_FOLDER_CHARS or ord(ch) < 32 for ch in part):
            raise ValueError(
                f'{text!r}: a folder name cannot contain any of < > : " | ? * or a drive letter'
            )
        if part.endswith((" ", ".")) or len(part) > 255:
            raise ValueError(f"{text!r}: {part!r} is not a name Windows allows")
        if part.split(".")[0].casefold() in _RESERVED_NAMES:
            raise ValueError(f"{text!r}: {part!r} is a reserved Windows name")
    return parts


def parse_rule(text: str) -> Rule:
    """Parse ``*.pdf=Documents/PDF``. Raises ``ValueError`` with a plain-English reason."""
    pattern_text, sep, folder_text = text.partition("=")
    if not sep:
        raise ValueError(f"{text!r} is not PATTERN=FOLDER, for example *.pdf=Documents/PDF")
    patterns = tuple(p.strip() for p in pattern_text.split("|"))
    for pattern in patterns:
        if not pattern:
            raise ValueError(f"{text!r} has an empty pattern")
        if any(ch in pattern for ch in '\\/:<>"'):
            raise ValueError(
                f'{text!r}: patterns match file names, so they cannot contain \\ / : < > or "'
            )
    return Rule(text=text.strip(), patterns=patterns, folder=parse_folder(folder_text))


class SortByTypeOptions(ToolOptions):
    # ----- where -----------------------------------------------------------------------
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to sort",
        description=(
            "The folders whose files are sorted. The sub-folders are created inside each chosen "
            "folder. A drive root or a protected folder is refused. The folder itself is never "
            "moved."
        ),
        json_schema_extra=_hint("Folders", widget="folders"),
    )
    recursive: bool = Field(
        default=False,
        title="Include sub-folders",
        description=(
            "Also sort files that are in the sub-folders of each chosen folder. The destination "
            "folders from the rules are never entered, so files that are already sorted stay "
            "put. Default: off (only files directly in the folder)."
        ),
        json_schema_extra=_hint("Folders"),
    )
    # ----- rules ------------------------------------------------------------------------
    rules: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RULES),
        title="Sorting rules",
        description=(
            "One rule per line, PATTERN=FOLDER, for example *.pdf=Documents/PDF. Use | between "
            "patterns to share a folder: *.jpg|*.png=Pictures. Patterns match the file name "
            "without regard to letter case, and the first matching rule wins, so put specific "
            "rules first. The folder is relative to the chosen folder (no drive, no ..). "
            "Default: documents, spreadsheets, presentations, pictures (RAW apart), videos, "
            "music, archives, installers and disk images."
        ),
        json_schema_extra=_hint("Rules"),
    )
    unmatched: Literal["leave", "other"] = Field(
        default="leave",
        title="Files that match no rule",
        description=(
            "leave keeps them where they are. other moves them into the 'Other' folder named "
            "below. Default: leave."
        ),
        json_schema_extra=_hint("Rules"),
    )
    other_folder: str = Field(
        default="Other",
        min_length=1,
        title="Folder for unmatched files",
        description=(
            "Where files that match no rule go when 'Files that match no rule' is other. A "
            "relative folder path such as Other or Misc/Unsorted. Default: Other."
        ),
        json_schema_extra=_hint("Rules", advanced=True, depends_on="unmatched=other"),
    )
    # ----- skipping ---------------------------------------------------------------------
    exclude_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS),
        title="Never move",
        description=(
            "File or folder names, or relative paths (patterns allowed), that are left alone, "
            "with everything inside a skipped folder. Matched without regard to letter case. "
            "Default: .git, node_modules and unfinished downloads (*.crdownload, *.part, "
            "*.partial, *.tmp)."
        ),
        json_schema_extra=_hint("Skip", advanced=True, widget="glob_list"),
    )
    min_age_days: int = Field(
        default=0,
        ge=0,
        le=36500,
        title="Leave recent files (days)",
        description=(
            "Files changed within this many days are left alone, so a file you are still "
            "working on is not moved. 0 sorts files of any age. Default: 0."
        ),
        json_schema_extra=_hint("Skip", advanced=True),
    )
    skip_hidden_system: bool = Field(
        default=True,
        title="Skip hidden and system files",
        description=(
            "Files and folders that Windows marks hidden or system are left alone. On by "
            "default, because programs create them."
        ),
        json_schema_extra=_hint("Skip", advanced=True),
    )
    skip_dot_files: bool = Field(
        default=True,
        title="Skip files that start with a dot",
        description=(
            "Files and folders whose name starts with '.' (such as .gitignore) are left alone. "
            "Default: on."
        ),
        json_schema_extra=_hint("Skip", advanced=True),
    )
    # ----- name clashes -----------------------------------------------------------------
    clash_handling: Literal["rename", "skip"] = Field(
        default="rename",
        title="When the name is taken",
        description=(
            "What to do when a file with the same name is already in the destination folder, "
            "or another file in this run is going there. rename gives the new file a number "
            "(see 'Name clash suffix'); skip leaves it where it is. Existing files are never "
            "replaced. Default: rename."
        ),
        json_schema_extra=_hint("Name clashes"),
    )
    clash_suffix: str = Field(
        default=DEFAULT_CLASH_SUFFIX,
        min_length=1,
        title="Name clash suffix",
        description=(
            "Text and a number added to a file name that is taken, for example report_dup1.pdf. "
            "Default: _dup."
        ),
        json_schema_extra=_hint("Name clashes", advanced=True, depends_on="clash_handling=rename"),
    )

    @field_validator("rules")
    @classmethod
    def _check_rules(cls, value: list[str]) -> list[str]:
        for index, line in enumerate(value, start=1):
            try:
                parse_rule(line)
            except ValueError as exc:
                raise ValueError(f"rule {index}: {exc}") from None
        return [line.strip() for line in value]

    @field_validator("other_folder")
    @classmethod
    def _check_other(cls, value: str) -> str:
        parse_folder(value)
        return value.strip()

    @field_validator("clash_suffix")
    @classmethod
    def _check_suffix(cls, value: str) -> str:
        if value != value.strip() or any(ch in value for ch in _FORBIDDEN_NAME_CHARS):
            raise ValueError("the suffix must be plain text that Windows allows in a file name")
        return value


# ----- scan ------------------------------------------------------------------------------------


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _attributes(path: Path, st: os.stat_result) -> int:
    """Windows attributes from a stat result already in hand. Opens nothing."""
    return getattr(st, "st_file_attributes", 0)


@dataclass
class _Notes:
    """Notes with a cap per kind, so a thousand clashes do not become a thousand lines."""

    sink: list[str]
    counts: dict[str, int] = field(default_factory=dict[str, int])

    def add(self, kind: str, message: str) -> None:
        count = self.counts.get(kind, 0) + 1
        self.counts[kind] = count
        if count <= MAX_NOTES_PER_KIND:
            self.sink.append(message)

    def finish(self) -> None:
        for kind, count in self.counts.items():
            if count > MAX_NOTES_PER_KIND:
                self.sink.append(f"... and {count - MAX_NOTES_PER_KIND} more {kind}")


@dataclass
class _Totals:
    to_move: int = 0
    renamed: int = 0
    unmatched: int = 0
    clash_skipped: int = 0
    cloud_only: int = 0
    hidden: int = 0
    recent: int = 0
    blocked: int = 0
    unreadable: int = 0
    folders: set[str] = field(default_factory=set[str])


@dataclass
class _Scan:
    ctx: RunContext
    opts: SortByTypeOptions
    rules: list[Rule]
    other: tuple[str, ...] | None
    cutoff: float | None
    plan: Plan
    notes: _Notes
    totals: _Totals = field(default_factory=_Totals)
    claimed: set[str] = field(default_factory=set[str])
    dest_problem: dict[str, str | None] = field(default_factory=dict[str, str | None])

    def destinations(self) -> frozenset[str]:
        """Every destination folder as a lower-case relative path, so it is never entered."""
        folders = {"/".join(rule.folder).casefold() for rule in self.rules}
        if self.other is not None:
            folders.add("/".join(self.other).casefold())
        return frozenset(folders)

    def destination_problem(self, root: Path, folder: Path) -> str | None:
        """Why files cannot go into ``folder``, or ``None``. Reads attributes only."""
        key = _key(folder)
        if key not in self.dest_problem:
            self.dest_problem[key] = _folder_problem(root, folder)
        return self.dest_problem[key]

    def free_name(self, folder: Path, name: str) -> Path | None:
        """A path in ``folder`` that is free on disk and in this plan, or ``None`` to skip."""
        candidate = folder / name
        if not self._taken(candidate):
            return candidate
        if self.opts.clash_handling == "skip":
            return None
        stem, suffix = Path(name).stem, Path(name).suffix
        counter = 0
        while self._taken(candidate):
            counter += 1
            candidate = folder / f"{stem}{self.opts.clash_suffix}{counter}{suffix}"
        return candidate

    def _taken(self, path: Path) -> bool:
        return _key(path) in self.claimed or os.path.lexists(long_path(path))

    def match(self, name: str) -> tuple[str, tuple[str, ...]] | None:
        """``(why, folder parts)`` for the first rule that matches ``name``, else the fallback."""
        for rule in self.rules:
            pattern = rule.matches(name)
            if pattern is not None:
                return f"{pattern} -> {rule.folder_text}", rule.folder
        if self.other is not None:
            return f"no rule matched -> {'/'.join(self.other)}", self.other
        return None

    def scan_root(self, root: Path) -> None:
        opts = self.opts
        dest_folders = self.destinations()
        stack: list[Path] = [root]
        while stack:
            self.ctx.check_cancelled()
            folder = stack.pop()
            try:
                with os.scandir(long_path(folder)) as iterator:
                    entries = sorted(iterator, key=lambda e: e.name.casefold())
            except OSError as exc:
                self.totals.unreadable += 1
                self.notes.add(
                    "unreadable folders",
                    f"Could not read {folder}; skipped ({exc.strerror or exc})",
                )
                continue
            subfolders: list[Path] = []
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
                    continue  # rule 4: never followed, never moved
                if any(
                    fnmatch.fnmatch(entry.name, g) or fnmatch.fnmatch(rel_posix, g)
                    for g in opts.exclude_globs
                ):
                    continue
                if opts.skip_dot_files and entry.name.startswith("."):
                    self.totals.hidden += 1
                    continue
                if opts.skip_hidden_system and attributes & (
                    FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
                ):
                    self.totals.hidden += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    # A destination folder is never entered: what is in it is already sorted.
                    if opts.recursive and rel_posix.casefold() not in dest_folders:
                        subfolders.append(path)
                    continue
                if entry.is_file(follow_symlinks=False):
                    self.plan_file(root, path, st, attributes)
            stack.extend(reversed(subfolders))

    def plan_file(self, root: Path, path: Path, st: os.stat_result, attributes: int) -> None:
        if not is_on_device(status_from_attributes(attributes)):
            self.totals.cloud_only += 1
            self.notes.add("cloud-only files", f"skipped: cloud-only: {path}")
            return
        if self.cutoff is not None and st.st_mtime > self.cutoff:
            self.totals.recent += 1
            return
        found = self.match(path.name)
        if found is None:
            self.totals.unmatched += 1
            return
        why, parts = found
        folder = root.joinpath(*parts)
        if _key(path.parent) == _key(folder):
            return
        problem = self.destination_problem(root, folder)
        if problem is not None:
            self.totals.blocked += 1
            self.notes.add("blocked destinations", f"Left in place, {problem}: {path}")
            return
        target = self.free_name(folder, path.name)
        if target is None:
            self.totals.clash_skipped += 1
            self.notes.add("name clashes", f"Left in place, name is taken in {folder}: {path}")
            return
        renamed = target.name != path.name
        if renamed:
            self.totals.renamed += 1
        self.claimed.add(_key(target))
        self.totals.to_move += 1
        self.totals.folders.add(_key(folder))
        self.plan.actions.append(
            PlannedAction(
                kind=ActionKind.MOVE,
                src=path,
                dst=target,
                reason=why + (f", renamed to {target.name} (name was taken)" if renamed else ""),
                group_id="/".join(parts),
            )
        )


def _folder_problem(root: Path, folder: Path) -> str | None:
    """Why files cannot be moved into ``folder``: a file or a junction is in the way."""
    refusal = destination_refusal(folder, root)
    if refusal is not None:
        return refusal
    current = root
    for part in folder.relative_to(root).parts:
        current = current / part
        if os.path.lexists(long_path(current)) and not os.path.isdir(long_path(current)):
            return f"a file named {part!r} is where the folder would go"
    return None


def _usable_roots(options: SortByTypeOptions, ctx: RunContext, notes: list[str]) -> list[Path]:
    """Folders that may be sorted. Duplicates and folders inside another root drop out."""
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


class SortByType:
    id = TOOL_ID
    name = "Sort by Type"
    description = (
        "Move files into sub-folders by simple name rules, such as *.pdf into Documents/PDF. "
        "Preview first; every move can be undone from History."
    )
    category: Category = "Organize"
    Options = SortByTypeOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, SortByTypeOptions):
            raise TypeError("SortByType.scan needs SortByTypeOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        roots = _usable_roots(opts, ctx, plan.notes)
        plan.roots = roots
        if not roots:
            plan.notes.append("No folders to sort.")
            return plan

        notes: list[str] = []
        scan = _Scan(
            ctx=ctx,
            opts=opts,
            rules=[parse_rule(line) for line in opts.rules],
            other=parse_folder(opts.other_folder) if opts.unmatched == "other" else None,
            cutoff=(
                time.time() - opts.min_age_days * SECONDS_PER_DAY if opts.min_age_days else None
            ),
            plan=plan,
            notes=_Notes(notes),
        )
        for index, root in enumerate(roots, start=1):
            ctx.report(index - 1, len(roots), f"Scanning {root}")
            scan.scan_root(root)
        ctx.report(len(roots), len(roots), "Scan finished")
        scan.notes.finish()

        totals = scan.totals
        parts = [
            f"Sort by type: {totals.to_move} file(s) to move into {len(totals.folders)} folder(s)",
        ]
        if totals.renamed:
            parts.append(f"{totals.renamed} renamed because the name was taken")
        if totals.unmatched:
            parts.append(f"{totals.unmatched} match no rule and stay")
        if totals.clash_skipped:
            parts.append(f"{totals.clash_skipped} left because the name is taken")
        if totals.blocked:
            parts.append(f"{totals.blocked} left because the destination is blocked")
        if totals.cloud_only:
            parts.append(f"{totals.cloud_only} cloud-only, skipped and not read")
        if totals.recent:
            parts.append(f"{totals.recent} changed too recently")
        if totals.hidden:
            parts.append(f"{totals.hidden} hidden or dot item(s) skipped")
        plan.notes.insert(0, "; ".join(parts) + ".")
        plan.notes.extend(notes)
        ctx.log.info("sort_by_type scan: %d move(s)", totals.to_move)
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        # An existing file is never replaced: the engine gives a moved file a numbered name when
        # its destination has been taken since the scan.
        return apply_plan(plan, ctx)


TOOL = SortByType()
