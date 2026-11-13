"""Tool sub-commands, generated from each tool's options model (plan §3.3 and Phase 4).

For every discovered tool this builds ``<tool> scan``, ``<tool> apply`` and
``presets save <tool>``. The flags come from :func:`various_tools.core.schema.describe`, so a new
tool or a new option field needs no CLI code. Hand-written flags exist only for the shared
``--preset``, ``--json``, ``--save-plan``, ``--plan``, ``--yes``, ``--export`` and
``--export-format``.

How values travel: every generated flag defaults to ``None`` meaning "not given". Only the flags
the user typed are collected as overrides, and they are applied on top of the preset (if any) and
validated by the tool's own options model. Flag values are text, converted by ``parse_value``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from pydantic import ValidationError
from rich.markup import escape

from various_tools.cli.export_cmd import (
    check_export_target,
    export_plan_file,
    export_result_file,
)
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
from various_tools.core.plan import Plan, RunResult
from various_tools.core.schema import FieldSpec, describe, format_value, parse_value
from various_tools.tools.base import Tool, ToolOptions

# Flag names the CLI itself uses. A tool option with one of these names becomes ``--opt-<name>``.
RESERVED_FLAGS = frozenset(
    {
        "preset",
        "json",
        "save-plan",
        "plan",
        "yes",
        "export",
        "export-format",
        "config",
        "exiftool",
        "ffmpeg",
        "ffprobe",
        "ollama-url",
        "help",
    }
)
FOLDER_WIDGETS = ("folders", "folder")
_P = inspect.Parameter


@dataclass(frozen=True)
class Flag:
    """One generated command-line flag for one options field."""

    spec: FieldSpec
    flag: str  # for example ``--max-width``
    param: str  # the Python parameter name Typer passes the value in


def flag_for(field_name: str) -> str:
    base = field_name.replace("_", "-")
    return f"--opt-{base}" if base in RESERVED_FLAGS else f"--{base}"


def make_flags(specs: list[FieldSpec]) -> list[Flag]:
    return [Flag(spec, flag_for(spec.name), f"opt_{spec.name}") for spec in specs]


def folder_spec(specs: list[FieldSpec]) -> FieldSpec | None:
    """The field that positional PATHS fill: the first folder or folders field."""
    return next((s for s in specs if s.widget in FOLDER_WIDGETS), None)


# ----- help text -------------------------------------------------------------------------


def help_text(spec: FieldSpec) -> str:
    """Title, description, default, bounds and the lock marker, escaped for Rich."""
    parts: list[str] = []
    title = spec.title.strip().rstrip(".")
    description = spec.description.strip()
    parts.append(f"{title}." if title else "")
    if description:
        parts.append(description)
    if spec.required:
        parts.append("Required.")
    elif "default" not in description.casefold():
        shown = format_value(spec.default)
        if shown:
            parts.append(f"Default: {shown}.")
        elif spec.default is None:
            parts.append("Default: none (use the app setting).")
        else:
            parts.append("Default: empty.")
    if spec.has_bounds:
        parts.append(f"Allowed: {spec.bounds_text}.")
    if spec.kind == "list":
        parts.append("Repeat the flag or separate items with commas.")
    elif spec.kind == "dict":
        parts.append("Repeat the flag as KEY=VALUE.")
    if spec.locked and "🔒" not in description:
        parts.append("🔒 Locked default: change it only if you know why.")
    return escape(" ".join(p for p in parts if p))


def _metavar(spec: FieldSpec) -> str:
    if spec.kind in ("choice", "list") and spec.choices:
        return "|".join(spec.choices)
    kind = spec.item_kind if spec.kind in ("list", "dict") and spec.item_kind else spec.kind
    if spec.kind == "dict":
        return "KEY=VALUE"
    return {"int": "INT", "float": "FLOAT", "path": "PATH", "bool": "BOOL"}.get(kind, "TEXT")


def _flag_annotation(flag: Flag) -> Any:
    spec = flag.spec
    panel = "Advanced" if spec.advanced else spec.group
    help_ = help_text(spec)
    if spec.kind == "bool":
        decl = f"{flag.flag}/--no-{flag.flag[2:]}"
        return Annotated[
            bool | None,
            typer.Option(decl, help=help_, rich_help_panel=panel, show_default=False),
        ]
    multiple = spec.kind in ("list", "dict")
    return Annotated[
        list[str] | None if multiple else str | None,
        typer.Option(
            flag.flag,
            metavar=_metavar(spec),
            help=help_,
            rich_help_panel=panel,
            show_default=False,
        ),
    ]


def _param(name: str, annotation: Any, default: Any = None) -> inspect.Parameter:
    return _P(name, _P.KEYWORD_ONLY, annotation=annotation, default=default)


def _required(name: str, annotation: Any) -> inspect.Parameter:
    return _P(name, _P.KEYWORD_ONLY, annotation=annotation)


def _install[F: Callable[..., Any]](func: F, params: list[inspect.Parameter]) -> F:
    """Give ``func`` a signature built at run time, which is how Typer sees the generated flags."""
    cast("Any", func).__signature__ = inspect.Signature(params)
    func.__annotations__ = {p.name: p.annotation for p in params}
    return func


# ----- turning flags into options ----------------------------------------------------------


def _parse_flag(flag: Flag, raw: Any) -> Any:
    spec = flag.spec
    try:
        if spec.kind == "bool":
            return bool(raw)
        if spec.kind == "list":
            items: list[Any] = []
            for text in cast("list[str]", raw):
                parsed = parse_value(spec, text)
                if parsed is None:
                    return None
                items.extend(cast("list[Any]", parsed))
            return items
        if spec.kind == "dict":
            merged: dict[str, Any] = {}
            for text in cast("list[str]", raw):
                parsed = parse_value(spec, text)
                if parsed is None:
                    return None
                merged.update(cast("dict[str, Any]", parsed))
            return merged
        return parse_value(spec, cast("str", raw))
    except ValueError as exc:
        message = str(exc).removeprefix(f"{spec.name}: ")
        usage_error(f"Invalid value for {flag.flag}: {message}")


def collect_overrides(
    flags: list[Flag],
    values: Mapping[str, Any],
    paths: list[str] | None,
    folders: FieldSpec | None,
) -> dict[str, Any]:
    """The options the user gave explicitly: flags that were typed, plus the positional PATHS."""
    given: dict[str, Any] = {}
    for flag in flags:
        raw = values.get(flag.param)
        if raw is not None:
            given[flag.spec.name] = _parse_flag(flag, raw)
    if paths:
        if folders is None:  # the PATHS argument only exists when there is a folder field
            usage_error("This tool does not take folders as arguments.")
        name = folders.name
        if folders.kind == "list":
            given[name] = [*cast("list[Any]", given.get(name) or []), *paths]
        else:
            existing = given.get(name)
            if len(paths) > 1 or (existing and str(existing) != paths[0]):
                usage_error(f"{flag_for(name)} takes one folder; got several.")
            given[name] = paths[0]
    return given


def validation_messages(exc: ValidationError, flags: list[Flag], folders: FieldSpec | None) -> str:
    by_name = {f.spec.name: f for f in flags}
    lines: list[str] = []
    for error in exc.errors():
        loc = error["loc"]
        name = str(loc[0]) if loc else "options"
        flag = by_name.get(name)
        label = flag.flag if flag else name
        if error["type"] == "missing":
            hint = " (or give it as an argument)" if folders and folders.name == name else ""
            lines.append(f"{label} is required{hint}.")
        else:
            lines.append(f"{label}: {error['msg']}")
    return "\n".join(lines)


def build_options(
    tool: Tool,
    state: CliState,
    preset: str | None,
    overrides: dict[str, Any],
    flags: list[Flag],
    folders: FieldSpec | None,
) -> ToolOptions:
    """Preset values, then the explicit flags, validated by the tool's options model."""
    try:
        if preset is None:
            return tool.Options.model_validate(overrides)
        return state.presets().build_options(tool.Options, tool.id, preset, overrides)
    except KeyError:
        known = state.presets().names(tool.id)
        listing = f" Saved presets: {', '.join(known)}." if known else " No presets are saved."
        usage_error(f"No preset {preset!r} for {tool.id}.{listing}")
    except ValidationError as exc:
        usage_error(validation_messages(exc, flags, folders))


