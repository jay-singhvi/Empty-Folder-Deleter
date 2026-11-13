"""Organize photos and videos by date (plan §3.6, the legacy metadata-8 script).

Each file moves to ``<dest>/<year>/<year> - <month>/`` by its capture date, or to the unknown-date
folder when no usable date exists. ``scan()`` decides every destination and writes nothing to the
media (the catalog is the one cache it writes). ``apply()`` runs the moves through the generic
engine, so each move is journaled and can be undone.

Rules that always hold (legacy docstring, rules 1-4, 🔒):
1. the date is the capture date when one is available (EXIF for photos, the container's creation
   time for videos). Only then does the file's modified time count, and only when it is plausible;
2. a file with no usable date and no plausible modified time goes to the unknown-date folder;
3. a cloud-only OneDrive file is never read, so its date is unknown, and it is never moved. It is
   reported as skipped. Nothing is downloaded;
4. only files already on this device are moved. Junctions are not followed (AGENTS.md rule 4).

Also: a file already in its place is not moved, the selected folders are never changed, protected
folders are refused (rule 6), and every move is journaled (rule 5).
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Literal

import platformdirs
from pydantic import Field, field_validator

from various_tools.core.context import RunContext
from various_tools.core.deps import install_hint
from various_tools.core.fs import (
    FileInfo,
    destination_refusal,
    long_path,
    protected_reason,
    root_refusal,
    walk,
)
from various_tools.core.media.exif import ExifInfo
from various_tools.core.onedrive import (
    CloudOnlyFileError,
    SyncStatus,
    get_sync_status,
    is_on_device,
)
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.core.settings import APP_NAME
from various_tools.tools.base import ToolOptions
from various_tools.tools.organize_by_date import dates
from various_tools.tools.organize_by_date.catalog import (
    DONE_STATUSES,
    Catalog,
    Known,
    now_text,
    path_key,
)
from various_tools.tools.organize_by_date.dates import DEFAULT_PRIORITY, DateSource

TOOL_ID = "organize_by_date"
CATALOG_FILE = "catalog.sqlite"
DEFAULT_TEMPLATE = "{year}/{year} - {month:02d}"
DEFAULT_UNKNOWN_FOLDER = "Unknown Date"
ALLOWED_TEMPLATE_FIELDS = ("year", "month")
_BAD_NAME_CHARS = '\\/:*?"<>|'


def default_catalog_path() -> Path:
    """The catalog lives in the user's data folder, not in any media folder."""
    return Path(platformdirs.user_data_dir(APP_NAME)) / CATALOG_FILE


class OrganizeByDateOptions(ToolOptions):
    sources: list[Path] = Field(
        default_factory=list,
        json_schema_extra={"widget": "folders", "group": "Files"},
    )
    recursive: bool = Field(
        default=False,
        json_schema_extra={"group": "Files"},
    )
    dest_root: Path | None = Field(
        default=None,
        json_schema_extra={"widget": "folder", "group": "Output"},
    )
    folder_template: str = Field(
        default=DEFAULT_TEMPLATE,
        json_schema_extra={"widget": "template", "group": "Output"},
    )
    unknown_folder: str = Field(
        default=DEFAULT_UNKNOWN_FOLDER,
        json_schema_extra={"group": "Output"},
    )
    date_priority: list[DateSource] = Field(
        default_factory=lambda: list(DEFAULT_PRIORITY),
        min_length=1,
        json_schema_extra={"group": "Dates", "advanced": True},
    )
    min_plausible_year: int = Field(
        default=1990,
        ge=1900,
        le=9999,
        json_schema_extra={"group": "Dates", "advanced": True},
    )
    workers: int = Field(
        default=1,
        ge=1,
        le=16,
        json_schema_extra={"group": "Speed", "advanced": True},
    )
    skip_processed: bool = Field(
        default=False,
        json_schema_extra={"group": "Resume"},
    )
    errors_only: bool = Field(
        default=False,
        json_schema_extra={"group": "Resume"},
    )
    catalog_path: Path | None = Field(
        default=None,
        json_schema_extra={"widget": "file", "group": "Advanced", "advanced": True},
    )

    @field_validator("date_priority")
    @classmethod
    def _each_source_once(cls, value: list[DateSource]) -> list[DateSource]:
        if len(set(value)) != len(value):
            raise ValueError("each date source may appear only once")
        return value


@dataclass(frozen=True)
class _Config:
    priority: tuple[str, ...]
    workers: int
    min_year: int
    now: datetime
    ffprobe: Path | None
    exiftool: Path | None


