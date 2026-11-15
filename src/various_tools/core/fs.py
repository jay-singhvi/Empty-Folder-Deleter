"""File-system helpers: walking with filters, link safety, collision-safe names and moves.

Nothing in this module opens file contents, except :func:`move_no_replace` when it has to copy
a file to another drive, and it checks OneDrive state first. Directory listings use
``os.scandir`` and the attributes it already has, so cloud-only OneDrive placeholders are not
recalled here.

**What counts as a link** (AGENTS.md rule 4) is decided in one place, :func:`stat_is_link`: a
symlink or a junction (``IO_REPARSE_TAG_SYMLINK``, ``IO_REPARSE_TAG_MOUNT_POINT``) is a link,
and so is any reparse point with a tag this module does not know (the safer side). Cloud-files
placeholders (the ``IO_REPARSE_TAG_CLOUD_*`` family, which OneDrive uses for every file and
folder it syncs) are ordinary files and folders, not links.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import stat as stat_module
import sys
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pathspec

log = logging.getLogger(__name__)

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # a junction (or a mounted volume folder)
IO_REPARSE_TAG_SYMLINK = 0xA000000C
# IO_REPARSE_TAG_CLOUD is 0x9000001A; CLOUD_1 to CLOUD_F set bits 12-15 (0x9000101A...0x9000F01A).
IO_REPARSE_TAG_CLOUD = 0x9000001A
_CLOUD_TAG_MASK = 0xFFFF0FFF
# Windows refuses many calls on paths at or past MAX_PATH unless they use the \\?\ prefix.
MAX_PATH = 240
_COPY_CHUNK = 1024 * 1024


def is_cloud_reparse_tag(tag: int) -> bool:
    """True for the cloud-files placeholder tags (``IO_REPARSE_TAG_CLOUD`` to ``CLOUD_F``)."""
    return (tag & _CLOUD_TAG_MASK) == IO_REPARSE_TAG_CLOUD


def stat_is_link(st: os.stat_result) -> bool:
    """True when a no-follow stat result is a symlink, a junction or an unknown reparse point.

    A cloud-files placeholder is not a link. Pure function, no I/O.
    """
    if stat_module.S_ISLNK(st.st_mode):
        return True
    attributes: int = getattr(st, "st_file_attributes", 0)
    if not attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag: int = getattr(st, "st_reparse_tag", 0)
    return not is_cloud_reparse_tag(tag)


def stat_no_follow(path: Path) -> os.stat_result | None:
    """``os.lstat`` of ``path`` (long paths allowed), or ``None`` when it cannot be read."""
    try:
        return os.lstat(long_path(path))
    except OSError:
        return None


def is_link(path: Path) -> bool:
    """True when ``path`` itself is a junction, a symlink or an unknown reparse point."""
    st = stat_no_follow(path)
    return st is not None and stat_is_link(st)


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
    # AGENTS.md rule 4: a .gitignore that is a link could point anywhere, so it is not read.
    if is_link(ignore_file):
        log.warning("Skipped %s: it is a link, so its rules are not applied", ignore_file)
        return None
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
    * ``respect_gitignore``: honour ``.gitignore`` files from ``root`` down. A ``.gitignore``
      that is a link is not read.
    * ``skip_reparse_points``: kept for compatibility and has no effect. Junctions, symlinks
      and unknown reparse points (see :func:`stat_is_link`) are always skipped and never
      followed, so nothing outside ``root`` is reached (AGENTS.md rule 4). Cloud placeholders
      are listed like any other file or folder.

    ``root`` itself is never yielded. Folders that cannot be listed are logged and skipped.
    """
    del skip_reparse_points  # rule 4 is not optional
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
            attrs: int = getattr(st, "st_file_attributes", 0)
            if entry.is_symlink() or entry.is_junction() or stat_is_link(st):
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


def is_case_only_rename(source: Path, destination: Path) -> bool:
    """True when ``destination`` is ``source`` spelled with different letter case.

    That is, the two paths compare equal the way Windows compares them, the names differ, and
    the existing entry at ``destination`` is ``source`` itself (same file id), so renaming
    replaces nothing. Reads attributes only.
    """
    if source.name == destination.name or not same_path(source, destination):
        return False
    a, b = stat_no_follow(source), stat_no_follow(destination)
    return a is not None and b is not None and os.path.samestat(a, b)


def rename_no_replace(source: Path, destination: Path) -> None:
    """Rename ``source`` to ``destination``, never replacing a file that is already there.

    Raises :class:`FileExistsError` when ``destination`` exists, unless it is ``source`` itself
    under a different letter case (:func:`is_case_only_rename`). On Windows ``os.rename`` itself
    refuses an existing target, so the check and the rename are one atomic step. Elsewhere a
    hard link is made first, which also fails when the target exists. Same volume only.
    """
    case_only = is_case_only_rename(source, destination)
    if not case_only and os.path.lexists(long_path(destination)):
        raise FileExistsError(
            f"the output file already exists, so it was not replaced: {destination}"
        )
    if sys.platform == "win32" or case_only:
        os.rename(long_path(source), long_path(destination))
    else:
        os.link(long_path(source), long_path(destination))
        os.unlink(long_path(source))