def _read_plan(path: str, tool: Tool) -> Plan:
    try:
        plan = Plan.from_json(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        usage_error(f"cannot read the plan file: {exc.strerror or exc}")
    except ValueError as exc:
        usage_error(f"{path} is not a usable plan: {exc}")
    if plan.tool != tool.id:
        usage_error(f"{path} is a plan for {plan.tool!r}, not {tool.id!r}.")
    return plan


# ----- generated commands -------------------------------------------------------------------


def _paths_param(tool: Tool, folders: FieldSpec | None) -> list[inspect.Parameter]:
    if folders is None:
        return []
    word = "Folders" if folders.kind == "list" else "The folder"
    annotation = Annotated[
        list[str] | None,
        typer.Argument(
            metavar="PATHS",
            help=escape(f"{word} to work on (fills {flag_for(folders.name)}). {folders.title}."),
        ),
    ]
    return [_param("paths", annotation)]


def _preset_param(extra: str) -> inspect.Parameter:
    return _param(
        "preset",
        Annotated[
            str | None,
            typer.Option("--preset", metavar="NAME", help=f"Start from a saved preset. {extra}"),
        ],
    )


def _scan_command(tool: Tool, flags: list[Flag], folders: FieldSpec | None) -> Callable[..., None]:
    def scan(**kw: Any) -> None:
        state = get_state(kw["ctx"])
        check_export_target(kw["export"], kw["export_format"])
        overrides = collect_overrides(flags, kw, kw.get("paths"), folders)
        opts = build_options(tool, state, kw["preset"], overrides, flags, folders)
        plan = run_job(
            state, lambda ctx: tool.scan(opts, ctx), label="Scanning", with_journal=False
        ).unwrap()
        if kw["save_plan"]:
            _save_plan(plan, kw["save_plan"])
        if kw["export"]:
            export_plan_file(plan, kw["export"], kw["export_format"], tool.name)
        if kw["as_json"]:
            typer.echo(plan.to_json())
            return
        console = out()
        print_plan(console, plan)
        console.print(
            "Dry run: nothing was changed. To apply, run the same command with "
            f"'{tool.id} apply', or save this plan with --save-plan FILE and use "
            "'apply --plan FILE'."
        )

    params = [
        _required("ctx", typer.Context),
        *_paths_param(tool, folders),
        _preset_param("Flags you type override the preset."),
        _param(
            "as_json",
            Annotated[
                bool, typer.Option("--json", help="Print the plan as JSON only (for scripts).")
            ],
            False,
        ),
        _param(
            "save_plan",
            Annotated[
                str | None,
                typer.Option(
                    "--save-plan",
                    metavar="FILE",
                    help="Write the plan to FILE. Apply it later with 'apply --plan FILE'.",
                ),
            ],
        ),
        *_export_params(),
        *[_param(f.param, _flag_annotation(f)) for f in flags],
    ]
    return _install(scan, params)


def _export_params() -> list[inspect.Parameter]:
    return [
        _param(
            "export",
            Annotated[
                str | None,
                typer.Option(
                    "--export",
                    metavar="FILE",
                    help="Also write a report to FILE (.csv, .xlsx, .html or .txt).",
                ),
            ],
        ),
        _param(
            "export_format",
            Annotated[
                str | None,
                typer.Option(
                    "--export-format",
                    metavar="FMT",
                    help="Report format: csv, xlsx, html, txt or log. Default: from the suffix.",
                ),
            ],
        ),
    ]


def _save_plan(plan: Plan, target: str) -> None:
    try:
        Path(target).write_text(plan.to_json(), encoding="utf-8")
    except OSError as exc:
        fail(f"cannot write the plan file: {exc.strerror or exc}")
    note(f"Plan saved to {target}")


def _apply_exit_code(result: RunResult, selected: int) -> int:
    if result.status == "cancelled":
        return EXIT_CANCELLED
    if result.count("failed") or (selected and not result.count("done")):
        return EXIT_FAILED
    return 0


def _apply_command(tool: Tool, flags: list[Flag], folders: FieldSpec | None) -> Callable[..., None]:
    def apply(**kw: Any) -> None:
        state = get_state(kw["ctx"])
        yes: bool = kw["yes"]
        check_export_target(kw["export"], kw["export_format"])
        overrides = collect_overrides(flags, kw, kw.get("paths"), folders)
        if kw["plan"]:
            if overrides or kw["preset"]:
                usage_error(
                    "--plan cannot be combined with option flags, PATHS or --preset. "
                    "Use the plan as saved, or drop --plan and give options."
                )
            plan = _read_plan(kw["plan"], tool)
        else:
            opts = build_options(tool, state, kw["preset"], overrides, flags, folders)
            plan = run_job(
                state, lambda ctx: tool.scan(opts, ctx), label="Scanning", with_journal=False
            ).unwrap()

        selected = plan.selected_actions()
        console = out()
        if not selected:
            print_plan(console, plan, show_table=False)
            console.print("Nothing to apply: no action is selected.")
            if kw["export"]:
                note("Nothing was exported: no action was applied.")
            return
        print_plan(console, plan, show_table=not yes)
        settings = state.settings()
        permanent = permanent_delete_requested(plan)
        if permanent and settings.delete_mode != "permanent":
            console.print(
                "[bold yellow]warning:[/bold yellow] this plan deletes permanently, but "
                "'delete_mode' in the settings is 'trash', so those actions will be skipped. "
                "Change it with: various-tools config set delete_mode permanent"
            )
        if not confirm(f"Apply {len(selected)} action(s) with {tool.name}?", yes=yes):
            note("Nothing was changed.")
            raise typer.Exit(EXIT_FAILED)
        if permanent and not confirm(
            "PERMANENT DELETE: these items will be erased and cannot be restored from the "
            "Recycle Bin. Are you sure?",
            yes=yes,
        ):
            note("Nothing was changed.")
            raise typer.Exit(EXIT_FAILED)

        outcome = run_job(
            state, lambda ctx: tool.apply(plan, ctx), label="Applying", with_journal=True
        )
        if outcome.value is not None:
            print_result(console, outcome.value)
            if kw["export"]:
                export_result_file(
                    plan, outcome.value, kw["export"], kw["export_format"], tool.name
                )
        if outcome.cancelled:
            note("Cancelled.")
            raise typer.Exit(EXIT_CANCELLED)
        result = outcome.unwrap()
        code = _apply_exit_code(result, len(selected))
        if code:
            raise typer.Exit(code)

    params = [
        _required("ctx", typer.Context),
        *_paths_param(tool, folders),
        _preset_param("Flags you type override the preset."),
        _param(
            "plan",
            Annotated[
                str | None,
                typer.Option(
                    "--plan",
                    metavar="FILE",
                    help="Apply a plan saved by 'scan --save-plan' instead of scanning again.",
                ),
            ],
        ),
        _param(
            "yes",
            Annotated[
                bool, typer.Option("--yes", help="Do not ask for confirmation (for scripts).")
            ],
            False,
        ),
        *_export_params(),
        *[_param(f.param, _flag_annotation(f)) for f in flags],
    ]
    return _install(apply, params)


def _save_preset_command(
    tool: Tool, flags: list[Flag], folders: FieldSpec | None
) -> Callable[..., None]:
    def save(**kw: Any) -> None:
        state = get_state(kw["ctx"])
        name: str = kw["name"].strip()
        if not name:
            usage_error("A preset needs a name.")
        overrides = collect_overrides(flags, kw, kw.get("paths"), folders)
        store = state.presets()
        base: dict[str, Any] = {}
        if kw["preset"]:
            try:
                base = store.get(tool.id, kw["preset"])
            except KeyError:
                usage_error(f"No preset {kw['preset']!r} for {tool.id} to start from.")
        values = {**base, **overrides}
        incomplete = False
        try:
            stored: Any = tool.Options.model_validate(values)
        except ValidationError as exc:
            if all(e["type"] == "missing" for e in exc.errors()):
                # A required option (for example a folder) can be given when the preset is used.
                stored, incomplete = values, True
            else:
                usage_error(validation_messages(exc, flags, folders))
        store.save(tool.id, name, stored)
        console = out()
        console.print(
            f"Saved preset {escape(repr(name))} for {tool.id} in {escape(str(store.path))}"
        )
        if incomplete:
            console.print(
                "It has no value yet for every required option; give those when you use it."
            )
        console.print(f"Use it with: various-tools {tool.id} scan --preset {escape(repr(name))}")

    params = [
        _required("ctx", typer.Context),
        _required(
            "name", Annotated[str, typer.Argument(metavar="NAME", help="The preset's name.")]
        ),
        *_paths_param(tool, folders),
        _preset_param("Start from this preset, then apply the flags you give."),
        *[_param(f.param, _flag_annotation(f)) for f in flags],
    ]
    return _install(save, params)


def build_tool_app(tool: Tool) -> typer.Typer:
    """The ``<tool>`` command group with ``scan`` and ``apply``."""
    specs = describe(tool.Options)
    flags = make_flags(specs)
    folders = folder_spec(specs)
    app = typer.Typer(
        name=tool.id,
        help=tool.description,
        no_args_is_help=True,
        add_completion=False,
    )
    app.command(
        "scan",
        help=(
            f"Preview what {tool.name} would do. Changes nothing: it lists the actions "
            "it would take. Save the plan with --save-plan to apply it later."
        ),
    )(_scan_command(tool, flags, folders))
    app.command(
        "apply",
        help=(
            f"Apply {tool.name}. Shows what will happen and asks first (use --yes to skip). "
            "Give --plan FILE, or the same options as scan."
        ),
    )(_apply_command(tool, flags, folders))
    return app


def build_preset_save_app(tools: Mapping[str, Tool]) -> typer.Typer:
    """``presets save <tool> NAME [options]``: one command per tool, with that tool's flags."""
    app = typer.Typer(
        name="save",
        help=escape("Save options as a named preset: presets save TOOL NAME [options]."),
        no_args_is_help=True,
        add_completion=False,
    )
    for tool in tools.values():
        specs = describe(tool.Options)
        app.command(
            tool.id,
            help=f"Save a preset for {tool.name}. Give the options you want it to remember.",
            rich_help_panel=tool.category,
        )(_save_preset_command(tool, make_flags(specs), folder_spec(specs)))
    return app
