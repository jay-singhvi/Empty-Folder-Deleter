"""``various-tools config``: show, get, set and unset settings.

Keys are the :class:`~various_tools.core.settings.Settings` field names. The saved paths of the
external programs also have the plan §3.5 form ``external_tools.<program>.path`` (or just
``external_tools.<program>``). Every change is validated by ``Settings`` before it is written, and
a settings file that cannot be read is never replaced.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Annotated, Any, cast

import typer
from pydantic import ValidationError
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from various_tools.cli.state import (
    EXIT_FAILED,
    CliState,
    confirm,
    fail,
    get_state,
    note,
    out,
    read_settings_file,
    usage_error,
)
from various_tools.core.deps import TOOL_SPECS
from various_tools.core.schema import FieldSpec, describe, format_value, parse_value
from various_tools.core.settings import Settings, save_settings

config_app = typer.Typer(
    name="config",
    help="Show and change settings: config show | get KEY | set KEY VALUE | unset KEY.",
    no_args_is_help=True,
    add_completion=False,
)

_SPECS: dict[str, FieldSpec] = {spec.name: spec for spec in describe(Settings)}
_TOOLS_FIELD = "external_tools"


@dataclass(frozen=True)
class Key:
    """A resolved settings key: a field, or one program inside ``external_tools``."""

    field: str
    program: str | None = None

    @property
    def text(self) -> str:
        return self.field if self.program is None else f"{self.field}.{self.program}.path"


def resolve_key(raw: str) -> Key:
    """Map user text to a :class:`Key`, or exit with a helpful message."""
    key = raw.strip()
    if key in _SPECS:
        return Key(key)
    if key.startswith(f"{_TOOLS_FIELD}."):
        parts = key.split(".")
        program = parts[1] if len(parts) > 1 else ""
        suffix = parts[2:]
        allowed_suffix = ([], ["path"], ["url"]) if program == "ollama" else ([], ["path"])
        if program in TOOL_SPECS and suffix in allowed_suffix:
            return Key(_TOOLS_FIELD, program)
        usage_error(
            f"Unknown program in {raw!r}. Use external_tools.<program>.path with one of: "
            f"{', '.join(TOOL_SPECS)}."
        )
    close = difflib.get_close_matches(key, [*_SPECS, "external_tools.exiftool.path"], n=3)
    hint = f" Did you mean {', '.join(close)}?" if close else ""
    usage_error(f"Unknown setting {raw!r}.{hint} List the keys with: various-tools config show")


def _raw(state: CliState) -> dict[str, Any]:
    try:
        return dict(read_settings_file(state.config_path))
    except ValueError as exc:
        fail(f"{exc}. Fix or delete the file first.", 2)


def _validate_only(raw: dict[str, Any]) -> Settings:
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        usage_error(
            "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        )


def _commit(state: CliState, raw: dict[str, Any]) -> Settings:
    """Validate ``raw`` as settings and write it. Exits 2 when it is not valid."""
    settings = _validate_only(raw)
    try:
        save_settings(settings, state.config_path)
    except OSError as exc:
        fail(f"cannot write {state.config_path}: {exc.strerror or exc}")
    return settings


def _current(settings: Settings, key: Key) -> Any:
    value = getattr(settings, key.field)
    if key.program is None:
        return value
    return cast("dict[str, str]", value).get(key.program)


def _shown(value: Any) -> str:
    return format_value(value)


@config_app.command("show")
def show(ctx: typer.Context) -> None:
    """Show every setting: its value, its default, what it does, and a lock for safety values."""
    state = get_state(ctx)
    settings = state.saved_settings()
    defaults = Settings()
    table = Table(title="Settings", title_justify="left", show_lines=True)
    table.add_column("Key", no_wrap=True)
    table.add_column("Value", overflow="fold", ratio=2)
    table.add_column("Default", overflow="fold", ratio=2)
    table.add_column("About", overflow="fold", ratio=5)
    for name, spec in _SPECS.items():
        value = _shown(getattr(settings, name))
        default = _shown(getattr(defaults, name))
        changed = value != default
        about = Text()
        if spec.locked:
            about.append("\U0001f512 ", style="bold")
        about.append(spec.title, style="bold")
        if spec.description:
            about.append(f"\n{spec.description}")
        table.add_row(
            Text(name, style="bold" if changed else ""),
            Text(value, style="yellow" if changed else ""),
            Text(default),
            about,
        )
    out().print(table)
    out().print(
        f"Settings file: {escape(str(state.config_path))}"
        + ("" if state.config_path.is_file() else " (not created yet)")
    )
    out().print(
        "Change with: various-tools config set KEY VALUE. "
        "Program paths: external_tools.<program>.path (exiftool, ffmpeg, ffprobe)."
    )


@config_app.command("get")
def get(
    ctx: typer.Context, key: Annotated[str, typer.Argument(metavar="KEY", help="Setting key.")]
) -> None:
    """Print one setting's value (nothing else, so scripts can use it)."""
    state = get_state(ctx)
    resolved = resolve_key(key)
    value = _current(state.saved_settings(), resolved)
    if resolved.program is not None and not value:
        note(f"{resolved.text} is not set (the program is auto-detected).")
        raise typer.Exit(EXIT_FAILED)
    typer.echo(_shown(value))


