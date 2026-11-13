"""Reports: export a plan, a journaled run or an apply result to a file.

Formats (all written through a temporary file, so a failed export never leaves half a file):

* ``csv``  UTF-8 with a byte order mark, so Excel opens it correctly.
* ``xlsx`` an Excel workbook with an "Actions" sheet and a "Summary" sheet.
* ``html`` one self-contained page. For the duplicate finder the rows are grouped by the copy that
  is kept, with small thumbnails of the kept file and of each duplicate.
* ``txt``  the "review list" the original ``find_duplicates.py`` wrote, one entry per action.
* ``log``  the CSV log the original duplicate scripts appended to (``moved_file``, ``moved_to``,
  ``original_name``, ``kept_original``, ``match_type``, ``original_path``, ``kept_path``).

Rule 3 (OneDrive cloud-only files are never read): this module never opens a file. Sizes come from
``lstat`` and thumbnails come only from :func:`various_tools.core.media.thumbs.thumbnail_jpeg`,
which refuses cloud-only files. A file that cannot be previewed gets a text placeholder.
"""

from __future__ import annotations

import base64
import csv
import html
import json
import os
import stat
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from various_tools.core.fs import long_path
from various_tools.core.journal import ActionRecord, RunRecord
from various_tools.core.plan import Plan, PlannedAction, RunResult

FORMATS: dict[str, str] = {
    "csv": "CSV",
    "xlsx": "Excel workbook",
    "html": "HTML report",
    "txt": "Review list",
    "log": "Duplicates log (CSV)",
}
"""Format id to the label shown in file dialogs."""

FORMAT_SUFFIXES: dict[str, tuple[str, ...]] = {
    "csv": (".csv",),
    "xlsx": (".xlsx",),
    "html": (".html", ".htm"),
    "txt": (".txt",),
    "log": (".csv",),
}
"""Suffixes of each format. The first one is the default. ``log`` is only chosen explicitly."""

EXPORT_FILTER = ";;".join(f"{label} (*{FORMAT_SUFFIXES[fid][0]})" for fid, label in FORMATS.items())
"""A ``QFileDialog`` filter string built from :data:`FORMATS`."""

MAX_THUMBNAILS = 2000
"""The HTML report previews at most this many files, so a huge plan stays a manageable page."""
THUMB_PX = 160
CLOUD_PLACEHOLDER = "cloud-only: not downloaded"
NO_PREVIEW = "no preview"

Cell = str | int


# ----- choosing a format ---------------------------------------------------------------------


def format_for(path: Path, fmt: str | None = None) -> str:
    """The format id for ``path``. An explicit ``fmt`` wins, otherwise the file suffix decides.

    Raises ``ValueError`` for an unknown format or a suffix that matches none.
    """
    if fmt:
        wanted = fmt.strip().lower().lstrip(".")
        if wanted == "htm":
            wanted = "html"
        if wanted not in FORMATS:
            raise ValueError(f"unknown export format {fmt!r}; choose one of: {', '.join(FORMATS)}")
        return wanted
    suffix = path.suffix.lower()
    for fid, suffixes in FORMAT_SUFFIXES.items():
        if fid != "log" and suffix in suffixes:
            return fid
    raise ValueError(
        f"cannot tell the export format from {path.name!r}; use a .csv, .xlsx, .html or .txt "
        f"name, or give the format explicitly ({', '.join(FORMATS)})"
    )


def format_from_filter(selected_filter: str) -> str | None:
    """The format id for the filter string a file dialog reported as selected, or None."""
    for fid, label in FORMATS.items():
        if selected_filter.startswith(f"{label} ("):
            return fid
    return None


# ----- the common row model ------------------------------------------------------------------


@dataclass
class _Entry:
    """One row, whatever it came from. Unused fields stay at their defaults."""

    kind: str
    src: str = ""
    dst: str = ""
    reason: str = ""
    group: str = ""
    seq: int | None = None
    selected: bool | None = None
    status: str = ""
    error: str = ""
    time: str = ""
    size: int | None = None
    preview: bool = False  # True: a thumbnail of ``src`` may be shown (plans only)


@dataclass
class _Column:
    header: str
    value: Callable[[_Entry], Cell]


