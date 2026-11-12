"""Settings (TOML round-trip, safe defaults) and the versioned file cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from various_tools.core.cache import FileCache
from various_tools.core.settings import (
    Settings,
    default_onedrive_root,
    load_settings,
    save_settings,
    to_toml,
)

# ----- settings ------------------------------------------------------------


def test_defaults_are_the_safe_values(tmp_path: Path) -> None:
    settings = Settings(onedrive_root=tmp_path)
    assert settings.delete_mode == "trash"  # 🔒 Recycle Bin by default
    assert settings.allow_protected_paths is False  # 🔒 protected paths refused
    assert settings.min_plausible_year == 1990
    assert settings.workers >= 1
    assert settings.ollama_url == "http://localhost:11434"


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "absent.toml")
    assert settings.delete_mode == "trash"


def test_round_trip_keeps_windows_paths_and_tables(tmp_path: Path) -> None:
    target = tmp_path / "settings.toml"
    settings = Settings(
        onedrive_root=Path(r"C:\Users\someone\OneDrive"),
        workers=3,
        external_tools={"exiftool": r"C:\Program Files\Exiftool\exiftool.exe"},
        ollama_url="http://gpu:11434",
    )
    save_settings(settings, target)
    loaded = load_settings(target)
    assert loaded == settings
    assert loaded.external_tools["exiftool"] == r"C:\Program Files\Exiftool\exiftool.exe"


def test_empty_external_tools_table_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "s.toml"
    save_settings(Settings(onedrive_root=tmp_path), target)
    assert load_settings(target).external_tools == {}


def test_corrupt_file_falls_back_to_defaults_and_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "settings.toml"
    target.write_text("this is = not [valid toml", encoding="utf-8")
    settings = load_settings(target)
    assert settings.delete_mode == "trash"
    assert target.read_text(encoding="utf-8") == "this is = not [valid toml"


def test_invalid_value_falls_back_to_defaults(tmp_path: Path) -> None:
    target = tmp_path / "settings.toml"
    target.write_text('delete_mode = "shred"\n', encoding="utf-8")
    assert load_settings(target).delete_mode == "trash"


def test_unknown_key_is_rejected_so_typos_are_not_silent(tmp_path: Path) -> None:
    target = tmp_path / "settings.toml"
    target.write_text('delete_modee = "permanent"\n', encoding="utf-8")
    assert load_settings(target).delete_mode == "trash"


def test_to_toml_quotes_keys_and_escapes_strings() -> None:
    text = to_toml({"name": 'say "hi"', "flag": True, "n": 2, "gone": None, "t": {"a b": "x"}})
    assert 'name = "say \\"hi\\""' in text
    assert "flag = true" in text
    assert "n = 2" in text
    assert "gone" not in text
    assert '["a b"]' not in text and '"a b" = "x"' in text


def test_default_onedrive_root_prefers_personal_folder(tmp_path: Path) -> None:
    (tmp_path / "OneDrive").mkdir()
    assert default_onedrive_root({"USERPROFILE": str(tmp_path)}) == tmp_path / "OneDrive"


def test_default_onedrive_root_falls_back_to_environment(tmp_path: Path) -> None:
    env = {"USERPROFILE": str(tmp_path), "OneDrive": str(tmp_path / "Business")}
    assert default_onedrive_root(env) == tmp_path / "Business"


def test_settings_validation_rejects_zero_workers() -> None:
    with pytest.raises(ValueError):
        Settings(workers=0)


# ----- cache ---------------------------------------------------------------


def test_cache_returns_value_only_while_size_and_mtime_match(tmp_path: Path) -> None:
    file_path = tmp_path / "a.jpg"
    with FileCache(tmp_path / "c.sqlite", name="features", version=1) as cache:
        cache.put(file_path, size=10, mtime=100.5, value={"phash": "abc"})
        assert cache.get(file_path, 10, 100.5) == {"phash": "abc"}
        assert cache.get(file_path, 11, 100.5) is None  # size changed
        assert cache.get(file_path, 10, 101.0) is None  # mtime changed
        assert cache.get(tmp_path / "other.jpg", 10, 100.5) is None


def test_cache_put_replaces_existing_row(tmp_path: Path) -> None:
    file_path = tmp_path / "a.jpg"
    with FileCache(tmp_path / "c.sqlite", name="features", version=1) as cache:
        cache.put(file_path, 1, 1.0, {"v": 1})
        cache.put(file_path, 1, 1.0, {"v": 2})
        assert cache.get(file_path, 1, 1.0) == {"v": 2}


def test_cache_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    file_path = tmp_path / "a.jpg"
    cache = FileCache(db, name="features", version=3)
    cache.put(file_path, 5, 2.0, {"ok": True})
    cache.close()
    with FileCache(db, name="features", version=3) as reopened:
        assert reopened.get(file_path, 5, 2.0) == {"ok": True}


def test_cache_is_dropped_when_version_changes(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    file_path = tmp_path / "a.jpg"
    with FileCache(db, name="features", version=1) as cache:
        cache.put(file_path, 5, 2.0, {"old": True})
    with FileCache(db, name="features", version=2) as cache:
        assert cache.get(file_path, 5, 2.0) is None


def test_cache_is_dropped_when_name_changes(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    file_path = tmp_path / "a.jpg"
    with FileCache(db, name="images", version=1) as cache:
        cache.put(file_path, 5, 2.0, {"x": 1})
    with FileCache(db, name="videos", version=1) as cache:
        assert cache.get(file_path, 5, 2.0) is None


def test_cache_keys_ignore_case_on_windows(tmp_path: Path) -> None:
    import os

    if os.name != "nt":
        pytest.skip("path case rules are Windows-specific here")
    with FileCache(tmp_path / "c.sqlite", name="f", version=1) as cache:
        cache.put(tmp_path / "Photo.JPG", 1, 1.0, {"a": 1})
        assert cache.get(tmp_path / "photo.jpg", 1, 1.0) == {"a": 1}
