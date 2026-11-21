"""Place names and library duplicates in the Timeline, the Map, the filter bar and the preview."""

from __future__ import annotations

import os
import shutil
from datetime import date
from pathlib import Path

from pytestqt.qtbot import QtBot
from tests.gui.test_map_page import fill_library, gps_jpeg  # noqa: PLC2701

from various_tools.core.media.library import MediaFilter
from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.gui.library_model import LibraryModel, library_model
from various_tools.gui.map_page import MapPage, place_text
from various_tools.gui.state import AppState
from various_tools.gui.timeline_page import TimelinePage
from various_tools.gui.widgets.filter_bar import ANY_PLACE, FilterBar
from various_tools.gui.widgets.media_grid import groups_from_days
from various_tools.gui.widgets.preview_pane import details_text


def _library(state: AppState, tmp_path: Path) -> Path:
    root = tmp_path / "pics"
    gps_jpeg(root / "paris.jpg", 48.8566, 2.3522, "2023:05:01 10:00:00")
    gps_jpeg(root / "lyon.jpg", 45.76, 4.84, "2023:05:02 10:00:00")
    gps_jpeg(root / "rome.jpg", 41.9, 12.5, "2024:07:01 10:00:00")
    gps_jpeg(root / "sea.jpg", 0.0, -30.0, "2024:07:02 10:00:00")
    gps_jpeg(root / "nowhere.jpg", None, None, "2024:07:03 10:00:00")
    shutil.copy2(root / "paris.jpg", root / "copy of paris.jpg")  # same content, size and time
    state.update_settings(library_folders=[str(root)])
    fill_library(state, [root])
    return root


def _timeline(qtbot: QtBot, state: AppState) -> TimelinePage:
    page = TimelinePage(state)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.grid.load_thumbnail = lambda _p: None
    page.preview.loader.load_picture = lambda _p, _px: None
    return page


def _plan(root: Path) -> Plan:
    plan = Plan(tool="duplicates", roots=[root])
    plan.actions.append(
        PlannedAction(
            ActionKind.TRASH,
            src=root / "copy of paris.jpg",
            reason="same",
            group_id=str(root / "paris.jpg"),
        )
    )
    return plan


# ----- place names ------------------------------------------------------------------------------


