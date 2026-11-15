"""Tool sub-commands, generated from each tool's options model (plan §3.3 and Phase 4).

For every discovered tool this builds ``<tool> scan``, ``<tool> apply``, one
``<tool> <extra-id> PATH`` per item of the tool's optional ``extras`` and ``presets save <tool>``.
The flags come from :func:`various_tools.core.schema.describe`, so a new tool or a new option
field needs no CLI code. Hand-written flags exist only for the shared
``--preset``, ``--json``, ``--save-plan``, ``--plan``, ``--yes``, ``--select-all``, ``--export``,
``--export-format``, ``--force``, ``--allow-locked`` and the one-off program flags
(``--exiftool``, ``--ffmpeg``, ``--ffprobe``, ``--ollama-url``; plan §3.5).

How values travel: every generated flag defaults to ``None`` meaning "not given". Only the flags
the user typed are collected as overrides, and they are applied on top of the preset (if any) and
validated by the tool's own options model. Flag values are text, converted by ``parse_value``.

🔒 Locked options (tuned thresholds and safety values): when the final options differ from a
locked default, whether typed or loaded from a preset, the command lists them and asks. ``--yes``
never answers that question; only ``--allow-locked`` does. With nobody to answer, it refuses.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from pydantic import ValidationError
from rich.markup import escape

from various_tools.cli import clipboard
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
    err,
    fail,
    get_state,
    note,
    out,
    usage_error,
)
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult
from various_tools.core.schema import FieldSpec, describe, format_value, parse_value
from various_tools.tools.base import Tool, ToolOptions

log = logging.getLogger(__name__)

# Flag names the CLI itself uses. A tool option with one of these names becomes ``--opt-<name>``.
RESERVED_FLAGS = frozenset(
    {
        "preset",
        "json",
        "save-plan",
        "plan",
        "yes",
        "select-all",
        "export",
        "export-format",
        "force",
        "allow-locked",
        "scheduled",
        "config",
        "exiftool",
        "ffmpeg",
        "ffprobe",
        "ollama-url",
        "help",
    }
)
# Kinds that change files inside the plan's roots. A plan with any of them must name its roots.
ROOTED_KINDS = frozenset({ActionKind.MOVE, ActionKind.RENAME, ActionKind.TRASH, ActionKind.DELETE})
FOLDER_WIDGETS = ("folders", "folder")
# The hidden flag 'schedule add' puts on every task's command line (see ``schedule_cmd``).
SCHEDULED_FLAG = "--scheduled"
_P = inspect.Parameter


def is_download(action: PlannedAction) -> bool:
    """A row whose tick is the consent to download a cloud-only file (AGENTS.md rule 3).

    Structural, not by wording: a ``convert`` with a source and no destination. That is how a
    tool offers a cloud-only file for download at apply (organize_by_date's ``ask`` and
    ``download`` choices); every other convert names the file it writes.
    """
    return action.kind is ActionKind.CONVERT and action.src is not None and action.dst is None


def download_actions(plan: Plan) -> list[PlannedAction]:
    """The selected actions that download a cloud-only file from OneDrive when applied."""
    return [a for a in plan.selected_actions() if is_download(a)]


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
    if not plan.roots and any(a.kind in ROOTED_KINDS for a in plan.actions):
        usage_error(
            f"{path} has no 'roots'. A plan that moves, renames or deletes files must list the "
            "folders it may change, so nothing outside them is touched. Scan again and save the "
            "plan with --save-plan, or add the folders to 'roots'."
        )
    return plan


# ----- locked (🔒) options -------------------------------------------------------------------


def _same_value(value: Any, default: Any) -> bool:
    if value == default:
        return True
    return format_value(value) == format_value(default)  # list vs tuple, Path vs str


def locked_changes(
    specs: list[FieldSpec], values: Mapping[str, Any]
) -> list[tuple[FieldSpec, Any]]:
    """The locked fields whose value in ``values`` differs from the tuned default."""
    return [
        (spec, values[spec.name])
        for spec in specs
        if spec.locked and spec.name in values and not _same_value(values[spec.name], spec.default)
    ]


def option_values(opts: ToolOptions, specs: list[FieldSpec]) -> dict[str, Any]:
    return {spec.name: getattr(opts, spec.name) for spec in specs if hasattr(opts, spec.name)}


def originals_changes(
    specs: list[FieldSpec], values: Mapping[str, Any]
) -> list[tuple[FieldSpec, Any]]:
    """The ``replaces_originals`` fields whose value differs from the default: options that
    replace, remove or download the user's own files."""
    return [
        (spec, values[spec.name])
        for spec in specs
        if spec.replaces_originals
        and spec.name in values
        and not _same_value(values[spec.name], spec.default)
    ]


