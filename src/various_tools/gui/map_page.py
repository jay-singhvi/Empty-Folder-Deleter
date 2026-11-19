"""The Map page: photos and videos with a GPS place, on a world map.

It reads the same media library as the Timeline (the folders in ``Settings.library_folders``;
the Timeline's *Update* button reads new files), so it never opens a file itself. Clicking a
circle on the map shows the photos and videos in it below, where they open in their default app.

The map is the built-in world outline. With ``Settings.map_online_tiles`` on, it also shows
OpenStreetMap pictures, cached in ``<cache folder>/map_tiles``; that sends the viewed area to
OpenStreetMap, so it is off by default.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSplitter, QVBoxLayout, QWidget

from various_tools import __version__
from various_tools.core.media.library import MediaItem, MediaLibrary, library_path
from various_tools.core.settings import Settings
from various_tools.gui.state import AppState
from various_tools.gui.widgets.media_grid import MediaGrid
from various_tools.gui.widgets.world_map import WorldMap

log = logging.getLogger(__name__)

ATTRIBUTION = "Map pictures © OpenStreetMap contributors"
TILE_FOLDER = "map_tiles"


def user_agent() -> str:
    """How the app names itself to the tile server (OpenStreetMap asks for this)."""
    return f"VariousTools/{__version__} (desktop photo map)"


def _things(count: int) -> str:
    return "1 photo or video" if count == 1 else f"{count:,} photos and videos"


class MapPage(QWidget):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._items: list[MediaItem] = []
        self._located: list[MediaItem] = []

        title = QLabel("<h2>Map</h2>")
        description = QLabel(
            "Where your photos and videos were taken. Click a circle to see them, scroll to "
            "zoom, drag to move. It uses the folders from the Timeline: press Update there to "
            "add new photos. Nothing is changed or moved."
        )
        description.setWordWrap(True)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.attribution = QLabel("")
        self.attribution.setVisible(False)
        self.map = WorldMap(self)
        self.place_label = QLabel("Click a circle on the map to see its photos and videos.")
        self.grid = MediaGrid(self)

        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addWidget(self.place_label)
        lower_layout.addWidget(self.grid, 1)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.map)
        splitter.addWidget(lower)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(self.status_label)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.attribution)

        self.map.clusterClicked.connect(self._cluster_clicked)
        state.on_settings_changed(self._settings_changed)
        self._apply_tiles(state.settings)

    # ----- data ----------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Read the library again for the chosen folders (fast: no file is opened)."""
        roots = [Path(p) for p in self.state.settings.library_folders]
        path = library_path(self.state.settings)
        items: list[MediaItem] = []
        if roots and path.is_file():
            try:
                with MediaLibrary(path) as lib:
                    items = lib.items(roots)
            except Exception as exc:  # noqa: BLE001 - a broken cache must not break the page
                log.warning("Could not read the media library %s: %s", path, exc)
        self._items = items
        self._located = [i for i in items if i.lat is not None and i.lon is not None]
        self.map.set_points([(i.lat, i.lon, i) for i in self._located])  # type: ignore[misc]
        self.grid.set_items([])
        self.place_label.setText("Click a circle on the map to see its photos and videos.")
        self.status_label.setText(self._summary(roots))

    def _summary(self, roots: Sequence[Path]) -> str:
        if not roots:
            return "Choose your photo folders on the Timeline page first."
        if not self._items:
            return "No photos or videos yet. Press Update on the Timeline page."
        text = f"{len(self._located):,} of {_things(len(self._items))} have a place."
        if len(self._located) < len(self._items):
            text += " The others have no GPS place in the file, so they are not on the map."
        return text

    def located_items(self) -> list[MediaItem]:
        return list(self._located)

    def located_paths(self) -> list[Path]:
        return [i.path for i in self._located]

    def _cluster_clicked(self, payloads: list[Any]) -> None:
        items = [p for p in payloads if isinstance(p, MediaItem)]
        items.sort(key=lambda i: (i.taken is None, i.taken or 0, str(i.path).casefold()))
        self.grid.set_items(items)
        self.place_label.setText(f"{_things(len(items))} here. Double-click one to open it.")

    # ----- online tiles ---------------------------------------------------------------------------

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

    def attribution_text(self) -> str:
        return self.attribution.text()

    # ----- page life ------------------------------------------------------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        self.refresh()  # the Timeline may have updated the library meanwhile

    def shutdown(self) -> None:
        self.grid.shutdown()
