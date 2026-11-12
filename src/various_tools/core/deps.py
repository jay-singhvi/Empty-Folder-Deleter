"""External programs: find ffmpeg, ffprobe, ExifTool and Ollama, or say they are missing.

This is the only module that may look up or hardcode an executable (AGENTS.md). Media code gets
resolved paths through ``RunContext.deps``.

Lookup order, first valid match wins (plan §3.5):

1. user override from settings (``external_tools.<tool>.path``, or the Ollama URL)
2. environment variables ``VARIOUS_TOOLS_FFMPEG``, ``VARIOUS_TOOLS_FFPROBE``,
   ``VARIOUS_TOOLS_EXIFTOOL`` and ``VARIOUS_TOOLS_OLLAMA_URL``
3. ``PATH`` (``shutil.which``)
4. known install locations (data in :data:`TOOL_SPECS`)

A candidate counts only when its version command succeeds. An override that stopped working
is logged and the lookup continues. A tool that is still missing yields ``valid=False``, and
the feature that needs it is disabled with a message.

Results are cached per :class:`ExternalTools` instance. :meth:`ExternalTools.redetect` clears them.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from various_tools.core.jsonutil import as_dict

log = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
VERSION_TIMEOUT_S = 5
OLLAMA_TIMEOUT_S = 2

# Without this flag Windows opens a console window per child process. The GUI must not flash one.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ToolSource(StrEnum):
    OVERRIDE = "override"
    ENVIRONMENT = "environment"
    PATH = "PATH"
    KNOWN_LOCATION = "auto-detected"
    DEFAULT = "default"
    NONE = "not found"


@dataclass(frozen=True)
class ToolSpec:
    """Data describing where one program lives. Adding a location is a one-line change here."""

    name: str
    exe_name: str
    version_args: tuple[str, ...]
    env_var: str
    winget_id: str
    # Templates are expanded with os.path.expandvars, then globbed (``**`` recurses).
    known_locations: tuple[str, ...] = ()
    # Look next to this tool's resolved location when that tool is found first.
    sibling_of: str | None = None


TOOL_SPECS: dict[str, ToolSpec] = {
    "ffmpeg": ToolSpec(
        name="ffmpeg",
        exe_name="ffmpeg",
        version_args=("-version",),
        env_var="VARIOUS_TOOLS_FFMPEG",
        winget_id="Gyan.FFmpeg",
        known_locations=(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Links",
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin",
            r"C:\ffmpeg\bin",
            r"%ProgramFiles%\ffmpeg\bin",
        ),
    ),
    "ffprobe": ToolSpec(
        name="ffprobe",
        exe_name="ffprobe",
        version_args=("-version",),
        env_var="VARIOUS_TOOLS_FFPROBE",
        winget_id="Gyan.FFmpeg",
        known_locations=(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Links",
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin",
            r"C:\ffmpeg\bin",
            r"%ProgramFiles%\ffmpeg\bin",
        ),
        sibling_of="ffmpeg",
    ),
    "exiftool": ToolSpec(
        name="exiftool",
        exe_name="exiftool",
        version_args=("-ver",),
        env_var="VARIOUS_TOOLS_EXIFTOOL",
        winget_id="OliverBetz.ExifTool",
        known_locations=(
            r"%ProgramFiles%\ExifTool",
            r"%ProgramFiles(x86)%\ExifTool",
            r"%LOCALAPPDATA%\Programs\ExifTool",
            r"%LOCALAPPDATA%\Microsoft\WinGet\Links",
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\OliverBetz.ExifTool*",
        ),
    ),
    "ollama": ToolSpec(
        name="ollama",
        exe_name="ollama",
        version_args=("--version",),
        env_var="VARIOUS_TOOLS_OLLAMA_URL",
        winget_id="Ollama.Ollama",
        known_locations=(r"%LOCALAPPDATA%\Programs\Ollama",),
    ),
}


@dataclass(frozen=True)
class ToolLocation:
    """What was found for one tool. ``path`` is None for Ollama, which is addressed by ``url``."""

    tool: str
    path: Path | None
    url: str | None
    version: str | None
    source: ToolSource
    valid: bool
    install_hint: str = ""
    problems: tuple[str, ...] = field(default=())

    @property
    def available(self) -> bool:
        return self.valid


def _windows_path_exts() -> tuple[str, ...]:
    """The extensions Windows tries for a bare program name (``PATHEXT``)."""
    raw = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return tuple(ext.lower() for ext in raw.split(";") if ext)


def run_tool(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run an already-resolved external program with no console window.

    Callers pass the resolved path as ``args[0]``. Can raise ``TimeoutExpired`` or ``OSError``.
    """
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=_NO_WINDOW,
    )