@config_app.command("set")
def set_(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(metavar="KEY", help="Setting key.")],
    value: Annotated[str, typer.Argument(metavar="VALUE", help="The new value.")],
    yes: Annotated[
        bool, typer.Option("--yes", help="Do not ask before changing a locked (safety) setting.")
    ] = False,
) -> None:
    """Change a setting. The value is checked before it is saved."""
    state = get_state(ctx)
    resolved = resolve_key(key)
    spec = _SPECS[resolved.field]
    raw = _raw(state)
    if resolved.program is None:
        try:
            parsed = parse_value(spec, value)
        except ValueError as exc:
            usage_error(
                f"Invalid value for {resolved.text}: {str(exc).removeprefix(spec.name + ': ')}"
            )
        if parsed is None:
            raw.pop(resolved.field, None)
        else:
            raw[resolved.field] = parsed
    else:
        table = dict(cast("dict[str, str]", raw.get(_TOOLS_FIELD) or {}))
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] == '"':
            cleaned = cleaned[1:-1]
        table[resolved.program] = cleaned
        raw[_TOOLS_FIELD] = table
    candidate = _validate_only(raw)
    changes_default = _current(candidate, resolved) != _current(Settings(), resolved)
    if (
        spec.locked
        and changes_default
        and not confirm(
            f"{resolved.text} is a locked safety setting ({spec.title}). Change it anyway?",
            yes=yes,
        )
    ):
        note("Nothing was changed.")
        raise typer.Exit(EXIT_FAILED)
    saved = _commit(state, raw)
    out().print(f"{escape(resolved.text)} = {escape(_shown(_current(saved, resolved)))}")


@config_app.command("unset")
def unset(
    ctx: typer.Context, key: Annotated[str, typer.Argument(metavar="KEY", help="Setting key.")]
) -> None:
    """Put a setting back to its default (a program path goes back to auto-detect)."""
    state = get_state(ctx)
    resolved = resolve_key(key)
    raw = _raw(state)
    if resolved.program is None:
        existed = raw.pop(resolved.field, None) is not None
    else:
        table = dict(cast("dict[str, str]", raw.get(_TOOLS_FIELD) or {}))
        existed = table.pop(resolved.program, None) is not None
        if table:
            raw[_TOOLS_FIELD] = table
        else:
            raw.pop(_TOOLS_FIELD, None)
    if not existed:
        out().print(f"{escape(resolved.text)} was already at its default.")
        return
    _commit(state, raw)
    out().print(f"{escape(resolved.text)} is back to its default.")
