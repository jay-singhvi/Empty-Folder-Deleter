"""Change date: set the taken date of photos and videos (for example a camera with a wrong clock).

Three ways to choose the new date:

* ``exact``: every file gets one date and time;
* ``month_year``: the month and year change, the day and the time of day stay (the day is
  clamped to the length of the new month, so 31 January becomes 28 February);
* ``shift``: every file moves by the same number of days, hours and minutes (can be negative).

``scan()`` reads each file's current date the way the media library does (camera date, video
date, else the modified time) and writes nothing. Every file that would change is a planned
``convert`` action whose reason reads like ``2019-07-04 10:12 → 2021-03-04 10:12``.

``apply()`` uses the in-place pattern of "Strip metadata": a copy with the new date is written
next to the file, the original goes to the Recycle Bin (its own journal row), the copy takes
its name and gets the new modified time. So the run is journaled and History can undo it.

* With ExifTool, a photo gets ``DateTimeOriginal``, ``CreateDate`` and ``ModifyDate``; an MP4 or
  QuickTime video gets the QuickTime create, modify, media and track dates.
* Without ExifTool, or for a file type ExifTool cannot write, only the modified time changes
  (the plan says so). Nothing inside the file changes then.
* A OneDrive cloud-only file is never read or changed (AGENTS.md rule 3).
* Files must be inside the selected folders; the folders themselves are never changed, links are
  not followed (rule 4) and protected folders are refused (rule 6).
"""

from __future__ import annotations

import calendar
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from various_tools.core.context import RunContext
from various_tools.core.deps import install_hint
from various_tools.core.fs import (
    destination_refusal,
    is_inside,
    protected_reason,
    rename_no_replace,
    root_refusal,
    walk,
)
from various_tools.core.media import exif
from various_tools.core.media.library import (
    MEDIA_EXTS,
    MediaItem,
    media_kind,
    read_media_item,
)
from various_tools.core.onedrive import ensure_on_device
from various_tools.core.plan import (
    ActionKind,
    Plan,
    PlannedAction,
    RunResult,
    apply_plan,
    trash_extra,
)
from various_tools.tools.base import Category, ToolOptions

TOOL_ID = "set_date"
# File types ExifTool can write without touching the picture or video data.
WRITABLE_PHOTO_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".dng"}
)
WRITABLE_VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".3gp"})
MIN_YEAR = 1980
MAX_YEAR = 2100
ARROW = "→"
DATE_TEXT = "%Y-%m-%d %H:%M"
MTIME_ONLY_NO_TOOL = " (modified time only: ExifTool not found)"
MTIME_ONLY_TYPE = " (modified time only: ExifTool cannot write this file type)"
NO_EXIFTOOL_NOTE = (
    "ExifTool was not found, so only the modified time of each file will change. The date "
    "inside the file (the camera date) stays as it is, so the Timeline keeps showing it. "
    f"Install ExifTool with `{install_hint('exiftool')}`, or set its path in Settings."
)


def _hint(group: str, *, advanced: bool = False, **extra: Any) -> Any:
    hints: dict[str, Any] = {"group": group}
    if advanced:
        hints["advanced"] = True
    hints.update(extra)
    return hints