def _changes_text(changes: list[tuple[FieldSpec, Any]], word: str) -> str:
    return "\n".join(
        f"  {flag_for(spec.name)} = {format_value(value)} ({word}: {format_value(spec.default)})"
        for spec, value in changes
    )


def unattended_refusal(opts: ToolOptions, specs: list[FieldSpec]) -> str | None:
    """Why these options must never run unattended (a scheduled run), or None.

    Refused, in this order: a permanent ``delete_mode``; a 🔒 locked option changed from its
    tuned default; an option marked ``replaces_originals`` changed from its default (an
    in-place output, recycling originals, downloading cloud-only files). The text completes the
    sentence "the preset P ...".
    """
    if getattr(opts, "delete_mode", None) == "permanent":
        return (
            "deletes permanently (delete_mode = permanent). Scheduled runs only ever use the "
            "Recycle Bin. Save a preset with delete_mode trash instead."
        )
    values = option_values(opts, specs)
    locked = locked_changes(specs, values)
    if locked:
        return (
            "changes 🔒 locked options away from their tuned defaults, and a scheduled run "
            "cannot ask you about that. Save a preset without them:\n"
            + _changes_text(locked, "tuned default")
        )
    originals = originals_changes(specs, values)
    if originals:
        return (
            "replaces, removes or downloads original files, and a scheduled run would do that "
            "again on every run without asking (for example shrinking the same pictures again "
            "and recycling the previous version each time, until the true original is gone from "
            "the Recycle Bin). Save a preset without them:\n" + _changes_text(originals, "default")
        )
    return None


def confirm_locked(changes: list[tuple[FieldSpec, Any]], *, allow: bool) -> None:
    """List changed locked values and ask before going on. ``--yes`` never answers this.

    ``allow`` (``--allow-locked``) goes ahead without asking. With nobody to answer, refuse.
    """
    if not changes:
        return
    console = err()
    console.print("[bold yellow]\U0001f512 Locked values differ from their tuned defaults:[/]")
    for spec, value in changes:
        console.print(
            f"  {escape(flag_for(spec.name))} ({escape(spec.title)}): "
            f"{escape(format_value(value))} (default: {escape(format_value(spec.default))})"
        )
    if allow:
        note("Going ahead with them because of --allow-locked.")
        return
    try:
        accepted = typer.confirm("Use these locked values?", default=False, err=True)
    except typer.Abort:
        fail(
            "refusing to use changed locked values without confirmation because input is not "
            "interactive. Add --allow-locked if you really mean them (--yes is not enough)."
        )
    if not accepted:
        note("Nothing was changed. Drop those flags (or the preset) to use the tuned defaults.")
        raise typer.Exit(EXIT_FAILED)


# ----- after apply ----------------------------------------------------------------------------


def copy_result_to_clipboard(plan: Plan, result: RunResult) -> None:
    """``copy_to_clipboard``: put the file the run wrote on the clipboard (not a huge one)."""
    if not clipboard.wants_clipboard(plan):
        return
    written = clipboard.written_file(result)
    if written is None:
        note("Nothing was copied to the clipboard: the run wrote no file.")
        return
    read = clipboard.read_for_clipboard(written)
    if read.text is None:
        note(read.message)
        return
    if clipboard.set_clipboard_text(read.text):
        note(read.message)
    else:
        note(f"Could not copy {written.name} to the clipboard. The file is at {written}")


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