@dataclass
class _Report:
    title: str
    tool_id: str
    tool_name: str
    columns: list[_Column]
    entries: list[_Entry]
    facts: list[tuple[str, str]]  # label and value, for the Summary
    counts: list[tuple[str, int]]
    notes: list[str]
    options_json: str
    applied: bool  # the entries describe things that were done, not things that would be done
    thumbnails: bool = False


def _str(value: str | None) -> str:
    return value or ""


def _size_cell(entry: _Entry) -> Cell:
    return "" if entry.size is None else entry.size


PLAN_COLUMNS = [
    _Column("Selected", lambda e: "yes" if e.selected else "no"),
    _Column("Kind", lambda e: e.kind),
    _Column("Source", lambda e: e.src),
    _Column("Destination", lambda e: e.dst),
    _Column("Reason", lambda e: e.reason),
    _Column("Group", lambda e: e.group),
    _Column("Size (bytes)", _size_cell),
]
RUN_COLUMNS = [
    _Column("Seq", lambda e: "" if e.seq is None else e.seq),
    _Column("Kind", lambda e: e.kind),
    _Column("Status", lambda e: e.status),
    _Column("Source", lambda e: e.src),
    _Column("Destination", lambda e: e.dst),
    _Column("Reason", lambda e: e.reason),
    _Column("Error", lambda e: e.error),
    _Column("Time", lambda e: e.time),
]
RESULT_COLUMNS = [
    _Column("Seq", lambda e: "" if e.seq is None else e.seq),
    _Column("Kind", lambda e: e.kind),
    _Column("Status", lambda e: e.status),
    _Column("Source", lambda e: e.src),
    _Column("Destination", lambda e: e.dst),
    _Column("Reason", lambda e: e.reason),
    _Column("Group", lambda e: e.group),
    _Column("Detail", lambda e: e.error),
]


def _pretty_options(options_json: str) -> str:
    try:
        return json.dumps(json.loads(options_json), indent=2, sort_keys=True, ensure_ascii=False)
    except ValueError:
        return options_json


def _count_lines(entries: Sequence[_Entry], attr: str) -> list[tuple[str, int]]:
    counter = Counter(cast("str", getattr(e, attr)) for e in entries)
    return [(name, number) for name, number in counter.items() if name]


def action_sizes(plan: Plan) -> list[int | None]:
    """The size in bytes of each action's source, in plan order.

    None when the action has no source, the source is gone, or it is a folder. This reads file
    metadata only (``lstat``), so it never opens a file and never downloads a cloud-only one.
    """
    sizes: list[int | None] = []
    for action in plan.actions:
        sizes.append(_size_of(action.src))
    return sizes


