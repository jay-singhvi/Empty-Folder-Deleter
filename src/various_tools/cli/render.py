"""Rich tables and summaries. All user data goes through ``Text`` so brackets in file names are
shown as they are and never read as markup."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from various_tools.core.deps import ToolLocation
from various_tools.core.journal import ActionRecord, RunRecord
from various_tools.core.plan import ActionKind, ActionOutcome, Plan, RunResult

MAX_TABLE_ROWS = 500
MAX_LISTED = 100
_REVERSIBLE = frozenset({"move", "rename"})


def _cell(value: object) -> Text:
    return Text("" if value is None else str(value), overflow="fold")


def plan_table(plan: Plan, limit: int = MAX_TABLE_ROWS) -> Table:
    table = Table(title=f"Plan for {plan.tool}", title_justify="left", show_lines=False)
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Source", overflow="fold", ratio=3)
    table.add_column("Destination", overflow="fold", ratio=3)
    table.add_column("Reason", overflow="fold", ratio=2)
    table.add_column("Selected", justify="center", no_wrap=True)
    for number, action in enumerate(plan.actions[:limit], start=1):
        table.add_row(
            str(number),
            str(action.kind),
            _cell(action.src),
            _cell(action.dst),
            _cell(action.reason),
            "yes" if action.selected else "no",
        )
    hidden = len(plan.actions) - limit
    if hidden > 0:
        table.caption = f"... {hidden} more action(s) not shown. Use --json or --save-plan."
    return table


def kind_counts(plan: Plan) -> str:
    counts = Counter(str(a.kind) for a in plan.selected_actions())
    return ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))


def print_plan(console: Console, plan: Plan, *, show_table: bool = True) -> None:
    """The actions, the notes and the totals. Changes nothing."""
    if show_table and plan.actions:
        console.print(plan_table(plan))
    if plan.notes:
        console.print("[bold]Notes[/bold]")
        for text in plan.notes[:MAX_LISTED]:
            console.print(Text(f"  - {text}"))
        if len(plan.notes) > MAX_LISTED:
            console.print(f"  ... {len(plan.notes) - MAX_LISTED} more note(s) (see --json)")
    selected = plan.selected_actions()
    if plan.actions:
        detail = f" ({kind_counts(plan)})" if selected else ""
        console.print(
            f"[bold]{len(plan.actions)}[/bold] action(s), "
            f"[bold]{len(selected)}[/bold] selected{escape(detail)}."
        )
    else:
        console.print("No actions planned.")


def permanent_delete_requested(plan: Plan) -> bool:
    """True when applying would erase items for good (not send them to the Recycle Bin)."""
    selected = plan.selected_actions()
    if not selected:
        return False
    if any(a.kind is ActionKind.DELETE for a in selected):
        return True
    try:
        options: Any = json.loads(plan.options_json)
    except ValueError:
        return False
    return isinstance(options, dict) and options.get("delete_mode") == "permanent"  # pyright: ignore[reportUnknownMemberType]


def outcomes_table(outcomes: Iterable[ActionOutcome], title: str) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Source", overflow="fold", ratio=3)
    table.add_column("Why", overflow="fold", ratio=3)
    for outcome in outcomes:
        table.add_row(str(outcome.seq), outcome.kind, _cell(outcome.src), _cell(outcome.detail))
    return table


def print_result(console: Console, result: RunResult) -> None:
    done, failed, skipped = (result.count(s) for s in ("done", "failed", "skipped"))
    console.print(
        f"Run [bold]{result.run_id}[/bold] {result.status}: "
        f"{done} done, {failed} failed, {skipped} skipped."
    )
    problems = [o for o in result.outcomes if o.status == "failed"]
    if problems:
        console.print(outcomes_table(problems[:MAX_LISTED], "Failed"))
    refused = [o for o in result.outcomes if o.status == "skipped"]
    if refused:
        console.print(outcomes_table(refused[:MAX_LISTED], "Skipped"))
    if len(problems) > MAX_LISTED or len(refused) > MAX_LISTED:
        console.print(
            f"Only the first {MAX_LISTED} of each are shown. See: various-tools history "
            f"{result.run_id}"
        )
    if done:
        reversible = sum(1 for o in result.outcomes if o.status == "done" and o.kind in _REVERSIBLE)
        if reversible:
            console.print(f"Undo with: various-tools undo {result.run_id}")
        else:
            console.print(
                "Deleted items went to the Recycle Bin (or were erased); undo cannot move them "
                f"back. See: various-tools history {result.run_id}"
            )


def runs_table(runs: list[RunRecord], counts: dict[int, int]) -> Table:
    table = Table(title="History", title_justify="left")
    table.add_column("Run", justify="right", no_wrap=True)
    table.add_column("Tool", no_wrap=True)
    table.add_column("Started", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Actions", justify="right", no_wrap=True)
    for run in runs:
        table.add_row(str(run.id), run.tool, run.started, run.status, str(counts.get(run.id, 0)))
    return table


def actions_table(actions: list[ActionRecord], title: str) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Source", overflow="fold", ratio=3)
    table.add_column("Destination", overflow="fold", ratio=3)
    table.add_column("Note", overflow="fold", ratio=2)
    for action in actions:
        table.add_row(
            str(action.seq),
            action.kind,
            action.status,
            _cell(action.src),
            _cell(action.dst),
            _cell(action.error or action.reason),
        )
    return table


def doctor_status(location: ToolLocation) -> str:
    """found, invalid (a configured path or URL failed and nothing else worked) or missing."""
    if location.valid:
        return "found"
    return "invalid" if location.problems else "missing"


_STATUS_STYLE = {"found": "green", "missing": "yellow", "invalid": "red"}


def doctor_table(locations: Iterable[ToolLocation]) -> Table:
    table = Table(title="External programs", title_justify="left")
    table.add_column("Program", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Path or URL", overflow="fold", ratio=3)
    table.add_column("Version", overflow="fold")
    table.add_column("Found by", no_wrap=True)
    table.add_column("Install", overflow="fold")
    table.add_column("Problems", overflow="fold", ratio=2)
    for loc in locations:
        status = doctor_status(loc)
        where = loc.url if loc.tool == "ollama" and loc.valid and loc.url else loc.path
        table.add_row(
            loc.tool,
            Text(status, style=_STATUS_STYLE[status]),
            _cell(where if loc.valid else loc.path),
            _cell(loc.version),
            str(loc.source) if loc.valid else "",
            _cell("" if loc.valid else loc.install_hint),
            _cell("; ".join(loc.problems)),
        )
    return table