def _allow_locked_param() -> inspect.Parameter:
    return _param(
        "allow_locked",
        Annotated[
            bool,
            typer.Option(
                "--allow-locked",
                help=(
                    "Accept 🔒 locked options that differ from their tuned defaults without "
                    "asking. --yes does not do this."
                ),
            ),
        ],
        False,
    )


_PROGRAM_FLAGS = (
    ("one_exiftool", "--exiftool", "PATH", "Use this ExifTool for this run only (not saved)."),
    ("one_ffmpeg", "--ffmpeg", "PATH", "Use this ffmpeg for this run only (not saved)."),
    ("one_ffprobe", "--ffprobe", "PATH", "Use this ffprobe for this run only (not saved)."),
    ("one_ollama_url", "--ollama-url", "URL", "Use this Ollama address for this run only."),
)


def program_params() -> list[inspect.Parameter]:
    """``--exiftool PATH`` and friends on a command (plan §3.5: any command accepts them)."""
    return [
        _param(
            name,
            Annotated[
                str | None,
                typer.Option(
                    flag, metavar=metavar, help=help_, rich_help_panel="External programs"
                ),
            ],
        )
        for name, flag, metavar, help_ in _PROGRAM_FLAGS
    ]


def state_with_programs(state: CliState, kw: Mapping[str, Any]) -> CliState:
    """The run's state with the command's one-off program flags on top of the global ones."""
    return state.with_programs(
        exiftool=kw.get("one_exiftool"),
        ffmpeg=kw.get("one_ffmpeg"),
        ffprobe=kw.get("one_ffprobe"),
        ollama_url=kw.get("one_ollama_url"),
    )


def _scan_command(tool: Tool, flags: list[Flag], folders: FieldSpec | None) -> Callable[..., None]:
    specs = [f.spec for f in flags]

    def scan(**kw: Any) -> None:
        state = state_with_programs(get_state(kw["ctx"]), kw)
        check_export_target(kw["export"], kw["export_format"], force=kw["force"])
        overrides = collect_overrides(flags, kw, kw.get("paths"), folders)
        opts = build_options(tool, state, kw["preset"], overrides, flags, folders)
        confirm_locked(locked_changes(specs, option_values(opts, specs)), allow=kw["allow_locked"])
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
        _allow_locked_param(),
        *program_params(),
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
        _param(
            "force",
            Annotated[
                bool,
                typer.Option("--force", help="Overwrite the --export file if it already exists."),
            ],
            False,
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
    specs = [f.spec for f in flags]

    def apply(**kw: Any) -> None:
        state = state_with_programs(get_state(kw["ctx"]), kw)
        scheduled: bool = kw["scheduled"]
        yes: bool = kw["yes"] or scheduled  # a scheduled run has nobody to answer
        check_export_target(kw["export"], kw["export_format"], force=kw["force"])
        overrides = collect_overrides(flags, kw, kw.get("paths"), folders)
        if scheduled and (kw["plan"] or overrides or not kw["preset"] or kw["select_all"]):
            usage_error(
                "--scheduled runs exactly one saved --preset, with no --plan, option flags, "
                "PATHS or --select-all. It is added by 'schedule add'."
            )
        if kw["plan"]:
            if overrides or kw["preset"]:
                usage_error(
                    "--plan cannot be combined with option flags, PATHS or --preset. "
                    "Use the plan as saved, or drop --plan and give options."
                )
            plan = _read_plan(kw["plan"], tool)
        else:
            opts = build_options(tool, state, kw["preset"], overrides, flags, folders)
            if scheduled:
                # The preset (or the settings) may have changed since 'schedule add' checked it,
                # so the same rules run again now, before anything is scanned or changed.
                refusal = unattended_refusal(opts, specs)
                if refusal is not None:
                    fail(f"refusing this scheduled run: the preset {kw['preset']!r} {refusal}")
            confirm_locked(
                locked_changes(specs, option_values(opts, specs)), allow=kw["allow_locked"]
            )
            plan = run_job(
                state, lambda ctx: tool.scan(opts, ctx), label="Scanning", with_journal=False
            ).unwrap()

        if kw["select_all"]:
            plan.select_all()
        selected = plan.selected_actions()
        if scheduled and permanent_delete_requested(plan):
            fail(
                "refusing this scheduled run: the plan deletes permanently, and a scheduled run "
                "only ever uses the Recycle Bin. Nothing was changed."
            )
        console = out()
        if not selected:
            print_plan(console, plan, show_table=False)
            console.print("Nothing to apply: no action is selected.")
            if plan.actions:
                console.print(
                    f"The plan has {len(plan.actions)} unselected action(s). Edit 'selected' in a "
                    "saved plan, or add --select-all to apply all of them."
                )
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
        downloads = download_actions(plan)
        if downloads:
            console.print(
                f"[bold yellow]Downloads:[/bold yellow] this will download {len(downloads)} "
                "cloud-only file(s) from OneDrive to this device as part of this apply. That "
                "uses disk space and network."
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
            copy_result_to_clipboard(plan, outcome.value)
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
        _param(
            "select_all",
            Annotated[
                bool,
                typer.Option(
                    "--select-all",
                    help=(
                        "Select every action before applying, including ones the tool left "
                        "unselected (for example the largest-files list). Still asks first."
                    ),
                ),
            ],
            False,
        ),
        *_export_params(),
        _allow_locked_param(),
        _param(
            "scheduled",
            Annotated[
                bool,
                typer.Option(
                    SCHEDULED_FLAG,
                    hidden=True,
                    help=(
                        "Set by 'schedule add' instead of --yes: answers the question, and "
                        "refuses anything a scheduled run must not do."
                    ),
                ),
            ],
            False,
        ),
        *program_params(),
        *[_param(f.param, _flag_annotation(f)) for f in flags],
    ]
    return _install(apply, params)


def _save_preset_command(
    tool: Tool, flags: list[Flag], folders: FieldSpec | None
) -> Callable[..., None]:
    specs = [f.spec for f in flags]

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
        checked = values if incomplete else option_values(stored, specs)
        confirm_locked(locked_changes(specs, checked), allow=kw["allow_locked"])
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
        _allow_locked_param(),
        *[_param(f.param, _flag_annotation(f)) for f in flags],
    ]
    return _install(save, params)