def _size_of(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        info = os.lstat(long_path(path))
    except OSError:
        return None
    return None if stat.S_ISDIR(info.st_mode) else info.st_size


def _plan_report(
    plan: Plan, sizes: Sequence[int | None] | None, tool_name: str | None, thumbnails: bool
) -> _Report:
    if sizes is not None and len(sizes) != len(plan.actions):
        raise ValueError("sizes must have one entry per action in the plan")
    entries = [
        _Entry(
            kind=str(action.kind),
            src=str(action.src) if action.src is not None else "",
            dst=str(action.dst) if action.dst is not None else "",
            reason=action.reason,
            group=_str(action.group_id),
            selected=action.selected,
            size=None if sizes is None else sizes[index],
            preview=action.src is not None,
        )
        for index, action in enumerate(plan.actions)
    ]
    selected = sum(1 for e in entries if e.selected)
    facts = [
        ("Roots", "; ".join(str(r) for r in plan.roots)),
        ("Created", plan.created),
        ("Actions", str(len(entries))),
        ("Selected", str(selected)),
    ]
    return _Report(
        title=f"{tool_name or plan.tool}: plan (preview, nothing changed)",
        tool_id=plan.tool,
        tool_name=tool_name or plan.tool,
        columns=PLAN_COLUMNS,
        entries=entries,
        facts=facts,
        counts=[(f"kind: {name}", n) for name, n in _count_lines(entries, "kind")],
        notes=list(plan.notes),
        options_json=plan.options_json,
        applied=False,
        thumbnails=thumbnails,
    )


def _run_report(run: RunRecord, actions: Sequence[ActionRecord], tool_name: str | None) -> _Report:
    entries = [
        _Entry(
            kind=a.kind,
            src=_str(a.src),
            dst=_str(a.dst),
            reason=a.reason,
            seq=a.seq,
            status=a.status,
            error=_str(a.error),
            time=a.ts,
        )
        for a in actions
    ]
    facts = [
        ("Run", str(run.id)),
        ("Status", run.status),
        ("Started", run.started),
        ("Finished", run.finished or ""),
        ("Actions", str(len(entries))),
    ]
    counts = [(f"status: {n}", c) for n, c in _count_lines(entries, "status")]
    counts += [(f"kind: {n}", c) for n, c in _count_lines(entries, "kind")]
    return _Report(
        title=f"{tool_name or run.tool}: run {run.id} ({run.status})",
        tool_id=run.tool,
        tool_name=tool_name or run.tool,
        columns=RUN_COLUMNS,
        entries=entries,
        facts=facts,
        counts=counts,
        notes=[],
        options_json=run.options_json,
        applied=True,
    )


def _result_report(plan: Plan, result: RunResult, tool_name: str | None) -> _Report:
    selected = plan.selected_actions()
    entries: list[_Entry] = []
    for outcome in result.outcomes:
        planned: PlannedAction | None = None
        if 1 <= outcome.seq <= len(selected):
            candidate = selected[outcome.seq - 1]
            if (str(candidate.src) if candidate.src is not None else None) == outcome.src:
                planned = candidate
        entries.append(
            _Entry(
                kind=outcome.kind,
                src=_str(outcome.src),
                dst=_str(outcome.dst),
                reason=planned.reason if planned else "",
                group=_str(planned.group_id) if planned else "",
                seq=outcome.seq,
                status=outcome.status,
                error=outcome.detail,
            )
        )
    facts = [
        ("Roots", "; ".join(str(r) for r in plan.roots)),
        ("Planned", plan.created),
        ("Run", str(result.run_id)),
        ("Status", result.status),
        ("Actions", str(len(entries))),
    ]
    counts = [(f"status: {n}", c) for n, c in _count_lines(entries, "status")]
    counts += [(f"kind: {n}", c) for n, c in _count_lines(entries, "kind")]
    return _Report(
        title=f"{tool_name or plan.tool}: run {result.run_id} ({result.status})",
        tool_id=plan.tool,
        tool_name=tool_name or plan.tool,
        columns=RESULT_COLUMNS,
        entries=entries,
        facts=facts,
        counts=counts,
        notes=list(plan.notes),
        options_json=plan.options_json,
        applied=True,
    )


# ----- writing --------------------------------------------------------------------------------


def _write_atomic(path: Path, write: Callable[[Path], None]) -> Path:
    """Run ``write(temp_path)`` in the target folder, then move the temp file over ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(handle)
    temp = Path(temp_name)
    try:
        write(temp)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return path


def _write_report(report: _Report, path: Path, fmt: str) -> Path:
    writers: dict[str, Callable[[_Report, Path], None]] = {
        "csv": _write_csv,
        "xlsx": _write_xlsx,
        "html": _write_html,
        "txt": _write_txt,
        "log": _write_log,
    }
    writer = writers[fmt]
    return _write_atomic(path, lambda temp: writer(report, temp))


def export_plan(
    plan: Plan,
    path: Path,
    fmt: str | None = None,
    *,
    sizes: Sequence[int | None] | None = None,
    tool_name: str | None = None,
    thumbnails: bool = True,
) -> Path:
    """Write ``plan`` to ``path`` and return the path.

    ``sizes`` (from :func:`action_sizes`, one per action) fills the size column; without it that
    column is left empty. ``thumbnails=False`` leaves the HTML report without previews.
    Raises ``ValueError`` for an unknown format and ``OSError`` when the file cannot be written.
    """
    chosen = format_for(path, fmt)
    return _write_report(_plan_report(plan, sizes, tool_name, thumbnails), path, chosen)


def export_run(
    run: RunRecord,
    actions: Sequence[ActionRecord],
    path: Path,
    fmt: str | None = None,
    *,
    tool_name: str | None = None,
) -> Path:
    """Write a journaled run (its record and its actions) to ``path`` and return the path."""
    chosen = format_for(path, fmt)
    return _write_report(_run_report(run, actions, tool_name), path, chosen)


def export_result(
    plan: Plan,
    result: RunResult,
    path: Path,
    fmt: str | None = None,
    *,
    tool_name: str | None = None,
) -> Path:
    """Write what an apply did: the plan's reasons and groups with each action's outcome."""
    chosen = format_for(path, fmt)
    return _write_report(_result_report(plan, result, tool_name), path, chosen)


# ----- CSV and the legacy log ----------------------------------------------------------------


def _write_csv(report: _Report, target: Path) -> None:
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([c.header for c in report.columns])
        for entry in report.entries:
            writer.writerow([c.value(entry) for c in report.columns])


LOG_HEADER = [
    "moved_file",
    "moved_to",
    "original_name",
    "kept_original",
    "match_type",
    "original_path",
    "kept_path",
]


def _match_type(entry: _Entry) -> str:
    """The duplicate finder's match type, read back from the reason text it wrote."""
    reason = entry.reason
    for prefix, name in (
        ("identical bytes", "exact"),
        ("pHash", "visual"),
        ("burst", "burst"),
        ("same footage", "re-encode"),
    ):
        if reason.startswith(prefix):
            return name
    return ""


def _name(text: str) -> str:
    return Path(text).name if text else ""


def _write_log(report: _Report, target: Path) -> None:
    """The old duplicate-log CSV. Planned rows are all listed. Run rows only if they were done."""
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(LOG_HEADER)
        for entry in report.entries:
            if not entry.src or (report.applied and entry.status != "done"):
                continue
            if entry.dst:
                moved_file, moved_to = _name(entry.dst), _name(str(Path(entry.dst).parent))
            else:
                moved_file, moved_to = _name(entry.src), "Recycle Bin"
            writer.writerow(
                [
                    moved_file,
                    moved_to,
                    _name(entry.src),
                    _name(entry.group),
                    _match_type(entry),
                    entry.src,
                    entry.group,
                ]
            )


# ----- the legacy review list (txt) ----------------------------------------------------------


def _txt_verb(entry: _Entry, applied: bool) -> str:
    if applied and entry.status != "done":
        detail = f": {entry.error}" if entry.error else ""
        return f"{entry.status or 'not done'}{detail}"
    lead = "" if applied else "would "
    if entry.kind == "trash":
        return f"{lead}{'sent' if applied else 'send'} to the Recycle Bin"
    if entry.kind == "delete":
        return f"{lead}{'deleted' if applied else 'delete'} permanently"
    if entry.dst:
        words = {"move": ("moved to", "move to"), "rename": ("renamed to", "rename to")}
        past, future = words.get(entry.kind, (f"{entry.kind} to", f"{entry.kind} to"))
        return f"{lead}{past if applied else future}: {entry.dst}"
    return f"{lead}{entry.kind}"


def _txt_sections(report: _Report) -> list[tuple[str, list[_Entry]]]:
    if report.tool_id == "duplicates":
        wanted = (
            ("TO DUPLICATE", ("exact", "visual")),
            ("TO REVIEW", ("burst", "re-encode")),
        )
        sections: list[tuple[str, list[_Entry]]] = []
        known = {kind for _, kinds in wanted for kind in kinds}
        for title, kinds in wanted:
            sections.append((title, [e for e in report.entries if _match_type(e) in kinds]))
        other = [e for e in report.entries if _match_type(e) not in known]
        if other:
            sections.append(("OTHER ACTIONS", other))
        return sections
    order: dict[str, list[_Entry]] = {}
    for entry in report.entries:
        order.setdefault(entry.kind.upper(), []).append(entry)
    return list(order.items())


def _write_txt(report: _Report, target: Path) -> None:
    lines: list[str] = [f"{report.tool_name} - review list", ""]
    for label, value in report.facts:
        if label != "Roots" and value:
            lines.append(f"{label:<8}: {value}")
    mode = "APPLIED (the changes were made)" if report.applied else "DRY RUN (nothing was changed)"
    lines.append(f"{'Mode':<8}: {mode}")
    roots = next((v for k, v in report.facts if k == "Roots" and v), "")
    if roots:
        lines.append("")
        lines.append("Folders scanned:")
        lines.extend(f"  {root}" for root in roots.split("; "))
    for note in report.notes:
        lines.append("")
        lines.append(note)
    for title, section in _txt_sections(report):
        lines += ["", "=" * 70, title, "=" * 70]
        if not section:
            lines.append("(none)")
        for entry in section:
            tag = _match_type(entry) if report.tool_id == "duplicates" else entry.kind
            suffix = "" if entry.selected in (None, True) else "  (not selected)"
            lines.append(f"[{tag or entry.kind}] {entry.src or entry.dst}{suffix}")
            lines.append(f"    {_txt_verb(entry, report.applied)}")
            if entry.group:
                lines.append(f"    keeps   : {entry.group}")
            if entry.reason:
                lines.append(f"    why     : {entry.reason}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----- XLSX ----------------------------------------------------------------------------------


def _write_xlsx(report: _Report, target: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    actions: Any = workbook.active
    actions.title = "Actions"
    bold = Font(bold=True)

    def put(sheet: Any, row: int, column: int, value: Cell, *, header: bool = False) -> None:
        cell = sheet.cell(row=row, column=column, value=value)
        if isinstance(value, str):
            cell.data_type = "s"  # a path or reason starting with "=" must stay text
        if header:
            cell.font = bold

    for col, column in enumerate(report.columns, start=1):
        put(actions, 1, col, column.header, header=True)
    widths = [len(c.header) for c in report.columns]
    for row, entry in enumerate(report.entries, start=2):
        for col, column in enumerate(report.columns, start=1):
            value = column.value(entry)
            put(actions, row, col, value)
            widths[col - 1] = max(widths[col - 1], len(str(value)))
    for col, width in enumerate(widths, start=1):
        actions.column_dimensions[get_column_letter(col)].width = min(max(width + 2, 8), 80)
    actions.freeze_panes = "A2"
    if report.entries:
        actions.auto_filter.ref = (
            f"A1:{get_column_letter(len(report.columns))}{len(report.entries) + 1}"
        )

    summary: Any = workbook.create_sheet("Summary")
    rows: list[tuple[str, str | int]] = [
        ("Tool", report.tool_name),
        ("Tool id", report.tool_id),
        *report.facts,
        *report.counts,
    ]
    rows += [("Note", n) for n in report.notes]
    rows.append(("Options", _pretty_options(report.options_json)))
    summary.column_dimensions["A"].width = 18
    summary.column_dimensions["B"].width = 100
    for row, (label, value) in enumerate(rows, start=1):
        put(summary, row, 1, label, header=True)
        put(summary, row, 2, value)

    with target.open("wb") as handle:
        workbook.save(handle)


# ----- HTML ----------------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#1b1f23; --muted:#59636e; --line:#d0d7de;
  --head:#f3f4f6; --accent:#0b5cad; }
@media (prefers-color-scheme: dark) { :root { --bg:#14181d; --fg:#e6e9ec; --muted:#9aa5b1;
  --line:#2f3740; --head:#1f252c; --accent:#6cb2ff; } }
body { background:var(--bg); color:var(--fg); font:14px/1.45 "Segoe UI", system-ui, sans-serif;
  margin:0 auto; max-width:1280px; padding:16px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:24px 0 8px; }
table { border-collapse:collapse; width:100%; margin:6px 0 16px; }
th, td { border:1px solid var(--line); padding:4px 8px; text-align:left; vertical-align:top;
  overflow-wrap:anywhere; }
th { background:var(--head); } td.num { text-align:right; white-space:nowrap; }
dl.facts { display:grid; grid-template-columns:max-content 1fr; gap:2px 16px; margin:8px 0; }
dl.facts dt { color:var(--muted); } dl.facts dd { margin:0; overflow-wrap:anywhere; }
.thumb { max-width:160px; max-height:160px; display:block; }
.placeholder { color:var(--muted); font-style:italic; }
.group { border:1px solid var(--line); border-radius:6px; padding:8px 12px; margin:12px 0; }
.keeper { display:flex; gap:12px; align-items:flex-start; margin-bottom:6px; }
.badge { background:var(--accent); color:var(--bg); border-radius:4px; padding:0 6px;
  font-size:12px; }
pre { background:var(--head); padding:8px; overflow:auto; }
@media (max-width:700px) { body { padding:8px; } }
"""


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


class _Thumbs:
    """Makes thumbnails lazily, once per path, up to :data:`MAX_THUMBNAILS`."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._cache: dict[str, str] = {}
        self._made = 0

    def html_for(self, text: str) -> str:
        if not self.enabled or not text:
            return ""
        if text not in self._cache:
            self._cache[text] = self._build(Path(text))
        return self._cache[text]

    def _build(self, path: Path) -> str:
        # Imported here so that the CSV and Excel exports never load Pillow.
        from various_tools.core.media import thumbs

        if self._made >= MAX_THUMBNAILS:
            return f'<span class="placeholder">{NO_PREVIEW} (preview limit reached)</span>'
        facts = thumbs.file_facts(path)
        if facts.cloud_only:
            return f'<span class="placeholder">{CLOUD_PLACEHOLDER}</span>'
        if not facts.exists:
            return '<span class="placeholder">missing</span>'
        data = thumbs.thumbnail_jpeg(path, THUMB_PX)
        if data is None:
            return f'<span class="placeholder">{NO_PREVIEW}</span>'
        self._made += 1
        encoded = base64.b64encode(data).decode("ascii")
        return f'<img class="thumb" alt="" src="data:image/jpeg;base64,{encoded}">'


def _html_cell(value: Cell) -> str:
    numeric = isinstance(value, int)
    return f'<td class="num">{_e(value)}</td>' if numeric else f"<td>{_e(value)}</td>"


def _html_table(report: _Report, entries: Sequence[_Entry], thumbs: _Thumbs | None = None) -> str:
    head = "".join(f"<th>{_e(c.header)}</th>" for c in report.columns)
    if thumbs is not None and thumbs.enabled:
        head = "<th>Preview</th>" + head
    rows: list[str] = []
    for entry in entries:
        cells = "".join(_html_cell(c.value(entry)) for c in report.columns)
        if thumbs is not None and thumbs.enabled:
            preview = thumbs.html_for(entry.src) if entry.preview else ""
            cells = f"<td>{preview}</td>" + cells
        rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _html_groups(report: _Report, thumbs: _Thumbs) -> str:
    groups: dict[str, list[_Entry]] = {}
    loose: list[_Entry] = []
    for entry in report.entries:
        if entry.group:
            groups.setdefault(entry.group, []).append(entry)
        else:
            loose.append(entry)
    parts: list[str] = []
    for number, (keeper, members) in enumerate(groups.items(), start=1):
        found = thumbs.html_for(keeper)
        keeper_thumb = f"<div>{found}</div>" if found else ""
        parts.append(
            '<section class="group">'
            f"<h3>Group {number}: {len(members)} duplicate(s)</h3>"
            '<div class="keeper">'
            f"{keeper_thumb}"
            f'<div><span class="badge">kept</span> {_e(keeper)}</div></div>'
            f"{_html_table(report, members, thumbs)}</section>"
        )
    if loose:
        parts.append(f"<h2>Other actions</h2>{_html_table(report, loose, thumbs)}")
    return "".join(parts)


def _write_html(report: _Report, target: Path) -> None:
    thumbs = _Thumbs(report.thumbnails)
    facts = [("Tool", report.tool_name), *report.facts, *report.counts]
    facts_html = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in facts if str(v) != "")
    notes_html = ""
    if report.notes:
        items = "".join(f"<li>{_e(n)}</li>" for n in report.notes)
        notes_html = f"<h2>Notes</h2><ul>{items}</ul>"
    grouped = report.tool_id == "duplicates" and any(e.group for e in report.entries)
    body = _html_groups(report, thumbs) if grouped else _html_table(report, report.entries)
    page = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(report.title)}</title><style>{_CSS}</style></head><body>"
        f"<h1>{_e(report.title)}</h1>"
        f'<dl class="facts">{facts_html}</dl>{notes_html}'
        f"<h2>Actions ({len(report.entries)})</h2>{body}"
        "<details><summary>Options</summary>"
        f"<pre>{_e(_pretty_options(report.options_json))}</pre></details>"
        "</body></html>"
    )
    target.write_text(page, encoding="utf-8")