def _existing_ancestor(path: Path) -> Path | None:
    current = Path(os.path.abspath(path))
    while not os.path.lexists(long_path(current)):
        if current.parent == current:
            return None
        current = current.parent
    return current


def same_volume(a: Path, b: Path) -> bool:
    """True when ``a`` and the nearest existing folder at or above ``b`` are on one volume.

    Compares the volume ids that ``os.lstat`` reports. When either cannot be read, the answer
    is False, so the caller takes the careful copy path or refuses.
    """
    there = _existing_ancestor(b)
    sa = stat_no_follow(a)
    sb = stat_no_follow(there) if there is not None else None
    return sa is not None and sb is not None and sa.st_dev == sb.st_dev


def move_no_replace(source: Path, destination: Path) -> None:
    """Move ``source`` to ``destination``. Never replaces anything, on any volume.

    * Same volume: :func:`rename_no_replace` (atomic on Windows).
    * Another volume, a file: refuses a cloud-only file (AGENTS.md rule 3), copies the contents
      to an exclusively created ``.partial`` file next to the destination, renames that into
      place with :func:`rename_no_replace`, checks the size and only then removes the source.
    * Another volume, a folder or a link: refused with :class:`OSError`.

    The destination folder must exist. Raises :class:`FileExistsError` when the destination
    exists or appears during the move; the source is then left as it was.
    """
    if same_volume(source, destination.parent):
        rename_no_replace(source, destination)
        return
    st = stat_no_follow(source)
    if st is None:
        raise FileNotFoundError(f"source is gone: {source}")
    if stat_is_link(st):
        raise OSError(f"a junction or symlink is not moved to another drive: {source}")
    if stat_module.S_ISDIR(st.st_mode):
        raise OSError(
            f"moving a folder to another drive is not supported; move its files instead: {source}"
        )
    if os.path.lexists(long_path(destination)):
        raise FileExistsError(
            f"the output file already exists, so it was not replaced: {destination}"
        )
    from various_tools.core.onedrive import ensure_on_device

    ensure_on_device(source)  # raises CloudOnlyFileError before anything is opened
    partial = destination.parent / f".{destination.name}.{uuid.uuid4().hex[:8]}.partial"
    try:
        with (
            open(long_path(source), "rb") as reader,
            open(long_path(partial), "xb") as writer,
        ):
            shutil.copyfileobj(reader, writer, _COPY_CHUNK)
        shutil.copystat(long_path(source), long_path(partial))
        rename_no_replace(partial, destination)
    except BaseException:
        if os.path.lexists(long_path(partial)):
            os.remove(long_path(partial))
        raise
    copied = os.lstat(long_path(destination)).st_size
    if copied != st.st_size:
        os.remove(long_path(destination))  # our own incomplete copy; the source is untouched
        raise OSError(f"the copy of {source} has {copied} bytes, not {st.st_size}; not moved")
    os.remove(long_path(source))


_PREFIX_UNC = "\\\\?\\UNC\\"
_PREFIXES = ("\\\\?\\", "\\\\.\\")


def _strip_prefix(text: str) -> str:
    """``text`` without a ``\\\\?\\`` (or ``\\\\?\\UNC\\``) prefix."""
    if text[: len(_PREFIX_UNC)].upper() == _PREFIX_UNC:
        return "\\\\" + text[len(_PREFIX_UNC) :]
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _get_long_path_name(text: str) -> str | None:
    """``GetLongPathNameW`` of an existing path, or None."""
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    func = kernel32.GetLongPathNameW
    func.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    func.restype = ctypes.c_uint32
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        needed = int(func(text, buffer, size))
        if needed == 0:
            return None
        if needed < size:
            return str(buffer.value)
        size = needed + 1


def short_path_name(path: Path) -> str | None:
    """The 8.3 short form of an existing path (``GetShortPathNameW``), or None."""
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    func = kernel32.GetShortPathNameW
    func.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    func.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(1024)
    needed = int(func(str(path), buffer, 1024))
    return str(buffer.value) if 0 < needed < 1024 else None


def _expand_short_names(text: str) -> str:
    """Replace 8.3 short names (``PROGRA~1``) with long ones, for the part of ``text`` that
    exists. Only called when a ``~`` is present, so most paths cost nothing."""
    head, tail = text, list[str]()
    while True:
        expanded = _get_long_path_name(head)
        if expanded is not None:
            return os.path.join(expanded, *reversed(tail))
        parent, name = os.path.split(head)
        if not name or parent == head:
            return text
        tail.append(name)
        head = parent