class SetDateOptions(ToolOptions):
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders to change",
        description=(
            "Every photo and video in these folders is changed. A drive root or a protected "
            "folder is refused. Default: none."
        ),
        json_schema_extra=_hint("Files", widget="folders"),
    )
    paths: list[Path] = Field(
        default_factory=list,
        title="Files to change",
        description=(
            "Individual photos or videos to change (the Timeline fills this in). Each must be "
            "a file, not a folder. Default: none."
        ),
        json_schema_extra=_hint("Files", advanced=True),
    )
    recursive: bool = Field(
        default=True,
        title="Include sub-folders",
        description="Also change files in the sub-folders of each chosen folder. Default: on.",
        json_schema_extra=_hint("Files"),
    )
    mode: Literal["exact", "month_year", "shift"] = Field(
        default="exact",
        title="How to change the date",
        description=(
            "exact: one date and time for every file. month_year: change the month and year "
            "but keep the day and the time (the day is cut back to the length of the month). "
            "shift: move every date by the same amount (a camera clock that was wrong). "
            "Default: exact."
        ),
        json_schema_extra=_hint("Date"),
    )
    new_date: str = Field(
        default="",
        title="New date and time",
        description=(
            "For 'exact': the date and time as YYYY-MM-DD HH:MM, for example 2021-03-04 10:12. "
            "A date alone (2021-03-04) means 12:00. Default: empty."
        ),
        json_schema_extra=_hint("Date", depends_on={"mode": "exact"}),
    )
    year: int = Field(
        default=0,
        ge=0,
        le=MAX_YEAR,
        title="Year",
        description="For 'month_year': the new year. 0 keeps each file's own year. Default: 0.",
        json_schema_extra=_hint("Date", depends_on={"mode": "month_year"}),
    )
    month: int = Field(
        default=0,
        ge=0,
        le=12,
        title="Month",
        description=(
            "For 'month_year': the new month, 1 to 12. 0 keeps each file's own month. Default: 0."
        ),
        json_schema_extra=_hint("Date", depends_on={"mode": "month_year"}),
    )
    shift_days: int = Field(
        default=0,
        ge=-36500,
        le=36500,
        title="Days to move",
        description="For 'shift': days to add (negative to go back). Default: 0.",
        json_schema_extra=_hint("Date", depends_on={"mode": "shift"}),
    )
    shift_hours: int = Field(
        default=0,
        ge=-8760,
        le=8760,
        title="Hours to move",
        description="For 'shift': hours to add (negative to go back). Default: 0.",
        json_schema_extra=_hint("Date", depends_on={"mode": "shift"}),
    )
    shift_minutes: int = Field(
        default=0,
        ge=-525600,
        le=525600,
        title="Minutes to move",
        description="For 'shift': minutes to add (negative to go back). Default: 0.",
        json_schema_extra=_hint("Date", depends_on={"mode": "shift"}),
    )


class DateChangeError(Exception):
    """A new copy could not be made or failed its check. The message goes to the journal."""


class _KeptCopyError(DateChangeError):
    """The original was recycled but the copy could not take its name; the copy must stay."""


# ----- the dates -----------------------------------------------------------------------------


def parse_new_date(text: str) -> datetime:
    """``YYYY-MM-DD HH:MM[:SS]`` or ``YYYY-MM-DD`` (12:00). Raises ``ValueError`` in plain words."""
    cleaned = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").replace(hour=12)
    except ValueError:
        raise ValueError(
            "Type the new date as YYYY-MM-DD HH:MM, for example 2021-03-04 10:12."
        ) from None


def _checked(value: datetime) -> datetime:
    if not MIN_YEAR <= value.year <= MAX_YEAR:
        raise ValueError(f"The new date must be between {MIN_YEAR} and {MAX_YEAR}.")
    return value


def new_date_for(old: datetime, opts: SetDateOptions) -> datetime:
    """The new date of a file that now has ``old``. Raises ``ValueError`` when it is not valid."""
    if opts.mode == "exact":
        return _checked(parse_new_date(opts.new_date))
    if opts.mode == "month_year":
        year = opts.year or old.year
        month = opts.month or old.month
        day = min(old.day, calendar.monthrange(year, month)[1])
        return _checked(old.replace(year=year, month=month, day=day))
    delta = timedelta(days=opts.shift_days, hours=opts.shift_hours, minutes=opts.shift_minutes)
    try:
        return _checked(old + delta)
    except OverflowError as exc:
        raise ValueError("The new date is out of range.") from exc


def _options_problem(opts: SetDateOptions) -> str | None:
    if not opts.paths and not opts.roots:
        return "Choose at least one file or folder."
    if opts.mode == "exact":
        try:
            parse_new_date(opts.new_date)
        except ValueError as exc:
            return str(exc)
    elif opts.mode == "month_year":
        if opts.year == 0 and opts.month == 0:
            return "Choose a new month, a new year, or both."
        if 0 < opts.year < MIN_YEAR:
            return f"The year must be {MIN_YEAR} or later."
    elif opts.shift_days == 0 and opts.shift_hours == 0 and opts.shift_minutes == 0:
        return "Choose how many days, hours or minutes to move the dates."
    return None


