"""Saved option presets (``core/presets.py``). Files live under ``tmp_path`` only."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from various_tools.core.presets import PresetStore
from various_tools.core.settings import Settings


class Opts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roots: list[Path] = Field(default_factory=list)
    mode: str = "fast"
    limit: int = Field(default=5, ge=1)
    verbose: bool = False


@pytest.fixture
def store(tmp_path: Path) -> PresetStore:
    return PresetStore(tmp_path / "data" / "presets.json")


def test_empty_store(store: PresetStore) -> None:
    assert store.names("t") == []
    assert store.all() == {}
    with pytest.raises(KeyError):
        store.get("t", "nope")
    assert not store.path.exists(), "reading creates nothing"


def test_for_settings_uses_the_presets_path(tmp_path: Path) -> None:
    settings = Settings(data_folder=tmp_path / "d")
    assert PresetStore.for_settings(settings).path == tmp_path / "d" / "presets.json"


def test_save_model_stores_only_non_defaults(store: PresetStore) -> None:
    store.save("t", "mine", Opts(mode="slow", limit=5, roots=[Path("a")]))
    assert store.get("t", "mine") == {"mode": "slow", "roots": [str(Path("a"))]}
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert on_disk["format"] == 1
    assert set(on_disk["presets"]["t"]) == {"mine"}


def test_save_mapping_is_stored_as_given(store: PresetStore) -> None:
    store.save("t", "m", {"mode": "x", "folder": Path("p"), "n": 1})
    assert store.get("t", "m") == {"mode": "x", "folder": "p", "n": 1}


def test_get_returns_a_copy(store: PresetStore) -> None:
    store.save("t", "m", {"items": [1]})
    got = store.get("t", "m")
    got["items"].append(2)
    assert store.get("t", "m") == {"items": [1]}


def test_names_all_and_overwrite(store: PresetStore) -> None:
    store.save("b_tool", "zeta", {"a": 1})
    store.save("b_tool", "Alpha", {"a": 1})
    store.save("a_tool", "one", {"a": 1})
    assert store.names("b_tool") == ["Alpha", "zeta"]
    assert store.all() == {"a_tool": ["one"], "b_tool": ["Alpha", "zeta"]}
    store.save("b_tool", "zeta", {"a": 2})
    assert store.get("b_tool", "zeta") == {"a": 2}
    assert store.names("b_tool") == ["Alpha", "zeta"]


def test_names_are_trimmed_and_must_not_be_empty(store: PresetStore) -> None:
    store.save("t", "  spaced  ", {"a": 1})
    assert store.names("t") == ["spaced"]
    assert store.get("t", " spaced ") == {"a": 1}
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="name"):
            store.save("t", bad, {"a": 1})
        with pytest.raises(ValueError, match="name"):
            store.delete("t", bad)


def test_delete(store: PresetStore) -> None:
    store.save("t", "a", {"x": 1})
    store.save("t", "b", {"x": 2})
    assert store.delete("t", " a ") is True
    assert store.names("t") == ["b"]
    assert store.delete("t", "a") is False
    assert store.delete("other", "b") is False
    assert store.delete("t", "b") is True
    assert store.all() == {}


def test_build_options_applies_preset_then_overrides(store: PresetStore) -> None:
    store.save("t", "mine", Opts(mode="slow", limit=9))
    plain = store.build_options(Opts, "t", None, {})
    assert plain == Opts()
    from_preset = store.build_options(Opts, "t", "mine", {})
    assert (from_preset.mode, from_preset.limit) == ("slow", 9)
    overridden = store.build_options(Opts, "t", "mine", {"limit": 2, "verbose": True})
    assert (overridden.mode, overridden.limit, overridden.verbose) == ("slow", 2, True)
    only_overrides = store.build_options(Opts, "t", None, {"mode": "x"})
    assert only_overrides.mode == "x"


def test_build_options_errors(store: PresetStore) -> None:
    store.save("t", "mine", {"limit": 3})
    with pytest.raises(KeyError):
        store.build_options(Opts, "t", "absent", {})
    with pytest.raises(ValidationError):
        store.build_options(Opts, "t", "mine", {"limit": 0})
    with pytest.raises(ValidationError):
        store.build_options(Opts, "t", "mine", {"unknown": 1})


def test_a_corrupt_file_is_treated_as_empty_and_not_changed_by_reads(
    store: PresetStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert store.names("t") == []
        assert store.all() == {}
    assert "presets" in caplog.text
    assert store.path.read_text(encoding="utf-8") == "{not json"


def test_saving_over_a_corrupt_file_keeps_a_copy(store: PresetStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    store.save("t", "fresh", {"a": 1})
    assert store.get("t", "fresh") == {"a": 1}
    backup = store.path.with_name("presets.json.corrupt")
    assert backup.read_text(encoding="utf-8") == "{not json"
    # a second corrupt file does not overwrite the first backup
    store.path.write_text("[also broken", encoding="utf-8")
    store.save("t", "again", {"a": 2})
    assert backup.read_text(encoding="utf-8") == "{not json"
    assert (
        store.path.with_name("presets.json.corrupt2").read_text(encoding="utf-8") == "[also broken"
    )


def test_wrong_format_is_treated_as_corrupt(store: PresetStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"format": 99, "presets": {"t": {"a": {}}}}), encoding="utf-8")
    assert store.all() == {}
    store.save("t", "b", {"x": 1})
    assert store.path.with_name("presets.json.corrupt").is_file()


def test_malformed_entries_are_skipped_but_good_ones_survive(store: PresetStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"format": 1, "presets": {"t": {"good": {"a": 1}, "bad": 5}, "u": 3}}),
        encoding="utf-8",
    )
    assert store.all() == {"t": ["good"]}


def test_writes_are_atomic_and_leave_no_temp_files(store: PresetStore) -> None:
    for number in range(3):
        store.save("t", f"p{number}", {"n": number})
    leftovers = [p.name for p in store.path.parent.iterdir() if p.name != "presets.json"]
    assert leftovers == []
    assert json.loads(store.path.read_text(encoding="utf-8"))["presets"]["t"]["p2"] == {"n": 2}


def test_a_failed_write_keeps_the_old_file(
    store: PresetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save("t", "keep", {"a": 1})

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("various_tools.core.presets.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.save("t", "new", {"a": 2})
    monkeypatch.undo()
    assert store.names("t") == ["keep"]
    assert [p.name for p in store.path.parent.iterdir()] == ["presets.json"]
