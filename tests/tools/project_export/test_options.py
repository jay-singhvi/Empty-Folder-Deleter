"""Project Export: options that used to be constants change what the tool does."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.settings import Settings
from various_tools.tools.project_export import TOOL, ProjectExportOptions
from various_tools.tools.project_export import tool as export_tool


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    with Journal(tmp_path / "journal.sqlite") as opened:
        yield opened


def _ctx(tmp_path: Path, journal: Journal | None = None, **settings: Any) -> RunContext:
    values: dict[str, Any] = {"onedrive_root": tmp_path, **settings}
    return RunContext(
        settings=Settings.model_validate(values),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "b.json").write_text('{"k": 1}\n', encoding="utf-8")
    return root


def _text(opts: ProjectExportOptions, ctx: RunContext) -> str:
    plan = TOOL.scan(opts, ctx)
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1
    (action,) = plan.actions
    assert action.dst is not None
    return action.dst.read_text(encoding="utf-8")


def test_defaults_are_the_original_values() -> None:
    opts = ProjectExportOptions(root=Path("."))
    assert opts.output_name == "Project-File-Content"
    assert opts.separator == "=" * 50
    assert opts.chars_per_token == 4
    assert opts.language_by_suffix[".py"] == "python"
    assert opts.language_by_suffix == export_tool.LANGUAGE_BY_SUFFIX


def test_output_name_sets_the_default_file_name(tmp_path: Path, project: Path) -> None:
    plan = TOOL.scan(ProjectExportOptions(root=project, output_name="context"), _ctx(tmp_path))
    (action,) = plan.actions
    assert action.dst == project / "context.txt"
    md = TOOL.scan(
        ProjectExportOptions(root=project, output_name="context", output_format="md"),
        _ctx(tmp_path),
    )
    assert md.actions[0].dst == project / "context.md"


def test_earlier_exports_with_a_custom_name_stay_out_of_the_export(
    tmp_path: Path, project: Path, journal: Journal
) -> None:
    ctx = _ctx(tmp_path, journal)
    opts = ProjectExportOptions(root=project, output_name="context")
    _text(opts, ctx)
    second = _text(opts, ctx)
    assert (project / "context_dup1.txt").is_file()
    assert "context.txt" not in second


@pytest.mark.parametrize("bad", ["a/b", "a\\b", "x:y", "con", "name.", "", "  ", "a*b"])
def test_output_name_must_be_a_plain_file_name(bad: str) -> None:
    with pytest.raises(ValidationError):
        ProjectExportOptions(root=Path("."), output_name=bad)


def test_separator_is_used_in_txt_exports(tmp_path: Path, project: Path, journal: Journal) -> None:
    text = _text(ProjectExportOptions(root=project, separator="#### "), _ctx(tmp_path, journal))
    assert "#### \nProject tree:" in text
    assert "=" * 50 not in text


def test_separator_must_be_one_line() -> None:
    with pytest.raises(ValidationError):
        ProjectExportOptions(root=Path("."), separator="a\nb")


def test_language_map_sets_the_code_fence_language(
    tmp_path: Path, project: Path, journal: Journal
) -> None:
    ctx = _ctx(tmp_path, journal)
    base: dict[str, Any] = {"root": project, "output_format": "md"}
    default = _text(ProjectExportOptions(**base, output_path=tmp_path / "one.md"), ctx)
    assert "```python\n" in default and "```json\n" in default
    custom = _text(
        ProjectExportOptions(
            **base, language_by_suffix={"PY": "py3"}, output_path=tmp_path / "two.md"
        ),
        ctx,
    )
    assert "```py3\n" in custom
    assert "```json\n" not in custom


def test_language_map_is_normalised_and_checked() -> None:
    opts = ProjectExportOptions(root=Path("."), language_by_suffix={"RS": " rust "})
    assert opts.language_by_suffix == {".rs": "rust"}
    with pytest.raises(ValidationError):
        ProjectExportOptions(root=Path("."), language_by_suffix={".rs": "two words"})


def test_chars_per_token_changes_the_estimate_only(tmp_path: Path, project: Path) -> None:
    ctx = _ctx(tmp_path)
    four = TOOL.scan(ProjectExportOptions(root=project), ctx)
    two = TOOL.scan(ProjectExportOptions(root=project, chars_per_token=2), ctx)

    def tokens(plan: Any) -> int:
        return int(plan.actions[0].reason.split("about ")[1].split(" tokens")[0].replace(",", ""))

    assert tokens(two) == tokens(four) * 2 or abs(tokens(two) - tokens(four) * 2) <= 1
    assert two.actions[0].dst == four.actions[0].dst


def test_protected_folders_come_from_the_settings(tmp_path: Path, project: Path) -> None:
    ctx = _ctx(tmp_path, protected_folders=[str(project)])
    plan = TOOL.scan(ProjectExportOptions(root=project), ctx)
    assert plan.actions == []
    assert any("rotected" in note for note in plan.notes)
    output_guard = _ctx(tmp_path, protected_folders=[str(tmp_path / "out")])
    out = tmp_path / "out"
    out.mkdir()
    refused = TOOL.scan(ProjectExportOptions(root=project, output_path=out / "x.txt"), output_guard)
    assert refused.actions == []
    assert any("protected location" in note for note in refused.notes)
