"""Project Export: parity with the original scripts, the §3.2 rules, and apply/undo safety.

Every tree is generated under ``tmp_path``. Outputs go to ``tmp_path`` too, never into a real
folder. The original scripts are read from ``legacy/``. The v3 module is imported (its entry code is
behind ``__main__``). The v1 function is extracted with ``ast``, because the v1 module walks the
current folder when it is imported.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib.util
import io
import json
import os
import tomllib
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from pydantic import ValidationError

from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.plan import Plan
from various_tools.core.settings import Settings
from various_tools.tools import project_export as project_export_pkg
from various_tools.tools.base import discover
from various_tools.tools.project_export import TOOL, ProjectExportOptions
from various_tools.tools.project_export import tool as export_tool
from various_tools.tools.project_export.source import file_text, strip_python_comments

REPO = Path(__file__).resolve().parents[3]
LEGACY_V1 = REPO / "legacy" / "python-v1-Extract_Project_Structure.py"
LEGACY_V3 = REPO / "legacy" / "python-v3-Extract_Project_Code.py"

NOTEBOOK = {
    "cells": [
        {"cell_type": "markdown", "source": ["# Title\n", "Some words"]},
        {
            "cell_type": "code",
            "source": ["import math\n", "# full comment line\n", "math.pi\n"],
            "outputs": [
                {"output_type": "stream", "name": "stdout", "text": ["hello\n"]},
                {
                    "output_type": "execute_result",
                    "data": {"text/plain": ["3.14159"]},
                },
            ],
        },
    ]
}

FIXTURE_FILES: dict[str, str] = {
    "readme.md": "# Demo\n\nSome text.\n",
    "notes.txt": "plain text\n",
    "a.py": "x = 2\n",
    "app/main.py": "import os  # os module\n\nprint('hi')  # say hi\nurl = 'http://x#frag'\n",
    "app/util/helpers.py": "# only a comment\nVALUE = 1\n",
    "notebooks/demo.ipynb": json.dumps(NOTEBOOK),
    "data.json": '{"a": 1}\n',
}


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


@pytest.fixture
def sent_to_trash(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_send2trash(path: str) -> None:
        calls.append(path)

    monkeypatch.setattr(plan_module, "send2trash", fake_send2trash)
    return calls


def _ctx(tmp_path: Path, journal: Journal | None) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


def _make_tree(root: Path, files: dict[str, str] = FIXTURE_FILES) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _opts(root: Path, output: Path, **fields: object) -> ProjectExportOptions:
    """Options for a parity-style run: no default exclusions, no .gitignore, output outside root."""
    base: dict[str, object] = {
        "root": root,
        "output_path": output,
        "exclude_globs": [],
        "respect_gitignore": False,
    }
    base.update(fields)
    return ProjectExportOptions.model_validate(base)


def _output_of(opts: ProjectExportOptions) -> Path:
    return opts.output_path or (Path(opts.root) / "Project-File-Content.txt")


def _export(tmp_path: Path, opts: ProjectExportOptions, journal: Journal) -> str:
    """Scan, apply, and return the written file's text."""
    ctx = _ctx(tmp_path, journal)
    TOOL.apply(TOOL.scan(opts, ctx), ctx)
    return _output_of(opts).read_text(encoding="utf-8")


def _load_legacy_v3() -> ModuleType:
    spec = importlib.util.spec_from_file_location("legacy_v3_extract_code", LEGACY_V3)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_v1_function() -> Callable[..., list[str]]:
    """``generate_file_structure`` from the v1 script, without running its example call."""
    tree = ast.parse(LEGACY_V1.read_text(encoding="utf-8"), filename=str(LEGACY_V1))
    func = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "generate_file_structure"
    )
    isolated = ast.fix_missing_locations(ast.Module(body=[func], type_ignores=[]))
    namespace: dict[str, object] = {"os": os, "fnmatch": fnmatch}
    exec(compile(isolated, str(LEGACY_V1), "exec"), namespace)  # noqa: S102 - legacy reference
    return cast("Callable[..., list[str]]", namespace["generate_file_structure"])


def _legacy_contents(root: Path, out: Path, *, strip: bool, outputs: bool) -> str:
    legacy = _load_legacy_v3()
    with out.open("w", encoding="utf-8") as handle, redirect_stdout(io.StringIO()):
        legacy.process_path(str(root), handle, strip, False, not outputs, [], [])
    return out.read_text(encoding="utf-8")