RESERVED_SUBCOMMANDS = frozenset({"scan", "apply", "explain"})


def _extra_command(extra: Any) -> Callable[..., None]:
    """``<tool> <extra-id> PATH``: run one of the tool's extras (``tool.extras``)."""

    def run_extra(**kw: Any) -> None:
        state = state_with_programs(get_state(kw["ctx"]), kw)
        target = Path(kw["path"])
        outcome = run_job(
            state,
            lambda ctx: str(extra.run(target, ctx)),
            label=str(extra.label),
            with_journal=False,
        )
        if isinstance(outcome.error, ValueError):
            fail(str(outcome.error))
        out().print(escape(outcome.unwrap()))

    verb = "Where to write" if extra.path_kind == "save_file" else "The file to read"
    params = [
        _required("ctx", typer.Context),
        _required(
            "path",
            Annotated[str, typer.Argument(metavar="PATH", help=escape(f"{verb}."))],
        ),
        *program_params(),
    ]
    return _install(run_extra, params)


def add_extra_commands(app: typer.Typer, tool: Tool) -> list[str]:
    """One sub-command per item of the tool's optional ``extras``. Returns their names.

    An extra whose id is not usable as a command name, or clashes with a built-in sub-command,
    is skipped (and logged), so a tool cannot hide ``scan`` or ``apply``.
    """
    added: list[str] = []
    for extra in tuple(getattr(tool, "extras", ()) or ()):
        name = str(getattr(extra, "id", ""))
        if not name or name in RESERVED_SUBCOMMANDS or name in added or " " in name:
            log.error("Skipping extra %r of %s: not a usable command name", name, tool.id)
            continue
        app.command(name, help=escape(f"{extra.label}. {extra.description}"))(_extra_command(extra))
        added.append(name)
    return added


def build_tool_app(tool: Tool) -> typer.Typer:
    """The ``<tool>`` command group with ``scan``, ``apply`` and the tool's extras."""
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
    add_extra_commands(app, tool)
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
