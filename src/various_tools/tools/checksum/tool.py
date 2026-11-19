"""Checksum and verify (plan §4 Phase 7): write or check a ``sha256sum``-style manifest.

``create`` plans ONE ``write`` action per selected folder: the manifest file. The scan lists the
files (nothing is hashed, so a preview is quick and the data is read only once). The write handler
lists and hashes the folder at apply time, so the manifest describes the folder as it is when you
press Apply, not as it was at scan time. The file count in the preview is therefore a guide.

``verify`` is read-only. It reads a manifest, hashes the listed files and reports the result in
the plan notes (see ``docs/tools/checksum.md`` for the note formats). It has no actions.

Rules that always hold:
* an existing manifest is never overwritten (the scan leaves that action unselected, and apply
  raises), and the manifest is written to a temporary file and renamed into place;
* cloud-only OneDrive files are never read or hashed (AGENTS.md rule 3). They are reported as
  "skipped: cloud-only", and a manifest never lists them;
* junctions and symlinks are never followed (rule 4), and protected folders are refused (rule 6);
* manifest paths that are absolute or contain ``..`` are reported as invalid lines, never read;
* a file that cannot be read stops the create (default), so a manifest is never silently short.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from various_tools.core.context import RunContext
from various_tools.core.fs import (
    FileInfo,
    destination_refusal,
    is_inside,
    long_path,
    protected_reason,
    rename_no_replace,
    reparse_ancestor,
    root_refusal,
    walk,
)
from various_tools.core.onedrive import (
    FILE_ATTRIBUTE_DIRECTORY,
    CloudOnlyFileError,
    get_attributes,
    is_on_device,
    status_from_attributes,
)
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.tools.base import Category, ToolOptions
from various_tools.tools.checksum import manifest as mf
from various_tools.tools.checksum.manifest import Algorithm

log = logging.getLogger(__name__)

TOOL_ID = "checksum"
FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
_FORBIDDEN_NAME_CHARS = '\\/:*?"<>|'


def _hint(group: str, *, advanced: bool = False, locked: bool = False, **extra: Any) -> Any:
    """The ``json_schema_extra`` hints the GUI and CLI read (see ``core/settings.py``)."""
    hints: dict[str, Any] = {"group": group}
    if advanced:
        hints["advanced"] = True
    if locked:
        hints["locked"] = True
    hints.update(extra)
    return hints


class ChecksumOptions(ToolOptions):
    # ----- what to do ------------------------------------------------------------------
    mode: Literal["create", "verify"] = Field(
        default="create",
        title="What to do",
        description=(
            "create writes a checksum manifest file into each chosen folder. verify reads a "
            "manifest, re-hashes the files it lists and reports what matches, what changed, "
            "what is missing and what is new. verify changes nothing. Default: create."
        ),
        json_schema_extra=_hint("Mode"),
    )
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders",
        description=(
            "The folders to list and hash (create) or to check (verify). Each folder gets its own "
            "manifest, with paths relative to that folder. A drive root or a protected folder is "
            "refused."
        ),
        json_schema_extra=_hint("Mode", widget="folders"),
    )
    algorithm: Algorithm = Field(
        default="sha256",
        title="Hash algorithm",
        description=(
            "sha256 is the default and the right choice for new manifests. sha1, md5 and sha512 "
            "exist so that a manifest made by another program can be checked. Verify must use the "
            "algorithm the manifest was written with. Default: sha256."
        ),
        json_schema_extra=_hint("Manifest"),
    )
    manifest_name: str | None = Field(
        default=None,
        title="Manifest file name",
        description=(
            "The name of the manifest file inside each chosen folder (a plain name, not a "
            "path). Empty uses SHA256SUMS.txt, or SHA1SUMS.txt, MD5SUMS.txt or SHA512SUMS.txt "
            "for the other algorithms. An existing file with this name is never replaced."
        ),
        json_schema_extra=_hint("Manifest"),
    )
    manifest_file: Path | None = Field(
        default=None,
        title="Manifest to verify",
        description=(
            "Verify only: check this manifest file instead of the one inside the folder. Use it "
            "to confirm that a copy of a folder (for example a backup) matches a manifest made "
            "from the original. Needs exactly one folder. Empty uses the manifest inside the "
            "folder."
        ),
        json_schema_extra=_hint("Manifest", widget="file", depends_on="mode=verify"),
    )

    # ----- which files -----------------------------------------------------------------
    recursive: bool = Field(
        default=True,
        title="Include sub-folders",
        description=(
            "Also list the files in the sub-folders of each chosen folder. Turning it off lists "
            "only the files directly inside. Default: on."
        ),
        json_schema_extra=_hint("Files"),
    )
    include_globs: list[str] = Field(
        default_factory=list,
        title="Only these files",
        description=(
            "File names or relative paths (patterns such as *.jpg allowed). When any are given, "
            "only matching files are listed. Empty means every file. Default: empty."
        ),
        json_schema_extra=_hint("Files", widget="glob_list"),
    )
    exclude_globs: list[str] = Field(
        default_factory=list,
        title="Never include",
        description=(
            "File or folder names or relative paths (patterns allowed) that are left out, for "
            "example .git or *.tmp. A matching folder is skipped with everything in it. "
            "Default: empty."
        ),
        json_schema_extra=_hint("Files", widget="glob_list"),
    )
    include_hidden: bool = Field(
        default=False,
        title="Include hidden files",
        description=(
            "Also list files and folders that Windows marks hidden or system, and names that "
            "start with a dot. Off by default, because programs create and change these (for "
            "example desktop.ini and Thumbs.db), which would make a later verify fail."
        ),
        json_schema_extra=_hint("Files"),
    )

    # ----- verify and safety -----------------------------------------------------------
    report_extra: bool = Field(
        default=True,
        title="Report new files",
        description=(
            "Verify only: also list files that are in the folder but not in the manifest. "
            "They do not count as a failure. Default: on."
        ),
        json_schema_extra=_hint("Verify", depends_on="mode=verify"),
    )
    unreadable_files: Literal["fail", "skip"] = Field(
        default="fail",
        title="If a file cannot be read",
        description=(
            "Create only. fail stops without writing a manifest, so a manifest is never missing "
            "files without you knowing. skip leaves that file out and only logs it. Cloud-only "
            "OneDrive files are always skipped and never downloaded. Default: fail."
        ),
        json_schema_extra=_hint("Manifest", advanced=True, depends_on="mode=create"),
    )
    read_chunk_kb: int | None = Field(
        default=None,
        ge=4,
        le=65536,
        title="Read size (KB)",
        description=(
            "How much of a file is read at a time while hashing. Affects speed and memory only, "
            "never the result. Empty uses the app setting hash_chunk_kb (default 1024 KB)."
        ),
        json_schema_extra=_hint("Performance", advanced=True),
    )

    @field_validator("manifest_name")
    @classmethod
    def _plain_name(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        problem = name_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


def name_problem(name: str) -> str | None:
    """Why ``name`` cannot be a manifest file name, or None. A plain name only, never a path."""
    if not name.strip() or name in (".", ".."):
        return "The manifest file name is empty."
    if any(ch in name for ch in _FORBIDDEN_NAME_CHARS):
        return (
            f'The manifest file name cannot contain any of {_FORBIDDEN_NAME_CHARS} (got "{name}").'
        )
    if name != name.rstrip(" ."):
        return "The manifest file name cannot end with a space or a dot, which Windows drops."
    return None


def _manifest_name(opts: ChecksumOptions) -> str:
    return opts.manifest_name or mf.default_name(opts.algorithm)


def _chunk(opts: ChecksumOptions, ctx: RunContext) -> int:
    return (opts.read_chunk_kb or ctx.settings.hash_chunk_kb) * 1024


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _same(a: Path, b: Path) -> bool:
    return _norm(a) == _norm(b)


def _cloud_only(info: FileInfo) -> bool:
    return not is_on_device(status_from_attributes(info.attributes))


def _remember_cloud_only(plan: Plan, path: Path) -> None:
    """Record a file the scan skipped because it is cloud-only (once), for the download action."""
    if path not in plan.cloud_only:
        plan.cloud_only.append(path)


def _size_label(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"  # pragma: no cover - the loop always returns


def _hidden_attributes(path: Path) -> int:
    try:
        return getattr(os.stat(path, follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return 0


def _is_hidden(info: FileInfo, root: Path, folders: dict[str, bool]) -> bool:
    """Hidden or system by attribute, or a dot name, for the file or any folder above it."""
    if info.attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
        return True
    if info.path.name.startswith("."):
        return True
    folder = root
    for part in info.path.relative_to(root).parts[:-1]:
        folder = folder / part
        key = _norm(folder)
        if key not in folders:
            folders[key] = part.startswith(".") or bool(
                _hidden_attributes(folder) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
            )
        if folders[key]:
            return True
    return False


def list_files(root: Path, opts: ChecksumOptions, manifest: Path | None) -> list[FileInfo]:
    """The files a manifest for ``root`` covers, sorted by relative path. Opens no file.

    Junctions and symlinks are skipped (never followed). ``manifest`` itself is left out, so a
    manifest never lists itself.
    """
    hidden_cache: dict[str, bool] = {}
    found: list[FileInfo] = []
    for info in walk(
        root,
        include_globs=opts.include_globs,
        exclude_globs=opts.exclude_globs,
        recursive=opts.recursive,
    ):
        if manifest is not None and _same(info.path, manifest):
            continue
        if not opts.include_hidden and _is_hidden(info, root, hidden_cache):
            continue
        found.append(info)
    found.sort(key=lambda i: _sort_key(rel_posix(i.path, root)))
    return found


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _sort_key(rel: str) -> tuple[str, str]:
    return (rel.casefold(), rel)


def _usable_roots(opts: ChecksumOptions, ctx: RunContext, notes: list[str]) -> list[Path]:
    roots: list[Path] = []
    for raw in opts.roots:
        root = Path(os.path.abspath(raw))
        refusal = root_refusal(root, ctx.settings.protected_paths())
        if refusal is not None:
            notes.append(refusal)
            continue
        if any(_same(root, other) for other in roots):
            continue
        roots.append(root)
    return roots


# ----- create ---------------------------------------------------------------------------------


def _plan_create(opts: ChecksumOptions, roots: list[Path], ctx: RunContext, plan: Plan) -> None:
    name = _manifest_name(opts)
    problem = name_problem(name)
    if problem is not None:
        plan.notes.append(problem)
        return
    if opts.manifest_file is not None:
        plan.notes.append("The 'Manifest to verify' option is only used when verifying; ignored.")

    detail_notes: list[str] = []
    total_files = total_bytes = cloud_only = writable = 0
    for index, root in enumerate(roots, start=1):
        ctx.check_cancelled()
        ctx.report(index - 1, len(roots), f"Listing {root}")
        manifest = root / name
        files = list_files(root, opts, manifest)
        readable = [info for info in files if not _cloud_only(info)]
        for info in files:
            if _cloud_only(info):
                detail_notes.append(f"skipped: cloud-only: {rel_posix(info.path, root)}")
                _remember_cloud_only(plan, info.path)
        cloud_only += len(files) - len(readable)
        if not readable:
            detail_notes.append(f"No files to list in {root}; no manifest planned.")
            continue
        size = sum(info.size for info in readable)
        total_files += len(readable)
        total_bytes += size
        selected = True
        reason = f"write {name} listing {len(readable)} file(s), {_size_label(size)}"
        if os.path.lexists(long_path(manifest)):
            selected = False
            reason += "; NOT written because the file already exists and is never replaced"
            detail_notes.append(
                f"Manifest already exists, never replaced: {manifest} "
                "(remove it or choose another manifest file name)"
            )
        else:
            refusal = destination_refusal(manifest, root)
            if refusal is not None:
                selected = False
                reason += f"; NOT written: {refusal}"
                detail_notes.append(refusal)
        writable += selected
        plan.actions.append(
            PlannedAction(kind=ActionKind.WRITE, dst=manifest, reason=reason, selected=selected)
        )
    ctx.report(len(roots), len(roots), "Listed")

    summary = (
        f"Create: {writable} manifest(s) ready to write for {total_files} file(s) "
        f"({_size_label(total_bytes)}), {mf.HEX_LENGTH[opts.algorithm] * 4}-bit {opts.algorithm}. "
        "Files are hashed when you apply, so the manifest reflects the folder at that moment."
    )
    if cloud_only:
        summary += f" {cloud_only} cloud-only file(s) skipped and never downloaded."
    plan.notes.insert(0, summary)
    plan.notes.extend(detail_notes)
    ctx.log.info("checksum create scan: %d manifest(s), %d file(s)", writable, total_files)


@dataclass
class _Written:
    count: int = 0
    cloud_only: list[str] = field(default_factory=list[str])
    unreadable: list[str] = field(default_factory=list[str])

    def detail(self) -> str:
        text = f"wrote {self.count} entr{'y' if self.count == 1 else 'ies'}"
        if self.cloud_only:
            text += f"; skipped {len(self.cloud_only)} cloud-only file(s)"
        if self.unreadable:
            text += f"; left out {len(self.unreadable)} unreadable file(s)"
        return text


class _ManifestWriter:
    """The ``write`` handler: re-checks the paths, hashes the folder and writes the manifest."""

    def __init__(self, opts: ChecksumOptions, roots: list[Path], ctx: RunContext) -> None:
        self.opts = opts
        self.roots = roots
        self.ctx = ctx
        self.written: dict[str, _Written] = {}

    def __call__(self, action: PlannedAction) -> Path | None:
        dst = action.dst
        if dst is None:
            raise ValueError("the manifest has no output path")
        root = self._check_paths(dst)
        if os.path.lexists(long_path(dst)):
            raise FileExistsError(f"the manifest already exists, so it was not replaced: {dst}")
        refusal = destination_refusal(dst, root)
        if refusal is not None:
            raise ValueError(refusal)

        entries, outcome = self._hash(root, dst)
        if not entries:
            raise ValueError(f"no files to list in {root}")
        self._write(dst, mf.render(entries))
        outcome.count = len(entries)
        self.written[_norm(dst)] = outcome
        return dst

    def _check_paths(self, dst: Path) -> Path:
        containing = [r for r in self.roots if is_inside(dst, r)]
        if not containing:
            raise ValueError(f"the manifest is outside the selected folders: {dst}")
        root = max(containing, key=lambda r: len(str(r)))
        if not _same(dst.parent, root):
            raise ValueError(f"the manifest must sit directly in the selected folder: {dst}")
        problem = name_problem(dst.name)
        if problem is not None:
            raise ValueError(problem)
        refusal = root_refusal(root, self.ctx.settings.protected_paths())
        if refusal is not None:
            raise ValueError(refusal)
        if not self.ctx.settings.allow_protected_paths:
            reason = protected_reason(dst, rules=self.ctx.settings.protected_paths())
            if reason:
                raise ValueError(f"protected path ({reason}): {dst}")
        return root

    def _hash(self, root: Path, manifest: Path) -> tuple[list[tuple[str, str]], _Written]:
        ctx = self.ctx
        chunk = _chunk(self.opts, ctx)
        files = list_files(root, self.opts, manifest)
        outcome = _Written()
        entries: list[tuple[str, str]] = []
        total = len(files)
        for index, info in enumerate(files, start=1):
            ctx.check_cancelled()
            rel = rel_posix(info.path, root)
            ctx.report(index - 1, total, f"Hashing {rel}")
            if _cloud_only(info):
                outcome.cloud_only.append(rel)
                ctx.log.info("checksum: skipped cloud-only %s", info.path)
                continue
            try:
                digest = mf.file_digest(info.path, self.opts.algorithm, chunk)
            except CloudOnlyFileError:
                outcome.cloud_only.append(rel)
                ctx.log.info("checksum: skipped cloud-only %s", info.path)
            except OSError as exc:
                if self.opts.unreadable_files == "fail":
                    raise OSError(f"could not read {rel}: {exc.strerror or exc}") from exc
                outcome.unreadable.append(rel)
                ctx.log.warning("checksum: left out unreadable %s: %s", info.path, exc)
            else:
                entries.append((rel, digest))
        ctx.report(total, total, "Hashed")
        return entries, outcome

    @staticmethod
    def _write(dst: Path, data: bytes) -> None:
        """Write to a temporary file in the same folder, then rename it in without replacing."""
        fd, tmp_name = tempfile.mkstemp(
            dir=long_path(dst.parent), prefix=f".{dst.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            rename_no_replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)


# ----- verify ---------------------------------------------------------------------------------


@dataclass
class _Tally:
    ok: int = 0
    mismatch: int = 0
    missing: int = 0
    unreadable: int = 0
    cloud_only: int = 0
    extra: int = 0
    invalid: int = 0
    no_manifest: int = 0

    @property
    def problems(self) -> int:
        return (
            self.mismatch
            + self.missing
            + self.unreadable
            + self.cloud_only
            + self.invalid
            + self.no_manifest
        )


def _check_entry(root: Path, entry: mf.Entry, algorithm: str, chunk: int) -> tuple[str, str]:
    """``(status, note)`` for one entry. Status: ok, mismatch, missing, unreadable or cloud."""
    path = root.joinpath(*entry.path.split("/"))
    link = reparse_ancestor(path, root)
    if link is not None:
        return "unreadable", f"unreadable: {entry.path} (junction or symlink, not followed)"
    attributes = get_attributes(path)
    if attributes is None:
        return "missing", f"missing: {entry.path}"
    if attributes & FILE_ATTRIBUTE_DIRECTORY:
        return "unreadable", f"unreadable: {entry.path} (not a regular file)"
    if not is_on_device(status_from_attributes(attributes)):
        return "cloud", f"skipped: cloud-only: {entry.path}"
    try:
        digest = mf.file_digest(path, algorithm, chunk)
    except CloudOnlyFileError:
        return "cloud", f"skipped: cloud-only: {entry.path}"
    except OSError as exc:
        return "unreadable", f"unreadable: {entry.path} ({exc.strerror or exc})"
    if digest != entry.digest:
        return "mismatch", f"mismatch: {entry.path}"
    return "ok", ""


def _verify_root(
    root: Path, manifest: Path, opts: ChecksumOptions, ctx: RunContext, tally: _Tally, plan: Plan
) -> list[str]:
    """Check one folder against its manifest. Returns that folder's notes (without the summary)."""
    notes: list[str] = []
    if not os.path.lexists(long_path(manifest)):
        tally.no_manifest += 1
        return [f"root: {root}", f"Manifest not found: {manifest}"]
    attributes = get_attributes(manifest) or 0
    if not is_on_device(status_from_attributes(attributes)):
        tally.no_manifest += 1
        _remember_cloud_only(plan, manifest)
        return [f"root: {root}", f"skipped: cloud-only: manifest {manifest}"]
    try:
        text = mf.decode(mf.read_bytes(manifest))
    except (OSError, ValueError) as exc:
        tally.no_manifest += 1
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        return [f"root: {root}", f"Could not read the manifest {manifest}: {reason}"]

    entries, invalid = mf.parse(text, opts.algorithm)
    tally.invalid += len(invalid)
    notes.append(
        f"root: {root} (manifest {manifest.name}, {len(entries)} entr"
        f"{'y' if len(entries) == 1 else 'ies'})"
    )
    for bad in invalid:
        notes.append(f"invalid: line {bad.line_no}: {bad.reason}")

    chunk = _chunk(opts, ctx)
    problems: dict[str, list[str]] = {
        "mismatch": [],
        "missing": [],
        "unreadable": [],
        "cloud": [],
    }
    total = len(entries)
    for index, entry in enumerate(entries, start=1):
        ctx.check_cancelled()
        ctx.report(index - 1, total, f"Checking {entry.path}")
        status, note = _check_entry(root, entry, opts.algorithm, chunk)
        if status == "ok":
            tally.ok += 1
            continue
        problems[status].append(note)
        if status == "cloud":
            tally.cloud_only += 1
            _remember_cloud_only(plan, root.joinpath(*entry.path.split("/")))
        else:
            setattr(tally, status, getattr(tally, status) + 1)
    ctx.report(total, total, "Checked")
    for status in ("mismatch", "missing", "unreadable", "cloud"):
        notes.extend(problems[status])

    if opts.report_extra:
        listed = {entry.path.casefold() for entry in entries}
        for info in list_files(root, opts, manifest):
            rel = rel_posix(info.path, root)
            if rel.casefold() not in listed:
                tally.extra += 1
                notes.append(f"extra: {rel}")
    return notes