def _non_blank_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _tree_paths(text: str, root: Path) -> set[str]:
    """File paths named by the tree block: two spaces per level, ``name/`` for folders."""
    lines = text.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("Project tree:"))
    stack: list[str] = []
    found: set[str] = set()
    for line in lines[header + 2 :]:  # skip the closing separator of the header
        if not line.strip() or line.startswith("="):
            break
        depth = (len(line) - len(line.lstrip(" "))) // 2
        name = line.strip()
        stack = stack[:depth]
        if name.endswith("/"):
            stack.append(name[:-1])
        else:
            found.add(os.path.normcase(str(root.joinpath(*stack, name))))
    return found


# ---- parity with the original scripts -------------------------------------------------


def test_contents_match_legacy_v3_exactly_without_stripping(
    tmp_path: Path, journal: Journal
) -> None:
    root = _make_tree(tmp_path / "proj")
    (root / "blob.dat").write_bytes(b"\xff\xfe bad utf-8")
    out_ours = tmp_path / "out" / "ours.txt"
    out_ours.parent.mkdir()
    ours = _export(
        tmp_path,
        _opts(root, out_ours, mode="contents", strip_comments=False, include_outputs=True),
        journal,
    )
    legacy = _legacy_contents(root, tmp_path / "out" / "legacy.txt", strip=False, outputs=True)
    assert ours == legacy


def test_contents_match_legacy_v3_with_comments_stripped(tmp_path: Path, journal: Journal) -> None:
    """Same lines in the same order. The legacy script dropped a file's final newline and
    blank lines; this port keeps the file's own line structure, so blank lines are compared out."""
    root = _make_tree(tmp_path / "proj")
    out_ours = tmp_path / "out" / "ours.txt"
    out_ours.parent.mkdir()
    ours = _export(
        tmp_path,
        _opts(root, out_ours, mode="contents", strip_comments=True, include_outputs=True),
        journal,
    )
    legacy = _legacy_contents(root, tmp_path / "out" / "legacy.txt", strip=True, outputs=True)
    assert _non_blank_lines(ours) == _non_blank_lines(legacy)
    assert "url = 'http://x#frag'" in ours
    assert "# os module" not in ours


@pytest.mark.parametrize(
    ("fields", "v1_include", "v1_exclude"),
    [
        ({}, [], []),
        ({"exts": ["py"]}, ["py"], []),
        ({"exclude_globs": ["*.md"]}, [], ["md"]),
    ],
    ids=["all-files", "include-py", "exclude-md"],
)
def test_tree_selects_the_same_files_as_legacy_v1(
    tmp_path: Path,
    journal: Journal,
    fields: dict[str, object],
    v1_include: list[str],
    v1_exclude: list[str],
) -> None:
    root = _make_tree(tmp_path / "proj")
    legacy_paths = _legacy_v1_function()(
        path=str(root),
        output_file=str(tmp_path / "structure.txt"),
        exclude_folder_list=[],
        exclude_filetype_list=v1_exclude,
        include_folder_list=[],
        include_filetype_list=v1_include,
    )
    out = tmp_path / "out" / "tree.txt"
    out.parent.mkdir()
    ours = _export(tmp_path, _opts(root, out, mode="tree", **fields), journal)
    assert _tree_paths(ours, root) == {os.path.normcase(p) for p in legacy_paths}
    assert len(legacy_paths) > 0


# ---- the comment stripper (plan §3.2: tokenize, keep string literals) -------------------


def test_triple_quoted_strings_are_kept_where_legacy_deleted_them(tmp_path: Path) -> None:
    source = 'def f():\n    """Doc line\n    # not a comment\n    """\n    return 1  # real\n'
    path = tmp_path / "mod.py"
    path.write_text(source, encoding="utf-8")

    result = strip_python_comments(source)
    assert '"""Doc line' in result
    assert "# not a comment" in result
    assert "return 1  \n" in result
    assert "# real" not in result

    # The original regex stripper deleted the triple-quoted string. This is the fixed bug.
    legacy = _load_legacy_v3()
    assert "Doc line" not in legacy.strip_comments_from_python(str(path))


def test_hash_inside_a_string_is_not_a_comment() -> None:
    assert strip_python_comments("x = 'a # b'  # c\n") == "x = 'a # b'  \n"


