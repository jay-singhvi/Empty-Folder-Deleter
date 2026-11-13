"""Project Export (plan §3.2): a folder's files as one text file, e.g. to give an LLM context.

``mode`` picks the tree (the file list, like the original ``Extract_Project_Structure``), the
contents (like ``Extract_Project_Code``) or both. The plan is a single ``write`` action. ``scan()``
reads the selected files to measure the export, but it writes nothing. ``apply()`` renders the
export again from the saved options, so a plan saved as JSON can be applied later.

Rules:
* the output file is never written over an existing file: a free name is used instead;
* the output file is left out of the export, even when it sits inside the selected folder, and
  so are earlier exports next to it (``<name>_dupN.txt`` / ``.md`` and leftover ``.partial`` files);
* cloud-only OneDrive files are never read (AGENTS.md rule 3); they are listed as skipped;
* junctions and symlinks are never followed (rule 4), and the selected folder is never written.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from various_tools.core.context import RunContext
from various_tools.core.fs import (
    FileInfo,
    destination_refusal,
    long_path,
    protected_reason,
    rename_no_replace,
    root_refusal,
    unique_destination,
    walk,
)
from various_tools.core.onedrive import is_on_device, status_from_attributes
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.tools.base import ToolOptions
from various_tools.tools.project_export.source import file_text

DEFAULT_EXCLUDE_GLOBS = (".git", ".venv", "__pycache__", "node_modules", "dist", "build")
DEFAULT_OUTPUT_NAME = "Project-File-Content"
SEPARATOR = "=" * 50
CHARS_PER_TOKEN = 4  # a rough estimate for the preview only

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".md": "markdown",
    ".json": "json",
    ".toml": "toml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".js": "javascript",
    ".ts": "typescript",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
    ".ps1": "powershell",
    ".sh": "bash",
    ".xml": "xml",
}


class ProjectExportOptions(ToolOptions):
    root: Path = Field(json_schema_extra={"widget": "folder", "group": "Source"})
    include_globs: list[str] = Field(
        default_factory=list,
        json_schema_extra={"widget": "glob_list", "group": "Source", "advanced": True},
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS),
        json_schema_extra={"widget": "glob_list", "group": "Source", "advanced": True},
    )
    exts: list[str] = Field(
        default_factory=list,
        json_schema_extra={"widget": "ext_list", "group": "Source"},
    )
    respect_gitignore: bool = Field(
        default=True,
        json_schema_extra={"group": "Source", "advanced": True},
    )
    mode: Literal["tree", "contents", "both"] = Field(
        default="both",
        json_schema_extra={"group": "Output"},
    )
    output_format: Literal["txt", "md"] = Field(
        default="txt",
        json_schema_extra={"group": "Output"},
    )
    output_path: Path | None = Field(
        default=None,
        json_schema_extra={"widget": "file", "group": "Output"},
    )
    strip_comments: bool = Field(
        default=True,
        json_schema_extra={"group": "Content"},
    )
    include_markdown_cells: bool = Field(
        default=True,
        json_schema_extra={"group": "Content", "advanced": True},
    )
    include_outputs: bool = Field(
        default=False,
        json_schema_extra={"group": "Content", "advanced": True},
    )
    max_file_kb: int | None = Field(
        default=None,
        ge=1,
        json_schema_extra={"group": "Content", "advanced": True},
    )


@dataclass(frozen=True)
class _Entry:
    rel: Path
    text: str  # empty in tree mode, where contents are never read


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _output_path(opts: ProjectExportOptions) -> Path:
    if opts.output_path is not None:
        return Path(os.path.abspath(opts.output_path))
    extension = "md" if opts.output_format == "md" else "txt"
    return Path(os.path.abspath(Path(opts.root) / f"{DEFAULT_OUTPUT_NAME}.{extension}"))


def _output_refusal(output: Path, ctx: RunContext) -> str | None:
    reason = protected_reason(output)
    if reason and not ctx.settings.allow_protected_paths:
        return f"Output path is a protected location ({reason}): {output}"
    if output.is_dir():
        return f"Output path is a folder: {output}"
    if not output.parent.is_dir():
        return f"Output folder does not exist: {output.parent}"
    refusal = destination_refusal(output)
    if refusal is not None:
        return f"Output path refused: {refusal}"
    return None


def _is_prior_export(path: Path, output: Path) -> bool:
    """True for the output file and earlier exports beside it: ``stem``, ``stem_dupN`` with a
    ``.txt`` or ``.md`` extension, plus their ``.partial`` leftovers."""
    if _key(path.parent) != _key(output.parent):
        return False
    pattern = re.escape(output.stem.casefold()) + r"(_dup\d+)?\.(txt|md)(\.partial)?"
    return re.fullmatch(pattern, path.name.casefold()) is not None


def _extensions(opts: ProjectExportOptions) -> set[str] | None:
    wanted = {"." + ext.strip().lower().lstrip(".") for ext in opts.exts}
    wanted.discard(".")
    return wanted or None


def _selected_files(root: Path, opts: ProjectExportOptions, output: Path) -> list[FileInfo]:
    """Files that pass the filters, in the original script's order (folders before the files
    after them, names compared without case). The output file itself is always left out."""
    found = [
        info
        for info in walk(
            root,
            include_globs=opts.include_globs,
            exclude_globs=opts.exclude_globs,
            exts=_extensions(opts),
            respect_gitignore=opts.respect_gitignore,
        )
        if not _is_prior_export(info.path, output)
    ]
    found.sort(key=lambda info: tuple(p.casefold() for p in info.path.relative_to(root).parts))
    return found


def _cloud_only(info: FileInfo) -> bool:
    return not is_on_device(status_from_attributes(info.attributes))


def _gather(
    root: Path,
    opts: ProjectExportOptions,
    output: Path,
    ctx: RunContext,
    notes: list[str],
) -> list[_Entry]:
    """Read the selected files. Skipped files are reported in ``notes``."""
    infos = _selected_files(root, opts, output)
    entries: list[_Entry] = []
    total = len(infos)
    for index, info in enumerate(infos, start=1):
        ctx.check_cancelled()
        rel = info.path.relative_to(root)
        ctx.report(index - 1, total, f"Reading {rel.as_posix()}")
        if opts.mode == "tree":
            entries.append(_Entry(rel, ""))
            continue
        if _cloud_only(info):
            notes.append(f"skipped: cloud-only, not downloaded: {rel.as_posix()}")
            continue
        if opts.max_file_kb is not None and info.size > opts.max_file_kb * 1024:
            notes.append(f"skipped: larger than {opts.max_file_kb} KB: {rel.as_posix()}")
            continue
        try:
            text = file_text(
                info.path,
                strip_comments=opts.strip_comments,
                include_markdown=opts.include_markdown_cells,
                include_outputs=opts.include_outputs,
            )
        except OSError as exc:
            notes.append(f"skipped: could not read {rel.as_posix()} ({exc.strerror or exc})")
            continue
        entries.append(_Entry(rel, text))
    ctx.report(total, total, "Export read")
    return entries


def _tree_lines(rels: list[Path]) -> list[str]:
    lines: list[str] = []
    shown: set[tuple[str, ...]] = set()
    for rel in rels:
        for depth in range(len(rel.parts) - 1):
            folder = rel.parts[: depth + 1]
            if folder not in shown:
                shown.add(folder)
                lines.append("  " * depth + folder[-1] + "/")
        lines.append("  " * (len(rel.parts) - 1) + rel.parts[-1])
    return lines


def _render_txt(root: Path, opts: ProjectExportOptions, entries: list[_Entry]) -> str:
    """The original script's layout: a separator block before each folder and each file."""
    abs_root = os.path.abspath(root)
    parts: list[str] = []
    if opts.mode in ("tree", "both"):
        tree = "\n".join(_tree_lines([e.rel for e in entries]))
        parts.append(f"{SEPARATOR}\nProject tree: {abs_root}\n{SEPARATOR}\n{tree}\n")
    if opts.mode in ("contents", "both"):
        parts.append(f"\n{SEPARATOR}\nDirectory: {abs_root}\n{SEPARATOR}\n")
        shown: set[tuple[str, ...]] = set()
        for entry in entries:
            for depth in range(len(entry.rel.parts) - 1):
                folder = entry.rel.parts[: depth + 1]
                if folder not in shown:
                    shown.add(folder)
                    folder_path = os.path.abspath(root.joinpath(*folder))
                    parts.append(f"\n{SEPARATOR}\nDirectory: {folder_path}\n{SEPARATOR}\n")
            file_path = os.path.abspath(root / entry.rel)
            parts.append(f"\n{SEPARATOR}\nFile: {file_path}\n{SEPARATOR}\n")
            parts.append(entry.text)
            parts.append("\n\n")
    return "".join(parts)


