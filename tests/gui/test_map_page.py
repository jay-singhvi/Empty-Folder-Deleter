"""The Map page and the "Photos & videos" sidebar section."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from pytestqt.qtbot import QtBot

from various_tools.core.context import make_context
from various_tools.core.media.library import MediaLibrary, library_path
from various_tools.core.settings import DEFAULT_TILE_URL, Settings
from various_tools.gui.main_window import MainWindow
from various_tools.gui.map_page import MapPage
from various_tools.gui.state import AppState
from various_tools.gui.timeline_page import TimelinePage

EXIF_IFD, GPS_IFD = 0x8769, 0x8825


def gps_jpeg(path: Path, lat: float | None, lon: float | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = Image.Exif()
    data.get_ifd(EXIF_IFD)[0x9003] = "2024:05:01 10:00:00"
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
    page.grid.load_thumbnail = lambda _p: None
    return page


def test_map_shows_only_photos_with_a_place(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = tmp_path / "pics"
    gps_jpeg(root / "paris.jpg", 48.0, 2.0)
    gps_jpeg(root / "rome.jpg", 41.0, 12.0)
    gps_jpeg(root / "nowhere.jpg", None, None)
    gui_state.update_settings(library_folders=[str(root)])
    fill_library(gui_state, [root])
    page = _page(qtbot, gui_state)
    page.refresh()
    assert sorted(p.name for p in page.located_paths()) == ["paris.jpg", "rome.jpg"]
    assert page.status_label.text().startswith("2 of 3 photos and videos have a place")


def test_clicking_a_cluster_shows_its_photos(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = tmp_path / "pics"
    gps_jpeg(root / "a.jpg", 48.0, 2.0)
    gui_state.update_settings(library_folders=[str(root)])
    fill_library(gui_state, [root])
    page = _page(qtbot, gui_state)
    page.refresh()
    items = list(page.located_items())
    page.map.clusterClicked.emit(items)
    assert [i.path.name for i in page.grid.items()] == ["a.jpg"]
    assert "1 photo or video here" in page.place_label.text()


def test_tiles_follow_the_setting(qtbot: QtBot, gui_state: AppState) -> None:
    page = _page(qtbot, gui_state)
    assert not page.map.tiles_enabled
    gui_state.update_settings(map_online_tiles=True)
    assert page.map.tiles_enabled
    assert "openstreetmap" in page.attribution_text().lower()
    gui_state.update_settings(map_online_tiles=False)
    assert not page.map.tiles_enabled


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
