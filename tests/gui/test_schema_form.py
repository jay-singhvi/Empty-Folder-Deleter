"""SchemaForm: renders every tool's options and the settings, round-trips values, and guards 🔒."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QCheckBox, QComboBox, QSpinBox, QTableWidgetItem
from pytestqt.qtbot import QtBot

from various_tools.core.schema import FieldSpec, describe
from various_tools.core.settings import Settings
from various_tools.gui.widgets.path_picker import PathEdit, PathListEdit
from various_tools.gui.widgets.schema_form import (
    APP_SETTING_TEXT,
    USE_APP_SETTING_TEXT,
    KeyValueEdit,
    NullableNumber,
    OrderedChecklist,
    SchemaForm,
)
from various_tools.tools.base import discover

TOOLS = discover()
TOOL_IDS = sorted(TOOLS)

# A few values per tool that differ from the defaults. Each must survive form -> model -> form.
NON_DEFAULTS: dict[str, dict[str, Any]] = {
    "duplicates": {
        "media": "images",
        "exact_only": True,
        "workers": 3,
        "phash_max": 1,
        "action": "trash",
        "verify_borderline_block": 1.5,
        "use_cache": False,
    },
    "empty_folders": {
        "min_age_days": 7,
        "skip_hidden_system": False,
        "treat_as_empty": ["a.tmp", "b.tmp"],
        "delete_mode": "permanent",
    },
    "media_resize": {
        "mode": "percent",
        "percent": 25,
        "output": "suffix",
        "suffix_tag": "_small",
        "video_crf": 20,
        "recursive": True,
        "jpeg_quality": 70,
    },
    "organize_by_date": {
        "recursive": True,
        "folder_template": "{year}",
        "date_priority": ["modified", "exif"],
        "workers": 2,
        "min_plausible_year": 2000,
        "unknown_folder": "Unk",
    },
    "project_export": {
        "mode": "tree",
        "output_format": "md",
        "language_by_suffix": {".x": "xlang", ".y": "ylang"},
        "chars_per_token": 3,
        "max_file_kb": 10,
        "exts": [".py", ".md"],
    },
    "rename": {
        "preset": "snapchat",
        "match": "*.jpg",
        "match_mode": "regex",
        "case": "lower",
        "min_plausible_year": 2001,
        "counter_scope": "global",
    },
    "checksum": {
        "mode": "verify",
        "algorithm": "sha512",
        "manifest_name": "SUMS.txt",
        "recursive": False,
        "include_globs": ["*.jpg", "docs/*"],
        "exclude_globs": [".git", "*.tmp"],
        "include_hidden": True,
        "report_extra": False,
        "unreadable_files": "skip",
        "read_chunk_kb": 64,
    },
    "corrupt_media": {
        "media": "images",
        "recursive": False,
        "image_extensions": [".jpg", ".png"],
        "video_extensions": [".mp4"],
        "include_raw": True,
        "exclude_globs": ["Thumbnails", "*.preview.jpg"],
        "include_hidden": True,
        "check_depth": "quick",
        "require_duration": False,
        "decode_timeout_s": 12.5,
        "workers": 2,
        "action": "trash",
        "quarantine_folder": "Broken",
    },
    "disk_usage": {
        "recursive": False,
        "top_files": 10,
        "top_folders": 5,
        "min_size_mb": 1.5,
        "category": "videos",
        "file_types": [".mp4", ".mkv"],
        "older_than_days": 30,
        "newer_than_days": 90,
        "exclude_globs": ["*.bak", "cache"],
        "include_hidden": True,
    },
    "media_convert": {
        "recursive": True,
        "convert_heic": False,
        "heic_target": "png",
        "webp_target": "png",
        "convert_mov": False,
        "jpeg_quality": 75,
        "background_color": "#102030",
        "keep_metadata": False,
        "optimize_output": False,
        "jpeg_extension": "jpeg",
        "video_mode": "remux",
        "reencode_codec": "h265",
        "video_crf": 22,
        "video_preset": "fast",
        "audio_bitrate_kbps": 128,
        "faststart": False,
        "duration_tolerance_s": 1.0,
        "output": "next_to_original",
        "output_folder_name": "out",
        "trash_originals": True,
    },
    "metadata_strip": {
        "recursive": True,
        "media": "both",
        "image_extensions": [".jpg", ".png"],
        "video_extensions": [".mp4", ".mov"],
        "scope": "all",
        "keep_orientation": False,
        "keep_color_profile": False,
        "output": "suffix",
        "output_folder_name": "clean",
        "suffix_tag": "_clean",
        "in_place": True,
        "ignore_minor_errors": True,
        "read_batch_size": 20,
        "duration_tolerance_s": 1.5,
    },
    "sort_by_type": {
        "recursive": True,
        "rules": ["*.pdf=Docs", "*.jpg|*.png=Pics"],
        "unmatched": "other",
        "other_folder": "Misc",
        "exclude_globs": ["*.bak", "keep"],
        "min_age_days": 3,
        "skip_hidden_system": False,
        "skip_dot_files": False,
        "clash_handling": "skip",
        "clash_suffix": "_copy",
    },
    "temp_cleaner": {
        "recursive": False,
        "min_age_days": 3,
        "include_patterns": ["*.tmp", "*.log"],
        "exclude_patterns": ["keep*"],
        "skip_hidden_system": False,
        "remove_empty_folders": False,
        "list_browser_caches": False,
        "measure_browser_caches": False,
    },
}


def make_form(tool_id: str, folder: Path, **kwargs: Any) -> SchemaForm:
    form = SchemaForm(TOOLS[tool_id].Options, **kwargs)
    assert form.set_first_folder(str(folder))
    return form


def spec_of(model: type[BaseModel], name: str) -> FieldSpec:
    return next(s for s in describe(model) if s.name == name)


def urls(*paths: str) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    return mime


# ----- every model renders --------------------------------------------------------------------


@pytest.mark.parametrize("tool_id", TOOL_IDS)
def test_form_renders_every_field_of_a_tool(qtbot: QtBot, tool_id: str) -> None:
    model = TOOLS[tool_id].Options
    form = SchemaForm(model)
    qtbot.addWidget(form)
    names = list(model.model_fields)
    assert [s.name for s in form.specs] == names
    for name in names:
        assert form.editor(name) is not None
    assert form.folder_fields()


@pytest.mark.parametrize("tool_id", TOOL_IDS)
def test_defaults_build_to_the_default_options(qtbot: QtBot, tool_id: str, tmp_path: Path) -> None:
    form = make_form(tool_id, tmp_path)
    qtbot.addWidget(form)
    built = form.build()
    assert form.errors() == {}
    first = form.folder_fields()[0]
    default_fields = {n: v for n, v in built.model_dump().items() if n != first}
    expected = TOOLS[tool_id].Options.model_validate({first: getattr(built, first)})
    assert default_fields == {n: v for n, v in expected.model_dump().items() if n != first}


def test_settings_form_renders_every_setting_but_external_tools(qtbot: QtBot) -> None:
    form = SchemaForm(Settings, Settings(), exclude={"external_tools"})
    qtbot.addWidget(form)
    names = [n for n in Settings.model_fields if n != "external_tools"]
    assert [s.name for s in form.specs] == names
    for name in names:
        assert form.editor(name) is not None
    with pytest.raises(KeyError):
        form.editor("external_tools")


def test_settings_defaults_build_and_keep_excluded_fields(qtbot: QtBot) -> None:
    settings = Settings(external_tools={"ffmpeg": "C:\\x\\ffmpeg.exe"})
    form = SchemaForm(Settings, settings, exclude={"external_tools"})
    qtbot.addWidget(form)
    built = form.build()
    assert isinstance(built, Settings)
    assert built.model_dump() == settings.model_dump()
    # An excluded field passes through unchanged.
    assert built.external_tools == {"ffmpeg": "C:\\x\\ffmpeg.exe"}


def test_settings_form_round_trips_changes(qtbot: QtBot) -> None:
    form = SchemaForm(Settings, Settings(), exclude={"external_tools"})
    qtbot.addWidget(form)
    form.update_values(
        {
            "theme": "dark",
            "workers": 3,
            "min_plausible_year": 2005,
            "duplicate_folder": "dupes",
            "remember_last_folders": False,
        }
    )
    built = form.build()
    assert isinstance(built, Settings)
    assert (built.theme, built.workers, built.min_plausible_year) == ("dark", 3, 2005)
    assert built.duplicate_folder == "dupes"
    assert built.remember_last_folders is False


# ----- round trips ----------------------------------------------------------------------------


def test_every_tool_has_a_round_trip_case() -> None:
    assert set(NON_DEFAULTS) == set(TOOL_IDS)


@pytest.mark.parametrize("tool_id", TOOL_IDS)
def test_non_default_values_round_trip(qtbot: QtBot, tool_id: str, tmp_path: Path) -> None:
    form = make_form(tool_id, tmp_path)
    qtbot.addWidget(form)
    values = NON_DEFAULTS[tool_id]
    form.update_values(values)
    built = form.build()
    for name, expected in values.items():
        assert getattr(built, name) == expected, name
    # Put the model back into the form and read it again: nothing may drift.
    again = SchemaForm(TOOLS[tool_id].Options, built)
    qtbot.addWidget(again)
    assert again.build() == built
    form.reset()
    assert form.raw_values().get("min_age_days", 0) == 0 or tool_id != "empty_folders"


def test_set_values_replaces_everything_update_values_does_not(
    qtbot: QtBot, tmp_path: Path
) -> None:
    form = make_form("empty_folders", tmp_path)
    qtbot.addWidget(form)
    form.update_values({"min_age_days": 5, "skip_hidden_system": False})
    form.update_values({"min_age_days": 6})
    assert form.raw_values()["skip_hidden_system"] is False
    form.set_values({"roots": [str(tmp_path)], "min_age_days": 9})
    raw = form.raw_values()
    assert raw["min_age_days"] == 9
    assert raw["skip_hidden_system"] is True  # back to its default


def test_changed_signal_fires_for_user_edits_not_for_loading(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("empty_folders", tmp_path)
    qtbot.addWidget(form)
    seen: list[int] = []
    form.changed.connect(lambda: seen.append(1))
    form.set_values({"roots": [str(tmp_path)], "min_age_days": 4})
    assert seen == []
    spin = form.editor("min_age_days")
    assert isinstance(spin, QSpinBox)
    spin.setValue(8)
    assert seen == [1]
    form.editor("skip_hidden_system").toggle()  # pyright: ignore[reportAttributeAccessIssue]
    assert seen == [1, 1]


# ----- nullable fields ------------------------------------------------------------------------


def test_nullable_number_uses_the_app_setting_until_unchecked(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("duplicates", tmp_path)
    qtbot.addWidget(form)
    editor = form.editor("workers")
    assert isinstance(editor, NullableNumber)
    assert editor.use_default.text() == USE_APP_SETTING_TEXT
    assert editor.use_default.isChecked()
    assert not editor.spin.isEnabled()
    assert form.build().workers is None  # type: ignore[attr-defined]
    editor.use_default.setChecked(False)
    assert editor.spin.isEnabled()
    editor.spin.setValue(4)
    assert form.build().workers == 4  # type: ignore[attr-defined]
    editor.use_default.setChecked(True)
    assert form.build().workers is None  # type: ignore[attr-defined]
    # Loading None checks the box again.
    form.update_values({"workers": 2})
    assert not editor.use_default.isChecked()
    form.update_values({"workers": None})
    assert editor.use_default.isChecked()


def test_nullable_choice_first_entry_is_the_app_setting(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("rename", tmp_path)
    qtbot.addWidget(form)
    combo = form.editor("case")
    assert isinstance(combo, QComboBox)
    assert combo.itemText(0) == APP_SETTING_TEXT
    assert combo.currentIndex() == 0
    assert form.build().case is None  # type: ignore[attr-defined]
    combo.setCurrentText("upper")
    assert form.build().case == "upper"  # type: ignore[attr-defined]
    combo.setCurrentIndex(0)
    assert form.build().case is None  # type: ignore[attr-defined]


def test_nullable_text_and_path_empty_mean_none(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("organize_by_date", tmp_path)
    qtbot.addWidget(form)
    built = form.build()
    assert built.dest_root is None  # type: ignore[attr-defined]
    assert built.unknown_folder is None  # type: ignore[attr-defined]
    form.update_values({"dest_root": str(tmp_path / "out"), "unknown_folder": "Nowhere"})
    built = form.build()
    assert built.dest_root == tmp_path / "out"  # type: ignore[attr-defined]
    assert built.unknown_folder == "Nowhere"  # type: ignore[attr-defined]
    form.update_values({"dest_root": None, "unknown_folder": None})
    assert form.build().dest_root is None  # type: ignore[attr-defined]


# ----- depends_on -----------------------------------------------------------------------------


def test_depends_on_shows_and_hides_fields(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("media_resize", tmp_path)
    qtbot.addWidget(form)
    mode = form.editor("mode")
    assert isinstance(mode, QComboBox)
    assert mode.currentText() == "fit_box"
    assert form.editor("size").isVisibleTo(form)
    assert not form.editor("percent").isVisibleTo(form)
    assert not form.editor("target_px").isVisibleTo(form)
    assert form.editor("size").parentWidget().isHidden() is False  # pyright: ignore[reportOptionalMemberAccess]

    mode.setCurrentText("percent")
    assert form.editor("percent").isVisibleTo(form)
    assert not form.editor("size").isVisibleTo(form)

    mode.setCurrentText("shorter_side")
    assert form.editor("target_px").isVisibleTo(form)
    mode.setCurrentText("longer_side")
    assert form.editor("target_px").isVisibleTo(form)  # "a|b" lists both choices
    assert not form.editor("percent").isVisibleTo(form)


def test_depends_on_chain_size_custom_and_output(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("media_resize", tmp_path)
    qtbot.addWidget(form)
    assert not form.editor("max_width").isVisibleTo(form)
    size = form.editor("size")
    assert isinstance(size, QComboBox)
    size.setCurrentText("custom")
    assert form.editor("max_width").isVisibleTo(form)
    assert form.editor("max_height").isVisibleTo(form)
    # Output: new_folder shows the folder name, suffix shows the tag.
    assert form.editor("output_folder_name").isVisibleTo(form)
    assert not form.editor("suffix_tag").isVisibleTo(form)
    output = form.editor("output")
    assert isinstance(output, QComboBox)
    output.setCurrentText("suffix")
    assert form.editor("suffix_tag").isVisibleTo(form)
    assert not form.editor("output_folder_name").isVisibleTo(form)


def test_hidden_fields_still_count_in_the_values(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("media_resize", tmp_path)
    qtbot.addWidget(form)
    form.update_values({"percent": 33})
    assert not form.editor("percent").isVisibleTo(form)  # mode is fit_box
    assert form.build().percent == 33  # type: ignore[attr-defined]


def test_depends_on_checkbox_controls_ollama_fields(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("duplicates", tmp_path)
    qtbot.addWidget(form)
    form.set_advanced_visible(True)
    assert not form.editor("ollama_model").isVisibleTo(form)
    check = form.editor("verify_with_ollama")
    assert isinstance(check, QCheckBox)
    check.setChecked(True)
    assert form.editor("ollama_model").isVisibleTo(form)
    assert form.editor("ollama_prompt").isVisibleTo(form)
    check.setChecked(False)
    assert not form.editor("ollama_model").isVisibleTo(form)


def test_depends_on_follows_loaded_values(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("media_resize", tmp_path)
    qtbot.addWidget(form)
    form.set_values({"roots": [str(tmp_path)], "mode": "percent"})
    assert form.editor("percent").isVisibleTo(form)
    form.reset()
    assert not form.editor("percent").isVisibleTo(form)


# ----- locked values --------------------------------------------------------------------------


class LockRecorder:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def __call__(self, spec: FieldSpec, _parent: object) -> bool:
        self.asked.append(spec.name)
        return self.answer


def test_locked_threshold_prompts_once_and_accepts(qtbot: QtBot, tmp_path: Path) -> None:
    recorder = LockRecorder(True)
    form = make_form("duplicates", tmp_path, confirm_locked=recorder)
    qtbot.addWidget(form)
    spin = form.editor("phash_max")
    assert isinstance(spin, QSpinBox)
    assert spin.value() == 2
    changes: list[int] = []
    form.changed.connect(lambda: changes.append(1))
    spin.setValue(5)
    assert recorder.asked == ["phash_max"]
    assert spin.value() == 5
    assert changes == [1]
    spin.setValue(6)  # confirmed once per form
    assert recorder.asked == ["phash_max"]
    assert form.build().phash_max == 6  # type: ignore[attr-defined]


def test_locked_threshold_declined_goes_back_to_the_old_value(qtbot: QtBot, tmp_path: Path) -> None:
    recorder = LockRecorder(False)
    form = make_form("duplicates", tmp_path, confirm_locked=recorder)
    qtbot.addWidget(form)
    spin = form.editor("phash_max")
    assert isinstance(spin, QSpinBox)
    changes: list[int] = []
    form.changed.connect(lambda: changes.append(1))
    spin.setValue(9)
    assert recorder.asked == ["phash_max"]
    assert spin.value() == 2
    assert changes == []
    assert form.build().phash_max == 2  # type: ignore[attr-defined]
    spin.setValue(9)  # still locked, so it asks again
    assert recorder.asked == ["phash_max", "phash_max"]


def test_locked_float_and_flag_fields_prompt(qtbot: QtBot, tmp_path: Path) -> None:
    recorder = LockRecorder(False)
    form = make_form("media_resize", tmp_path, confirm_locked=recorder)
    qtbot.addWidget(form)
    form.set_advanced_visible(True)
    never_upscale = form.editor("never_upscale")
    assert isinstance(never_upscale, QCheckBox)
    never_upscale.setChecked(False)
    assert recorder.asked == ["never_upscale"]
    assert never_upscale.isChecked()  # refused: it is back on


def test_locked_setting_delete_mode_prompts_and_reverts(qtbot: QtBot) -> None:
    recorder = LockRecorder(False)
    form = SchemaForm(Settings, Settings(), exclude={"external_tools"}, confirm_locked=recorder)
    qtbot.addWidget(form)
    combo = form.editor("delete_mode")
    assert isinstance(combo, QComboBox)
    combo.setCurrentText("permanent")
    assert recorder.asked == ["delete_mode"]
    assert combo.currentText() == "trash"
    assert form.build().delete_mode == "trash"  # type: ignore[attr-defined]
    recorder.answer = True
    combo.setCurrentText("permanent")
    assert combo.currentText() == "permanent"
    assert form.build().delete_mode == "permanent"  # type: ignore[attr-defined]


def test_locked_protection_flags_in_settings(qtbot: QtBot) -> None:
    recorder = LockRecorder(True)
    form = SchemaForm(Settings, Settings(), exclude={"external_tools"}, confirm_locked=recorder)
    qtbot.addWidget(form)
    form.editor("protect_drive_roots").toggle()  # pyright: ignore[reportAttributeAccessIssue]
    form.editor("allow_protected_paths").toggle()  # pyright: ignore[reportAttributeAccessIssue]
    assert recorder.asked == ["protect_drive_roots", "allow_protected_paths"]
    built = form.build()
    assert built.protect_drive_roots is False  # type: ignore[attr-defined]
    assert built.allow_protected_paths is True  # type: ignore[attr-defined]


def test_loading_values_never_prompts_and_back_to_default_does_not(
    qtbot: QtBot, tmp_path: Path
) -> None:
    recorder = LockRecorder(False)
    form = make_form("duplicates", tmp_path, confirm_locked=recorder)
    qtbot.addWidget(form)
    form.update_values({"phash_max": 7, "dhash_max": 9})
    form.set_values({"roots": [str(tmp_path)], "phash_max": 3})
    form.reset()
    assert recorder.asked == []
    spin = form.editor("phash_max")
    assert isinstance(spin, QSpinBox)
    form.update_values({"phash_max": 4})
    spin.setValue(2)  # the default again: allowed without asking
    assert recorder.asked == []


def test_lock_marker_is_in_the_label_and_tooltip(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("duplicates", tmp_path)
    qtbot.addWidget(form)
    assert spec_of(TOOLS["duplicates"].Options, "phash_max").locked
    assert "🔒" in form._fields["phash_max"].label.text()  # pyright: ignore[reportPrivateUsage]
    assert "🔒" not in form._fields["exact_only"].label.text()  # pyright: ignore[reportPrivateUsage]


# ----- validation -----------------------------------------------------------------------------


def test_missing_required_folder_is_reported(qtbot: QtBot) -> None:
    form = SchemaForm(TOOLS["project_export"].Options)
    qtbot.addWidget(form)
    with pytest.raises(ValidationError):
        form.build()
    assert form.errors()["root"] == "This is required."
    label = form._fields["root"].error_label  # pyright: ignore[reportPrivateUsage]
    assert not label.isHidden()
    assert label.text() == "This is required."


def test_errors_clear_after_a_good_build(qtbot: QtBot, tmp_path: Path) -> None:
    form = SchemaForm(TOOLS["project_export"].Options)
    qtbot.addWidget(form)
    with pytest.raises(ValidationError):
        form.build()
    form.set_first_folder(str(tmp_path))
    form.build()
    assert form.errors() == {}
    assert form._fields["root"].error_label.isHidden()  # pyright: ignore[reportPrivateUsage]


def test_value_error_from_the_model_is_shown_under_its_field(qtbot: QtBot) -> None:
    form = SchemaForm(Settings, Settings(), exclude={"external_tools"})
    qtbot.addWidget(form)
    form.update_values({"default_output_folder": ""})
    with pytest.raises(ValidationError):
        form.build()
    assert "default_output_folder" in form.errors()
    assert form.errors()["default_output_folder"]


def test_unparsable_list_text_is_reported(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("duplicates", tmp_path)
    qtbot.addWidget(form)
    editor = form.editor("sample_at")
    editor.setText("not, numbers")  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        form.build()
    assert "sample_at" in form.errors()
    # The advanced section opens so the problem is visible.
    assert form._advanced_button is not None  # pyright: ignore[reportPrivateUsage]
    assert form._advanced_button.isChecked()  # pyright: ignore[reportPrivateUsage]
    form.update_values({"sample_at": [0.25, 0.5]})
    assert form.build().sample_at == [0.25, 0.5]  # type: ignore[attr-defined]


def test_cross_field_error_for_checklist_duplicates(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("organize_by_date", tmp_path)
    qtbot.addWidget(form)
    form.update_values({"date_priority": ["nope"]})
    with pytest.raises(ValidationError):
        form.build()
    assert "date_priority" in form.errors()


# ----- special editors ------------------------------------------------------------------------


def test_ordered_checklist_reorders_and_builds_in_order(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("organize_by_date", tmp_path)
    qtbot.addWidget(form)
    checklist = form.editor("date_priority")
    assert isinstance(checklist, OrderedChecklist)
    start = checklist.checked()
    assert len(start) >= 2
    seen: list[int] = []
    form.changed.connect(lambda: seen.append(1))
    checklist.list_widget.setCurrentRow(1)
    checklist.move_current(-1)
    moved = checklist.checked()
    assert moved[0] == start[1]
    assert moved[1] == start[0]
    assert seen == [1]
    assert form.build().date_priority == moved  # type: ignore[attr-defined]
    # Out-of-range moves do nothing.
    checklist.list_widget.setCurrentRow(0)
    checklist.move_current(-1)
    assert checklist.checked() == moved
    # Unchecking removes the choice from the value but keeps it listed.
    item = checklist.list_widget.item(0)
    item.setCheckState(item.checkState().__class__.Unchecked)
    assert moved[0] not in checklist.checked()
    assert moved[0] in checklist.order()


def test_key_value_editor_round_trip_and_rows(qtbot: QtBot, tmp_path: Path) -> None:
    form = make_form("project_export", tmp_path)
    qtbot.addWidget(form)
    table = form.editor("language_by_suffix")
    assert isinstance(table, KeyValueEdit)
    form.update_values({"language_by_suffix": {".x": "xlang"}})
    assert table.items() == {".x": "xlang"}
    seen: list[int] = []
    form.changed.connect(lambda: seen.append(1))
    table._append(".z", "zlang")  # pyright: ignore[reportPrivateUsage]
    table.table.setItem(1, 1, QTableWidgetItem("zed"))
    assert seen
    assert table.items() == {".x": "xlang", ".z": "zed"}
    assert form.build().language_by_suffix == {".x": "xlang", ".z": "zed"}  # type: ignore[attr-defined]
    table.table.setCurrentCell(0, 0)
    table.remove_selected()
    assert table.items() == {".z": "zed"}
    # A blank key row is ignored.
    table._append("", "ignored")  # pyright: ignore[reportPrivateUsage]
    assert table.items() == {".z": "zed"}


# ----- folder fields and drops ----------------------------------------------------------------


def test_set_first_folder_for_a_list_and_for_a_single_folder(qtbot: QtBot, tmp_path: Path) -> None:
    listed = SchemaForm(TOOLS["empty_folders"].Options)
    qtbot.addWidget(listed)
    seen: list[int] = []
    listed.changed.connect(lambda: seen.append(1))
    assert listed.set_first_folder(str(tmp_path))
    assert seen == [1]
    assert listed.raw_values()["roots"] == [str(tmp_path)]
    single = SchemaForm(TOOLS["project_export"].Options)
    qtbot.addWidget(single)
    assert single.set_first_folder(str(tmp_path))
    assert str(single.raw_values()["root"]) == str(tmp_path)


def test_set_first_folder_without_a_folder_field_returns_false(qtbot: QtBot) -> None:
    class Plain(BaseModel):
        flag: bool = False

    form = SchemaForm(Plain)
    qtbot.addWidget(form)
    assert form.folder_fields() == []
    assert form.set_first_folder("C:\\x") is False


def test_folder_fields_are_listed_in_order(qtbot: QtBot) -> None:
    form = SchemaForm(TOOLS["duplicates"].Options)
    qtbot.addWidget(form)
    assert form.folder_fields()[0] == "roots"
    assert "duplicate_folder" in form.folder_fields()
    assert SchemaForm(TOOLS["project_export"].Options).folder_fields() == ["root"]


def test_path_edit_accepts_a_dropped_folder_only_in_folder_mode(
    qtbot: QtBot, tmp_path: Path
) -> None:
    folder = tmp_path / "dropped"
    folder.mkdir()
    file = tmp_path / "a.txt"
    file.write_text("x")
    edit = PathEdit("folder")
    qtbot.addWidget(edit)
    changes: list[int] = []
    edit.changed.connect(lambda: changes.append(1))
    assert edit.accept_mime(urls(str(file))) is False
    assert edit.text() == ""
    assert edit.accept_mime(urls(str(file), str(folder))) is True
    assert edit.text() == str(folder)
    assert changes == [1]
    files = PathEdit("file")
    qtbot.addWidget(files)
    assert files.accept_mime(urls(str(folder))) is False
    assert files.accept_mime(urls(str(file))) is True
    assert files.text() == str(file)


def test_path_edit_ignores_web_links_and_empty_drops(qtbot: QtBot) -> None:
    edit = PathEdit("folder")
    qtbot.addWidget(edit)
    web = QMimeData()
    web.setUrls([QUrl("https://example.com/x")])
    assert edit.accept_mime(web) is False
    assert edit.accept_mime(QMimeData()) is False


def test_path_edit_set_text_is_silent(qtbot: QtBot) -> None:
    edit = PathEdit("folder")
    qtbot.addWidget(edit)
    changes: list[int] = []
    edit.changed.connect(lambda: changes.append(1))
    edit.set_text("C:\\somewhere")
    assert changes == []
    edit.line_edit.setText("C:\\other")
    assert changes == [1]


def test_path_list_accepts_drops_without_duplicates(qtbot: QtBot, tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    file = tmp_path / "f.txt"
    file.write_text("x")
    edit = PathListEdit()
    qtbot.addWidget(edit)
    changes: list[int] = []
    edit.changed.connect(lambda: changes.append(1))
    assert edit.accept_mime(urls(str(file))) is False
    assert edit.paths() == []
    assert edit.accept_mime(urls(str(one), str(two), str(file))) is True
    assert edit.paths() == [str(one), str(two)]
    assert changes == [1]
    edit.accept_mime(urls(str(one)))  # already there
    assert edit.paths() == [str(one), str(two)]
    assert changes == [1]
    edit.list_widget.setCurrentRow(0)
    edit.list_widget.item(0).setSelected(True)
    edit.remove_selected()
    assert edit.paths() == [str(two)]


def test_dropping_folders_on_a_form_field_reaches_the_options(qtbot: QtBot, tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    form = SchemaForm(TOOLS["duplicates"].Options)
    qtbot.addWidget(form)
    picker = form.editor("roots")
    assert isinstance(picker, PathListEdit)
    assert picker.accept_mime(urls(str(folder)))
    assert form.build().roots == [folder]  # type: ignore[attr-defined]
    single = SchemaForm(TOOLS["project_export"].Options)
    qtbot.addWidget(single)
    root = single.editor("root")
    assert isinstance(root, PathEdit)
    assert root.accept_mime(urls(str(folder)))
    assert single.build().root == folder  # type: ignore[attr-defined]
