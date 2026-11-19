"""Rename (plan §3.3): give a batch of files one name pattern, e.g. Snapchat or WhatsApp exports.

Each matching file is dated (capture time, modified or created time, or a date in its name),
numbered per minute, per day or over the whole batch, and renamed to the template. ``scan()``
changes nothing; the plan lists every old → new name. ``apply()`` renames through the generic
engine, so every rename is journaled and can be undone from History.

Rules:
* a name that is taken, either by a file on disk or by another file in the batch, is never
  overwritten. The row is listed but not selected, with the reason. Apply checks again, so a
  plan that was edited or left for a while cannot rename onto a file that appeared since;
* cloud-only OneDrive files are never read (AGENTS.md rule 3). Their capture date is skipped;
  renaming them by modified or created time changes only the name;
* junctions and symlinks are not followed (rule 4), and a root folder is never renamed;
* protected folders are refused (rule 6), and the engine guards every action again at apply time.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from various_tools.core.context import RunContext
from various_tools.core.fs import (
    FileInfo,
    is_case_only_rename,
    is_inside,
    long_path,
    root_refusal,
    walk,
)
from various_tools.core.media import ffmpeg
from various_tools.core.media.exif import capture_time, is_plausible_date
from various_tools.core.onedrive import CloudOnlyFileError, is_on_device, status_from_attributes
from various_tools.core.plan import (
    ActionKind,
    ActionOutcome,
    Plan,
    PlannedAction,
    RunResult,
    apply_plan,
)
from various_tools.tools.base import Category, ToolOptions
from various_tools.tools.rename.naming import (
    FILENAME_DATE_PATTERN,
    CaseMode,
    apply_case,
    date_from_filename,
    filename_pattern_problem,
    name_problem,
    render_name,
    template_fields,
    template_problem,
)
from various_tools.tools.rename.presets import (
    PRESETS,
    CounterScope,
    DateSource,
    MatchMode,
    RenameSpec,
)

TOOL_ID = "rename"
PresetName = Literal["custom", "snapchat", "whatsapp", "screenshot"]


class RenameOptions(ToolOptions):
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders",
        description=(
            "The folders whose files are renamed. The folders themselves are never renamed, "
            "and a drive root or protected folder is refused."
        ),
        json_schema_extra={"widget": "folders", "group": "Files"},
    )
    recursive: bool = Field(
        default=False,
        title="Include subfolders",
        description=(
            "Also rename matching files inside subfolders. Off by default, as in the original "
            "Snapchat script."
        ),
        json_schema_extra={"group": "Files", "advanced": True},
    )
    preset: PresetName = Field(
        default="custom",
        title="Preset",
        description=(
            "A ready-made rule set for Snapchat, WhatsApp or screenshot files. It fills in the "
            "match, name template, date source, counter and case; anything you set yourself "
            "wins. Custom starts empty."
        ),
        json_schema_extra={"group": "Match"},
    )
    match: str | None = Field(
        default=None,
        title="Match",
        description=(
            "Which file names to rename: a glob such as IMG_*.jpg, or a regular expression when "
            "the match mode is regex. Empty uses the preset's pattern."
        ),
        json_schema_extra={"group": "Match"},
    )
    match_mode: MatchMode | None = Field(
        default=None,
        title="Match mode",
        description=(
            "glob matches simple patterns with * and ?; regex matches a regular expression. "
            "Empty uses the preset's mode, or glob when you typed your own match."
        ),
        json_schema_extra={"group": "Match", "advanced": True},
    )
    template: str | None = Field(
        default=None,
        title="Name template",
        description=(
            "The new name. Placeholders: {date:%Y%m%d}, {time:%H_%M} (needs a time of day), "
            "{counter:03}, {name} (the old name without extension) and {ext} (required, keeps "
            "the file type). Empty uses the preset's template."
        ),
        json_schema_extra={"widget": "template", "group": "Name"},
    )
    date_source: DateSource | None = Field(
        default=None,
        title="Date source",
        description=(
            "Where each file's date comes from: capture (EXIF, then ExifTool, then ffprobe), "
            "modified, created, or filename (a date inside the old name). Empty uses the "
            "preset's source, or modified."
        ),
        json_schema_extra={"group": "Name"},
    )
    counter_scope: CounterScope | None = Field(
        default=None,
        title="Counter restarts",
        description=(
            "When the {counter} number starts again at 1: every minute, every day, or never "
            "(one count across the whole batch). Empty uses the preset's choice, or per-minute."
        ),
        json_schema_extra={"group": "Name"},
    )
    case: CaseMode | None = Field(
        default=None,
        title="Extension case",
        description=(
            "Letter case of the file extension: keep, lower or upper. The rest of the name is "
            "never changed. Empty uses the preset's choice, or keep."
        ),
        json_schema_extra={"group": "Name", "advanced": True},
    )
    min_plausible_year: int | None = Field(
        default=None,
        ge=1900,
        le=9999,
        title="Earliest believable year",
        description=(
            "Capture dates and dates found in file names before this year, or in the future, "
            "are treated as wrong. Empty uses the Settings value. 1990 is a hand-tuned "
            "default: change it only if you know why."
        ),
        json_schema_extra={"group": "Dates", "advanced": True},
    )
    filename_date_pattern: str = Field(
        default=FILENAME_DATE_PATTERN,
        min_length=1,
        title="Filename date pattern",
        description=(
            "Regular expression that finds a date in a file name when the date source is "
            "filename. Needs 3 groups (year, month, day), or 6 with hour, minute and second. "
            "Default: YYYYMMDD with an optional HHMMSS, ignoring longer numbers."
        ),
        json_schema_extra={"group": "Dates", "advanced": True},
    )


def resolve_spec(opts: RenameOptions) -> tuple[RenameSpec | None, str | None]:
    """The effective rename settings: the preset, with any explicit option overriding it.

    Returns ``(spec, None)``, or ``(None, problem)`` when the options cannot be used. A custom
    match pattern is a glob unless ``match_mode`` says regex, even when a preset is chosen.
    """
    base = PRESETS.get(opts.preset)
    if opts.match is not None:
        match, default_mode = opts.match, "glob"
    elif base is not None:
        match, default_mode = base.match, base.match_mode
    else:
        return None, "Choose a preset, or give a match pattern and a name template."
    if not match:
        return None, "The match pattern is empty."

    template = opts.template if opts.template is not None else (base.template if base else None)
    if template is None:
        return None, "Choose a preset, or give a name template."

    spec = RenameSpec(
        label=base.label if base is not None else "Custom rename",
        match=match,
        match_mode=opts.match_mode or default_mode,
        template=template,
        date_source=opts.date_source or (base.date_source if base else "modified"),
        counter_scope=opts.counter_scope or (base.counter_scope if base else "per-minute"),
        case=opts.case or (base.case if base else "keep"),
    )
    if spec.match_mode == "regex":
        try:
            re.compile(spec.match, re.IGNORECASE)
        except re.error as exc:
            return None, f"The match pattern is not a valid regular expression: {exc}"
    if spec.date_source == "filename":
        pattern_problem = filename_pattern_problem(opts.filename_date_pattern)
        if pattern_problem is not None:
            return None, pattern_problem
    problem = template_problem(spec.template)
    if problem is not None:
        return None, problem
    try:
        render_name(
            spec.template,
            when=datetime(2000, 1, 1, 12, 0),
            has_time=True,
            counter=1,
            ext=".jpg",
            stem="name",
        )
    except (KeyError, ValueError, TypeError, IndexError, AttributeError) as exc:
        return None, f"The name template cannot be used: {exc}"
    return spec, None


@dataclass(frozen=True)
class _DateRules:
    """How a date is read: the plausible-year floor and the filename date pattern."""

    min_year: int
    filename_pattern: str


def _date_rules(opts: RenameOptions, ctx: RunContext) -> _DateRules:
    year = opts.min_plausible_year
    return _DateRules(
        min_year=year if year is not None else ctx.settings.min_plausible_year,
        filename_pattern=opts.filename_date_pattern,
    )


@dataclass(frozen=True)
class _Dated:
    info: FileInfo
    when: datetime
    has_time: bool


def _folder_key(path: Path) -> str:
    return os.path.normcase(str(path.parent))


def _claim_key(path: Path) -> tuple[str, str]:
    """Two names clash when they sit in the same folder and differ only by letter case."""
    return _folder_key(path), path.name.casefold()


def _name_matches(name: str, spec: RenameSpec) -> bool:
    if spec.match_mode == "regex":
        return re.fullmatch(spec.match, name, re.IGNORECASE) is not None
    return fnmatch.fnmatch(name, spec.match)


def _matching(roots: Iterable[Path], spec: RenameSpec, recursive: bool) -> list[FileInfo]:
    """Files whose name matches, once each even when roots overlap."""
    seen: set[str] = set()
    found: list[FileInfo] = []
    for root in roots:
        for info in walk(root, recursive=recursive):
            key = os.path.normcase(os.path.abspath(info.path))
            if key in seen:
                continue
            seen.add(key)
            if _name_matches(info.path.name, spec):
                found.append(info)
    return found


CLOUD_ONLY_REASON = "cloud-only, not read"


def _cloud_only(info: FileInfo) -> bool:
    return not is_on_device(status_from_attributes(info.attributes))


def _capture(info: FileInfo, ctx: RunContext, min_year: int) -> datetime | None:
    """EXIF, then ExifTool, then ffprobe for videos. Only called for files on the device."""
    try:
        when = capture_time(
            info.path,
            exiftool=ctx.deps.exiftool(),
            min_year=min_year,
            exiftool_timeout=ctx.settings.exiftool_read_timeout_s,
        )
    except OSError, CloudOnlyFileError:
        when = None
    if when is None:
        ffprobe = ctx.deps.ffprobe()
        if ffprobe is not None:
            video_time = ffmpeg.creation_time(
                ffprobe, info.path, timeout=ctx.settings.ffprobe_timeout_s
            )
            if is_plausible_date(video_time, min_year=min_year):
                when = video_time
    return when


def _when(
    info: FileInfo, spec: RenameSpec, ctx: RunContext, rules: _DateRules
) -> tuple[datetime, bool] | str:
    """The date for a file as ``(when, has_time)``, or the reason it has none."""
    if spec.date_source == "filename":
        found = date_from_filename(info.path.name, rules.min_year, rules.filename_pattern)
        return found if found is not None else "no date in the file name"
    if spec.date_source == "modified":
        return datetime.fromtimestamp(info.mtime), True
    if spec.date_source == "created":
        try:
            stat = os.stat(long_path(info.path))
        except OSError as exc:
            return f"could not read the date ({exc.strerror or exc})"
        return datetime.fromtimestamp(getattr(stat, "st_birthtime", stat.st_ctime)), True
    # capture: the file's contents are read, so cloud-only files are never touched.
    if _cloud_only(info):
        return CLOUD_ONLY_REASON
    when = _capture(info, ctx, rules.min_year)
    return (when, True) if when is not None else "no capture date"


def _dated(
    infos: list[FileInfo],
    spec: RenameSpec,
    ctx: RunContext,
    rules: _DateRules,
    notes: list[str],
    cloud_only: list[Path],
) -> list[_Dated]:
    """Date every file. Files without a usable date are listed in ``notes`` and left out.
    Files that are not on this device are never read; they are also added to ``cloud_only``."""
    uses_time = "time" in template_fields(spec.template)
    dated: list[_Dated] = []
    total = len(infos)
    for index, info in enumerate(infos, start=1):
        ctx.check_cancelled()
        ctx.report(index - 1, total, f"Dating {info.path.name}")
        result = _when(info, spec, ctx, rules)
        if isinstance(result, str):
            notes.append(f"skipped: {result}: {info.path}")
            if result == CLOUD_ONLY_REASON and info.path not in cloud_only:
                cloud_only.append(info.path)
            continue
        when, has_time = result
        if uses_time and not has_time:
            notes.append(f"skipped: the template needs a time, and this file has none: {info.path}")
            continue
        dated.append(_Dated(info, when, has_time))
    ctx.report(total, total, "Dated")
    return dated


def _scope_key(when: datetime, scope: CounterScope, folder: str) -> tuple[str, str]:
    """The group that a counter counts within. ``global`` counts across the whole batch."""
    if scope == "global":
        return ("", "")
    if scope == "per-day":
        return (folder, when.strftime("%Y%m%d"))
    return (folder, when.strftime("%Y%m%d-%H_%M"))


def _conflict(src: Path, dst: Path) -> str | None:
    """Why ``src`` cannot be renamed to ``dst``, or None. Checks the disk, never changes it."""
    if dst.name == src.name:
        return "it already has this name"
    if dst.name.casefold() == src.name.casefold():
        # Only the letter case changes: fine when the name that "exists" is this very file.
        if not os.path.lexists(long_path(dst)) or is_case_only_rename(src, dst):
            return None
        return "a file with this name already exists"
    if os.path.lexists(long_path(dst)):
        return "a file with this name already exists"
    return None


def _actions(dated: list[_Dated], spec: RenameSpec, notes: list[str]) -> list[PlannedAction]:
    """Number and name every dated file, in date order, and mark the names that cannot be used."""
    ordered = sorted(
        dated,
        key=lambda d: (d.when, _folder_key(d.info.path), d.info.path.name.casefold()),
    )
    counts: dict[tuple[str, str], int] = {}
    claimed: set[tuple[str, str]] = set()
    actions: list[PlannedAction] = []
    for item in ordered:
        src = item.info.path
        scope_key = _scope_key(item.when, spec.counter_scope, _folder_key(src))
        counts[scope_key] = counts.get(scope_key, 0) + 1
        new_name = render_name(
            spec.template,
            when=item.when,
            has_time=item.has_time,
            counter=counts[scope_key],
            ext=apply_case(src.suffix, spec.case),
            stem=src.stem,
        )
        if new_name == src.name:
            notes.append(f"unchanged, already has this name: {src}")
            continue
        problem = name_problem(new_name)
        if problem is not None:
            notes.append(f"skipped: the new name {new_name!r} {problem}: {src}")
            continue
        dst = src.parent / new_name
        conflict = _conflict(src, dst)
        if conflict is None and _claim_key(dst) in claimed:
            conflict = "another file in this batch takes this name"
        if conflict is None:
            claimed.add(_claim_key(dst))
            actions.append(
                PlannedAction(
                    kind=ActionKind.RENAME, src=src, dst=dst, reason=f"rename to {new_name}"
                )
            )
        else:
            actions.append(
                PlannedAction(
                    kind=ActionKind.RENAME,
                    src=src,
                    dst=dst,
                    reason=f"not renamed: {conflict} ({new_name})",
                    selected=False,
                )
            )
    return actions


def _recheck(plan: Plan, ctx: RunContext) -> tuple[Plan, list[ActionOutcome]]:
    """Clear the selection of any rename whose name is now taken, so nothing is renamed onto a
    file. Returns the plan to run and the outcomes of the rows that were dropped."""
    refused = [
        root
        for root in plan.roots
        if root_refusal(root, ctx.settings.protected_paths()) is not None
    ]
    claimed: set[tuple[str, str]] = set()
    kept: list[PlannedAction] = []
    dropped: list[ActionOutcome] = []
    for seq, action in enumerate(plan.actions, start=1):
        if (
            not action.selected
            or action.kind is not ActionKind.RENAME
            or action.src is None
            or action.dst is None
        ):
            kept.append(action)
            continue
        reason: str | None
        if any(is_inside(action.src, root) for root in refused):
            reason = "its root folder is refused"
        else:
            reason = _conflict(action.src, action.dst)
            if reason is None and _claim_key(action.dst) in claimed:
                reason = "another file in this batch takes this name"
        if reason is not None:
            kept.append(replace(action, selected=False))
            dropped.append(
                ActionOutcome(
                    seq,
                    str(action.kind),
                    "skipped",
                    str(action.src),
                    str(action.dst),
                    f"not renamed: {reason}",
                )
            )
            continue
        claimed.add(_claim_key(action.dst))
        kept.append(action)
    return replace(plan, actions=kept), dropped


def _journal_dropped(ctx: RunContext, result: RunResult, dropped: list[ActionOutcome]) -> None:
    """Record the rows dropped before the engine ran as skipped, like the engine's own refusals."""
    journal = ctx.journal
    seq = len(result.outcomes)
    for outcome in dropped:
        seq += 1
        if journal is not None:
            journal.record_action(
                result.run_id,
                seq,
                outcome.kind,
                Path(outcome.src) if outcome.src else None,
                Path(outcome.dst) if outcome.dst else None,
                outcome.detail,
                status="skipped",
                error=outcome.detail,
            )
        result.outcomes.append(replace(outcome, seq=seq))


