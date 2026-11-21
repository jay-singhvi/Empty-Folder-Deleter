"""Saved recipes: a named, ordered list of steps, each a tool id plus that tool's options.

A recipe runs on the working folder, one step after the other. Each step is scanned, shown to
the person (the GUI) or to ``confirm_step`` (the CLI), applied only when they say yes, and
journaled as its own run, so History can undo every step. Every step's run carries
``"_recipe": {"name", "run_id", "step", "steps"}`` in the journal's copy of its options (see
``RunContext.recipe_tag``), which is how History finds the steps of one recipe run. The marker is
never part of a tool's options, so exports and re-runs validate as before.

Storage is ``recipes.json`` next to the settings file, with a format version, written through a
temporary file. A file that cannot be read is renamed aside (``recipes.json.bad-<time>``) and the
problem is reported; it is never silently replaced. A step whose tool is missing or whose options
no longer validate is "needs attention" and blocks the run until it is fixed.

The workspace option (the folder the tool looks in) is not stored. It is filled in at run time
from the working folder, unless the step's options hold folders of their own (the tool page's
"Use other folders"), which are then kept.

No Qt here, no ``print``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from various_tools.core.context import Cancelled, RunContext
from various_tools.core.fs import ProtectedPaths
from various_tools.core.journal import Journal, RunRecord, UndoReport, undo_run
from various_tools.core.jsonutil import as_dict, as_list
from various_tools.core.plan import Plan, RunResult
from various_tools.core.schema import describe
from various_tools.tools.base import Tool, ToolOptions

log = logging.getLogger(__name__)

RECIPES_FILE = "recipes.json"
FORMAT_VERSION = 1
RECIPE_KEY = "_recipe"
WORKSPACE_FIELD_NAMES = frozenset({"roots", "sources", "root"})
# The option value that means "delete for good". A saved recipe never keeps it: the step falls
# back to the field's default, so a permanent delete is always chosen again on purpose.
PERMANENT = "permanent"


# ----- the data -------------------------------------------------------------------------------


@dataclass
class RecipeStep:
    tool: str
    options: dict[str, Any] = field(default_factory=dict[str, Any])
    label: str = ""

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"tool": self.tool, "options": self.options}
        if self.label:
            data["label"] = self.label
        return data


@dataclass
class Recipe:
    name: str
    steps: list[RecipeStep] = field(default_factory=list[RecipeStep])

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "steps": [s.to_json() for s in self.steps]}


@dataclass
class LoadedRecipes:
    """What :func:`load_recipes` found. ``notice`` is set when the file was unreadable."""

    recipes: list[Recipe] = field(default_factory=list[Recipe])
    notice: str | None = None


def recipes_path(settings_path: Path) -> Path:
    """``recipes.json`` in the same folder as the settings file."""
    return settings_path.parent / RECIPES_FILE


def _parse_step(raw: object) -> RecipeStep:
    data = as_dict(raw)
    tool = data.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ValueError("a step has no tool")
    options: object = data.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("a step's options are not a table")
    label = data.get("label", "")
    return RecipeStep(
        tool, as_dict(cast("object", options)), label if isinstance(label, str) else ""
    )


def _parse(text: str) -> list[Recipe]:
    data = as_dict(json.loads(text))
    if data.get("format") != FORMAT_VERSION:
        raise ValueError(f"unknown format {data.get('format')!r}")
    recipes: list[Recipe] = []
    for raw in as_list(data.get("recipes")):
        entry = as_dict(raw)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a recipe has no name")
        recipes.append(Recipe(name, [_parse_step(s) for s in as_list(entry.get("steps"))]))
    return recipes


def load_recipes(path: Path, *, now: datetime | None = None) -> LoadedRecipes:
    """Read ``recipes.json``. A missing file is an empty list.

    A file that cannot be read is renamed to ``recipes.json.bad-<time>`` and the result's
    ``notice`` says so, so the person can recover it by hand. It is never dropped silently.
    """
    if not path.is_file():
        return LoadedRecipes()
    try:
        return LoadedRecipes(_parse(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        aside = path.with_name(f"{path.name}.bad-{stamp}")
        try:
            os.replace(path, aside)
            where = f"It was kept as {aside.name}."
        except OSError:
            where = "It could not be renamed, so it was left where it is."
        message = f"The recipes file could not be read ({exc}). {where}"
        log.warning("%s", message)
        return LoadedRecipes(notice=message)


def save_recipes(path: Path, recipes: Iterable[Recipe]) -> Path:
    """Write the recipes through a temporary file, so a crash never leaves a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        {"format": FORMAT_VERSION, "recipes": [r.to_json() for r in recipes]},
        indent=2,
        ensure_ascii=False,
    )
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


# ----- steps and options ----------------------------------------------------------------------


