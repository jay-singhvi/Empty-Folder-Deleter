"""The Map page: photos and videos with a GPS place, on a world map.

It reads the same media library and the same filter bar as the Timeline (``library_model``), so it
never opens a file itself. Clicking a circle on the map lists its photos and videos in the side
panel; hovering shows how many there are and from when to when. "Fit all" shows every place;
double-click zooms in. "Show on map" in the Timeline calls :meth:`MapPage.focus_item`.

The map is the built-in world outline. With ``Settings.map_online_tiles`` on, it also shows
OpenStreetMap pictures, cached in ``<cache folder>/map_tiles``; that sends the viewed area to
OpenStreetMap, so it is off by default and the banner explains why before it is turned on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from various_tools import __version__
from various_tools.core.media.library import MediaFilter, MediaItem
from various_tools.core.settings import Settings
from various_tools.gui.change_date_dialog import ChangeDateDialog
from various_tools.gui.library_model import LibraryModel, library_model
from various_tools.gui.state import AppState
from various_tools.gui.widgets.filter_bar import FilterBar
from various_tools.gui.widgets.media_grid import MediaGrid
from various_tools.gui.widgets.world_map import WorldMap

log = logging.getLogger(__name__)

ATTRIBUTION = "Map pictures © OpenStreetMap contributors"
TILE_FOLDER = "map_tiles"
FOCUS_ZOOM = 12
MIN_READY_SIZE = 50  # a map narrower than this has not been laid out yet
BANNER_OFF = (
    "This map shows only the outline of the land. A detailed street map is available, but it "
    "is loaded from OpenStreetMap, so OpenStreetMap would see which areas you look at (roughly "
    "where your photos were taken). It is off unless you turn it on."
)
BANNER_ON = (
    "The detailed map is on: the area you look at is sent to OpenStreetMap to get its pictures."
)
NO_PLACE_TEXT = "Click a circle on the map to see its photos and videos."


def user_agent() -> str:
    """How the app names itself to the tile server (OpenStreetMap asks for this)."""
    return f"VariousTools/{__version__} (desktop photo map)"


def _things(count: int) -> str:
    return "1 photo or video" if count == 1 else f"{count:,} photos and videos"


def span_text(items: Sequence[MediaItem]) -> str:
    """For example "May 2019 to August 2021", or "May 2019", or "" when none has a date."""
    dates = [i.taken for i in items if i.taken is not None]
    if not dates:
        return ""
    first, last = min(dates), max(dates)
    a, b = f"{first:%B %Y}", f"{last:%B %Y}"
    return a if a == b else f"{a} to {b}"


class MapPage(QWidget):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.model: LibraryModel = library_model(state)
        self._located: list[MediaItem] = []
        self.confirm: Callable[[str, str], bool] = self._default_confirm
        self.change_dialog: ChangeDateDialog | None = None

        title = QLabel("<h2>Map</h2>")
        description = QLabel(
            "Where your photos and videos were taken. Click a circle to see them, scroll or "
            "double-click to zoom, drag to move. It uses the folders and filters of the "
            "Timeline: press Update there to add new photos."
        )
        description.setWordWrap(True)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.attribution = QLabel("")
        self.attribution.setVisible(False)

        self.banner = QFrame()
        self.banner.setFrameShape(QFrame.Shape.StyledPanel)
        self.banner_label = QLabel(BANNER_OFF)
        self.banner_label.setWordWrap(True)
        self.online_button = QPushButton("Turn on the detailed map…")
        self.online_button.clicked.connect(lambda _c=False: self.toggle_online_map())
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.addWidget(self.banner_label, 1)
        banner_layout.addWidget(self.online_button)

        self.filter_bar = FilterBar(self.model)
        self.map = WorldMap(self)
        self.map.describe = self._describe
        self.fit_button = QPushButton("Fit all")
        self.fit_button.setToolTip("Zoom out or in so that every place is on screen")
        self.fit_button.clicked.connect(lambda _c=False: self.fit_all())
        self.place_label = QLabel(NO_PLACE_TEXT)
        self.place_label.setWordWrap(True)
        self.grid = MediaGrid(self, captions=True, thumb_size=96)
        self.grid.changeDateRequested.connect(self.change_dates)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        top_row = QHBoxLayout()
        top_row.addWidget(self.fit_button)
        top_row.addStretch(1)
        left_layout.addLayout(top_row)
        left_layout.addWidget(self.map, 1)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(6, 0, 0, 0)
        side_layout.addWidget(self.place_label)
        side_layout.addWidget(self.grid, 1)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(side)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(self.banner)
        layout.addWidget(self.filter_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.attribution)

        self.map.clusterClicked.connect(self._cluster_clicked)
        self.model.changed.connect(self._rebuild)
        self._pending_focus: Path | None = None
        state.on_settings_changed(self._settings_changed)
        self._apply_tiles(state.settings)
        self._rebuild()

    # ----- data ----------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Read the library again for the chosen folders (fast: no file is opened)."""
        self.model.reload()

    def _rebuild(self) -> None:
        self._located = self.model.located()
        self.map.set_points([(i.lat, i.lon, i) for i in self._located])  # type: ignore[misc]
        self.map.set_highlight(None, None)
        self.grid.set_items([])
        self.place_label.setText(NO_PLACE_TEXT)
        self.status_label.setText(self._summary(self.model.roots()))

    def _summary(self, roots: Sequence[Path]) -> str:
        if not roots:
            return "Choose your photo folders on the Timeline page first (Library folders…)."
        shown = self.model.shown
        if not self.model.all_items:
            return "No photos or videos yet. Press Update on the Timeline page."
        text = f"{len(self._located):,} of {_things(len(shown))} have a place."
        if len(self._located) < len(shown):
            text += " The others have no GPS place in the file, so they are not on the map."
        if len(shown) != len(self.model.all_items):
            text += f" (filtered from {len(self.model.all_items):,})"
        return text

    def located_items(self) -> list[MediaItem]:
        return list(self._located)

    def located_paths(self) -> list[Path]:
        return [i.path for i in self._located]

    def _describe(self, members: list[object]) -> str:
        items = [m for m in members if isinstance(m, MediaItem)]
        span = span_text(items)
        return _things(len(items)) + (f"\n{span}" if span else "")

    def _cluster_clicked(self, payloads: list[Any]) -> None:
        items = [p for p in payloads if isinstance(p, MediaItem)]
        items.sort(key=lambda i: (i.taken is None, i.taken or 0, str(i.path).casefold()))
        self.grid.set_items(items)
        span = span_text(items)
        self.place_label.setText(
            f"{_things(len(items))} here"
            + (f" ({span})" if span else "")
            + ". Double-click one to open it."
        )

    def fit_all(self) -> None:
        self.map.fit_all()

    def focus_item(self, path: Path) -> bool:
        """Centre the map on this item, ring it and list the photos around it. False if unknown.

        Before the map has its real size (the page was never shown) the circles a place is
        drawn in do not exist yet, so the list of photos around the item waits until the page
        is shown; the map is centred and the item ringed at once.
        """
        self.model.reload_if_stale()
        item = self.model.item_for(path)
        if item is None or item.lat is None or item.lon is None:
            return False
        cleared = False
        if item not in self._located:
            self.model.set_filter(MediaFilter())  # a filter hides it: show everything again
            cleared = True
            item = self.model.item_for(path)
            if item is None or item.lat is None or item.lon is None:
                return False
        self.map.set_view(item.lat, item.lon, max(self.map.zoom, FOCUS_ZOOM))
        self.map.set_highlight(item.lat, item.lon)
        if cleared:
            self.status_label.setText(
                f"{self._summary(self.model.roots())} The filter was cleared to show this item."
            )
        if self._map_ready():
            self._list_around(item, path)
        else:
            self._pending_focus = path
        return True

    def _map_ready(self) -> bool:
        """The map is on screen with a real size, so its circles can be looked up."""
        return self.isVisible() and self.map.width() > MIN_READY_SIZE

    def _list_around(self, item: MediaItem, path: Path) -> None:
        assert item.lat is not None and item.lon is not None
        self.map.set_view(item.lat, item.lon, self.map.zoom)  # centre for the real size
        self.map.set_highlight(item.lat, item.lon)
        members = [m for m in self.map.members_near(item.lat, item.lon) if isinstance(m, MediaItem)]
        self._cluster_clicked(members or [item])
        row = self.grid.row_of_path(path)
        if row >= 0:
            self.grid.set_current(row)

    def _apply_pending_focus(self) -> None:
        path, self._pending_focus = self._pending_focus, None
        if path is None or not shiboken6.isValid(self) or not self._map_ready():
            if path is not None and shiboken6.isValid(self):
                self._pending_focus = path  # not sized yet: the next show tries again
            return
        item = self.model.item_for(path)
        if item is not None and item.lat is not None and item.lon is not None:
            self._list_around(item, path)

    def change_dates(self, paths: object) -> ChangeDateDialog | None:
        if not isinstance(paths, list) or not paths:
            return None
        files = [Path(str(p)) for p in paths]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        dialog = ChangeDateDialog(self.state, files, self)
        dialog.datesChanged.connect(lambda _p: self.refresh())
        self.change_dialog = dialog
        dialog.open()
        return dialog

    # ----- online tiles ---------------------------------------------------------------------------

    def toggle_online_map(self) -> bool:
        """Turn the detailed map on (after explaining why it sends data) or off."""
        settings = self.state.settings
        if settings.map_online_tiles:
            self.state.update_settings(map_online_tiles=False)
            return True
        if not self.confirm("Turn on the detailed map?", BANNER_OFF + "\n\nTurn it on?"):
            return False
        self.state.update_settings(map_online_tiles=True)
        return True

    def _settings_changed(self, settings: Settings) -> None:
        if shiboken6.isValid(self):
            self._apply_tiles(settings)

    def _apply_tiles(self, settings: Settings) -> None:
        if settings.map_online_tiles:
            cache = settings.resolved_cache_folder() / TILE_FOLDER
            self.map.set_tile_source(settings.map_tile_url, cache, user_agent())
        else:
            self.map.set_tile_source(None, None, "")
        self.attribution.setText(ATTRIBUTION if settings.map_online_tiles else "")
        self.attribution.setVisible(settings.map_online_tiles)
        self.banner_label.setText(BANNER_ON if settings.map_online_tiles else BANNER_OFF)
        self.online_button.setText(
            "Turn off the detailed map"
            if settings.map_online_tiles
            else "Turn on the detailed map…"
        )

    def attribution_text(self) -> str:
        return self.attribution.text()

    def _default_confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ----- page life ------------------------------------------------------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        self.model.reload_if_stale()  # the Timeline may have updated the library meanwhile
        if self._pending_focus is not None:
            QTimer.singleShot(0, self._apply_pending_focus)  # after the layout has sized the map

    def shutdown(self) -> None:
        self.grid.shutdown()
