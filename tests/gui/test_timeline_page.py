"""TimelinePage: the grouped grid, filters, preview, viewer, library folders and change date."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem
from pytestqt.qtbot import QtBot

from various_tools.core.context import make_context
from various_tools.core.media.library import MediaFilter, MediaLibrary, SyncReport, library_path
from various_tools.gui.library_folders_dialog import LibraryFoldersDialog
from various_tools.gui.state import AppState
from various_tools.gui.timeline_page import TimelinePage, summary_text

EXIF_IFD, GPS_IFD = 0x8769, 0x8825


def jpeg(path: Path, taken: str, place: tuple[float, float] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = Image.Exif()
    data.get_ifd(EXIF_IFD)[0x9003] = taken
    if place is not None:
        gps = data.get_ifd(GPS_IFD)
        gps[1], gps[2] = "N", (place[0], 0.0, 0.0)
        gps[3], gps[4] = "E", (place[1], 0.0, 0.0)
    Image.new("RGB", (16, 12), (1, 2, 3)).save(path, "JPEG", exif=data.tobytes())
    return path


def fill_library(state: AppState, roots: list[Path]) -> None:
    ctx = make_context(state.settings, with_journal=False)
    try:
        with MediaLibrary(library_path(state.settings)) as lib:
            lib.sync(roots, ctx)
    finally:
        ctx.close()


def make_page(qtbot: QtBot, state: AppState) -> TimelinePage:
    page = TimelinePage(state)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.grid.load_thumbnail = lambda _p: None
    page.preview.loader.load_picture = lambda _p, _px: None
    return page


def top(page: TimelinePage, i: int) -> QTreeWidgetItem:
    node = page.months.topLevelItem(i)
    assert node is not None
    return node


def tree_texts(page: TimelinePage) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for i in range(page.months.topLevelItemCount()):
        year = top(page, i)
        children = [year.child(j) for j in range(year.childCount())]
        out.append((year.text(0), [c.text(0) for c in children if c is not None]))
    return out


def test_summary_text_in_plain_words() -> None:
    assert summary_text(1234, 56) == "1,234 photos and 56 videos."
    assert summary_text(1, 1) == "1 photo and 1 video."
    report = SyncReport(added=12, removed=3)
    assert summary_text(1234, 56, report) == (
        "1,234 photos and 56 videos. Updated: 12 new, 3 removed."
    )
    cloud = SyncReport(added=1, cloud_only=2, errors=["x"])
    assert summary_text(0, 0, cloud) == (
        "0 photos and 0 videos. Updated: 1 new, 0 removed. 2 are in OneDrive only. "
        "1 note (point here to read it)."
    )


def library_of_three(tmp_path: Path, make_state: Callable[..., AppState]) -> tuple[AppState, Path]:
    root = tmp_path / "pics"
    jpeg(root / "a.jpg", "2023:05:01 10:00:00")
    jpeg(root / "b.jpg", "2023:05:20 10:00:00")
    jpeg(root / "c.jpg", "2024:01:02 10:00:00")
    state = make_state(library_folders=[str(root)])
    fill_library(state, [root])
    return state, root


def test_on_open_it_shows_everything_grouped_by_month(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    state, _root = library_of_three(tmp_path, make_state)
    page = make_page(qtbot, state)

    assert tree_texts(page) == [("2024 (1)", ["January (1)"]), ("2023 (2)", ["May (2)"])]
    # one grid with every item, newest month first, with day headers
    assert [i.path.name for i in page.grid.items()] == ["c.jpg", "b.jpg", "a.jpg"]
    assert page.grid.header_texts() == [
        "January 2024 · 1",
        "Tuesday 2 January 2024 · 1",
        "May 2023 · 2",
        "Saturday 20 May 2023 · 1",
        "Monday 1 May 2023 · 1",
    ]
    assert page.status_label.text() == "3 photos and 0 videos."


def test_the_jump_list_scrolls_to_a_month(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    root = tmp_path / "pics"
    for n in range(60):
        jpeg(root / f"{n}.jpg", f"{2020 + n % 3}:0{1 + n % 9}:15 10:00:00")
    state = make_state(library_folders=[str(root)])
    fill_library(state, [root])
    page = make_page(qtbot, state)
    page.show()
    qtbot.waitExposed(page)
    page.grid.verticalScrollBar().setValue(0)
    last = top(page, page.months.topLevelItemCount() - 1).child(0)
    assert last is not None
    page.months.setCurrentItem(last)
    assert page.grid.verticalScrollBar().value() > 0


def test_items_of_other_folders_are_not_shown(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    mine, old = tmp_path / "mine", tmp_path / "old"
    jpeg(mine / "a.jpg", "2023:05:01 10:00:00")
    jpeg(old / "b.jpg", "2020:05:01 10:00:00")
    state = make_state(library_folders=[str(mine)])
    fill_library(state, [mine, old])
    page = make_page(qtbot, state)
    assert tree_texts(page) == [("2023 (1)", ["May (1)"])]


def test_the_filter_bar_filters_the_grid(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    state, _root = library_of_three(tmp_path, make_state)
    page = make_page(qtbot, state)

    page.filter_bar.search.setText("b.j")
    page.filter_bar.apply_now()
    assert [i.path.name for i in page.grid.items()] == ["b.jpg"]
    assert "filtered from 3" in page.status_label.text()

    page.filter_bar.clear_button.click()
    assert len(page.grid.items()) == 3 and page.filter_bar.search.text() == ""

    index = page.filter_bar.from_box.findData("2024-01")
    page.filter_bar.from_box.setCurrentIndex(index)
    assert [i.path.name for i in page.grid.items()] == ["c.jpg"]

    page.filter_bar.clear_button.click()
    page.filter_bar.kind_box.setCurrentIndex(page.filter_bar.kind_box.findData("video"))
    assert page.grid.items() == []


def test_update_runs_sync_and_refreshes(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    root = tmp_path / "pics"
    jpeg(root / "a.jpg", "2022:03:04 05:06:07")
    jpeg(root / "b.jpg", "2022:03:05 05:06:07")
    page = make_page(qtbot, make_state(library_folders=[str(root)]))
    assert tree_texts(page) == []

    worker = page.update_library()
    assert worker is not None
    assert not page.update_button.isEnabled()
    qtbot.waitUntil(lambda: page.worker is None, timeout=30000)

    assert tree_texts(page) == [("2022 (2)", ["March (2)"])]
    assert page.status_label.text() == "2 photos and 0 videos. Updated: 2 new, 0 removed."
    assert page.update_button.isEnabled()

    (root / "a.jpg").unlink()
    assert page.update_library() is not None
    qtbot.waitUntil(lambda: page.worker is None, timeout=30000)
    assert page.status_label.text() == "1 photo and 0 videos. Updated: 0 new, 1 removed."


def test_update_without_folders_asks_for_one(
    qtbot: QtBot, make_state: Callable[..., AppState]
) -> None:
    page = make_page(qtbot, make_state())
    assert page.update_library() is None
    assert "folder" in page.status_label.text().lower()


def test_library_folders_dialog_saves_and_refreshes(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    state = make_state()
    page = make_page(qtbot, state)
    folder = tmp_path / "Camera Roll"
    folder.mkdir()
    dialog = page.choose_folders()
    qtbot.addWidget(dialog)
    dialog.folders.add_paths([str(folder)])
    dialog.accept()
    assert state.settings.library_folders == [folder]
    assert page.roots() == [folder]


def test_add_the_working_folder_is_enabled_only_when_there_is_one(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    state = make_state()
    dialog = LibraryFoldersDialog(state)
    qtbot.addWidget(dialog)
    assert not dialog.add_workspace_button.isEnabled()

    work = tmp_path / "work"
    work.mkdir()
    state.workspace = lambda: [work]  # type: ignore[attr-defined]
    other = LibraryFoldersDialog(state)
    qtbot.addWidget(other)
    assert other.add_workspace_button.isEnabled()
    other.add_workspace_button.click()
    assert other.chosen() == [work]


def test_the_preview_pane_toggles_and_shows_details(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    state, root = library_of_three(tmp_path, make_state)
    page = make_page(qtbot, state)
    page.show()
    qtbot.waitExposed(page)
    assert not page.preview.isVisible()

    page.grid.setFocus()
    qtbot.keyClick(page.grid, Qt.Key.Key_I)
    assert page.preview.isVisible() and page.preview_visible()
    page.preview_button.click()
    assert not page.preview.isVisible()

    page.preview_button.click()
    page.grid.set_current(0)
    text = page.preview.details.text()
    assert "c.jpg" in text and "2 January 2024" in text and str(root) in text
    assert "Pixels: 16 × 12" in text and "Place: none" in text
    page.grid.select_all()
    assert page.preview.details.text() == "3 selected"


def test_preview_of_a_cloud_only_item_reads_nothing(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    from various_tools.core import onedrive

    state, _root = library_of_three(tmp_path, make_state)
    page = make_page(qtbot, state)
    asked: list[Path] = []
    page.preview.loader.load_picture = lambda p, _px: asked.append(p)
    page.preview_button.setChecked(True)
    cloud = page.grid.items()[0]
    from dataclasses import replace

    page.preview.show_items([replace(cloud, cloud_only=True)])
    qtbot.wait(300)
    assert asked == [] and onedrive is not None
    assert "OneDrive" in page.preview.picture.text()


def test_enter_opens_the_viewer_and_arrows_and_escape_work(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    state, _root = library_of_three(tmp_path, make_state)
    page = make_page(qtbot, state)
    page.show()
    qtbot.waitExposed(page)
    page.grid.set_current(0)
    qtbot.keyClick(page.grid, Qt.Key.Key_Return)
    viewer = page.viewer
    assert viewer is not None
    viewer.loader.load_picture = lambda _p, _px: None
    assert viewer.current_item().path.name == "c.jpg"
    qtbot.keyClick(viewer, Qt.Key.Key_Right)
    assert viewer.current_item().path.name == "b.jpg"
    assert page.grid.current_row == 1
    qtbot.keyClick(viewer, Qt.Key.Key_Left)
    qtbot.keyClick(viewer, Qt.Key.Key_Left)  # at the first photo: stays
    assert viewer.current_item().path.name == "c.jpg"
    qtbot.keyClick(viewer, Qt.Key.Key_Escape)
    assert not viewer.isVisible()
    page.shutdown()


def test_a_video_opens_in_the_default_app_not_the_viewer(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    state, root = library_of_three(tmp_path, make_state)
    (root / "v.mp4").write_bytes(b"not a real video")
    fill_library(state, [root])
    page = make_page(qtbot, state)
    page.refresh()
    opened: list[Path] = []
    page.grid.open_file = lambda p: opened.append(p) is None
    row = page.grid.row_of_path(root / "v.mp4")
    assert row >= 0
    page.activate_row(row)
    assert opened == [root / "v.mp4"] and page.viewer is None


def test_right_click_menu_on_the_selection_and_show_on_map(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    root = tmp_path / "pics"
    here = jpeg(root / "a.jpg", "2023:05:01 10:00:00", place=(48.0, 2.0))
    jpeg(root / "b.jpg", "2023:05:02 10:00:00")
    state = make_state(library_folders=[str(root)])
    fill_library(state, [root])
    page = make_page(qtbot, state)
    page.grid.set_current(page.grid.row_of_path(here))
    texts = [a.text() for a in page.grid.menu_for_rows([page.grid.current_row]).actions()]
    assert "Show on map" in texts and "Change date…" in texts and "Properties" in texts

    asked: list[Path] = []
    page.showOnMap.connect(asked.append)
    next(
        a
        for a in page.grid.menu_for_rows([page.grid.current_row]).actions()
        if a.text() == "Show on map"
    ).trigger()
    assert asked == [here]


def test_model_is_shared_with_the_map_page(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    from various_tools.gui.map_page import MapPage

    state, _root = library_of_three(tmp_path, make_state)
    page = make_page(qtbot, state)
    other = MapPage(state)
    qtbot.addWidget(other)
    assert other.model is page.model
    page.model.set_filter(MediaFilter(text="zzz"))
    assert page.grid.items() == [] and other.filter_bar.search.text() == "zzz"


def test_qt_repaint_method_is_not_hidden() -> None:
    from PySide6.QtWidgets import QWidget

    assert "update" not in TimelinePage.__dict__
    assert TimelinePage.update is QWidget.update


@pytest.mark.parametrize("name", ["showOnMap", "shutdown"])
def test_the_names_the_main_window_uses_exist(name: str) -> None:
    assert hasattr(TimelinePage, name)


def test_a_selection_of_dated_items_is_ordered_oldest_first_in_a_day(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    root = tmp_path / "pics"
    jpeg(root / "late.jpg", "2023:05:01 18:00:00")
    jpeg(root / "early.jpg", "2023:05:01 08:00:00")
    state = make_state(library_folders=[str(root)])
    fill_library(state, [root])
    page = make_page(qtbot, state)
    assert [i.path.name for i in page.grid.items()] == ["early.jpg", "late.jpg"]
    assert page.grid.items()[0].taken == datetime(2023, 5, 1, 8, 0)
