"""Plain-language words for the tool page and the plan table (no Qt)."""

from __future__ import annotations

from pathlib import Path

from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.gui.wording import (
    apply_label,
    found_nouns,
    kind_label,
    scan_label,
    summary_text,
)


def _plan(*kinds: ActionKind, selected: bool = True) -> Plan:
    actions = [
        PlannedAction(kind=k, src=Path(f"C:/x/{i}"), selected=selected) for i, k in enumerate(kinds)
    ]
    return Plan(tool="t", roots=[Path("C:/x")], actions=actions)


def test_apply_label_without_a_selection() -> None:
    assert apply_label(None) == "Apply…"
    assert apply_label(_plan(ActionKind.TRASH, selected=False)) == "Apply…"


def test_apply_label_per_kind() -> None:
    assert apply_label(_plan(*[ActionKind.TRASH] * 12)) == "Move 12 to the Recycle Bin…"
    assert apply_label(_plan(ActionKind.MOVE)) == "Move 1 item…"
    assert apply_label(_plan(*[ActionKind.MOVE] * 3)) == "Move 3 items…"
    assert apply_label(_plan(ActionKind.RENAME, ActionKind.RENAME)) == "Rename 2 items…"
    assert apply_label(_plan(ActionKind.DELETE)) == "Delete 1 item forever…"
    assert apply_label(_plan(ActionKind.WRITE, ActionKind.WRITE)) == "Create 2 files…"
    assert apply_label(_plan(ActionKind.CONVERT, ActionKind.CONVERT)) == "Convert 2 items…"


def test_apply_label_mixed_kinds_counts_only_selected() -> None:
    plan = _plan(*[ActionKind.TRASH] * 3, ActionKind.MOVE, ActionKind.MOVE)
    plan.actions.append(PlannedAction(kind=ActionKind.MOVE, selected=False))
    assert apply_label(plan) == "Apply 5 changes…"


def test_summary_text() -> None:
    nouns = ("empty folder", "empty folders")
    plan = _plan(*[ActionKind.TRASH] * 12)
    sizes: list[int | None] = [3 * 1024 * 1024] + [None] * 11
    assert summary_text(plan, sizes, nouns) == (
        "Found 12 empty folders (3.0 MB). Only ticked rows are changed, and you confirm first."
    )
    one = _plan(ActionKind.TRASH)
    assert summary_text(one, [None], nouns) == (
        "Found 1 empty folder. Only ticked rows are changed, and you confirm first."
    )
    assert summary_text(_plan(), [], nouns) == "Nothing found. Nothing needs to change."


def test_kind_label() -> None:
    assert kind_label("trash") == "Move to Recycle Bin"
    assert kind_label(ActionKind.DELETE) == "Delete forever"
    assert kind_label(ActionKind.WRITE) == "Create file"
    assert kind_label("odd") == "odd"


def test_tool_words_fall_back() -> None:
    class Bare:
        pass

    class Worded:
        scan_label = "Find duplicates"
        found_nouns = ("duplicate", "duplicates")

    assert scan_label(Bare()) == "Look for files"
    assert found_nouns(Bare()) == ("item", "items")
    assert scan_label(Worded()) == "Find duplicates"
    assert found_nouns(Worded()) == ("duplicate", "duplicates")