@dataclass(frozen=True)
class _Inspection:
    """What reading one file produced. ``state`` is ``ok``, ``cloud_only`` or ``error``."""

    info: FileInfo
    sync: str
    state: Literal["ok", "cloud_only", "error"]
    reading: dates.Reading | None = None
    error: str | None = None


@dataclass
class _Totals:
    to_move: int = 0
    organized: int = 0
    cloud_only: int = 0
    errors: int = 0
    unknown: int = 0
    by_source: Counter[str] = field(default_factory=Counter)


def _is_plain_name(name: str) -> bool:
    text = name.strip()
    return bool(text) and text not in (".", "..") and not any(c in text for c in _BAD_NAME_CHARS)


def _template_problem(template: str) -> str | None:
    """Why the folder template cannot be used, or None. Only {year} and {month} are allowed."""
    try:
        rendered = template.format(year=2000, month=1)
    except (KeyError, IndexError, ValueError) as exc:
        return f"The folder template must use only {{year}} and {{month}} ({exc})."
    relative = PureWindowsPath(rendered)
    if not rendered.strip() or relative.anchor or ".." in relative.parts:
        return "The folder template must name a folder inside the destination."
    return None


def _options_problem(opts: OrganizeByDateOptions) -> str | None:
    if not opts.sources:
        return "Choose at least one folder to organize."
    template = _template_problem(opts.folder_template)
    if template is not None:
        return template
    if not _is_plain_name(opts.unknown_folder):
        return "The unknown-date folder name must be a plain folder name."
    return None


def folder_for(dest_root: Path, template: str, unknown_folder: str, when: datetime | None) -> Path:
    """The folder a file with this date goes to. A file with no date goes to the unknown folder."""
    if when is None:
        return dest_root / unknown_folder
    return dest_root / Path(template.format(year=when.year, month=when.month))


def _dest_root(opts: OrganizeByDateOptions, ctx: RunContext) -> Path:
    if opts.dest_root is not None:
        return Path(os.path.abspath(opts.dest_root))
    return Path(os.path.abspath(ctx.settings.onedrive_root / "Pictures" / "Camera Roll"))


def _dest_refusal(dest_root: Path, ctx: RunContext) -> str | None:
    if dest_root.exists() and not dest_root.is_dir():
        return f"The destination is not a folder: {dest_root}"
    # AGENTS.md rule 4: never write through a junction or symlink. Only the folder itself is
    # checked here; the apply engine refuses a destination that passes through one deeper down.
    link = destination_refusal(dest_root, dest_root)
    if link is not None:
        return f"The destination is a junction or symlink, not followed: {dest_root}"
    reason = protected_reason(dest_root)
    if reason and not ctx.settings.allow_protected_paths:
        return f"The destination is a protected folder ({reason}): {dest_root}"
    return None


def _candidates(roots: Iterable[Path], recursive: bool) -> list[FileInfo]:
    """Media files to consider, once each even when folders overlap. Dot-files are skipped."""
    seen: set[str] = set()
    found: list[FileInfo] = []
    for root in roots:
        for info in walk(root, exts=dates.PROCESSABLE_EXTS, recursive=recursive):
            if info.path.name.startswith("."):
                continue
            key = path_key(info.path)
            if key in seen:
                continue
            seen.add(key)
            found.append(info)
    found.sort(key=lambda info: path_key(info.path))
    return found


def _inspect(info: FileInfo, cfg: _Config) -> _Inspection:
    """Read one file's date. Never raises: a failure is returned as an ``error`` inspection."""
    path = info.path
    sync = SyncStatus.UNKNOWN
    try:
        sync = get_sync_status(path)
        if not is_on_device(sync):
            return _Inspection(info, sync.value, "cloud_only")
        reading = dates.read_capture_date(
            path,
            modified=datetime.fromtimestamp(info.mtime),
            priority=cfg.priority,
            min_year=cfg.min_year,
            now=cfg.now,
            ffprobe=cfg.ffprobe,
            exiftool=cfg.exiftool,
        )
    except CloudOnlyFileError:
        return _Inspection(info, SyncStatus.CLOUD_ONLY.value, "cloud_only")
    except Exception as exc:  # one unreadable file must not stop the run; it is reported
        return _Inspection(info, sync.value, "error", error=f"{type(exc).__name__}: {exc}")
    return _Inspection(info, sync.value, "ok", reading=reading)


def _relative_label(path: Path, dest_root: Path) -> str:
    try:
        return str(path.relative_to(dest_root))
    except ValueError:
        return str(path)