def test_items_have_place_names_and_the_preview_and_map_show_them(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    _library(gui_state, tmp_path)
    page = _timeline(qtbot, gui_state)
    items = {i.path.name: i for i in page.model.all_items}
    assert items["paris.jpg"].place is not None
    assert items["paris.jpg"].place.text == "Paris, Île-de-France, France"
    assert items["sea.jpg"].place is None  # open sea: farther than 50 km from any city
    assert items["nowhere.jpg"].place is None
    assert "Place: Paris, Île-de-France, France (48.8566, 2.3522)" in details_text(
        items["paris.jpg"]
    )
    assert "Place: 0.0000, -30.0000" in details_text(items["sea.jpg"])
    assert place_text([items["paris.jpg"], items["lyon.jpg"], items["paris.jpg"]]).endswith(
        "and 1 other place"
    )
    assert "Paris" in page.grid.tooltip_for(page.grid.row_of_path(items["paris.jpg"].path))

    map_page = MapPage(gui_state)
    qtbot.addWidget(map_page)
    map_page.grid.load_thumbnail = lambda _p: None
    map_page.map.clusterClicked.emit([items["paris.jpg"]])
    assert "near Paris, Île-de-France, France" in map_page.place_label.text()
    assert "Paris, Île-de-France, France" in map_page._describe([items["paris.jpg"]])  # pyright: ignore[reportPrivateUsage]


def test_place_filter_in_the_bar_goes_country_then_region(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    _library(gui_state, tmp_path)
    model = library_model(gui_state)
    bar = FilterBar(model)
    qtbot.addWidget(bar)
    countries = [bar.country_box.itemText(i) for i in range(bar.country_box.count())]
    assert countries[0] == ANY_PLACE
    assert {"France", "Italy"} <= set(countries)
    assert not bar.region_box.isEnabled()

    bar.country_box.setCurrentIndex(bar.country_box.findData("France"))
    assert model.filter.place_country == "France"
    assert {i.path.name for i in model.shown} == {
        "paris.jpg",
        "copy of paris.jpg",
        "lyon.jpg",
    }
    assert bar.region_box.isEnabled()
    bar.region_box.setCurrentIndex(bar.region_box.findData("Île-de-France"))
    assert model.filter.place_region == "Île-de-France"
    assert {i.path.name for i in model.shown} == {"paris.jpg", "copy of paris.jpg"}

    bar.clear_button.click()
    assert model.filter == MediaFilter()
    assert bar.country_box.currentData() is None
    assert len(model.shown) == 6


# ----- duplicates -------------------------------------------------------------------------------


def test_find_duplicates_asks_the_window_to_open_the_tool_on_the_library_folders(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = _library(gui_state, tmp_path)
    page = _timeline(qtbot, gui_state)
    with qtbot.waitSignal(page.openTool) as blocker:
        page.duplicates_button.click()
    assert blocker.args == ["duplicates", [str(root)]]

    gui_state.update_settings(library_folders=[])
    page.refresh()
    seen: list[object] = []
    page.openTool.connect(lambda *a: seen.append(a))
    assert page.find_duplicates() is False
    assert seen == []  # no folder: nothing to open


def test_duplicates_badge_filter_and_other_copies(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = _library(gui_state, tmp_path)
    page = _timeline(qtbot, gui_state)
    model = page.model
    assert not model.has_duplicate(model.all_items[0])
    assert not page.filter_bar.duplicates_box.isEnabled()  # nothing found yet

    assert model.update_duplicates(_plan(root)) == 1  # synchronous in tests
    assert (model.duplicates_file()).is_file()
    by_name = {i.path.name: i for i in model.all_items}
    assert model.has_duplicate(by_name["paris.jpg"])
    assert model.has_duplicate(by_name["copy of paris.jpg"])
    assert not model.has_duplicate(by_name["lyon.jpg"])
    is_duplicate = page.grid.is_duplicate
    assert is_duplicate is not None
    assert is_duplicate(by_name["paris.jpg"])
    assert "duplicate" in page.grid.tooltip_for(page.grid.row_of_path(by_name["paris.jpg"].path))
    assert page.filter_bar.duplicates_box.isEnabled()

    page.filter_bar.duplicates_box.setChecked(True)
    assert model.filter.duplicates_only is True
    assert {i.path.name for i in model.shown} == {"paris.jpg", "copy of paris.jpg"}
    assert {i.path.name for i in page.grid.items()} == {"paris.jpg", "copy of paris.jpg"}
    page.filter_bar.clear_button.click()

    # The preview lists the other copy with a Show link, and Show selects it in the grid.
    page.set_preview_visible(True)
    row = page.grid.row_of_path(by_name["paris.jpg"].path)
    page.grid.set_current(row)
    assert page.preview.copies.isVisibleTo(page.preview)
    assert "copy of paris.jpg" in page.preview.copies.text()
    with qtbot.waitSignal(page.preview.showRequested) as blocker:
        page.preview._copy_link("show:0")  # pyright: ignore[reportPrivateUsage]
    assert blocker.args is not None
    assert blocker.args == [by_name["copy of paris.jpg"].path]
    assert page.show_copy(blocker.args[0]) is True
    assert page.grid.current_row == page.grid.row_of_path(by_name["copy of paris.jpg"].path)
    page.grid.set_current(page.grid.row_of_path(by_name["lyon.jpg"].path))
    assert not page.preview.copies.isVisibleTo(page.preview)
    page.shutdown()  # no picture job may outlive the page


def test_a_changed_file_drops_out_of_the_duplicates(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = _library(gui_state, tmp_path)
    model = library_model(gui_state)
    model.update_duplicates(_plan(root))
    assert len(model.duplicate_index) == 1
    changed = root / "copy of paris.jpg"
    st = changed.stat()
    os.utime(changed, (st.st_atime, st.st_mtime + 100))
    fill_library(gui_state, [root])  # the library notices the new time
    model.reload()
    assert len(model.duplicate_index) == 0
    assert not model.has_duplicate(model.all_items[0])


def test_another_tools_plan_changes_nothing(gui_state: AppState, tmp_path: Path) -> None:
    root = _library(gui_state, tmp_path)
    model = LibraryModel(gui_state)
    plan = _plan(root)
    plan.tool = "empty_folders"
    assert model.update_duplicates(plan) == 0
    assert len(model.duplicate_index) == 0


# ----- the grouped layout -----------------------------------------------------------------------


def test_groups_from_days_matches_the_headers_in_a_filtered_timeline(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    _library(gui_state, tmp_path)
    page = _timeline(qtbot, gui_state)
    model = page.model
    all_groups = model.timeline_groups
    assert all_groups.month_counts == {(2024, 7): 3, (2023, 5): 3}
    assert all_groups.day_counts[date(2023, 5, 1)] == 2  # paris and its copy
    model.set_filter(MediaFilter(text="lyon"))
    groups = model.timeline_groups
    assert groups.month_counts == {(2023, 5): 1}
    assert groups.runs[0][2:] == (0, 1)
    assert page.grid.month_headers() == ["May 2023 · 1"]
    assert groups_from_days([]).runs == []
