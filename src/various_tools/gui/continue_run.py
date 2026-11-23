"""Continue a stopped apply: drop the rows that are done and keep the rest of the list.

Pure helpers, no Qt. After a Stop (or an apply that ended with failures) the page keeps its list.
The rows that were done are removed; the rows that were not reached, and the ones that failed,
stay so that they can be applied again as a new journaled run.

Done rows are matched by the action's kind and source. A row without a source (a ``write``) is
matched by its kind and its place in the list, because an outcome records the name the file
really got (a free name is picked when the planned one is taken), not the planned one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from various_tools.core.plan import ActionKind, ActionOutcome, Plan, PlannedAction

# Tools whose rows depend on each other while they apply, so that a list with the done rows
# removed would not apply the same way. They fall back to "scan again":
# * organize_by_date, but only when its list has resize or download rows (``convert``): a
#   resize row only runs together with its move row (the applier follows the move), and a
#   download row is tied to the files that wait for it. A list of moves alone (the default
#   options) continues: each move stands on its own.
# * wrong_extension: a sidecar row is renamed only after its file's row was done in the same
#   run (it follows the name the file really got), so without it the sidecar is skipped.
NO_CONTINUE_TOOLS = frozenset({"wrong_extension"})
MOVES_ONLY_TOOLS = frozenset({"organize_by_date"})

CONTINUE_STATUSES = frozenset({"cancelled", "partial"})


def can_continue(tool_id: str, status: str, plan: Plan | None = None) -> bool:
    """Whether a run that ended with ``status`` can be continued for this tool and list."""
    if status not in CONTINUE_STATUSES or tool_id in NO_CONTINUE_TOOLS:
        return False
    if tool_id in MOVES_ONLY_TOOLS:
        return plan is not None and all(a.kind is ActionKind.MOVE for a in plan.actions)
    return True


def _key(kind: str, src: str | None) -> tuple[str, str | None]:
    return kind, src


def drop_done(
    plan: Plan, outcomes: Sequence[ActionOutcome], sizes: Sequence[int | None]
) -> list[int | None]:
    """Remove from ``plan`` the checked rows that an outcome says were done.

    Returns ``sizes`` for the rows that are left (``sizes[i]`` belongs to ``plan.actions[i]``).
    A row is removed once per done outcome, so two identical rows are not both removed by one.
    Skipped and failed outcomes keep their rows.
    """
    done = Counter(_key(o.kind, o.src) for o in outcomes if o.status == "done" and o.src)
    # A row without a source is found by its place: ``seq`` counts the checked rows from 1.
    checked = [i for i, a in enumerate(plan.actions) if a.selected]
    done_places: set[int] = set()
    for o in outcomes:
        if o.status == "done" and o.src is None and 1 <= o.seq <= len(checked):
            index = checked[o.seq - 1]
            if str(plan.actions[index].kind) == o.kind and plan.actions[index].src is None:
                done_places.add(index)
    kept_actions: list[PlannedAction] = []
    kept_sizes: list[int | None] = []
    for index, action in enumerate(plan.actions):
        if action.src is None:
            if index in done_places:
                continue
            kept_actions.append(action)
            kept_sizes.append(sizes[index] if index < len(sizes) else None)
            continue
        key = _key(str(action.kind), str(action.src))
        if action.selected and done[key] > 0:
            done[key] -= 1
            continue
        kept_actions.append(action)
        kept_sizes.append(sizes[index] if index < len(sizes) else None)
    plan.actions = kept_actions
    return kept_sizes