def workspace_field(tool: Tool) -> str | None:
    """The option that receives the working folder (the same rule the tool page uses)."""
    folders = [
        s.name
        for s in describe(tool.Options)
        if s.widget in ("folder", "folders")
        or (
            s.widget != "file"
            and (s.kind == "path" or (s.kind == "list" and s.item_kind == "path"))
        )
    ]
    if hasattr(tool, "workspace_field"):
        name = getattr(tool, "workspace_field")  # noqa: B009 (an optional capability)
        return str(name) if name is not None and str(name) in folders else None
    if folders and folders[0] in WORKSPACE_FIELD_NAMES:
        return folders[0]
    return None


def _is_list_field(tool: Tool, name: str) -> bool:
    return any(s.name == name and s.kind == "list" for s in describe(tool.Options))


def uses_own_folders(step: RecipeStep, tool: Tool) -> bool:
    """The step keeps folders of its own instead of taking the working folder."""
    name = workspace_field(tool)
    return name is not None and bool(step.options.get(name))


def step_values(step: RecipeStep, tool: Tool, folders: Sequence[Path]) -> dict[str, Any]:
    """The step's options with the working folder put into the workspace option."""
    values = dict(step.options)
    name = workspace_field(tool)
    if name is None or values.get(name):
        return values
    paths = [str(p) for p in folders]
    values[name] = paths if _is_list_field(tool, name) else (paths[0] if paths else "")
    return values


def step_options(step: RecipeStep, tool: Tool, folders: Sequence[Path]) -> ToolOptions:
    """Validated options for a run. Raises ``pydantic.ValidationError`` when they no longer fit."""
    return tool.Options.model_validate(step_values(step, tool, folders))


def check_step(step: RecipeStep, tools: Mapping[str, Tool]) -> str | None:
    """None when the step can run, else why it "needs attention" (in plain words)."""
    tool = tools.get(step.tool)
    if tool is None:
        return f"The tool {step.tool!r} is not available any more."
    try:
        step_options(step, tool, [Path(".")])
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return f"The saved options no longer fit {tool.name}: {where}: {first['msg']}."
    return None


def check_recipe(recipe: Recipe, tools: Mapping[str, Tool]) -> list[str | None]:
    """One entry per step: None when it is fine, else the problem."""
    return [check_step(step, tools) for step in recipe.steps]


def step_from_options(tool: Tool, values: Mapping[str, Any], *, own_folders: bool) -> RecipeStep:
    """A step from a tool page's current option values.

    The workspace option is dropped unless the page uses other folders, and a permanent-delete
    value is replaced by the option's default (see :data:`PERMANENT`).
    """
    name = workspace_field(tool)
    defaults = {s.name: s.default for s in describe(tool.Options)}
    kept: dict[str, Any] = {}
    for key, value in values.items():
        if key == name and not own_folders:
            continue
        if value == PERMANENT:
            if key in defaults:
                kept[key] = json.loads(json.dumps(defaults[key], default=str))
            continue
        kept[key] = value
    return RecipeStep(tool.id, kept)


def step_title(step: RecipeStep, tools: Mapping[str, Tool]) -> str:
    tool = tools.get(step.tool)
    base = tool.name if tool is not None else step.tool
    return f"{base}: {step.label}" if step.label else base


# ----- the marker in History ------------------------------------------------------------------


def new_recipe_run_id() -> str:
    return uuid.uuid4().hex


def recipe_tag(name: str, run_id: str, step: int, steps: int) -> dict[str, object]:
    """The marker stored under ``"_recipe"``. ``step`` counts from 1."""
    return {"name": name, "run_id": run_id, "step": step, "steps": steps}


@dataclass(frozen=True)
class RecipeMark:
    name: str
    run_id: str
    step: int
    steps: int

    @property
    def label(self) -> str:
        return f"Recipe: {self.name}, step {self.step} of {self.steps}"


def parse_recipe_mark(options_json: str) -> RecipeMark | None:
    """The marker of a journaled run, or None for a run that did not come from a recipe."""
    try:
        data = as_dict(json.loads(options_json))
    except ValueError:
        return None
    raw = as_dict(data.get(RECIPE_KEY))
    name, run_id, step, steps = (raw.get(k) for k in ("name", "run_id", "step", "steps"))
    if not (isinstance(name, str) and isinstance(run_id, str) and run_id):
        return None
    if not (isinstance(step, int) and isinstance(steps, int)):
        return None
    return RecipeMark(name, run_id, step, steps)


def strip_recipe_mark(options_json: str) -> str:
    """The options JSON without the marker (what a tool's options model accepts)."""
    try:
        data = json.loads(options_json)
    except ValueError:
        return options_json
    if not isinstance(data, dict) or RECIPE_KEY not in data:
        return options_json
    data.pop(RECIPE_KEY)  # pyright: ignore[reportUnknownMemberType]
    return json.dumps(data, sort_keys=True)


def recipe_run_members(runs: Iterable[RunRecord], recipe_run_id: str) -> list[RunRecord]:
    """The journal runs of one recipe run, newest first (the order they are undone in)."""
    members = [
        r
        for r in runs
        if (mark := parse_recipe_mark(r.options_json)) is not None and mark.run_id == recipe_run_id
    ]
    return sorted(members, key=lambda r: r.id, reverse=True)