def _writes_tags(path: Path) -> bool:
    return path.suffix.lower() in WRITABLE_PHOTO_EXTS | WRITABLE_VIDEO_EXTS


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


# ----- apply ---------------------------------------------------------------------------------


@dataclass
class _Setter:
    opts: SetDateOptions
    roots: list[Path]
    ctx: RunContext

    def __call__(self, action: PlannedAction) -> Path | None:
        src, dst = action.src, action.dst
        if src is None or dst is None or not _same(src, dst):
            raise ValueError("a date change must keep the file's own path")
        self._check_paths(src)
        ensure_on_device(src)  # rule 3, checked again: the file may have been recalled since scan
        item = read_media_item(src, self.ctx)
        if item is None:
            raise FileNotFoundError(f"source is gone or is not a photo or video: {src}")
        if item.cloud_only:
            raise DateChangeError("cloud-only files are never changed")
        if item.taken is None:
            raise DateChangeError("the file has no date to change")
        new = new_date_for(item.taken, self.opts)
        refusal = destination_refusal(dst, stop=self._root_of(dst))
        if refusal is not None:
            raise ValueError(refusal)
        return self._replace_with_dated_copy(src, new)

    def _root_of(self, path: Path) -> Path | None:
        return next((root for root in self.roots if is_inside(path, root)), None)

    def _check_paths(self, src: Path) -> None:
        if not any(is_inside(src, root) and not _same(src, root) for root in self.roots):
            raise ValueError(f"source is outside the selected folders: {src}")
        if not self.ctx.settings.allow_protected_paths:
            reason = protected_reason(src, rules=self.ctx.settings.protected_paths())
            if reason:
                raise ValueError(f"protected path ({reason}): {src}")

    def _replace_with_dated_copy(self, src: Path, new: datetime) -> Path:
        staging = src.parent / f".setdate-{uuid.uuid4().hex[:12]}.tmp"
        keep_staging = False
        try:
            exiftool = self.ctx.deps.exiftool()
            if exiftool is not None and _writes_tags(src):
                self._write_copy(exiftool, src, staging, new)
            else:
                shutil.copy2(src, staging)
            stamp = new.timestamp()
            os.utime(staging, (stamp, stamp))
            # Its own journal row (rule 5), so undo can put the original back from the Recycle
            # Bin. When the recycle fails or is refused, the original stays and the error
            # propagates.
            trash_extra(src, reason="original replaced by the copy with the new date")
            try:
                rename_no_replace(staging, src)
            except OSError as exc:
                keep_staging = True
                raise _KeptCopyError(
                    f"the original is in the Recycle Bin, and the copy is at {staging}; "
                    f"rename it to {src.name} to finish ({exc})"
                ) from exc
        finally:
            if not keep_staging:
                staging.unlink(missing_ok=True)
        return src

    def _write_copy(self, exiftool: Path, src: Path, staging: Path, new: datetime) -> None:
        video = src.suffix.lower() in WRITABLE_VIDEO_EXTS
        tags = exif.video_date_args(new) if video else exif.photo_date_args(new)
        args = ["-m", "-o", str(staging), *tags, str(src)]
        try:
            result = exif.run_exiftool(exiftool, args, self.ctx.settings.exiftool_write_timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise DateChangeError(f"ExifTool timed out on {src.name}") from exc
        except (OSError, ValueError) as exc:
            raise DateChangeError(f"ExifTool could not run on {src.name}: {exc}") from exc
        if result.returncode != 0 or not staging.is_file():
            detail = (result.stderr or result.stdout or "").strip()[:300]
            raise DateChangeError(f"ExifTool could not write the copy of {src.name}: {detail}")
        if staging.stat().st_size == 0:
            raise DateChangeError(f"the copy of {src.name} is empty")
        if not video:
            written = exif.exiftool_capture_time(
                exiftool, staging, self.ctx.settings.exiftool_read_timeout_s
            )
            if written is None or abs((written - new).total_seconds()) > 1:
                raise DateChangeError(
                    f"the date in the copy of {src.name} could not be checked; it was not kept"
                )


# ----- the tool ------------------------------------------------------------------------------


class SetDate:
    id = TOOL_ID
    name = "Change date"
    # Plain words for the GUI (gui/wording.py).
    scan_label = "Preview the new dates"
    found_nouns = ("file to change", "files to change")
    description = (
        "Change the date photos and videos were taken: one exact date, a new month and year "
        "(keeping the day and time), or a shift by days, hours and minutes for a camera clock "
        "that was wrong. A preview shows each file's old and new date. Needs ExifTool to "
        "change the date inside the file; without it only the modified time changes."
    )
    category: Category = "Organize"
    Options = SetDateOptions

    def scan(self, opts: SetDateOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, SetDateOptions):
            raise TypeError("SetDate.scan needs SetDateOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        problem = _options_problem(opts)
        if problem is not None:
            plan.notes.append(problem)
            return plan

        rules = ctx.settings.protected_paths()
        roots: list[Path] = []
        files: dict[str, Path] = {}

        def add_root(root: Path) -> bool:
            root = Path(os.path.abspath(root))
            refusal = root_refusal(root, rules)
            if refusal is not None:
                plan.notes.append(refusal)
                return False
            if not any(_same(root, r) for r in roots):
                roots.append(root)
            return True

        for raw in opts.roots:
            root = Path(os.path.abspath(raw))
            if add_root(root):
                for info in walk(root, exts=MEDIA_EXTS, recursive=opts.recursive):
                    files.setdefault(_key(info.path), info.path)
        for raw in opts.paths:
            path = Path(os.path.abspath(raw))
            if not path.is_file() or media_kind(path) is None:
                plan.notes.append(f"skipped: not a photo or video file: {path}")
            elif add_root(path.parent):
                files.setdefault(_key(path), path)
        plan.roots = roots

        exiftool = ctx.deps.exiftool()
        if exiftool is None:
            plan.notes.append(NO_EXIFTOOL_NOTE)

        actions: list[PlannedAction] = []
        skipped = same = 0
        total = len(files)
        for done, path in enumerate(files.values()):
            ctx.check_cancelled()
            ctx.report(done, total, path.name)
            item = read_media_item(path, ctx)
            if item is None:
                skipped += 1
                plan.notes.append(f"skipped: could not read the file: {path}")
                continue
            if item.cloud_only:
                skipped += 1
                plan.notes.append(f"skipped: cloud-only, not read: {path}")
                if path not in plan.cloud_only:
                    plan.cloud_only.append(path)
                continue
            old = item.taken
            if old is None:
                skipped += 1
                plan.notes.append(f"skipped: no date to change: {path}")
                continue
            try:
                new = new_date_for(old, opts)
            except ValueError as exc:
                skipped += 1
                plan.notes.append(f"skipped: {exc} {path}")
                continue
            if new == old:
                same += 1
                continue
            actions.append(_action(path, item, old, new, exiftool is not None))
        ctx.report(total, total, "Checked")
        plan.actions = actions
        plan.notes.insert(
            0,
            f"Change date: {len(actions)} file(s) will change, {same} already have that date, "
            f"{skipped} skipped. Originals go to the Recycle Bin and can be restored from History.",
        )
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        opts = SetDateOptions.model_validate_json(plan.options_json)
        return apply_plan(plan, ctx, handlers={ActionKind.CONVERT: _Setter(opts, plan.roots, ctx)})


def _action(
    path: Path, item: MediaItem, old: datetime, new: datetime, have_exiftool: bool
) -> PlannedAction:
    reason = f"{old:{DATE_TEXT}} {ARROW} {new:{DATE_TEXT}}"
    if not have_exiftool:
        reason += MTIME_ONLY_NO_TOOL
    elif not _writes_tags(item.path):
        reason += MTIME_ONLY_TYPE
    return PlannedAction(kind=ActionKind.CONVERT, src=path, dst=path, reason=reason)


TOOL = SetDate()
