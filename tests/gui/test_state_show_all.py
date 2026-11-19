"""The app-wide "Show all settings" switch is remembered in gui_state.json."""

from __future__ import annotations

from pathlib import Path

from various_tools.core.settings import Settings, save_settings
from various_tools.gui.state import GUI_STATE_FILE, AppState


def _state(tmp_path: Path, **settings: object) -> AppState:
    path = tmp_path / "settings.toml"
    if not path.exists():
        save_settings(Settings(data_folder=tmp_path / "data", **settings), path)  # type: ignore[arg-type]
    return AppState.load(path, tools={})


def test_off_by_default(tmp_path: Path) -> None:
    assert _state(tmp_path).show_all_settings() is False


def test_switch_is_remembered_and_announced(tmp_path: Path) -> None:
    state = _state(tmp_path)
    heard: list[bool] = []
    state.on_show_all_changed(heard.append)
    state.set_show_all_settings(True)
    assert heard == [True]
    assert _state(tmp_path).show_all_settings() is True


def test_switch_is_kept_without_remembered_options(tmp_path: Path) -> None:
    state = _state(tmp_path, remember_last_folders=False)
    state.set_show_all_settings(True)
    assert _state(tmp_path).show_all_settings() is True


def test_switch_and_last_options_live_side_by_side(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.remember_options("empty_folders", {"min_age_days": 3})
    state.set_show_all_settings(True)
    state.remember_options("duplicates", {"x": 1})
    again = _state(tmp_path)
    assert again.show_all_settings() is True
    assert again.last_options("empty_folders") == {"min_age_days": 3}


def test_unreadable_file_means_off(tmp_path: Path) -> None:
    state = _state(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / GUI_STATE_FILE).write_text("{not json", encoding="utf-8")
    assert state.show_all_settings() is False
