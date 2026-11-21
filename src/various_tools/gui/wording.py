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


# ----- the working folder and the options summary -------------------------------------------

WORKSPACE_TITLE = "Working folder"
NO_WORKSPACE = "not chosen yet"
CHOOSE_WORKSPACE_FIRST = "Choose a working folder first (File menu, or Ctrl+O)."
USE_OTHER_FOLDERS = "Use other folders for this tool"
WORKSPACE_CHANGED = "The working folder changed. Look again to update the list."
CHANGE_SETTINGS = "Change settings"
HIDE_SETTINGS = "Hide settings"
SHOW_DETAILS = "Show details"
HIDE_DETAILS = "Hide details"


def looks_in_text(paths: Sequence[object], *, first_only: bool = False) -> str:
    """The one line on a tool page that says where it looks.

    ``first_only`` is for a tool whose folder option takes one folder: with several working
    folders it uses the first, and the line says so.
    """
    if first_only and len(paths) > 1:
        return (
            f"Looks in: the first working folder only ({paths[0]}); this tool uses one folder, "
            f"so the other {len(paths) - 1} are not used"
        )
    where = "; ".join(str(p) for p in paths) if paths else NO_WORKSPACE
    return f"Looks in: the working folder ({where})"


def plain_value(value: object) -> str:
    """An option value in plain words, for the folded options summary."""
    if value is None or value == "":
        return "not set"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list | tuple | set | frozenset):
        items = [plain_value(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        return ", ".join(items) if items else "none"
    return str(getattr(value, "value", value))


# ----- picking rows, applying in batches ---------------------------------------------------------

TICK_ALL = "Tick all"
UNTICK_ALL = "Untick all"
TICK_ALL_SHOWN = "Tick all shown"
UNTICK_ALL_SHOWN = "Untick all shown"
LIST_OUT_OF_DATE = "The list is out of date."
REFRESH_LIST = "Refresh"
REFRESH_LIST_TIP = "Show only the rows that match the Show choice now."
PAUSE_TEXT = "Pause"
RESUME_TEXT = "Resume"
SKIP_WAIT_TEXT = "Skip the wait"
STOP_TEXT = "Stop"

HOW_FAST = "How fast?"
ALL_AT_ONCE = "All at once"
IN_BATCHES = "In batches"
CUSTOM_PRESET = "Custom"
# (label, files per batch, seconds to wait): the choices of the Apply window's preset list.
PACE_PRESETS: tuple[tuple[str, int, int], ...] = (
    ("50, then wait 5 minutes (gentle on OneDrive)", 50, 300),
    ("100, then wait 1 minute", 100, 60),
    ("1, then wait 2 seconds", 1, 2),
)
ONEDRIVE_BATCH_REASON = (
    "This changes {count} files ({size}) in a OneDrive folder, so working in batches is "
    "suggested. OneDrive copes better when it is not flooded."
)


def _unit(count: int, singular: str) -> str:
    return f"{count} {singular}{'' if count == 1 else 's'}"


def format_wait(seconds: float) -> str:
    """A waiting time in plain words, for example ``3 hours 45 minutes`` or ``2 seconds``."""
    whole = max(0, round(seconds))
    if whole < 60:
        return _unit(whole, "second")
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(_unit(hours, "hour"))
    if minutes:
        parts.append(_unit(minutes, "minute"))
    if secs and not hours:
        parts.append(_unit(secs, "second"))
    return " ".join(parts)