class _Planner:
    """Turns the inspections into actions and catalog rows. Runs on one thread, in path order."""

    def __init__(
        self, plan: Plan, opts: OrganizeByDateOptions, dest_root: Path, stamp: str
    ) -> None:
        self.plan = plan
        self.opts = opts
        self.dest_root = dest_root
        self.stamp = stamp
        self.claimed: set[str] = set()
        self.actions: list[PlannedAction] = []
        self.rows: list[dict[str, object]] = []
        self.totals = _Totals()

    def add(self, insp: _Inspection) -> None:
        path = insp.info.path
        row: dict[str, object] = {
            "key": path_key(path),
            "path": str(path),
            "original_path": str(path),
            "file_name": path.name,
            "size": insp.info.size,
            "mtime": insp.info.mtime,
            "sync_status": insp.sync,
            "checked": self.stamp,
        }
        if insp.state == "cloud_only":
            self._cloud_only(insp, row)
        elif insp.state == "error" or insp.reading is None:
            self._error(insp, row)
        else:
            self._dated(insp, insp.reading, row)
        self.rows.append(row)

    def _cloud_only(self, insp: _Inspection, row: dict[str, object]) -> None:
        path = insp.info.path
        self.totals.cloud_only += 1
        self.actions.append(
            PlannedAction(
                kind=ActionKind.MOVE,
                src=path,
                dst=None,
                reason="skipped: cloud-only (not on device). Not read, so its date is unknown.",
                selected=False,
            )
        )
        row.update(status="skipped", last_action="skipped: cloud-only")

    def _error(self, insp: _Inspection, row: dict[str, object]) -> None:
        path = insp.info.path
        self.totals.errors += 1
        detail = insp.error or "no date reading"
        self.plan.notes.append(f"error: could not read {path.name}: {detail}")
        row.update(status="error", last_action="scan failed", error=detail)

    def _dated(self, insp: _Inspection, reading: dates.Reading, row: dict[str, object]) -> None:
        path = insp.info.path
        folder = folder_for(
            self.dest_root, self.opts.folder_template, self.opts.unknown_folder, reading.date
        )
        exif: ExifInfo | None = reading.exif
        row.update(
            capture_time=reading.date.isoformat(timespec="seconds") if reading.date else None,
            date_source=reading.label,
            camera_make=exif.camera_make if exif else None,
            camera_model=exif.camera_model if exif else None,
            width=exif.width if exif else None,
            height=exif.height if exif else None,
            gps_lat=exif.gps[0] if exif and exif.gps else None,
            gps_lon=exif.gps[1] if exif and exif.gps else None,
        )
        if reading.date is None:
            self.totals.unknown += 1
        self.totals.by_source[reading.label] += 1

        if path_key(folder / path.name) == path_key(path):
            self.totals.organized += 1
            row.update(status="organized", last_action="already in place", destination=str(path))
            return

        dst = self._claim(folder, path.name)
        when = reading.date.strftime("%Y-%m-%d %H:%M:%S") if reading.date else "no usable date"
        self.actions.append(
            PlannedAction(
                kind=ActionKind.MOVE,
                src=path,
                dst=dst,
                reason=f"{reading.label}: {when} -> {_relative_label(dst.parent, self.dest_root)}",
            )
        )
        self.totals.to_move += 1
        row.update(status="planned", last_action="planned move", destination=str(dst))

    def _claim(self, folder: Path, name: str) -> Path:
        """A free destination: the first name that is neither on disk nor claimed by this batch."""
        stem, suffix = Path(name).stem, Path(name).suffix
        candidate = folder / name
        counter = 0
        while path_key(candidate) in self.claimed or os.path.lexists(long_path(candidate)):
            counter += 1
            candidate = folder / f"{stem}_dup{counter}{suffix}"
        self.claimed.add(path_key(candidate))
        return candidate


def _summary(opts: OrganizeByDateOptions, totals: _Totals, skipped_processed: int) -> str:
    parts = [
        f"Organize by date: {totals.to_move} file(s) to move"
        f" ({totals.unknown} with no usable date go to '{opts.unknown_folder}')",
        f"{totals.organized} already in place",
        f"{totals.cloud_only} cloud-only, skipped and not read",
        f"{totals.errors} could not be read",
    ]
    if skipped_processed:
        parts.append(f"{skipped_processed} skipped as already processed")
    text = ", ".join(parts) + "."
    if totals.by_source:
        sources = "; ".join(f"{label}: {count}" for label, count in totals.by_source.most_common())
        text += f" Dates came from: {sources}."
    return text


