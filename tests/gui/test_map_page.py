"""The Map page and the "Photos & videos" sidebar section."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPointF
from pytestqt.qtbot import QtBot

from various_tools.core.context import make_context
from various_tools.core.media.library import MediaFilter, MediaLibrary, library_path
from various_tools.core.settings import DEFAULT_TILE_URL, Settings
from various_tools.gui.main_window import MainWindow
from various_tools.gui.map_page import MapPage
from various_tools.gui.state import AppState
from various_tools.gui.timeline_page import TimelinePage

EXIF_IFD, GPS_IFD = 0x8769, 0x8825


def gps_jpeg(
    path: Path, lat: float | None, lon: float | None, taken: str = "2024:05:01 10:00:00"
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = Image.Exif()
    data.get_ifd(EXIF_IFD)[0x9003] = taken
    if lat is not None and lon is not None:
        gps = data.get_ifd(GPS_IFD)
        gps[1], gps[2] = ("N" if lat >= 0 else "S"), (abs(lat), 0.0, 0.0)
        gps[3], gps[4] = ("E" if lon >= 0 else "W"), (abs(lon), 0.0, 0.0)
    Image.new("RGB", (16, 12), (1, 2, 3)).save(path, "JPEG", exif=data.tobytes())
    return path


def fill_library(state: AppState, roots: list[Path]) -> None:
    ctx = make_context(state.settings, with_journal=False)
    try:
        with MediaLibrary(library_path(state.settings)) as lib:
            lib.sync(roots, ctx)
    finally:
        ctx.close()


def test_online_tiles_are_off_by_default() -> None:
    settings = Settings()
    assert settings.map_online_tiles is False
    assert settings.map_tile_url == DEFAULT_TILE_URL
    assert "openstreetmap.org" in DEFAULT_TILE_URL


def _page(qtbot: QtBot, state: AppState) -> MapPage:
    page = MapPage(state)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.grid.load_thumbnail = lambda _p: None
    return page


def _two_places(state: AppState, tmp_path: Path) -> Path:
    root = tmp_path / "pics"
    gps_jpeg(root / "paris.jpg", 48.0, 2.0, "2023:05:01 10:00:00")
    gps_jpeg(root / "rome.jpg", 41.0, 12.0, "2024:07:01 10:00:00")
    gps_jpeg(root / "nowhere.jpg", None, None)
    state.update_settings(library_folders=[str(root)])
    fill_library(state, [root])
    return root


def test_map_shows_only_photos_with_a_place(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    _two_places(gui_state, tmp_path)
    page = _page(qtbot, gui_state)
    page.refresh()
    assert sorted(p.name for p in page.located_paths()) == ["paris.jpg", "rome.jpg"]
    assert page.status_label.text().startswith("2 of 3 photos and videos have a place")


def test_clicking_a_circle_fills_the_side_panel(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    _two_places(gui_state, tmp_path)
    page = _page(qtbot, gui_state)
    items = page.located_items()
    page.map.clusterClicked.emit(items)
    assert sorted(i.path.name for i in page.grid.items()) == ["paris.jpg", "rome.jpg"]
    text = page.place_label.text()
    assert "2 photos and videos here" in text and "May 2023 to July 2024" in text
    # the panel sits beside the map, not below it
    side = page.grid.parentWidget()
    assert side is not None and page.splitter.indexOf(side) == 1


def test_hover_tooltip_has_count_and_date_span(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    _two_places(gui_state, tmp_path)
    page = _page(qtbot, gui_state)
    page.show()
    qtbot.waitExposed(page)
    page.fit_all()
    paris = next(i for i in page.located_items() if i.path.name == "paris.jpg")
    assert paris.lat is not None and paris.lon is not None
    point = page.map.lonlat_to_screen(paris.lon, paris.lat)
    tip = page.map.tooltip_at(point)
    assert tip is not None and "1 photo or video" in tip and "May 2023" in tip
    assert page.map.tooltip_at(QPointF(1, 1)) is None


def test_fit_all_shows_every_place_and_double_click_zooms(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    _two_places(gui_state, tmp_path)
    page = _page(qtbot, gui_state)
    page.show()
    qtbot.waitExposed(page)
    page.map.set_view(0.0, 0.0, 1)
    page.fit_button.click()
    for item in page.located_items():
        assert item.lat is not None and item.lon is not None
        p = page.map.lonlat_to_screen(item.lon, item.lat)
        assert 0 <= p.x() <= page.map.width() and 0 <= p.y() <= page.map.height()
    before = page.map.zoom
    from PySide6.QtCore import Qt

    qtbot.mouseDClick(page.map, Qt.MouseButton.LeftButton, pos=page.map.rect().center())
    assert page.map.zoom == before + 1 or page.map.zoom == 18


def test_focus_item_centres_highlights_and_lists_the_place(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = _two_places(gui_state, tmp_path)
    page = _page(qtbot, gui_state)
    page.show()
    qtbot.waitExposed(page)
    assert page.focus_item(root / "rome.jpg")
    assert page.map.highlight == (41.0, 12.0)
    assert page.map.zoom >= 12
    assert [i.path.name for i in page.grid.items()] == ["rome.jpg"]
    assert page.grid.selected_rows() == [0]
    centre = page.map.lonlat_to_screen(12.0, 41.0)
    assert abs(centre.x() - page.map.width() / 2) < 2
    assert not page.focus_item(root / "nowhere.jpg")  # it has no place


def test_focus_item_clears_a_filter_that_hides_it(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = _two_places(gui_state, tmp_path)
    page = _page(qtbot, gui_state)
    page.model.set_filter(MediaFilter(text="paris"))
    assert [p.name for p in page.located_paths()] == ["paris.jpg"]
    assert page.focus_item(root / "rome.jpg")
    assert page.model.filter == MediaFilter()


def test_the_filters_apply_on_the_map(qtbot: QtBot, gui_state: AppState, tmp_path: Path) -> None:
    _two_places(gui_state, tmp_path)
    page = _page(qtbot, gui_state)
    page.filter_bar.to_box.setCurrentIndex(page.filter_bar.to_box.findData("2023-05"))
    assert [p.name for p in page.located_paths()] == ["paris.jpg"]


def test_tiles_follow_the_setting(qtbot: QtBot, gui_state: AppState) -> None:
    page = _page(qtbot, gui_state)
    assert not page.map.tiles_enabled
    gui_state.update_settings(map_online_tiles=True)
    assert page.map.tiles_enabled
    assert "openstreetmap" in page.attribution_text().lower()
    gui_state.update_settings(map_online_tiles=False)
    assert not page.map.tiles_enabled


def test_the_banner_explains_privacy_and_asks_before_turning_the_online_map_on(
    qtbot: QtBot, gui_state: AppState
) -> None:
    page = _page(qtbot, gui_state)
    assert "OpenStreetMap" in page.banner_label.text() and "off" in page.banner_label.text()
    asked: list[str] = []
    page.confirm = lambda title, _t: asked.append(title) is None and False
    assert not page.toggle_online_map()
    assert asked and not gui_state.settings.map_online_tiles

    page.confirm = lambda _t, _x: True
    assert page.toggle_online_map()
    assert gui_state.settings.map_online_tiles and page.map.tiles_enabled
    assert page.online_button.text() == "Turn off the detailed map"
    page.toggle_online_map()
    assert not gui_state.settings.map_online_tiles


def test_sidebar_has_a_photos_and_videos_section(window: MainWindow) -> None:
    labels = []
    for i in range(window.sidebar.topLevelItemCount()):
        header = window.sidebar.topLevelItem(i)
        assert header is not None
        children = [header.child(j) for j in range(header.childCount())]
        labels.append((header.text(0), [c.text(0) for c in children if c is not None]))
    assert ("Photos & videos", ["Timeline", "Map"]) in labels
    assert labels[-1][0] == "App"
    assert isinstance(window.show_page("__timeline__"), TimelinePage)
    assert isinstance(window.show_page("__map__"), MapPage)


def test_focus_item_before_the_page_was_shown_lists_the_place_once_it_is_shown(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = _two_places(gui_state, tmp_path)
    page = _page(qtbot, gui_state)
    assert page.focus_item(root / "rome.jpg")  # never shown: the map has no real size yet
    assert page.map.highlight == (41.0, 12.0)
    page.show()
    qtbot.waitExposed(page)
    qtbot.waitUntil(lambda: [i.path.name for i in page.grid.items()] == ["rome.jpg"], timeout=5000)
    assert page.grid.selected_rows() == [0]
    centre = page.map.lonlat_to_screen(12.0, 41.0)
    assert abs(centre.x() - page.map.width() / 2) < 2


def test_focus_item_says_when_it_cleared_a_filter(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = _two_places(gui_state, tmp_path)
    page = _page(qtbot, gui_state)
    page.model.set_filter(MediaFilter(text="paris"))
    assert page.focus_item(root / "rome.jpg")
    assert "filter was cleared" in page.status_label.text()


def test_tiles_use_a_folder_per_source_and_the_old_layout_is_removed(
    qtbot: QtBot, gui_state: AppState
) -> None:
    from various_tools.gui.map_page import TILE_FOLDER
    from various_tools.gui.widgets.world_map import tile_source_folder

    root = gui_state.settings.resolved_cache_folder() / TILE_FOLDER
    (root / "4" / "2").mkdir(parents=True)
    (root / "4" / "2" / "1.png").write_bytes(b"old")
    page = _page(qtbot, gui_state)

    gui_state.update_settings(map_online_tiles=True, map_tile_cache_mb=50)

    assert page.map.tiles_enabled
    qtbot.waitUntil(lambda: not (root / "4").exists())  # removed in the background
    assert page.map._cache_dir == tile_source_folder(root, gui_state.settings.map_tile_url)  # pyright: ignore[reportPrivateUsage]
    assert page.map._cache_limit_bytes == 50 * 1024 * 1024  # pyright: ignore[reportPrivateUsage]
