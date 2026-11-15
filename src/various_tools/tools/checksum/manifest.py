"""The checksum manifest: hashing, and reading and writing the ``sha256sum`` text format.

A manifest line is ``<hex digest>  <relative/posix/path>`` (two spaces, UTF-8, ``\\n`` line ends),
the same layout that ``sha256sum`` writes and ``sha256sum -c`` reads. Reading is a little more
tolerant: a single ``*`` before the path (binary mode), CRLF line ends, a UTF-8 byte-order mark,
``\\`` as a path separator, blank lines and ``#`` comment lines are all accepted.

Nothing here lists folders. Hashing checks OneDrive state first (AGENTS.md rule 3), so a
cloud-only file raises ``CloudOnlyFileError`` instead of being downloaded.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from various_tools.core.fs import long_path
from various_tools.core.onedrive import ensure_on_device

Algorithm = Literal["sha256", "sha1", "md5", "sha512"]
ALGORITHMS: tuple[str, ...] = get_args(Algorithm)

# Length of the hex digest, which tells a line written for one algorithm from another.
HEX_LENGTH: dict[str, int] = {"sha256": 64, "sha1": 40, "md5": 32, "sha512": 128}
# Default manifest file name for each algorithm. ``sha256sum`` users know ``SHA256SUMS``.
DEFAULT_NAMES: dict[str, str] = {
    "sha256": "SHA256SUMS.txt",
    "sha1": "SHA1SUMS.txt",
    "md5": "MD5SUMS.txt",
    "sha512": "SHA512SUMS.txt",
}

_LINE = re.compile(r"^([0-9A-Fa-f]+) [ *](.+)$")
_WINDOWS_FORBIDDEN = '<>:"|?*'
_DRIVE = re.compile(r"^[A-Za-z]:")


def read_bytes(path: Path) -> bytes:
    """The whole file, opened through ``long_path`` so a path past 260 characters works."""
    with open(long_path(path), "rb") as handle:
        return handle.read()


def default_name(algorithm: str) -> str:
    return DEFAULT_NAMES[algorithm]


def file_digest(path: Path, algorithm: str, chunk_size: int) -> str:
    """Streaming hex digest of a file's contents.

    Raises :class:`~various_tools.core.onedrive.CloudOnlyFileError` for a cloud-only file, and
    ``OSError`` when the file cannot be read. ``chunk_size`` (bytes per read) changes speed and
    memory only, never the digest. MD5 and SHA-1 are offered to match existing manifests; they
    are checksums here, not security measures.
    """
    ensure_on_device(path)
    digest = hashlib.new(algorithm, usedforsecurity=False)
    # Through long_path, so a file past 260 characters opens even without LongPathsEnabled.
    with open(long_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(max(1, chunk_size)), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_line(digest: str, relative_posix: str) -> str:
    """One manifest line, without its line end."""
    return f"{digest.lower()}  {relative_posix}"


def render(entries: list[tuple[str, str]]) -> bytes:
    """The manifest file contents for ``(relative_posix_path, digest)`` pairs, as UTF-8 bytes."""
    return "".join(format_line(digest, rel) + "\n" for rel, digest in entries).encode("utf-8")


@dataclass(frozen=True)
class Entry:
    """A valid manifest line: the file's relative path (posix) and its lower-case digest."""

    line_no: int
    digest: str
    path: str


@dataclass(frozen=True)
class InvalidLine:
    line_no: int
    reason: str


def path_problem(raw: str) -> str | None:
    """Why a manifest path cannot be used, or ``None``. Only relative paths below the root pass."""
    if not raw.strip():
        return "the path is empty"
    if raw.startswith(("/", "\\")) or _DRIVE.match(raw):
        return "the path is absolute, so it could point outside the folder"
    if "\x00" in raw:
        return "the path contains a NUL character"
    parts = [p for p in raw.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return "the path is empty"
    if ".." in parts:
        return "the path contains '..', so it could point outside the folder"
    if any(ch in part for part in parts for ch in _WINDOWS_FORBIDDEN):
        return "the path has a character Windows does not allow in file names"
    return None


def normalise_path(raw: str) -> str:
    """The posix relative path of an accepted manifest path (separators and ``.`` tidied)."""
    return "/".join(p for p in raw.replace("\\", "/").split("/") if p not in ("", "."))


def decode(data: bytes) -> str:
    """Manifest text from bytes. UTF-8 only (a byte-order mark is ignored); else ``ValueError``."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("the manifest is not valid UTF-8 text") from exc


def parse(text: str, algorithm: str) -> tuple[list[Entry], list[InvalidLine]]:
    """Read manifest text. Returns the valid entries and the lines that could not be used.

    A line is invalid when it is not ``<hex>  <path>``, when the digest is not the length that
    ``algorithm`` produces, or when the path is absolute or escapes the folder. Invalid lines are
    reported, never guessed at.
    """
    expected = HEX_LENGTH[algorithm]
    entries: list[Entry] = []
    invalid: list[InvalidLine] = []
    for number, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("\\"):
            invalid.append(InvalidLine(number, "escaped file names are not supported"))
            continue
        match = _LINE.match(line)
        if match is None:
            invalid.append(InvalidLine(number, "not in the form '<digest>  <path>'"))
            continue
        digest, raw_path = match.group(1), match.group(2)
        if len(digest) != expected:
            invalid.append(
                InvalidLine(number, f"the digest is not {expected} hex characters ({algorithm})")
            )
            continue
        problem = path_problem(raw_path)
        if problem is not None:
            invalid.append(InvalidLine(number, problem))
            continue
        entries.append(Entry(number, digest.lower(), normalise_path(raw_path)))
    return entries, invalid
