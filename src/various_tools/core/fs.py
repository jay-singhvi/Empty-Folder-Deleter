"""File-system helpers: walking with filters, reparse-point safety, collision-safe names.

Nothing in this module opens file contents. Directory listings use ``os.scandir`` and the
attributes it already has, so cloud-only OneDrive placeholders are not recalled here.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pathspec

log = logging.getLogger(__name__)

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Windows refuses many calls on paths at or past MAX_PATH unless they use the \\?\ prefix.
MAX_PATH = 240


def long_path(path: Path | str) -> str:
    """The path as a string that Win32 calls accept even when it is longer than MAX_PATH.

    Adds the ``\\\\?\\`` prefix (or ``\\\\?\\UNC\\`` for network paths) on Windows, only when
    needed. Use the result for system calls only. Keep the plain :class:`Path` for results.
    """
    text: str = os.path.abspath(str(path))
    if sys.platform != "win32" or len(text) < MAX_PATH or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


@dataclass(frozen=True)
class FileInfo:
    """A regular file found by :func:`walk`, with the metadata the listing already provided."""

    path: Path
    size: int
    mtime: float
    attributes: int

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _matches_any(name: str, rel_posix: str, globs: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel_posix, g) for g in globs)


def _load_gitignore(folder: Path) -> pathspec.GitIgnoreSpec | None:
    ignore_file = folder / ".gitignore"
    # AGENTS.md rule 3: a cloud-only .gitignore is never recalled just to read its rules.
    from various_tools.core.onedrive import get_sync_status, is_on_device

    if ignore_file.exists() and not is_on_device(get_sync_status(ignore_file)):
        log.warning("Skipped cloud-only %s; its rules are not applied", ignore_file)
        return None
    try:
        lines = ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Could not read %s: %s", ignore_file, exc)
        return None
    return pathspec.GitIgnoreSpec.from_lines(lines)


def _gitignored(path: Path, is_dir: bool, rules: list[tuple[Path, pathspec.GitIgnoreSpec]]) -> bool:
    """Apply every .gitignore on the way down, each relative to the folder it lives in."""
    for base, spec in rules:
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            continue
        if is_dir:
            rel += "/"
        if spec.match_file(rel):
            return True
    return False


def walk(
    root: Path,
    include_globs: Iterable[str] = (),
    exclude_globs: Iterable[str] = (),
    exts: Iterable[str] | None = None,
    recursive: bool = True,
    respect_gitignore: bool = False,
    skip_reparse_points: bool = True,
) -> Iterator[FileInfo]:
    """Yield regular files under ``root`` that pass the filters.

    * ``include_globs``: when given, a file must match one of them (by name or relative path).
    * ``exclude_globs``: matching files *and* folders are skipped (``.git``, ``node_modules``).
    * ``exts``: lower-case extensions with a dot, e.g. ``{".jpg", ".png"}``.
    * ``respect_gitignore``: honour ``.gitignore`` files from ``root`` down.
    * ``skip_reparse_points``: never follow junctions or symlinks, so nothing outside ``root``
      is reached. Enabled by default and required by AGENTS.md rule 4.

    ``root`` itself is never yielded. Folders that cannot be listed are logged and skipped.
    """
    includes = tuple(include_globs)
    excludes = tuple(exclude_globs)
    extensions = {e.lower() for e in exts} if exts is not None else None
    root = Path(root)

    root_rules: list[tuple[Path, pathspec.GitIgnoreSpec]] = []
    if respect_gitignore:
        spec = _load_gitignore(root)
        if spec is not None:
            root_rules.append((root, spec))

    # Each stack entry is (folder, the .gitignore rules that apply inside it).
    stack: list[tuple[Path, list[tuple[Path, pathspec.GitIgnoreSpec]]]] = [(root, root_rules)]
    while stack:
        folder, rules = stack.pop()
        try:
            with os.scandir(long_path(folder)) as it:
                entries = sorted(it, key=lambda e: e.name.casefold())
        except OSError as exc:
            log.warning("Skipping folder %s: %s", folder, exc)
            continue

        subfolders: list[tuple[Path, list[tuple[Path, pathspec.GitIgnoreSpec]]]] = []
        for entry in entries:
            # Build child paths from the logical folder, so no \\?\ prefix leaks into results.
            path = folder / entry.name
            rel_posix = path.relative_to(root).as_posix()
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                log.warning("Skipping %s: %s", path, exc)
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            is_link = entry.is_symlink() or bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
            if skip_reparse_points and is_link:
                continue
            is_dir = entry.is_dir(follow_symlinks=False)

            if _matches_any(entry.name, rel_posix, excludes):
                continue
            if respect_gitignore and _gitignored(path, is_dir, rules):
                continue

            if is_dir:
                if recursive:
                    child_rules = rules
                    if respect_gitignore:
                        spec = _load_gitignore(path)
                        if spec is not None:
                            child_rules = [*rules, (path, spec)]
                    subfolders.append((path, child_rules))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if extensions is not None and path.suffix.lower() not in extensions:
                continue
            if includes and not _matches_any(entry.name, rel_posix, includes):
                continue
            yield FileInfo(path=path, size=st.st_size, mtime=st.st_mtime, attributes=attrs)

        # Reverse so that the first folder in sorted order is processed first.
        stack.extend(reversed(subfolders))


def unique_destination(folder: Path, name: str) -> Path:
    """Return ``folder/name``, or ``stem_dupN.ext`` for the first N that is free.

    Names are compared case-insensitively, as Windows does. The folder is listed with
    ``os.scandir`` rather than probed per name, so no file is opened.
    """
    taken: set[str] = set()
    if folder.is_dir():
        with os.scandir(long_path(folder)) as it:
            taken = {entry.name.casefold() for entry in it}
    if name.casefold() not in taken:
        return folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while True:
        candidate = f"{stem}_dup{counter}{suffix}"
        if candidate.casefold() not in taken:
            return folder / candidate
        counter += 1


def rename_no_replace(source: Path, destination: Path) -> None:
    """Move ``source`` to ``destination``, never replacing a file that is already there.

    Raises :class:`FileExistsError` when ``destination`` exists. On Windows ``os.rename`` itself
    refuses an existing target, so the check and the move are one atomic step. Elsewhere a hard
    link is made first, which also fails when the target exists.
    """
    if os.path.lexists(destination):
        raise FileExistsError(
            f"the output file already exists, so it was not replaced: {destination}"
        )
    if sys.platform == "win32":
        os.rename(long_path(source), long_path(destination))
    else:
        os.link(long_path(source), long_path(destination))
        os.unlink(long_path(source))


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


# Folders that, with everything inside them, are protected targets (AGENTS.md rule 6) 🔒.
# The user can edit the list in settings (``protected_folders``); this is the default.
DEFAULT_PROTECTED_FOLDERS: tuple[str, ...] = (
    "%SystemRoot%",
    "%WINDIR%",
    "%ProgramFiles%",
    "%ProgramFiles(x86)%",
    "%ProgramW6432%",
)
_FOLDER_LABELS = {
    "%systemroot%": "Windows folder",
    "%windir%": "Windows folder",
    "%programfiles%": "Program Files",
    "%programfiles(x86)%": "Program Files",
    "%programw6432%": "Program Files",
}
_ENV_VAR = re.compile(r"%([^%]+)%")


@dataclass(frozen=True)
class ProtectedPaths:
    """The protected-path rules 🔒. Built from settings by ``Settings.protected_paths()``.

    ``allowed`` is the user's "allow protected folders" override; the checks below still report
    the reason, and callers decide (``root_refusal`` and the apply engine honour ``allowed``).
    """

    folders: tuple[str, ...] = DEFAULT_PROTECTED_FOLDERS
    drive_roots: bool = True
    user_profile: bool = True
    allowed: bool = False


DEFAULT_PROTECTION = ProtectedPaths()


def _expand(template: str, environ: Mapping[str, str]) -> str | None:
    """``%VAR%`` expanded from ``environ`` (case-insensitively), or None if a variable is unset."""
    lowered = {key.lower(): value for key, value in environ.items()}
    missing = False

    def sub(match: re.Match[str]) -> str:
        nonlocal missing
        value = lowered.get(match.group(1).lower())
        if not value:
            missing = True
            return ""
        return value

    expanded = _ENV_VAR.sub(sub, template)
    return None if missing or not expanded.strip() else expanded


def protected_reason(
    path: Path, env: Mapping[str, str] | None = None, rules: ProtectedPaths | None = None
) -> str | None:
    """Why ``path`` is a protected target (AGENTS.md rule 6), or ``None`` if it is not.

    Default rules: drive roots, the user profile folder itself, and everything inside the
    Windows and Program Files folders. ``rules`` (from settings) can change the list.
    """
    environ = os.environ if env is None else env
    active = rules or DEFAULT_PROTECTION
    target = _norm(path)
    anchor = _norm(Path(path).anchor or path)
    if active.drive_roots and target == anchor:
        return "drive root"

    user_profile = environ.get("USERPROFILE")
    if active.user_profile and user_profile and target == _norm(user_profile):
        return "user profile folder"

    def within(base: str) -> bool:
        return target == base or target.startswith(base + os.sep)

    for template in active.folders:
        value = _expand(template, environ)
        if value and within(_norm(value)):
            return _FOLDER_LABELS.get(template.lower(), f"protected folder {template}")
    return None


def is_inside(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or below it, compared without resolving links."""
    target, base = _norm(path), _norm(root)
    return target == base or target.startswith(base + os.sep)