def run_binary(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
    """Like :func:`run_tool`, but stdout is raw bytes (used for image data from ffmpeg)."""
    return subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=_NO_WINDOW,
    )


def probe_version(path: Path, version_args: tuple[str, ...]) -> str | None:
    """Run the program's version command. Returns the first output line, or None on failure."""
    try:
        result = run_tool([str(path), *version_args], VERSION_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Version probe failed for %s: %s", path, exc)
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        if line.strip():
            return line.strip()[:120]
    return None


def probe_ollama(url: str) -> str | None:
    """GET ``/api/version`` on an Ollama server. Returns its version, or None if unreachable."""
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        with urllib.request.urlopen(  # noqa: S310 - scheme checked above
            f"{url.rstrip('/')}/api/version", timeout=OLLAMA_TIMEOUT_S
        ) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("Ollama not reachable at %s: %s", url, exc)
        return None
    version = as_dict(data).get("version")
    return str(version) if version else None


VersionProbe = Callable[[Path, tuple[str, ...]], str | None]
OllamaProbe = Callable[[str], str | None]


class ExternalTools:
    """Resolves external programs once per session. See the module docstring for the order.

    ``overrides`` maps a tool name to a path (or, for ``ollama``, a URL). The probes are
    injectable so tests can avoid real programs. ``env`` defaults to ``os.environ``.
    """

    def __init__(
        self,
        overrides: Mapping[str, str | None] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        version_probe: VersionProbe = probe_version,
        ollama_probe: OllamaProbe = probe_ollama,
    ) -> None:
        self._overrides = dict(overrides or {})
        self._env = os.environ if env is None else env
        self._version_probe = version_probe
        self._ollama_probe = ollama_probe
        self._cache: dict[str, ToolLocation] = {}
        self._versions: dict[str, str] = {}

    def redetect(self) -> None:
        """Forget cached results so the next :meth:`resolve` searches again."""
        self._cache.clear()

    def resolve(self, tool: str) -> ToolLocation:
        """Location for ``tool`` (one of :data:`TOOL_SPECS`), cached after the first call."""
        if tool not in TOOL_SPECS:
            raise KeyError(f"unknown external tool: {tool}")
        if tool not in self._cache:
            self._cache[tool] = (
                self._resolve_ollama() if tool == "ollama" else self._resolve_exe(tool)
            )
        return self._cache[tool]

    def resolve_all(self) -> dict[str, ToolLocation]:
        return {name: self.resolve(name) for name in TOOL_SPECS}

    def path(self, tool: str) -> Path | None:
        """The resolved executable for ``tool`` if it is valid, else None."""
        location = self.resolve(tool)
        return location.path if location.valid else None

    # Named accessors, so that tool code never spells out a program name (AGENTS.md).
    def exiftool(self) -> Path | None:
        return self.path("exiftool")

    def ffprobe(self) -> Path | None:
        return self.path("ffprobe")

    # ----- executables -------------------------------------------------

    def _resolve_exe(self, tool: str) -> ToolLocation:
        spec = TOOL_SPECS[tool]
        problems: list[str] = []

        override = self._overrides.get(tool)
        if override:
            found = self._validate_exe(Path(override), spec)
            if found is not None:
                return self._location(spec, found, ToolSource.OVERRIDE, problems)
            msg = f"saved path for {tool} no longer works: {override}; falling back"
            log.warning(msg)
            problems.append(msg)

        env_value = self._env.get(spec.env_var)
        if env_value:
            found = self._validate_exe(Path(env_value), spec)
            if found is not None:
                return self._location(spec, found, ToolSource.ENVIRONMENT, problems)

        which = shutil.which(spec.exe_name)
        if which:
            found = self._validate_exe(Path(which), spec)
            if found is not None:
                return self._location(spec, found, ToolSource.PATH, problems)

        for candidate in self._known_candidates(spec):
            found = self._validate_exe(candidate, spec)
            if found is not None:
                return self._location(spec, found, ToolSource.KNOWN_LOCATION, problems)

        return ToolLocation(
            tool=tool,
            path=None,
            url=None,
            version=None,
            source=ToolSource.NONE,
            valid=False,
            install_hint=install_hint(tool),
            problems=tuple(problems),
        )

    def _known_candidates(self, spec: ToolSpec) -> list[Path]:
        """Files to try in known locations, in order. Duplicates are dropped case-insensitively."""
        folders: list[str] = []
        if spec.sibling_of:
            sibling = self.path(spec.sibling_of)
            if sibling is not None:
                folders.append(str(sibling.parent))
        for template in spec.known_locations:
            expanded = os.path.expandvars(template)
            if "*" in expanded:
                folders.extend(sorted(glob.glob(expanded, recursive=True)))
            else:
                folders.append(expanded)

        exts = _windows_path_exts() if os.name == "nt" else ("",)
        seen: set[str] = set()
        candidates: list[Path] = []
        for folder in folders:
            for ext in exts:
                file_path = Path(folder) / f"{spec.exe_name}{ext}"
                key = os.path.normcase(str(file_path))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(file_path)
        return candidates

    def _validate_exe(self, candidate: Path, spec: ToolSpec) -> Path | None:
        """The path if it is an existing file whose version command succeeds, else None."""
        try:
            if not candidate.is_file():
                return None
        except OSError:
            return None
        version = self._version_probe(candidate, spec.version_args)
        if version is None:
            return None
        self._versions[str(candidate)] = version
        return candidate

    def _location(
        self, spec: ToolSpec, path: Path, source: ToolSource, problems: list[str]
    ) -> ToolLocation:
        return ToolLocation(
            tool=spec.name,
            path=path,
            url=None,
            version=self._versions.get(str(path)),
            source=source,
            valid=True,
            install_hint=install_hint(spec.name),
            problems=tuple(problems),
        )

    # ----- Ollama --------------------------------------------------------

    def _resolve_ollama(self) -> ToolLocation:
        spec = TOOL_SPECS["ollama"]
        problems: list[str] = []
        candidates: list[tuple[str, ToolSource]] = []
        override = self._overrides.get("ollama")
        if override:
            candidates.append((override, ToolSource.OVERRIDE))
        env_url = self._env.get(spec.env_var)
        if env_url:
            candidates.append((env_url, ToolSource.ENVIRONMENT))
        candidates.append((DEFAULT_OLLAMA_URL, ToolSource.DEFAULT))

        for url, source in candidates:
            version = self._ollama_probe(url)
            if version is not None:
                return ToolLocation(
                    tool="ollama",
                    path=self._ollama_exe(),
                    url=url,
                    version=version,
                    source=source,
                    valid=True,
                    install_hint=install_hint("ollama"),
                    problems=tuple(problems),
                )
            if source is ToolSource.OVERRIDE:
                msg = f"saved Ollama URL is not responding: {url}; falling back"
                log.warning(msg)
                problems.append(msg)

        return ToolLocation(
            tool="ollama",
            path=self._ollama_exe(),
            url=DEFAULT_OLLAMA_URL,
            version=None,
            source=ToolSource.NONE,
            valid=False,
            install_hint=install_hint("ollama"),
            problems=tuple(problems),
        )

    def _ollama_exe(self) -> Path | None:
        """Ollama's executable, if installed in a known place. The server does not need it."""
        spec = TOOL_SPECS["ollama"]
        for candidate in self._known_candidates(spec):
            if candidate.is_file():
                return candidate
        return None


SYSTEM_TOOLS: dict[str, str] = {
    # Windows' own tool, used to pin and unpin OneDrive files. It is not one of the four
    # external programs, but its name lives here too, so that only this module names executables.
    "attrib": "attrib.exe",
}


def system_tool(name: str) -> str:
    """Path of a Windows system tool in ``%SystemRoot%\\System32``, or the bare name if absent."""
    exe = SYSTEM_TOOLS[name]
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    candidate = Path(system_root) / "System32" / exe
    return str(candidate) if candidate.is_file() else exe


def install_hint(tool: str) -> str:
    """The winget command that installs ``tool``, for messages and the copyable hint in the GUI."""
    spec = TOOL_SPECS[tool]
    return f"winget install {spec.winget_id}"