def test_python_that_cannot_be_tokenized_is_kept_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    raw = 'x = """never closed\n# still inside\n'
    path.write_text(raw, encoding="utf-8")
    assert file_text(path, strip_comments=True, include_markdown=True, include_outputs=False) == raw


def test_notebook_outputs_are_left_out_by_default(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(tmp_path / "proj", {"nb.ipynb": json.dumps(NOTEBOOK)})
    out = tmp_path / "out" / "default.txt"
    out.parent.mkdir()
    text = _export(tmp_path, _opts(root, out, mode="contents"), journal)
    assert "CELL OUTPUT" not in text
    assert "# MARKDOWN CELL" in text
    assert "math.pi" in text


# ---- the rules -------------------------------------------------------------------------


def test_defaults_leave_out_git_venv_and_cache_folders(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(
        tmp_path / "proj",
        {
            "keep.py": "x = 1\n",
            ".git/config": "[core]\n",
            ".venv/lib.py": "y = 2\n",
            "__pycache__/m.py": "z = 3\n",
            "node_modules/pkg.js": "w\n",
        },
    )
    out = tmp_path / "out" / "defaults.txt"
    out.parent.mkdir()
    opts = ProjectExportOptions.model_validate(
        {"root": root, "output_path": out, "respect_gitignore": False, "mode": "tree"}
    )
    text = _export(tmp_path, opts, journal)
    assert _tree_paths(text, root) == {os.path.normcase(str(root / "keep.py"))}


def test_output_file_inside_the_root_is_left_out(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(tmp_path / "proj", {"a.py": "x = 1\n"})
    opts = ProjectExportOptions.model_validate({"root": root, "respect_gitignore": False})
    first = _export(tmp_path, opts, journal)
    assert (root / "Project-File-Content.txt").exists()
    second = _export(tmp_path, opts, journal)
    assert "a.py" in first and "a.py" in second
    assert "Project-File-Content" not in second


def test_scan_writes_nothing(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(tmp_path / "proj")
    before = sorted(
        (str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file()
    )
    out = tmp_path / "out" / "scan-only.txt"
    out.parent.mkdir()
    plan = TOOL.scan(_opts(root, out), _ctx(tmp_path, journal))
    after = sorted(
        (str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file()
    )
    assert before == after
    assert not out.exists()
    assert [a.kind for a in plan.actions] == ["write"]
    assert plan.notes[0].startswith("Export of ")


def test_apply_never_overwrites_an_existing_export(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(tmp_path / "proj", {"a.py": "x = 1\n"})
    out = tmp_path / "out" / "export.txt"
    out.parent.mkdir()
    out.write_text("keep me", encoding="utf-8")
    ctx = _ctx(tmp_path, journal)
    result = TOOL.apply(TOOL.scan(_opts(root, out), ctx), ctx)
    assert out.read_text(encoding="utf-8") == "keep me"
    written = out.parent / "export_dup1.txt"
    assert written.exists()
    assert result.status == "done"
    assert result.outcomes[0].dst == str(written)
    run = journal.run(result.run_id)
    assert run is not None and run.status == "done"


def test_cloud_only_files_are_never_read(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_tree(tmp_path / "proj", {"local.py": "a = 1\n", "secret.py": "b = 2\n"})
    monkeypatch.setattr(export_tool, "_cloud_only", lambda info: info.path.name == "secret.py")
    read: list[str] = []
    real_file_text = export_tool.file_text

    def recording(path: Path, **kwargs: object) -> str:
        read.append(path.name)
        return real_file_text(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(export_tool, "file_text", recording)
    out = tmp_path / "out" / "cloud.txt"
    out.parent.mkdir()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root, out, mode="contents"), ctx)
    assert "secret.py" not in read
    assert any("skipped: cloud-only" in note and "secret.py" in note for note in plan.notes)
    TOOL.apply(plan, ctx)
    text = out.read_text(encoding="utf-8")
    assert "a = 1" in text
    assert "b = 2" not in text


def test_tree_mode_never_reads_contents(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_tree(tmp_path / "proj", {"a.py": "x = 1\n"})

    def boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("tree mode must not read files")

    monkeypatch.setattr(export_tool, "file_text", boom)
    out = tmp_path / "out" / "tree-only.txt"
    out.parent.mkdir()
    text = _export(tmp_path, _opts(root, out, mode="tree"), journal)
    assert "a.py" in text
    assert "x = 1" not in text


def test_files_over_the_size_limit_are_skipped(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(tmp_path / "proj", {"big.txt": "x" * 3000, "small.txt": "tiny\n"})
    out = tmp_path / "out" / "size.txt"
    out.parent.mkdir()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root, out, mode="contents", max_file_kb=1), ctx)
    assert any("larger than 1 KB" in note and "big.txt" in note for note in plan.notes)
    TOOL.apply(plan, ctx)
    text = out.read_text(encoding="utf-8")
    assert "tiny" in text and "xxxx" not in text


def test_protected_root_is_refused_before_anything_is_listed(
    tmp_path: Path, journal: Journal
) -> None:
    drive_root = Path(tmp_path.anchor)
    out = tmp_path / "out" / "refused.txt"
    out.parent.mkdir()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(drive_root, out), ctx)
    assert plan.actions == []
    assert any("Protected folder refused" in note for note in plan.notes)
    result = TOOL.apply(plan, ctx)
    assert result.outcomes == []
    assert not out.exists()


def test_output_into_a_protected_location_is_refused(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = tmp_path / "Windows"
    windows.mkdir()
    monkeypatch.setenv("SystemRoot", str(windows))
    root = _make_tree(tmp_path / "proj", {"a.py": "x = 1\n"})
    plan = TOOL.scan(_opts(root, windows / "export.txt"), _ctx(tmp_path, journal))
    assert plan.actions == []
    assert any("protected location" in note for note in plan.notes)


def test_output_folder_must_already_exist(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(tmp_path / "proj", {"a.py": "x = 1\n"})
    plan = TOOL.scan(_opts(root, tmp_path / "missing" / "export.txt"), _ctx(tmp_path, journal))
    assert plan.actions == []
    assert any("Output folder does not exist" in note for note in plan.notes)


def test_binary_files_become_a_placeholder(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(tmp_path / "proj")
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00 not text")
    out = tmp_path / "out" / "binary.txt"
    out.parent.mkdir()
    text = _export(tmp_path, _opts(root, out, mode="contents"), journal)
    assert f"[Binary file or unsupported encoding: {root / 'blob.bin'}]" in text


def test_extension_filter_and_folder_order_ignore_case(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(tmp_path / "proj", {"Beta.py": "b\n", "alpha.py": "a\n", "c.txt": "c\n"})
    out = tmp_path / "out" / "order.txt"
    out.parent.mkdir()
    text = _export(tmp_path, _opts(root, out, mode="contents", exts=["PY"]), journal)
    assert "c.txt" not in text
    assert text.index("alpha.py") < text.index("Beta.py")


def test_markdown_output_uses_a_fence_longer_than_any_backtick_run(
    tmp_path: Path, journal: Journal
) -> None:
    root = _make_tree(tmp_path / "proj", {"doc.md": "Use ```code``` here.\n"})
    out = tmp_path / "out" / "export.md"
    out.parent.mkdir()
    text = _export(tmp_path, _opts(root, out, output_format="md"), journal)
    assert "## Tree" in text and "## Files" in text
    assert "### doc.md" in text
    assert "````markdown" in text
    assert text.count("````") >= 2


def test_plan_saved_as_json_applies_later(tmp_path: Path, journal: Journal) -> None:
    root = _make_tree(tmp_path / "proj", {"a.py": "x = 1\n"})
    out = tmp_path / "out" / "later.txt"
    out.parent.mkdir()
    plan = TOOL.scan(_opts(root, out), _ctx(tmp_path, journal))
    saved = plan.to_json()
    restored = Plan.from_json(saved)
    result = TOOL.apply(restored, _ctx(tmp_path, journal))
    assert result.status == "done"
    assert "x = 1" in out.read_text(encoding="utf-8")


def test_options_reject_unknown_fields_and_need_a_root() -> None:
    with pytest.raises(ValidationError):
        ProjectExportOptions.model_validate({"root": "x", "bogus": True})
    with pytest.raises(ValidationError):
        ProjectExportOptions.model_validate({})


def test_registered_in_the_tool_registry_and_pyproject() -> None:
    assert project_export_pkg.TOOL is TOOL
    entries = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    declared = entries["project"]["entry-points"]["various_tools.tools"]
    assert declared["project_export"] == "various_tools.tools.project_export:TOOL"
    assert "project_export" in discover()
    assert TOOL.id == "project_export"