def _plan_verify(opts: ChecksumOptions, roots: list[Path], ctx: RunContext, plan: Plan) -> None:
    name = _manifest_name(opts)
    if opts.manifest_file is None:
        problem = name_problem(name)
        if problem is not None:
            plan.notes.append(problem)
            return
    elif len(roots) != 1:
        plan.notes.append("A manifest file can be checked against one folder at a time.")
        return

    tally = _Tally()
    body: list[str] = []
    for root in roots:
        manifest = Path(os.path.abspath(opts.manifest_file)) if opts.manifest_file else root / name
        body.extend(_verify_root(root, manifest, opts, ctx, tally, plan))

    verdict = "OK" if tally.problems == 0 else "PROBLEMS FOUND"
    plan.notes.insert(
        0,
        f"Verify: {verdict} - {tally.ok} OK, {tally.mismatch} mismatched, {tally.missing} "
        f"missing, {tally.unreadable} unreadable, {tally.cloud_only} skipped (cloud-only), "
        f"{tally.extra} extra, {tally.invalid} invalid line(s), "
        f"{tally.no_manifest} manifest(s) not usable.",
    )
    plan.notes.extend(body)
    ctx.log.info("checksum verify: %s", plan.notes[0])


class Checksum:
    id = TOOL_ID
    name = "Checksum and verify"
    # Plain words for the GUI (gui/wording.py).
    scan_label = "Check the files"
    found_nouns = ("file", "files")
    description = (
        "Write a SHA-256 checksum manifest for a folder, or check a folder against one, for "
        "example to confirm that a backup copy matches. Compatible with sha256sum."
    )
    category: Category = "Developer"
    Options = ChecksumOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, ChecksumOptions):
            raise TypeError("Checksum.scan needs ChecksumOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        if not opts.roots:
            plan.notes.append("Choose at least one folder.")
            return plan
        roots = _usable_roots(opts, ctx, plan.notes)
        if not roots:
            plan.notes.append("Nothing to do: no usable folder.")
            return plan
        plan.roots = roots
        if opts.mode == "create":
            _plan_create(opts, roots, ctx, plan)
        else:
            _plan_verify(opts, roots, ctx, plan)
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        opts = ChecksumOptions.model_validate_json(plan.options_json)
        writer = _ManifestWriter(opts, plan.roots, ctx)
        result = apply_plan(plan, ctx, handlers={ActionKind.WRITE: writer})
        # Say how many entries each manifest got, and what was left out.
        for index, outcome in enumerate(result.outcomes):
            if outcome.status == "done" and outcome.dst is not None:
                written = writer.written.get(_norm(Path(outcome.dst)))
                if written is not None:
                    result.outcomes[index] = replace(outcome, detail=written.detail())
        return result


TOOL = Checksum()
