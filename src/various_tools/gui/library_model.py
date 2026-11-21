"""The media library as the Timeline and the Map share it: one list, one filter.

``library_model(state)`` returns the one :class:`LibraryModel` of an ``AppState`` (it is created
on first use), so the filter bar of the Timeline and of the Map edit the same filter and both
pages show the same items. Reading the library opens no photo (a cache is read, nothing else),
so it is safe for cloud-only files (AGENTS.md rule 3).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from various_tools.core.media.library import (
    MediaFilter,
    MediaItem,
    MediaLibrary,
    filter_items,
    library_path,
)
from various_tools.gui.state import AppState

log = logging.getLogger(__name__)

_ATTRIBUTE = "_library_model"


class LibraryModel(QObject):
    itemsChanged = Signal()  # noqa: N815 (Qt name) the library was read again
    filterChanged = Signal()  # noqa: N815 (Qt name) the filter was edited
    changed = Signal()  # either of the above: what is shown changed

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._items: list[MediaItem] = []
        self._by_path: dict[str, MediaItem] | None = None
        self._shown: list[MediaItem] = []
        self._filter = MediaFilter()
        self._stamp: tuple[object, ...] | None = None
        self.reload()

    # ----- reading ---------------------------------------------------------------------------

    def roots(self) -> list[Path]:
        return [Path(p) for p in self.state.settings.library_folders]

    def _stamp_now(self) -> tuple[object, ...]:
        db = library_path(self.state.settings)
        stats: list[object] = [tuple(str(r) for r in self.roots())]
        for name in (db, Path(f"{db}-wal")):
            try:
                st = os.stat(name)
                stats.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stats.append(None)
        return tuple(stats)

    def reload(self) -> None:
        """Read the library again for the chosen folders and tell both views."""
        roots = self.roots()
        path = library_path(self.state.settings)
        items: list[MediaItem] = []
        if roots and path.is_file():
            try:
                with MediaLibrary(path) as lib:
                    items = lib.items(roots)
            except Exception as exc:  # noqa: BLE001 - a broken cache shows as empty
                log.warning("Could not read the media library %s: %s", path, exc)
        self._stamp = self._stamp_now()
        self._items = items
        self._by_path = None
        self._shown = filter_items(items, self._filter)
        self.itemsChanged.emit()
        self.changed.emit()

    def reload_if_stale(self) -> bool:
        """Reload only when the library file or the folders changed. True when it did."""
        if self._stamp_now() == self._stamp:
            return False
        self.reload()
        return True

    # ----- what is there ---------------------------------------------------------------------

    @property
    def all_items(self) -> list[MediaItem]:
        return self._items

    @property
    def shown(self) -> list[MediaItem]:
        """The items that pass the filter, in library order (oldest first)."""
        return self._shown

    @property
    def filter(self) -> MediaFilter:
        return self._filter

    def set_filter(self, flt: MediaFilter) -> None:
        if flt == self._filter:
            return
        self._filter = flt
        self._shown = filter_items(self._items, flt)
        self.filterChanged.emit()
        self.changed.emit()

    def months(self) -> list[tuple[int, int]]:
        """Every (year, month) that has a dated item, newest first."""
        found = {(i.taken.year, i.taken.month) for i in self._items if i.taken is not None}
        return sorted(found, reverse=True)

    def located(self) -> list[MediaItem]:
        return [i for i in self._shown if i.lat is not None and i.lon is not None]

    def item_for(self, path: Path) -> MediaItem | None:
        index = self._by_path
        if index is None:  # built on first use, dropped when the items are read again
            index = {}
            for item in self._items:
                index.setdefault(os.path.normcase(os.path.normpath(str(item.path))), item)
            self._by_path = index
        return index.get(os.path.normcase(os.path.normpath(str(path))))


def library_model(state: AppState) -> LibraryModel:
    """The shared model of ``state`` (created on first use)."""
    model = vars(state).get(_ATTRIBUTE)
    if not isinstance(model, LibraryModel):
        model = LibraryModel(state)
        vars(state)[_ATTRIBUTE] = model
    return model
