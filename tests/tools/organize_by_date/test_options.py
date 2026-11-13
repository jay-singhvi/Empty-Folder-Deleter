"""Organize by date: every option that used to be a constant changes what the tool does.

Owner requirement: nothing is constant. An option that is left empty (None) takes its value from
the app settings; a value on the option wins over the setting.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.settings import Settings
from various_tools.tools.organize_by_date import TOOL, OrganizeByDateOptions
from various_tools.tools.organize_by_date import dates as dates_module
from various_tools.tools.organize_by_date import tool as obd


def _ctx(tmp_path: Path, **settings: Any) -> RunContext:
    values: dict[str, Any] = {"onedrive_root": tmp_path, "data_folder": tmp_path / "appdata"}
    values.update(settings)
    return RunContext(
        settings=Settings.model_validate(values),
        deps=ExternalTools(ollama_probe=lambda _u: None),
    )


def _photo(path: Path, modified: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8)).save(path, format="JPEG")
    stamp = datetime.fromisoformat(modified).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _plain(path: Path, modified: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real image")
    stamp = datetime.fromisoformat(modified).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _opts(tmp_path: Path, src: Path, **kwargs: Any) -> OrganizeByDateOptions:
    values: dict[str, Any] = {
        "sources": [src],
        "dest_root": tmp_path / "Dest",
        "catalog_path": tmp_path / "catalog.sqlite",
    }
    values.update(kwargs)
    return OrganizeByDateOptions(**values)


@pytest.fixture
def src(tmp_path: Path) -> Path:
    folder = tmp_path / "Source"
    folder.mkdir()
    return folder


def _only_dst(tmp_path: Path, src: Path, ctx: RunContext, **kwargs: Any) -> Path:
    plan = TOOL.scan(_opts(tmp_path, src, **kwargs), ctx)
    (action,) = plan.selected_actions()
    assert action.dst is not None
    return action.dst


# ----- unknown-date folder -------------------------------------------------------------------


def test_unknown_folder_comes_from_the_setting_when_the_option_is_empty(
    tmp_path: Path, src: Path
) -> None:
    _photo(src / "old.jpg", "1975-06-01 10:00:00")
    default = _only_dst(tmp_path, src, _ctx(tmp_path))
    assert default == tmp_path / "Dest" / "Unknown Date" / "old.jpg"
    from_setting = _only_dst(tmp_path, src, _ctx(tmp_path, unknown_date_folder="No Date"))
    assert from_setting == tmp_path / "Dest" / "No Date" / "old.jpg"


def test_unknown_folder_option_wins_over_the_setting(tmp_path: Path, src: Path) -> None:
    _photo(src / "old.jpg", "1975-06-01 10:00:00")
    ctx = _ctx(tmp_path, unknown_date_folder="No Date")
    assert _only_dst(tmp_path, src, ctx, unknown_folder="Mystery").parent.name == "Mystery"


def test_unknown_folder_from_the_setting_is_checked_too(tmp_path: Path, src: Path) -> None:
    _photo(src / "old.jpg", "1975-06-01 10:00:00")
    ctx = _ctx(tmp_path, unknown_date_folder="a/b")
    plan = TOOL.scan(_opts(tmp_path, src), ctx)
    assert plan.selected_actions() == []
    assert any("plain folder name" in note for note in plan.notes)


# ----- plausible year, workers, timeouts ------------------------------------------------------


def test_min_plausible_year_comes_from_the_setting_and_the_option_overrides_it(
    tmp_path: Path, src: Path
) -> None:
    _photo(src / "p.jpg", "1995-03-09 12:00:00")
    assert _only_dst(tmp_path, src, _ctx(tmp_path)).parent.name == "1995 - 03"
    strict_setting = _ctx(tmp_path, min_plausible_year=2000)
    assert _only_dst(tmp_path, src, strict_setting).parent.name == "Unknown Date"
    overridden = _only_dst(tmp_path, src, strict_setting, min_plausible_year=1990)
    assert overridden.parent.name == "1995 - 03"
    assert _only_dst(tmp_path, src, _ctx(tmp_path), min_plausible_year=2000).parent.name == (
        "Unknown Date"
    )


def test_workers_come_from_the_setting_and_the_option_overrides_it(
    tmp_path: Path, src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _photo(src / "p.jpg", "2020-01-01 10:00:00")
    seen: list[int] = []
    real_pool = obd.ThreadPoolExecutor

    def spy(max_workers: int) -> Any:
        seen.append(max_workers)
        return real_pool(max_workers=max_workers)

    monkeypatch.setattr(obd, "ThreadPoolExecutor", spy)
    TOOL.scan(_opts(tmp_path, src), _ctx(tmp_path, workers=3))
    TOOL.scan(_opts(tmp_path, src, workers=2), _ctx(tmp_path, workers=3))
    assert seen == [3, 2]


def test_tool_timeouts_come_from_the_settings(
    tmp_path: Path, src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _photo(src / "p.jpg", "2020-01-01 10:00:00")
    captured: dict[str, Any] = {}
    real_read = dates_module.read_capture_date

    def spy(path: Path, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_read(path, **kwargs)

    monkeypatch.setattr(obd.dates, "read_capture_date", spy)
    ctx = _ctx(tmp_path, ffprobe_timeout_s=7, exiftool_read_timeout_s=9)
    TOOL.scan(_opts(tmp_path, src), ctx)
    assert captured["ffprobe_timeout"] == 7
    assert captured["exiftool_timeout"] == 9


def test_read_capture_date_passes_the_timeouts_to_the_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = _plain(tmp_path / "clip.mp4", "2020-01-01 10:00:00")
    raw = _plain(tmp_path / "IMG.cr3", "2020-01-01 10:00:00")
    seen: dict[str, float] = {}

    def fake_creation(ffprobe: Path, path: Path, *, timeout: float) -> None:
        seen["ffprobe"] = timeout

    def fake_exiftool(exiftool: Path, path: Path, timeout: float) -> None:
        seen["exiftool"] = timeout

    monkeypatch.setattr(dates_module, "creation_time", fake_creation)
    monkeypatch.setattr(dates_module, "exiftool_capture_time", fake_exiftool)
    common: dict[str, Any] = {
        "modified": datetime(2020, 1, 1),
        "min_year": 1990,
        "now": datetime(2026, 1, 1),
        "ffprobe": Path("ffprobe.exe"),
        "exiftool": Path("exiftool.exe"),
        "ffprobe_timeout": 11,
        "exiftool_timeout": 13,
    }
    dates_module.read_capture_date(clip, priority=("video_tag",), **common)
    dates_module.read_capture_date(raw, priority=("exiftool",), **common)
    assert seen == {"ffprobe": 11, "exiftool": 13}


# ----- catalog and destination ----------------------------------------------------------------


def test_catalog_lives_in_the_settings_data_folder_when_no_path_is_given(
    tmp_path: Path, src: Path
) -> None:
    _photo(src / "p.jpg", "2020-01-01 10:00:00")
    data = tmp_path / "my data"
    opts = OrganizeByDateOptions(sources=[src], dest_root=tmp_path / "Dest")
    TOOL.scan(opts, _ctx(tmp_path, data_folder=data))
    assert (data / obd.CATALOG_FILE).is_file()


def test_default_destination_is_the_onedrive_sub_path_and_it_is_configurable(
    tmp_path: Path, src: Path
) -> None:
    _photo(src / "p.jpg", "2020-01-01 10:00:00")
    ctx = _ctx(tmp_path)
    default = OrganizeByDateOptions(sources=[src], catalog_path=tmp_path / "c.sqlite")
    (action,) = TOOL.scan(default, ctx).selected_actions()
    assert action.dst == tmp_path / "Pictures" / "Camera Roll" / "2020" / "2020 - 01" / "p.jpg"
    custom = OrganizeByDateOptions(
        sources=[src], catalog_path=tmp_path / "c.sqlite", dest_subfolder="Photos/Inbox"
    )
    (action,) = TOOL.scan(custom, ctx).selected_actions()
    assert action.dst == tmp_path / "Photos" / "Inbox" / "2020" / "2020 - 01" / "p.jpg"


@pytest.mark.parametrize("bad", ["../escape", "C:/elsewhere", "/rooted", "a/../b", "a*b"])
def test_destination_sub_path_must_stay_inside_onedrive(
    tmp_path: Path, src: Path, bad: str
) -> None:
    _photo(src / "p.jpg", "2020-01-01 10:00:00")
    opts = OrganizeByDateOptions(
        sources=[src], catalog_path=tmp_path / "c.sqlite", dest_subfolder=bad
    )
    plan = TOOL.scan(opts, _ctx(tmp_path))
    assert plan.selected_actions() == []
    assert any("relative folder path" in note for note in plan.notes)


def test_protected_folders_come_from_the_settings(tmp_path: Path, src: Path) -> None:
    _photo(src / "p.jpg", "2020-01-01 10:00:00")
    guarded = tmp_path / "Guarded"
    ctx = _ctx(tmp_path, protected_folders=[str(guarded)])
    plan = TOOL.scan(_opts(tmp_path, src, dest_root=guarded / "Photos"), ctx)
    assert plan.selected_actions() == []
    assert any("protected folder" in note for note in plan.notes)
    other = _ctx(tmp_path, protected_folders=[str(tmp_path / "Elsewhere")])
    assert TOOL.scan(_opts(tmp_path, src, dest_root=guarded / "Photos"), other).selected_actions()


# ----- file types, dot-files, clash suffix ----------------------------------------------------


def test_extension_lists_are_normalised() -> None:
    opts = OrganizeByDateOptions(photo_exts=["JPG", " .Png ", "jpg", ""], raw_exts=["ARW"])
    assert opts.photo_exts == [".jpg", ".png"]
    assert opts.raw_exts == [".arw"]


def test_default_extension_lists_are_the_legacy_ones() -> None:
    opts = OrganizeByDateOptions()
    assert set(opts.photo_exts) == dates_module.PHOTO_EXTS
    assert set(opts.raw_exts) == dates_module.RAW_EXTS
    assert set(opts.video_exts) == dates_module.VIDEO_EXTS


def test_a_custom_file_type_is_picked_up_and_a_removed_one_is_ignored(
    tmp_path: Path, src: Path
) -> None:
    _plain(src / "odd.xyz", "2015-03-09 12:00:00")
    _photo(src / "p.jpg", "2016-04-10 12:00:00")
    ctx = _ctx(tmp_path)
    ignored = TOOL.scan(_opts(tmp_path, src), ctx)
    assert [a.src.name for a in ignored.selected_actions() if a.src] == ["p.jpg"]
    picked = TOOL.scan(_opts(tmp_path, src, video_exts=[".xyz"], photo_exts=[]), ctx)
    assert [a.src.name for a in picked.selected_actions() if a.src] == ["odd.xyz"]


def test_bad_file_type_lists_are_reported(tmp_path: Path, src: Path) -> None:
    _photo(src / "p.jpg", "2016-04-10 12:00:00")
    ctx = _ctx(tmp_path)
    bad = TOOL.scan(_opts(tmp_path, src, photo_exts=["a/b"]), ctx)
    assert bad.selected_actions() == [] and any("file types" in n for n in bad.notes)
    none = TOOL.scan(_opts(tmp_path, src, photo_exts=[], raw_exts=[], video_exts=[]), ctx)
    assert none.selected_actions() == [] and any("at least one" in n for n in none.notes)


def test_dot_files_can_be_included(tmp_path: Path, src: Path) -> None:
    _photo(src / ".hidden.jpg", "2016-04-10 12:00:00")
    ctx = _ctx(tmp_path)
    assert TOOL.scan(_opts(tmp_path, src), ctx).selected_actions() == []
    included = TOOL.scan(_opts(tmp_path, src, skip_dot_files=False), ctx)
    assert [a.src.name for a in included.selected_actions() if a.src] == [".hidden.jpg"]


def test_clash_suffix_is_configurable_and_checked(tmp_path: Path, src: Path) -> None:
    _photo(src / "a.jpg", "2016-04-10 12:00:00")
    taken = tmp_path / "Dest" / "2016" / "2016 - 04"
    _photo(taken / "a.jpg", "2016-04-10 12:00:00")
    ctx = _ctx(tmp_path)
    (action,) = TOOL.scan(_opts(tmp_path, src), ctx).selected_actions()
    assert action.dst == taken / "a_dup1.jpg"
    (action,) = TOOL.scan(_opts(tmp_path, src, clash_suffix="-copy"), ctx).selected_actions()
    assert action.dst == taken / "a-copy1.jpg"
    refused = TOOL.scan(_opts(tmp_path, src, clash_suffix="a/b"), ctx)
    assert refused.selected_actions() == []
    assert any("clash suffix" in note for note in refused.notes)