def undo_recipe_run(
    journal: Journal,
    run_ids: Sequence[int],
    *,
    protection: ProtectedPaths | None = None,
    can_undo: Callable[[int], str | None] | None = None,
) -> list[tuple[int, UndoReport | str]]:
    """Undo the given runs in the order given (newest first). One entry per run: its
    :class:`UndoReport`, or a sentence saying why it was left alone.

    ``can_undo(run_id)`` returns None when the run may be undone now, else the reason it may not
    (for example "still applying"); such a run is skipped and the others still go on.
    """
    results: list[tuple[int, UndoReport | str]] = []
    for run_id in run_ids:
        reason = can_undo(run_id) if can_undo is not None else None
        if reason is not None:
            results.append((run_id, reason))
            continue
        try:
            results.append((run_id, undo_run(journal, run_id, protection=protection)))
        except Exception as exc:  # reported per step; the other steps still get their turn
            results.append((run_id, f"could not be undone: {exc}"))
    return results


# ----- running --------------------------------------------------------------------------------

ConfirmStep = Callable[[RecipeStep, Plan], bool]


@dataclass
class StepOutcome:
    index: int  # from 1
    tool: str
    status: str  # applied, declined, nothing, failed, cancelled, blocked
    message: str = ""
    run_id: int | None = None


@dataclass
class RecipeRun:
    recipe_run_id: str
    outcomes: list[StepOutcome] = field(default_factory=list[StepOutcome])

    @property
    def completed(self) -> bool:
        return bool(self.outcomes) and all(o.status == "applied" for o in self.outcomes)


def scan_step(
    step: RecipeStep, tool: Tool, ctx: RunContext, folders: Sequence[Path], *, select_all: bool
) -> Plan:
    """Scan one step on the given folders (never changes the disk)."""
    plan = tool.scan(step_options(step, tool, folders), ctx)
    if select_all:
        plan.select_all()
    return plan


def apply_step(plan: Plan, tool: Tool, ctx: RunContext, tag: Mapping[str, object]) -> RunResult:
    """Apply one scanned step as its own journaled run, marked with the recipe tag."""
    ctx.recipe_tag = tag
    try:
        return tool.apply(plan, ctx)
    finally:
        ctx.recipe_tag = None


def result_failed(result: RunResult, selected: int) -> bool:
    return bool(result.count("failed") or (selected and not result.count("done")))


def run_recipe(
    recipe: Recipe,
    ctx: RunContext,
    confirm_step: ConfirmStep,
    *,
    tools: Mapping[str, Tool],
    folders: Sequence[Path],
    select_all: bool = False,
    recipe_run_id: str | None = None,
) -> RecipeRun:
    """Run the steps in order: scan, ``confirm_step(step, plan)``, apply when it says yes.

    Stops at a refusal (``confirm_step`` said no), a step with nothing selected, a cancel, a
    failed step, or a step that needs attention (checked for every step before anything runs, so
    nothing is applied by a recipe that cannot finish). ``ctx`` needs a journal.
    """
    run = RecipeRun(recipe_run_id or new_recipe_run_id())
    total = len(recipe.steps)
    problems = check_recipe(recipe, tools)
    if any(problems):
        first = next(i for i, p in enumerate(problems) if p)
        run.outcomes.append(
            StepOutcome(first + 1, recipe.steps[first].tool, "blocked", str(problems[first]))
        )
        return run
    for index, step in enumerate(recipe.steps, start=1):
        tool = tools[step.tool]
        outcome = StepOutcome(index, step.tool, "applied")
        run.outcomes.append(outcome)
        try:
            plan = scan_step(step, tool, ctx, folders, select_all=select_all)
            selected = len(plan.selected_actions())
            if not selected:
                outcome.status, outcome.message = "nothing", "No action is selected."
                break
            if not confirm_step(step, plan):
                outcome.status, outcome.message = "declined", "Not applied."
                break
            tag = recipe_tag(recipe.name, run.recipe_run_id, index, total)
            result = apply_step(plan, tool, ctx, tag)
        except Cancelled:
            outcome.status = "cancelled"
            break
        except Exception as exc:  # a failed step ends the recipe; the earlier steps stay applied
            outcome.status, outcome.message = "failed", f"{type(exc).__name__}: {exc}"
            break
        outcome.run_id = result.run_id
        if result.status == "cancelled":
            outcome.status = "cancelled"
            break
        if result_failed(result, selected):
            outcome.status = "failed"
            outcome.message = f"{result.count('failed')} action(s) failed."
            break
    return run


# ----- the starter recipes --------------------------------------------------------------------


def starter_recipes() -> list[Recipe]:
    """Offered on the empty page, never created without a click."""
    return [
        Recipe("Tidy a folder", [RecipeStep("empty_files"), RecipeStep("empty_folders")]),
        Recipe(
            "Prepare photos to share",
            [
                RecipeStep("media_resize"),
                RecipeStep("metadata_strip", {"output": "new_folder", "in_place": False}),
            ],
        ),
        Recipe("Fix photo names", [RecipeStep("wrong_extension"), RecipeStep("rename")]),
    ]
