"""Shared CLI plumbing: the per-invocation state, output consoles, errors and confirmation.

Only ``cli/`` prints. The core and the tools log and report progress through ``RunContext``.
Consoles are created per call, so they follow whatever ``sys.stdout`` is at that moment (the
test runner swaps it).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NoReturn

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

from various_tools.core.presets import PresetStore
from various_tools.core.settings import Settings, default_settings_path

EXIT_OK = 0
EXIT_FAILED = 1  # a failure, a refusal, a partial apply, or a declined confirmation
EXIT_USAGE = 2  # bad flags, bad option values, unknown names
EXIT_CANCELLED = 130  # Ctrl+C

CONFIG_ENV_VAR = "VARIOUS_TOOLS_CONFIG"
ONE_OFF_PROGRAMS = ("exiftool", "ffmpeg", "ffprobe")


def out() -> Console:
    """Console on standard output, for results."""
    return Console(highlight=False, soft_wrap=False)


def err() -> Console:
    """Console on standard error, for messages and progress, so ``--json`` output stays clean."""
    return Console(stderr=True, highlight=False)


def fail(message: str, code: int = EXIT_FAILED) -> NoReturn:
    """Print ``error: message`` on standard error and exit with ``code``."""
    err().print(f"[bold red]error:[/bold red] {escape(message)}")
    raise typer.Exit(code)


def usage_error(message: str) -> NoReturn:
    fail(message, EXIT_USAGE)


def note(message: str) -> None:
    err().print(escape(message))


def read_settings_file(path: Path) -> dict[str, object]:
    """The raw TOML table of a settings file. A missing file is an empty table.

    Raises ``ValueError`` for a file that cannot be read or parsed, so callers never replace a
    file the user wrote by hand with defaults.
    """
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return dict(tomllib.load(handle))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read settings file {path}: {exc}") from None


def load_checked_settings(path: Path) -> Settings:
    """Settings from ``path`` (defaults when it does not exist). Raises ``ValueError`` if broken."""
    raw = read_settings_file(path)
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(f"invalid settings in {path}: {problems}") from None


@dataclass
class CliState:
    """What the root callback collected: the settings file and the one-off overrides.

    Nothing here is saved. ``one_off`` holds ``--exiftool`` style paths for this run only, and
    ``ollama_url`` replaces ``settings.ollama_url`` for this run only.
    """

    config_path: Path = field(default_factory=default_settings_path)
    one_off: dict[str, str | None] = field(default_factory=dict[str, str | None])
    ollama_url: str | None = None

    def saved_settings(self) -> Settings:
        """The settings as saved in the file (what ``config`` shows and edits)."""
        try:
            return load_checked_settings(self.config_path)
        except ValueError as exc:
            fail(f"{exc}. Fix or delete the file, or pass another one with --config.", EXIT_USAGE)

    def settings(self) -> Settings:
        """The settings for this run: the saved ones, with the one-off Ollama address applied."""
        saved = self.saved_settings()
        if self.ollama_url:
            return saved.model_copy(update={"ollama_url": self.ollama_url})
        return saved

    def presets(self) -> PresetStore:
        return PresetStore.for_settings(self.saved_settings())

    def with_programs(
        self,
        *,
        exiftool: str | None = None,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
        ollama_url: str | None = None,
    ) -> CliState:
        """A copy with one-off program paths given on a command (they win over the global ones).

        This is how ``TOOL scan --exiftool PATH`` and ``doctor --ffmpeg PATH`` work: the same as
        the global options before the command, for this run only. Nothing is saved.
        """
        one_off = dict(self.one_off)
        for name, value in (("exiftool", exiftool), ("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
            if value:
                one_off[name] = value
        url = ollama_url or self.ollama_url
        if url:
            one_off["ollama"] = url
        return replace(self, one_off=one_off, ollama_url=url)


def get_state(ctx: typer.Context) -> CliState:
    obj = ctx.find_root().obj
    if isinstance(obj, CliState):
        return obj
    env_config = os.environ.get(CONFIG_ENV_VAR)
    return CliState(config_path=Path(env_config)) if env_config else CliState()


def confirm(question: str, *, yes: bool) -> bool:
    """Ask a yes/no question (default no). ``yes`` skips the question.

    When there is nobody to answer (standard input is closed), the run is refused instead of
    guessing: pass ``--yes`` in scripts.
    """
    if yes:
        return True
    try:
        return typer.confirm(question, default=False)
    except typer.Abort:
        fail(
            "refusing to continue without confirmation because input is not interactive. "
            "Use --yes to confirm in a script."
        )
