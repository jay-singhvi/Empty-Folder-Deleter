"""File-system helpers: walking with filters, reparse-point safety, collision-safe names.

Nothing in this module opens file contents. Directory listings use ``os.scandir`` and the
attributes it already has, so cloud-only OneDrive placeholders are not recalled here.
"""

from __future__ import annotations

import fnmatch
import logging
import os
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


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def protected_reason(path: Path, env: Mapping[str, str] | None = None) -> str | None:
    """Why ``path`` is a protected target (AGENTS.md rule 6), or ``None`` if it is not.

    Protected: drive roots, the user profile folder itself, the Windows folder and
    Program Files (both bitnesses). Everything inside Windows and Program Files is protected.
    """
    environ = os.environ if env is None else env
    target = _norm(path)
    anchor = _norm(Path(path).anchor or path)
    if target == anchor:
        return "drive root"

    user_profile = environ.get("USERPROFILE")
    if user_profile and target == _norm(user_profile):
        return "user profile folder"

    def within(base: str) -> bool:
        return target == base or target.startswith(base + os.sep)

    for key, label in (
        ("SystemRoot", "Windows folder"),
        ("WINDIR", "Windows folder"),
        ("ProgramFiles", "Program Files"),
        ("ProgramFiles(x86)", "Program Files"),
        ("ProgramW6432", "Program Files"),
    ):
        value = environ.get(key)
        if value and within(_norm(value)):
            return label
    return None


def is_inside(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or below it, compared without resolving links."""
    target, base = _norm(path), _norm(root)
    return target == base or target.startswith(base + os.sep)