def reparse_ancestor(path: Path, stop: Path | None = None) -> Path | None:
    """The first existing junction or symlink at ``path`` or above it, or ``None``.

    Walks up from ``path`` through every folder that exists, up to and including ``stop``
    (or the drive root when ``stop`` is ``None`` or not above ``path``). Use it before
    writing to a destination, so output never lands outside the chosen folder through a
    junction (AGENTS.md rule 4). Reads attributes only and opens no file.
    """
    current = Path(os.path.abspath(path))
    stop_norm = _norm(stop) if stop is not None and is_inside(current, stop) else None
    while True:
        try:
            st = os.stat(long_path(current), follow_symlinks=False)
        except OSError:
            st = None
        if st is not None:
            attributes = getattr(st, "st_file_attributes", 0)
            if attributes & FILE_ATTRIBUTE_REPARSE_POINT or os.path.islink(current):
                return current
        if stop_norm is not None and _norm(current) == stop_norm:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def destination_refusal(dst: Path, stop: Path | None = None) -> str | None:
    """Why ``dst`` must not be written to, or ``None``. See :func:`reparse_ancestor`."""
    link = reparse_ancestor(dst, stop)
    if link is not None:
        return f"destination goes through a junction or symlink, not followed: {link}"
    return None


def root_refusal(root: Path, allow_protected: bool | ProtectedPaths = False) -> str | None:
    """Why ``root`` cannot be used as a target folder, or ``None``.

    Refuses a missing folder, a junction or symlink, and a protected path (AGENTS.md rules 4
    and 6) unless protected paths are allowed. Pass ``ctx.settings.protected_paths()`` so the
    user's protected-folder list is used; a plain bool keeps the default list. Opens no file.
    """
    rules = (
        allow_protected
        if isinstance(allow_protected, ProtectedPaths)
        else ProtectedPaths(allowed=allow_protected)
    )
    if not root.is_dir():
        return f"Not a folder: {root}"
    try:
        attributes = getattr(
            os.stat(long_path(root), follow_symlinks=False), "st_file_attributes", 0
        )
    except OSError as exc:
        return f"Could not read the folder: {exc.strerror or exc}"
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return f"Junction or symlink, not followed: {root}"
    reason = protected_reason(root, rules=rules)
    if reason and not rules.allowed:
        return f"Protected folder refused ({reason}): {root}"
    return None
