"""Plain-language words for the tool page and the plan table.

The app is meant to be usable by someone who has never heard of a "plan" or an "action kind",
so the GUI shows these words instead of the raw names. CLI output and exports keep the raw
names, because they are a file format. No Qt here.

Tools may set two optional attributes (read with ``getattr``):

* ``scan_label``: the scan button's text, for example "Look for empty folders";
* ``found_nouns``: ``(singular, plural)`` for what a scan finds, for example
  ``("empty folder", "empty folders")``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from various_tools.core.plan import ActionKind, Plan
from various_tools.gui.plan_edit import human_size

APPLY_TEXT = "Apply…"
DEFAULT_SCAN_LABEL = "Look for files"
DEFAULT_NOUNS = ("item", "items")

KIND_LABELS: dict[ActionKind, str] = {
    ActionKind.TRASH: "Move to Recycle Bin",
    ActionKind.MOVE: "Move",
    ActionKind.RENAME: "Rename",
    ActionKind.DELETE: "Delete forever",
    ActionKind.WRITE: "Create file",
    ActionKind.CONVERT: "Convert",
}


def kind_label(kind: ActionKind | str) -> str:
    """The plain word for an action kind; an unknown kind is shown as it is."""
    try:
        return KIND_LABELS[ActionKind(kind)]
    except ValueError:
        return str(kind)


def _items(count: int, singular: str = "item", plural: str = "items") -> str:
    return f"{count} {singular if count == 1 else plural}"


def apply_label(plan: Plan | None) -> str:
    """The Apply button's text: what the checked actions will do, with how many."""
    if plan is None:
        return APPLY_TEXT
    counts = Counter(a.kind for a in plan.selected_actions())
    total = sum(counts.values())
    if not total:
        return APPLY_TEXT
    if len(counts) > 1:
        return f"Apply {_items(total, 'change', 'changes')}…"
    kind = next(iter(counts))
    if kind is ActionKind.TRASH:
        return f"Move {total} to the Recycle Bin…"
    if kind is ActionKind.DELETE:
        return f"Delete {_items(total)} forever…"
    if kind is ActionKind.WRITE:
        return f"Create {_items(total, 'file', 'files')}…"
    verb = {ActionKind.MOVE: "Move", ActionKind.RENAME: "Rename", ActionKind.CONVERT: "Convert"}
    return f"{verb.get(kind, 'Apply')} {_items(total)}…"


def scan_label(tool: object) -> str:
    return str(getattr(tool, "scan_label", DEFAULT_SCAN_LABEL))


def found_nouns(tool: object) -> tuple[str, str]:
    nouns = getattr(tool, "found_nouns", DEFAULT_NOUNS)
    singular, plural = nouns
    return str(singular), str(plural)


def summary_text(plan: Plan, sizes: Sequence[int | None], nouns: tuple[str, str]) -> str:
    """One plain sentence about what a scan found."""
    count = len(plan.actions)
    if not count:
        return "Nothing found. Nothing needs to change."
    text = f"Found {_items(count, *nouns)}"
    known = [s for s in sizes if s is not None]
    if known:
        text += f" ({human_size(sum(known))})"
    return text + ". Only ticked rows are changed, and you confirm first."