def _norm(path: str | os.PathLike[str]) -> str:
    """The comparable form of a path: no long-path prefix, absolute, normalised, 8.3 names
    expanded, and case-folded on Windows."""
    text = os.path.normpath(os.path.abspath(_strip_prefix(os.fspath(path))))
    if sys.platform == "win32" and "~" in text:
        text = _expand_short_names(text)
    return os.path.normcase(text)


def comparable_path(path: str | os.PathLike[str]) -> str:
    """The form in which two paths can be compared for equality (see :func:`same_path`)."""
    return _norm(path)


def strip_long_prefix(text: str) -> str:
    """``text`` without a ``\\\\?\\`` or ``\\\\?\\UNC\\`` prefix (the inverse of
    :func:`long_path` for display and comparison)."""
    return _strip_prefix(text)


def same_path(a: Path | str, b: Path | str) -> bool:
    """True when two paths name the same place, compared the way Windows does. No links are
    resolved, but ``\\\\?\\`` prefixes and 8.3 short names are."""
    return _norm(a) == _norm(b)


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


def _is_drive_root(normalized: str) -> bool:
    """True for ``c:\\``, ``\\\\server\\share`` and ``/``, given a :func:`_norm` result."""
    drive, rest = os.path.splitdrive(normalized)
    return not rest.strip("\\/") and (bool(drive) or rest != "")


def protected_reason(
    path: Path, env: Mapping[str, str] | None = None, rules: ProtectedPaths | None = None
) -> str | None:
    """Why ``path`` is a protected target (AGENTS.md rule 6), or ``None`` if it is not.

    Default rules: drive roots, the user profile folder itself, and everything inside the
    Windows and Program Files folders. ``rules`` (from settings) can change the list. Paths are
    compared without ``\\\\?\\`` prefixes and with 8.3 short names expanded, so neither can be
    used to slip past the list.
    """
    environ = os.environ if env is None else env
    active = rules or DEFAULT_PROTECTION
    text = _norm(path)
    as_drive = _admin_share_as_drive(text)
    for target in (text,) if as_drive is None else (text, as_drive):
        reason = _protected_reason_of(target, environ, active)
        if reason is not None:
            return reason
    return None


# ``\\host\c$\...``: an administrative share is the drive itself, on whatever machine.
_ADMIN_SHARE = re.compile(r"^\\\\[^\\]+\\([A-Za-z])\$(\\.*)?$")


def _admin_share_as_drive(normalized: str) -> str | None:
    """``c:\\rest`` for an administrative share path ``\\\\host\\c$\\rest``, else None.

    Used for the protected-path check only, so ``\\\\localhost\\C$\\Windows`` counts as the
    Windows folder. Shares on another machine count too, which is the safer side."""
    if sys.platform != "win32":
        return None
    match = _ADMIN_SHARE.match(normalized)
    if match is None:
        return None
    return os.path.normcase(f"{match.group(1)}:" + (match.group(2) or "\\"))


def _protected_reason_of(
    target: str, environ: Mapping[str, str], active: ProtectedPaths
) -> str | None:
    if active.drive_roots and _is_drive_root(target):
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


def real_path(path: Path | str) -> str:
    """Where ``path`` really is, with junctions, symlinks, subst and mapped drives resolved
    (``os.path.realpath``), without a long-path prefix. The part that does not exist is kept
    as written. Opens folders' metadata only."""
    return _strip_prefix(os.path.realpath(_strip_prefix(os.fspath(path))))


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
    current = Path(os.path.abspath(_strip_prefix(os.fspath(path))))
    stop_norm = _norm(stop) if stop is not None and is_inside(current, stop) else None
    while True:
        st = stat_no_follow(current)
        if st is not None and stat_is_link(st):
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

    Refuses a missing folder, a junction or symlink or a folder below one (always), and a
    protected path (AGENTS.md rules 4 and 6) unless protected paths are allowed. The protected
    check looks at the path as written (administrative shares count as their drive) and at its
    real path (:func:`real_path`), so neither a link nor a subst or mapped drive can hide a
    protected folder. Pass ``ctx.settings.protected_paths()`` so the user's protected-folder
    list is used; a plain bool keeps the default list. Opens no file.
    """
    rules = (
        allow_protected
        if isinstance(allow_protected, ProtectedPaths)
        else ProtectedPaths(allowed=allow_protected)
    )
    if not root.is_dir():
        return f"Not a folder: {root}"
    try:
        st = os.lstat(long_path(_strip_prefix(os.fspath(root))))
    except OSError as exc:
        return f"Could not read the folder: {exc.strerror or exc}"
    if stat_is_link(st):
        return f"Junction or symlink, not followed: {root}"
    link = reparse_ancestor(root)
    if link is not None:
        return f"Inside a junction or symlink, not followed: {link}"
    reason = protected_reason(root, rules=rules)
    if reason is None:
        try:
            reason = protected_reason(Path(real_path(root)), rules=rules)
        except OSError as exc:
            return f"Could not resolve the folder's real path: {exc.strerror or exc}"
    if reason and not rules.allowed:
        return f"Protected folder refused ({reason}): {root}"
    return None
