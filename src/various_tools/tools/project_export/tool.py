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

from pydantic import Field, field_validator

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
from various_tools.tools.base import Category, ToolOptions
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


_BAD_NAME_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|$)", re.IGNORECASE)


def _name_problem(name: str) -> str | None:
    """Why ``name`` cannot be the stem of an output file name on Windows, or None."""
    if not name.strip() or name in (".", ".."):
        return "must not be empty"
    if any(ch in _BAD_NAME_CHARS or ord(ch) < 32 for ch in name):
        return "must be a plain file name, without folders or characters Windows does not allow"
    if name.endswith((" ", ".")):
        return "must not end with a space or a dot"
    if _RESERVED_NAME.match(name):
        return "is a reserved Windows name"
    return None


class ProjectExportOptions(ToolOptions):
    root: Path = Field(
        title="Folder to export",
        description=(
            "The folder whose files are exported. It is never written to, and a drive root or "
            "protected folder is refused."
        ),
        json_schema_extra={"widget": "folder", "group": "Source"},
    )
    include_globs: list[str] = Field(
        default_factory=list,
        title="Only include",
        description=(
            "When not empty, only files whose name or relative path matches one of these "
            "patterns (for example *.py or src/*) are exported. Empty includes everything."
        ),
        json_schema_extra={"widget": "glob_list", "group": "Source", "advanced": True},
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS),
        title="Leave out",
        description=(
            "Files and folders whose name or relative path matches one of these patterns are "
            "left out, with everything inside them. Default: .git, .venv, __pycache__, "
            "node_modules, dist, build."
        ),
        json_schema_extra={"widget": "glob_list", "group": "Source", "advanced": True},
    )
    exts: list[str] = Field(
        default_factory=list,
        title="File types",
        description=(
            "When not empty, only files with these extensions are exported, for example py or "
            ".ipynb (the dot is optional). Empty includes every file type."
        ),
        json_schema_extra={"widget": "ext_list", "group": "Source"},
    )
    respect_gitignore: bool = Field(
        default=True,
        title="Respect .gitignore",
        description=(
            "Leave out files that a .gitignore in the folder or its subfolders ignores. "
            "On by default."
        ),
        json_schema_extra={"group": "Source", "advanced": True},
    )
    mode: Literal["tree", "contents", "both"] = Field(
        default="both",
        title="What to export",
        description=(
            "tree writes only the file list as an indented tree, contents writes only the file "
            "text, both writes the tree and then the contents."
        ),
        json_schema_extra={"group": "Output"},
    )
    output_format: Literal["txt", "md"] = Field(
        default="txt",
        title="Output format",
        description=(
            "txt uses separator lines, like the original scripts. md writes headings and fenced "
            "code blocks for Markdown viewers."
        ),
        json_schema_extra={"group": "Output"},
    )
    output_path: Path | None = Field(
        default=None,
        title="Output file",
        description=(
            "Where the export is written. Empty writes the output name below, with the format's "
            "extension, into the exported folder. An existing file is never overwritten."
        ),
        json_schema_extra={"widget": "file", "group": "Output"},
    )
    output_name: str = Field(
        default=DEFAULT_OUTPUT_NAME,
        min_length=1,
        title="Output file name",
        description=(
            "Name, without extension, of the export file when no output file is chosen. A "
            "plain file name: no folders. Default: Project-File-Content."
        ),
        json_schema_extra={"group": "Output"},
    )
    separator: str = Field(
        default=SEPARATOR,
        min_length=1,
        max_length=200,
        title="Separator line",
        description=(
            "The line printed above and below each folder, file and tree heading in txt "
            "exports. One line of text. Default: 50 equals signs. Not used in md exports."
        ),
        json_schema_extra={"group": "Output", "advanced": True},
    )
    language_by_suffix: dict[str, str] = Field(
        default_factory=lambda: dict(LANGUAGE_BY_SUFFIX),
        title="Code block languages",
        description=(
            "For md exports: the language written after each code fence, by file extension "
            "(for example .py = python), so viewers colour the code. Files with other "
            "extensions get a plain block."
        ),
        json_schema_extra={"group": "Output", "advanced": True},
    )
    chars_per_token: int = Field(
        default=CHARS_PER_TOKEN,
        ge=1,
        le=100,
        title="Characters per token",
        description=(
            "Only for the size estimate in the preview: how many characters count as one AI "
            "token. About 4 suits English text and code. It never changes the export."
        ),
        json_schema_extra={"group": "Output", "advanced": True},
    )
    strip_comments: bool = Field(
        default=True,
        title="Remove Python comments",
        description=(
            "Remove comments from Python files and notebook code cells. String literals are "
            "kept, including triple-quoted strings and a # inside a string."
        ),
        json_schema_extra={"group": "Content"},
    )
    include_markdown_cells: bool = Field(
        default=True,
        title="Include notebook text cells",
        description="Include the Markdown (text) cells of Jupyter notebooks in the export.",
        json_schema_extra={"group": "Content", "advanced": True},
    )
    include_outputs: bool = Field(
        default=False,
        title="Include notebook outputs",
        description=(
            "Include printed output and results of notebook cells. They can contain secrets, so "
            "this is off by default."
        ),
        json_schema_extra={"group": "Content", "advanced": True},
    )
    max_file_kb: int | None = Field(
        default=None,
        ge=1,
        le=1_048_576,
        title="Largest file (KB)",
        description=(
            "Files larger than this many kilobytes are skipped and listed in the preview. "
            "Empty means no limit."
        ),
        json_schema_extra={"group": "Content", "advanced": True},
    )

    @field_validator("output_name")
    @classmethod
    def _plain_output_name(cls, value: str) -> str:
        problem = _name_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value

    @field_validator("separator")
    @classmethod
    def _one_line_separator(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("must be a single line")
        return value

    @field_validator("language_by_suffix")
    @classmethod
    def _clean_languages(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for suffix, language in value.items():
            key = "." + suffix.strip().lower().lstrip(".")
            name = language.strip()
            if key == "." or any(c.isspace() or c == "`" for c in name):
                raise ValueError(f"{suffix!r}: needs an extension and a one-word language name")
            cleaned[key] = name
        return cleaned


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
    return Path(os.path.abspath(Path(opts.root) / f"{opts.output_name}.{extension}"))


def _output_refusal(output: Path, ctx: RunContext) -> str | None:
    reason = protected_reason(output, rules=ctx.settings.protected_paths())
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
    sep = opts.separator
    parts: list[str] = []
    if opts.mode in ("tree", "both"):
        tree = "\n".join(_tree_lines([e.rel for e in entries]))
        parts.append(f"{sep}\nProject tree: {abs_root}\n{sep}\n{tree}\n")
    if opts.mode in ("contents", "both"):
        parts.append(f"\n{sep}\nDirectory: {abs_root}\n{sep}\n")
        shown: set[tuple[str, ...]] = set()
        for entry in entries:
            for depth in range(len(entry.rel.parts) - 1):
                folder = entry.rel.parts[: depth + 1]
                if folder not in shown:
                    shown.add(folder)
                    folder_path = os.path.abspath(root.joinpath(*folder))
                    parts.append(f"\n{sep}\nDirectory: {folder_path}\n{sep}\n")
            file_path = os.path.abspath(root / entry.rel)
            parts.append(f"\n{sep}\nFile: {file_path}\n{sep}\n")
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
            language = opts.language_by_suffix.get(entry.rel.suffix.lower(), "")
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
    category: Category = "Developer"
    Options = ProjectExportOptions

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        if not isinstance(opts, ProjectExportOptions):
            raise TypeError("ProjectExport.scan needs ProjectExportOptions")
        plan = Plan(tool=self.id, roots=[], options_json=opts.model_dump_json())
        root = Path(os.path.abspath(opts.root))
        refusal = root_refusal(root, ctx.settings.protected_paths())
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
        tokens = len(text) // opts.chars_per_token
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
            refusal = root_refusal(root, ctx.settings.protected_paths()) or _output_refusal(
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
