"""``various-tools recipe list | show NAME | run NAME PATHS... [--apply]``.

A recipe is a saved list of steps (see ``core/recipe.py``). Without ``--apply``, ``run`` scans
the first step only, because later steps depend on what the earlier ones change. With ``--apply``
every step is scanned, shown, confirmed (``--yes`` skips the question) and applied in turn as its
own journaled run, so ``history`` and ``undo`` work per step. The folders to work on are given on
the command line, because the command line has no working folder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from various_tools.cli.render import permanent_delete_requested, print_plan, print_result
from various_tools.cli.runner import run_job
from various_tools.cli.state import (
    EXIT_CANCELLED,
    EXIT_FAILED,
    CliState,
    confirm,
    fail,
    get_state,
    note,
    out,
    usage_error,
)
from various_tools.cli.toolcmds import (
    _apply_exit_code,  # pyright: ignore[reportPrivateUsage]
    _pace_from_flags,  # pyright: ignore[reportPrivateUsage]
    confirm_locked,
    download_actions,
    locked_changes,
    option_values,
)
from various_tools.core.recipe import (
    Recipe,
    RecipeStep,
    apply_step,
    check_recipe,
    load_recipes,
    new_recipe_run_id,
    recipe_tag,
    recipes_path,
    scan_step,
    step_options,
    step_title,
    uses_own_folders,
)
from various_tools.core.schema import describe
from various_tools.tools.base import Tool, discover

recipe_app = typer.Typer(
    help="Saved recipes: a named list of steps, each a tool with its options.",
    no_args_is_help=True,
)


def _load(state: CliState) -> list[Recipe]:
    loaded = load_recipes(recipes_path(state.config_path))
    if loaded.notice:
        note(loaded.notice)
    return loaded.recipes


def _find(recipes: list[Recipe], name: str) -> Recipe:
    wanted = name.strip().casefold()
    for recipe in recipes:
        if recipe.name.casefold() == wanted:
            return recipe
    known = ", ".join(r.name for r in recipes) or "none saved yet"
    usage_error(f"No recipe is named {name!r}. Recipes: {known}.")


@recipe_app.command("list")
def list_recipes(ctx: typer.Context) -> None:
    """List the saved recipes."""
    recipes = _load(get_state(ctx))
    console = out()
    if not recipes:
        console.print("No recipes yet. Create them on the Recipes page of the app.")
        return
    tools = discover()
    table = Table(title="Recipes", title_justify="left")
    table.add_column("Name", no_wrap=True)
    table.add_column("Steps", justify="right", no_wrap=True)
    table.add_column("Tools", overflow="fold")
    for recipe in recipes:
        table.add_row(
            recipe.name,
            str(len(recipe.steps)),
            " > ".join(step_title(s, tools) for s in recipe.steps),
        )
    console.print(table)


@recipe_app.command("show")
def show_recipe(ctx: typer.Context, name: Annotated[str, typer.Argument()]) -> None:
    """Show the steps of a recipe and whether each one can run."""
    recipe = _find(_load(get_state(ctx)), name)
    tools = discover()
    problems = check_recipe(recipe, tools)
    console = out()
    console.print(f"Recipe: {escape(recipe.name)}")
    if not recipe.steps:
        console.print("  (no steps)")
    for number, (step, problem) in enumerate(zip(recipe.steps, problems, strict=True), start=1):
        folders = " [own folders]" if _own_folders(step, tools) else ""
        console.print(f"  {number}. {escape(step_title(step, tools))}{folders}")
        if step.options:
            shown = ", ".join(f"{k}={v}" for k, v in sorted(step.options.items()))
            console.print(f"     options: {escape(shown)}")
        if problem:
            console.print(f"     [bold yellow]needs attention:[/bold yellow] {escape(problem)}")


def _own_folders(step: RecipeStep, tools: dict[str, Tool]) -> bool:
    tool = tools.get(step.tool)
    return tool is not None and uses_own_folders(step, tool)


@recipe_app.command("run")
def run_recipe_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The recipe to run.")],
    paths: Annotated[
        list[str] | None,
        typer.Argument(metavar="PATHS", help="The folder or folders to work on."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Apply each step after scanning it. Without it only the first step is scanned.",
        ),
    ] = False,
    select_all: Annotated[
        bool,
        typer.Option(
            "--select-all",
            help="Select every action of every step before applying (still asks first).",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Do not ask for confirmation (for scripts).")
    ] = False,
    allow_locked: Annotated[
        bool,
        typer.Option(
            "--allow-locked", help="Accept changed \U0001f512 locked options without asking."
        ),
    ] = False,
    batch_size: Annotated[
        int | None,
        typer.Option("--batch-size", metavar="N", help="Apply N actions, then wait (--pause)."),
    ] = None,
    pause: Annotated[
        str | None,
        typer.Option(
            "--pause", metavar="DURATION", help="Wait between batches: 90, 30s, 5m or 1h."
        ),
    ] = None,
    wait_for_upload: Annotated[
        bool,
        typer.Option(
            "--wait-for-upload", help="After each batch, wait until OneDrive has uploaded it."
        ),
    ] = False,
    upload_timeout: Annotated[
        str | None,
        typer.Option("--upload-timeout", metavar="DURATION", help="Give up waiting after this."),
    ] = None,
) -> None:
    """Run a recipe on the given folders (a dry run unless --apply)."""
    state = get_state(ctx)
    pace = _pace_from_flags(batch_size, pause, wait_for_upload, upload_timeout)
    recipe = _find(_load(state), name)
    tools = discover()
    if not recipe.steps:
        fail(f"The recipe {recipe.name!r} has no steps.")
    problems = check_recipe(recipe, tools)
    if any(problems):
        lines = [f"step {i}: {p}" for i, p in enumerate(problems, start=1) if p]
        fail("The recipe needs attention before it can run: " + " ".join(lines))
    folders = [Path(p) for p in paths or []]
    if not folders and not all(_own_folders(s, tools) for s in recipe.steps):
        usage_error("Give the folder or folders to work on: various-tools recipe run NAME PATHS")
    console = out()
    recipe_run_id = new_recipe_run_id()
    total = len(recipe.steps)
    for number, step in enumerate(recipe.steps, start=1):
        tool = tools[step.tool]
        specs = describe(tool.Options)
        opts = step_options(step, tool, folders)
        confirm_locked(locked_changes(specs, option_values(opts, specs)), allow=allow_locked)
        console.print(f"[bold]Step {number} of {total}: {escape(step_title(step, tools))}[/bold]")
        plan = run_job(
            state,
            lambda c, step=step, tool=tool: scan_step(
                step, tool, c, folders, select_all=select_all
            ),
            label="Scanning",
            with_journal=False,
        ).unwrap()
        selected = plan.selected_actions()
        if not apply:
            print_plan(console, plan)
            console.print(
                "Dry run: nothing was changed. Only the first step is scanned, because "
                f"the other {total - 1} step(s) depend on what it changes. Add --apply to run "
                "every step."
                if total > 1
                else "Dry run: nothing was changed. Add --apply to apply it."
            )
            return
        if not selected:
            print_plan(console, plan, show_table=False)
            console.print(
                "Nothing to apply: no action is selected. The recipe stops here."
                + (" Add --select-all to apply every action." if plan.actions else "")
            )
            return
        print_plan(console, plan, show_table=not yes)
        permanent = permanent_delete_requested(plan)
        downloads = download_actions(plan)
        if downloads:
            console.print(
                f"[bold yellow]Downloads:[/bold yellow] this will download {len(downloads)} "
                "cloud-only file(s) from OneDrive to this device as part of this step."
            )
        if not confirm(
            f"Apply {len(selected)} action(s) with {tool.name} (step {number} of {total})?",
            yes=yes,
        ):
            note("Nothing more was changed. The recipe stops here.")
            raise typer.Exit(EXIT_FAILED)
        if permanent and not confirm(
            "PERMANENT DELETE: these items will be erased and cannot be restored from the "
            "Recycle Bin. Are you sure?",
            yes=yes,
        ):
            note("Nothing more was changed. The recipe stops here.")
            raise typer.Exit(EXIT_FAILED)
        tag = recipe_tag(recipe.name, recipe_run_id, number, total)
        outcome = run_job(
            state,
            lambda c, plan=plan, tool=tool, tag=tag: apply_step(plan, tool, c, tag),
            label="Applying",
            with_journal=True,
            pace=pace,
        )
        if outcome.value is not None:
            print_result(console, outcome.value)
        if outcome.cancelled:
            note("Cancelled. The recipe stops here.")
            raise typer.Exit(EXIT_CANCELLED)
        result = outcome.unwrap()
        code = _apply_exit_code(result, len(selected))
        if code:
            note(f"Step {number} did not finish cleanly. The recipe stops here.")
            raise typer.Exit(code)
    console.print(f"Recipe {escape(recipe.name)} finished: {total} step(s) applied.")
