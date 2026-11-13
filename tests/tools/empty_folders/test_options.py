"""Empty Folders: option hints, the locked delete mode and settings-driven protected folders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.settings import Settings
from various_tools.tools.empty_folders import TOOL, EmptyFoldersOptions


def _ctx(tmp_path: Path, **settings: Any) -> RunContext:
    values: dict[str, Any] = {"onedrive_root": tmp_path, **settings}
    return RunContext(
        settings=Settings.model_validate(values),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
    )


def test_delete_mode_is_a_locked_advanced_option_that_defaults_to_trash() -> None:
    info = EmptyFoldersOptions.model_fields["delete_mode"]
    extra = info.json_schema_extra
    assert isinstance(extra, dict)
    assert info.default == "trash"
    assert extra["locked"] is True and extra["advanced"] is True
    description = info.description or ""
    assert description.startswith("🔒")
    assert "delete_mode = permanent" in description
    assert "confirmation" in description and "cannot be undone" in description


def test_defaults_are_the_original_values() -> None:
    opts = EmptyFoldersOptions()
    assert opts.treat_as_empty == ["desktop.ini", "Thumbs.db", ".DS_Store"]
    assert opts.exclude_globs == [".git", "node_modules"]
    assert opts.min_age_days == 0 and opts.skip_hidden_system is True


def test_protected_folders_come_from_the_settings(tmp_path: Path) -> None:
    guarded = tmp_path / "Guarded"
    (guarded / "empty").mkdir(parents=True)
    options = EmptyFoldersOptions(roots=[guarded])
    refused = TOOL.scan(options, _ctx(tmp_path, protected_folders=[str(guarded)]))
    assert refused.roots == [] and refused.actions == []
    assert any("Protected folder refused" in note for note in refused.notes)
    allowed = TOOL.scan(options, _ctx(tmp_path, protected_folders=[str(tmp_path / "Other")]))
    assert allowed.roots == [guarded]
    assert [a.src.name for a in allowed.actions if a.src] == ["empty"]
    override = TOOL.scan(
        options, _ctx(tmp_path, protected_folders=[str(guarded)], allow_protected_paths=True)
    )
    assert override.roots == [guarded]