class Rename:
    id = TOOL_ID
    name = "Rename"
    # Plain words for the GUI (gui/wording.py).
    scan_label = "Preview new names"
    found_nouns = ("file to rename", "files to rename")
    description = (
        "Give a batch of files one name pattern, dated from their capture, modified or created "
        "time, or from a date in the name. Built-in presets cover Snapchat, WhatsApp and "
        "screenshot files."
    )
    category: Category = "Organize"
    Options = RenameOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, RenameOptions):
            raise TypeError("Rename.scan needs RenameOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        spec, problem = resolve_spec(opts)
        if spec is None:
            plan.notes.append(problem or "The options cannot be used.")
            return plan
        if not opts.roots:
            plan.notes.append("Choose at least one folder.")
            return plan

        roots: list[Path] = []
        for raw in opts.roots:
            root = Path(os.path.abspath(raw))
            refusal = root_refusal(root, ctx.settings.protected_paths())
            if refusal is None:
                roots.append(root)
            else:
                plan.notes.append(refusal)
        if not roots:
            plan.notes.append("Nothing to rename.")
            return plan
        plan.roots = roots

        infos = _matching(roots, spec, opts.recursive)
        dated = _dated(infos, spec, ctx, _date_rules(opts, ctx), plan.notes, plan.cloud_only)
        actions = _actions(dated, spec, plan.notes)
        plan.actions = actions
        renamed = sum(1 for a in actions if a.selected)
        blocked = len(actions) - renamed
        plan.notes.insert(
            0,
            f"{spec.label}: {len(infos)} file(s) match. {renamed} will be renamed; "
            f"{blocked} cannot be renamed because the name is taken.",
        )
        ctx.log.info("rename scan: %d match, %d to rename", len(infos), renamed)
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        checked, dropped = _recheck(plan, ctx)
        result = apply_plan(checked, ctx)
        _journal_dropped(ctx, result, dropped)
        return result


TOOL = Rename()
