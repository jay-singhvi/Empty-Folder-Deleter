"""Settings: protected-path rules, TOML persistence, derived folders and the locked defaults."""

from __future__ import annotations

import tomllib
from pathlib import Path

import platformdirs
import pytest

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.fs import (
    DEFAULT_PROTECTED_FOLDERS,
    ProtectedPaths,
    protected_reason,
    root_refusal,
)
from various_tools.core.journal import Journal
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.core.settings import (
    APP_NAME,
    JOURNAL_FILE,
    Settings,
    load_settings,
    save_settings,
)

# ----- protected_reason with custom rules ----------------------------------------------------


def test_custom_folder_expands_variables_from_the_mapping(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    rules = ProtectedPaths(folders=("%MYVAULT%",), drive_roots=False, user_profile=False)
    env = {"myvault": str(vault)}  # variable names match case-insensitively
    assert protected_reason(vault, env=env, rules=rules) == "protected folder %MYVAULT%"
    assert protected_reason(vault / "a" / "b", env=env, rules=rules) is not None
    assert protected_reason(tmp_path / "Vault2", env=env, rules=rules) is None
    assert protected_reason(tmp_path / "other", env=env, rules=rules) is None


def test_unset_variable_is_ignored_not_treated_as_root(tmp_path: Path) -> None:
    rules = ProtectedPaths(folders=("%NOPE%", "%NOPE%\\sub"), drive_roots=False, user_profile=False)
    assert protected_reason(tmp_path / "x", env={}, rules=rules) is None
    assert protected_reason(Path(tmp_path.anchor) / "x", env={"NOPE": ""}, rules=rules) is None


def test_drive_roots_and_user_profile_can_be_switched_off(tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)
    profile = tmp_path / "me"
    env = {"USERPROFILE": str(profile)}
    on = ProtectedPaths(folders=())
    assert protected_reason(root, env=env, rules=on) == "drive root"
    assert protected_reason(profile, env=env, rules=on) == "user profile folder"
    assert protected_reason(profile / "Docs", env=env, rules=on) is None
    no_roots = ProtectedPaths(folders=(), drive_roots=False)
    no_profile = ProtectedPaths(folders=(), user_profile=False)
    assert protected_reason(root, env=env, rules=no_roots) is None
    assert protected_reason(profile, env=env, rules=no_profile) is None
    assert protected_reason(root, env=env, rules=no_profile) == "drive root"


def test_emptied_folder_list_protects_nothing_inside(tmp_path: Path) -> None:
    win = tmp_path / "Win"
    env = {"SystemRoot": str(win)}
    assert protected_reason(win / "System32", env=env) is not None  # default rules
    rules = ProtectedPaths(folders=(), drive_roots=False, user_profile=False)
    assert protected_reason(win / "System32", env=env, rules=rules) is None


def test_root_refusal_allowed_versus_default() -> None:
    drive = Path(Path.cwd().anchor)
    assert root_refusal(drive) is not None
    assert root_refusal(drive, ProtectedPaths()) is not None
    assert root_refusal(drive, ProtectedPaths(allowed=True)) is None
    assert root_refusal(drive, True) is None
    assert root_refusal(drive, False) is not None


def test_root_refusal_honours_a_custom_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    monkeypatch.setenv("VT_GUARDED", str(guarded))
    assert root_refusal(guarded, ProtectedPaths(folders=("%VT_GUARDED%",))) is not None
    allowed = ProtectedPaths(folders=("%VT_GUARDED%",), allowed=True)
    assert root_refusal(guarded, allowed) is None
    assert root_refusal(guarded) is None


def test_settings_protected_paths_mirrors_the_fields() -> None:
    assert Settings().protected_paths() == ProtectedPaths()
    custom = Settings(
        protected_folders=["%A%", "D:\\x"],
        protect_drive_roots=False,
        protect_user_profile=False,
        allow_protected_paths=True,
    ).protected_paths()
    assert custom.folders == ("%A%", "D:\\x")
    assert custom.drive_roots is False
    assert custom.user_profile is False
    assert custom.allowed is True


# ----- apply engine honours the settings list ------------------------------------------------


def _apply_move(tmp_path: Path, dst_dir: Path, **settings: object) -> tuple[RunResult, Path, Path]:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    src = root / "f.txt"
    src.write_text("x", encoding="utf-8")
    ctx = RunContext(
        settings=Settings.model_validate(settings),
        deps=ExternalTools(env={}),
        journal=Journal(tmp_path / "j.sqlite"),
    )
    try:
        plan = Plan(
            tool="t",
            roots=[root, dst_dir.parent],
            actions=[PlannedAction(ActionKind.MOVE, src=src, dst=dst_dir / "f.txt")],
        )
        return apply_plan(plan, ctx), src, dst_dir / "f.txt"
    finally:
        ctx.close()


def test_apply_refuses_a_move_into_a_user_protected_folder(tmp_path: Path) -> None:
    guarded = tmp_path / "guarded"
    result, src, dst = _apply_move(tmp_path, guarded, protected_folders=[str(guarded)])
    assert result.outcomes[0].status != "done"
    assert "protected" in result.outcomes[0].detail
    assert src.exists() and not dst.exists()


def test_apply_allows_it_when_protected_paths_are_allowed(tmp_path: Path) -> None:
    guarded = tmp_path / "guarded"
    result, src, dst = _apply_move(
        tmp_path, guarded, protected_folders=[str(guarded)], allow_protected_paths=True
    )
    assert result.outcomes[0].status == "done"
    assert dst.exists() and not src.exists()


def test_apply_allows_the_same_move_when_the_folder_is_not_listed(tmp_path: Path) -> None:
    result, _src, dst = _apply_move(tmp_path, tmp_path / "free", protected_folders=[])
    assert result.outcomes[0].status == "done" and dst.exists()


# ----- persistence ---------------------------------------------------------------------------


def test_save_writes_only_non_default_values(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    save_settings(Settings(), path)
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {}
    save_settings(Settings(delete_mode="permanent"), path)
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {"delete_mode": "permanent"}


def test_lists_round_trip_through_toml(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    folders = ["C:\\Windows", "%ProgramFiles%", 'D:\\odd "quoted" \\path', "E:\\\u00fcn\u00ef"]
    save_settings(Settings(protected_folders=folders), path)
    assert load_settings(path).protected_folders == folders
    save_settings(Settings(protected_folders=[]), path)
    assert load_settings(path).protected_folders == []


def test_full_round_trip_keeps_safety_values(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    original = Settings(
        delete_mode="permanent",
        allow_protected_paths=True,
        protect_drive_roots=False,
        protect_user_profile=False,
        data_folder=tmp_path / "d",
        external_tools={"ffmpeg": "C:\\x\\ffmpeg.exe"},
    )
    save_settings(original, path)
    assert load_settings(path) == original


# ----- derived folders -----------------------------------------------------------------------


def test_journal_follows_the_data_folder(tmp_path: Path) -> None:
    s = Settings(data_folder=tmp_path / "data")
    assert s.resolved_data_folder() == tmp_path / "data"
    assert s.journal_path() == tmp_path / "data" / JOURNAL_FILE


def test_data_and_cache_folders_default_to_the_per_user_folders() -> None:
    s = Settings()
    assert s.resolved_data_folder() == Path(platformdirs.user_data_dir(APP_NAME))
    assert s.resolved_cache_folder() == Path(platformdirs.user_cache_dir(APP_NAME))


def test_cache_folder_override(tmp_path: Path) -> None:
    s = Settings(cache_folder=tmp_path / "cache", data_folder=tmp_path / "data")
    assert s.resolved_cache_folder() == tmp_path / "cache"
    assert s.resolved_data_folder() == tmp_path / "data"


# ----- locked defaults -----------------------------------------------------------------------


def test_locked_defaults_are_unchanged() -> None:
    s = Settings()
    assert s.delete_mode == "trash"
    assert s.allow_protected_paths is False
    assert s.protect_drive_roots is True
    assert s.protect_user_profile is True
    assert s.cloud_only_files == "skip"
    assert tuple(s.protected_folders) == DEFAULT_PROTECTED_FOLDERS
    assert DEFAULT_PROTECTED_FOLDERS == (
        "%SystemRoot%",
        "%WINDIR%",
        "%ProgramFiles%",
        "%ProgramFiles(x86)%",
        "%ProgramW6432%",
    )


def test_default_protected_list_is_not_shared_between_instances() -> None:
    a, b = Settings(), Settings()
    a.protected_folders.append("X")
    assert "X" not in b.protected_folders