class OrganizeByDate:
    id = TOOL_ID
    name = "Organize photos and videos by date"
    description = (
        "Move photos and videos into year and month folders, by capture date (EXIF or the video's "
        "creation time), or by modified date when no capture date exists. Cloud-only files are "
        "skipped and never read."
    )
    category = "Organize"
    Options = OrganizeByDateOptions

    def scan(self, opts: OrganizeByDateOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, OrganizeByDateOptions):
            raise TypeError("OrganizeByDate.scan needs OrganizeByDateOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        problem = _options_problem(opts)
        if problem is not None:
            plan.notes.append(problem)
            return plan

        dest_root = _dest_root(opts, ctx)
        refusal = _dest_refusal(dest_root, ctx)
        if refusal is not None:
            plan.notes.append(refusal)
            return plan

        roots: list[Path] = []
        for raw in opts.sources:
            root = Path(os.path.abspath(raw))
            problem = root_refusal(root, ctx.settings.allow_protected_paths)
            if problem is None:
                roots.append(root)
            else:
                plan.notes.append(problem)
        if not roots:
            plan.notes.append("Nothing to organize.")
            return plan
        plan.roots = roots

        ffprobe = ctx.deps.ffprobe()
        exiftool = ctx.deps.exiftool()
        if "video_tag" in opts.date_priority and ffprobe is None:
            plan.notes.append(
                "Videos use their file date: ffprobe was not found, so their embedded creation "
                f"time is not read. Install it with `{install_hint('ffmpeg')}`, or set its path "
                "in Settings."
            )
        if "exiftool" in opts.date_priority and exiftool is None:
            plan.notes.append(
                "RAW files use their file date: ExifTool was not found. Set its path in Settings."
            )
        cfg = _Config(
            priority=tuple(opts.date_priority),
            workers=opts.workers,
            min_year=opts.min_plausible_year,
            now=datetime.now(),
            ffprobe=ffprobe,
            exiftool=exiftool,
        )

        catalog_path = opts.catalog_path or default_catalog_path()
        with Catalog(catalog_path) as catalog:
            known = catalog.snapshot()
            selected, skipped_processed = _select(_candidates(roots, opts.recursive), known, opts)
            if opts.errors_only and not selected:
                plan.notes.append("No errored files to process. The catalog has none.")
                return plan
            inspections = self._inspect_all(selected, cfg, ctx)
            planner = _Planner(plan, opts, dest_root, now_text())
            for insp in inspections:
                planner.add(insp)
            plan.actions = planner.actions
            catalog.write_scan(planner.rows)

        plan.notes.insert(0, _summary(opts, planner.totals, skipped_processed))
        ctx.log.info(
            "organize_by_date scan: %d candidate(s), %d to move",
            len(selected),
            planner.totals.to_move,
        )
        return plan

    def _inspect_all(
        self, files: list[FileInfo], cfg: _Config, ctx: RunContext
    ) -> list[_Inspection]:
        results: list[_Inspection] = []
        total = len(files)
        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            futures: list[Future[_Inspection]] = [
                pool.submit(_inspect, info, cfg) for info in files
            ]
            try:
                pairs = zip(files, futures, strict=True)
                for index, (info, future) in enumerate(pairs, start=1):
                    ctx.check_cancelled()
                    ctx.report(index - 1, total, f"Reading {info.path.name}")
                    results.append(future.result())
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        ctx.report(total, total, "Read")
        return results

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        opts = OrganizeByDateOptions.model_validate_json(plan.options_json)
        # AGENTS.md rule 3: a file that became cloud-only after the scan is not moved, because a
        # move across volumes would read it.
        for action in plan.actions:
            if (
                action.selected
                and action.src is not None
                and not is_on_device(get_sync_status(action.src))
            ):
                action.selected = False
                plan.notes.append(f"skipped: became cloud-only after the scan: {action.src}")

        result = apply_plan(plan, ctx)

        catalog_path = opts.catalog_path or default_catalog_path()
        with Catalog(catalog_path) as catalog:
            for outcome in result.outcomes:
                if outcome.src is None:
                    continue
                src = Path(outcome.src)
                if outcome.status == "done" and outcome.dst is not None:
                    catalog.record_move(src, Path(outcome.dst))
                elif outcome.status == "failed":
                    catalog.record_failure(src, outcome.detail)
        return result


def _select(
    files: list[FileInfo], known: dict[str, Known], opts: OrganizeByDateOptions
) -> tuple[list[FileInfo], int]:
    """Apply the resume and retry filters before any file is opened. Returns (selected, skipped)."""
    selected: list[FileInfo] = []
    skipped = 0
    for info in files:
        row = known.get(path_key(info.path))
        if (
            opts.skip_processed
            and row is not None
            and row.status in DONE_STATUSES
            and row.size == info.size
            and row.mtime == info.mtime
        ):
            skipped += 1
            continue
        if opts.errors_only and (row is None or row.status != "error"):
            continue
        selected.append(info)
    return selected, skipped


TOOL = OrganizeByDate()