def _fence(text: str) -> str:
    """A code fence longer than any backtick run in ``text``, so the file cannot close it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _render_md(root: Path, opts: ProjectExportOptions, entries: list[_Entry]) -> str:
    parts: list[str] = [f"# Project export: {root.name}\n\nRoot: {os.path.abspath(root)}\n"]
    if opts.mode in ("tree", "both"):
        tree = "\n".join(_tree_lines([e.rel for e in entries]))
        parts.append(f"\n## Tree\n\n```text\n{tree}\n```\n")
    if opts.mode in ("contents", "both"):
        parts.append("\n## Files\n")
        for entry in entries:
            body = entry.text if entry.text.endswith("\n") else entry.text + "\n"
            fence = _fence(body)
            language = LANGUAGE_BY_SUFFIX.get(entry.rel.suffix.lower(), "")
            parts.append(f"\n### {entry.rel.as_posix()}\n\n{fence}{language}\n{body}{fence}\n")
    return "".join(parts)


def _render(root: Path, opts: ProjectExportOptions, entries: list[_Entry]) -> str:
    if opts.output_format == "md":
        return _render_md(root, opts, entries)
    return _render_txt(root, opts, entries)


def _export_text(root: Path, opts: ProjectExportOptions, output: Path, ctx: RunContext) -> str:
    """The whole export as text. Used by scan (to measure) and by apply (to write)."""
    entries = _gather(root, opts, output, ctx, notes=[])
    return _render(root, opts, entries)


class ProjectExport:
    id = "project_export"
    name = "Project Export"
    description = (
        "Write a folder's file list and file contents to one text file, for example to give an "
        "AI assistant the context of a project. Comments can be stripped from Python files."
    )
    category = "Developer"
    Options = ProjectExportOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, ProjectExportOptions):
            raise TypeError("ProjectExport.scan needs ProjectExportOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        root = Path(os.path.abspath(opts.root))
        refusal = root_refusal(root, ctx.settings.allow_protected_paths)
        if refusal is not None:
            plan.notes.append(refusal)
            plan.notes.append("Nothing to export.")
            return plan
        output = _output_path(opts)
        refusal = _output_refusal(output, ctx)
        if refusal is not None:
            plan.notes.append(refusal)
            return plan

        plan.roots = [root]
        entries = _gather(root, opts, output, ctx, plan.notes)
        text = _render(root, opts, entries)
        tokens = len(text) // CHARS_PER_TOKEN
        plan.actions = [
            PlannedAction(
                kind=ActionKind.WRITE,
                dst=output,
                reason=f"write {len(entries)} file(s), about {tokens:,} tokens",
            )
        ]
        plan.notes.insert(
            0,
            f"Export of {len(entries)} file(s), about {tokens:,} tokens "
            f"({len(text):,} characters), to {output}.",
        )
        ctx.log.info("project_export scan: %d file(s)", len(entries))
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        def write_export(action: PlannedAction) -> Path | None:
            assert action.dst is not None
            opts = ProjectExportOptions.model_validate_json(plan.options_json)
            root = Path(os.path.abspath(opts.root))
            refusal = root_refusal(root, ctx.settings.allow_protected_paths) or _output_refusal(
                action.dst, ctx
            )
            if refusal is not None:
                raise PermissionError(refusal)
            text = _export_text(root, opts, action.dst, ctx)
            # Never overwrite: an existing file keeps its place and the export gets a free name.
            target = unique_destination(action.dst.parent, action.dst.name)
            partial = unique_destination(target.parent, target.name + ".partial")
            # "x" and a no-replace rename: nothing that appears meanwhile is ever overwritten.
            with open(long_path(partial), "x", encoding="utf-8") as handle:
                handle.write(text)
            try:
                rename_no_replace(partial, target)
            except OSError:
                os.unlink(long_path(partial))
                raise
            return target

        return apply_plan(plan, ctx, handlers={ActionKind.WRITE: write_export})


TOOL = ProjectExport()
