"""The media library as the Timeline and the Map share it: one list, one filter.

``library_model(state)`` returns the one :class:`LibraryModel` of an ``AppState`` (it is created
on first use), so the filter bar of the Timeline and of the Map edit the same filter and both
pages show the same items. Reading the library opens no photo (a cache is read, nothing else),
so it is safe for cloud-only files (AGENTS.md rule 3).

The library is read in a worker thread (``reload``), so a large library never freezes the window:
until it is read, ``loading`` is true and the pages show :data:`LOADING_TEXT`. Filtering uses a
:class:`~various_tools.core.media.library.FilterIndex` made in that worker, and a filter that only
asks for less (more typing in the search box) starts from the previous answer.

The Timeline's month and day headers (``Groups``), the list of places for the Place filter and the
photos that have a duplicate (from the last Duplicate Finder result, see
:mod:`~various_tools.core.media.library_duplicates`) are also made in that worker, so the window
thread only hands them over: rebuilding a 200,000 item Timeline stays quick.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Signal

from various_tools.core.media.library import (
    FilterIndex,
    MediaFilter,
    MediaItem,
    MediaLibrary,
    library_path,
)
from various_tools.core.media.library_duplicates import (
    EMPTY,
    DuplicateIndex,
    LibraryDuplicates,
    duplicates_path,
    path_key,
)
from various_tools.core.plan import Plan
from various_tools.gui.state import AppState
from various_tools.gui.widgets.media_grid import Groups, days_of, groups_from_days
from various_tools.gui.workers import Worker

log = logging.getLogger(__name__)

_ATTRIBUTE = "_library_model"
LOADING_TEXT = "Loading your photos…"


def timeline_order(items: Sequence[MediaItem]) -> list[MediaItem]:
    """Newest month and day first; inside a day oldest first; items without a date last."""

    def key(item: MediaItem) -> tuple[int, int, datetime, str]:
        taken = item.taken
        if taken is None:
            return (1, 0, datetime.min, str(item.path).casefold())
        # Plain integers and datetimes compare much faster than timestamps (200,000 items).
        return (0, -taken.toordinal(), taken, str(item.path).casefold())

    return sorted(items, key=key)


@dataclass
class _Loaded:
    """What the worker read: the items, their filter index and the months they cover."""

    generation: int
    stamp: tuple[object, ...]
    items: list[MediaItem]
    index: FilterIndex
    months: list[tuple[int, int]]
    order: list[int]  # indices into ``items`` in timeline order, made once here
    order_days: Any = None  # numpy array: the day ordinal of each item, in timeline order
    ranked: list[MediaItem] | None = None  # the items in timeline order
    groups: Groups | None = None  # headers of ``ranked``
    places: dict[str, list[str]] | None = None  # country -> its regions, for the Place filter
    duplicates: DuplicateIndex = EMPTY


def prepare(
    generation: int,
    stamp: tuple[object, ...],
    items: list[MediaItem],
    duplicates: LibraryDuplicates | None = None,
) -> _Loaded:
    """Everything the window needs from a list of items, made in the worker thread."""
    found = {(i.taken.year, i.taken.month) for i in items if i.taken is not None}
    ranked = timeline_order(items)
    position = {id(item): n for n, item in enumerate(items)}
    order = [position[id(item)] for item in ranked]
    order_days = days_of(ranked)
    countries: dict[str, set[str]] = {}
    for item in items:
        if item.place is not None:
            regions = countries.setdefault(item.place.country, set())
            if item.place.region:
                regions.add(item.place.region)
    places = {c: sorted(r, key=str.casefold) for c, r in countries.items()}
    index = FilterIndex(items)
    dup_index = EMPTY
    if duplicates is not None and duplicates.groups:
        by_path = {path_key(i.path): i for i in items}
        dup_index = duplicates.index_for(by_path.get)
        keys = dup_index.keys()
        index.duplicates = frozenset(n for n, i in enumerate(items) if path_key(i.path) in keys)
    return _Loaded(
        generation, stamp, items, index, sorted(found, reverse=True), order,
        order_days, ranked, groups_from_days(order_days), places, dup_index,
    )  # fmt: skip


def _read_library(
    generation: int,
    roots: list[Path],
    path: Path,
    stamp: tuple[object, ...],
    duplicates_file: Path | None = None,
) -> _Loaded:
    """Read the library file (in the worker thread: SQLite connections stay in one thread)."""
    items: list[MediaItem] = []
    if roots and path.is_file():
        try:
            with MediaLibrary(path) as lib:
                items = lib.items(roots)
        except Exception as exc:  # noqa: BLE001 - a broken cache shows as empty
            log.warning("Could not read the media library %s: %s", path, exc)
    duplicates = LibraryDuplicates(duplicates_file) if duplicates_file is not None else None
    return prepare(generation, stamp, items, duplicates)


class LibraryModel(QObject):
    itemsChanged = Signal()  # noqa: N815 (Qt name) the library was read again
    filterChanged = Signal()  # noqa: N815 (Qt name) the filter was edited
    changed = Signal()  # either of the above: what is shown changed
    loadingChanged = Signal(bool)  # noqa: N815 (Qt name) a read started (True) or ended (False)

    # Tests set this so that ``reload`` reads at once instead of in a worker.
    synchronous = False

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._items: list[MediaItem] = []
        self._index = FilterIndex(self._items)
        self._months: list[tuple[int, int]] = []
        self._by_path: dict[str, MediaItem] | None = None
        self._shown_idx: Sequence[int] = []
        self._shown_cache: list[MediaItem] | None = None
        self._order: list[int] = []
        self._timeline_cache: list[MediaItem] | None = None
        self._groups_cache: Groups | None = None
        self._order_days: Any = np.empty(0, dtype=np.int64)
        self._ranked: list[MediaItem] | None = None
        self._ranked_groups: Groups | None = None
        self._places: dict[str, list[str]] = {}
        self._duplicates = EMPTY
        self._located_cache: list[MediaItem] | None = None
        self._filter = MediaFilter()
        self._stamp: tuple[object, ...] | None = None
        self._generation = 0
        self._loading = False
        self.reload()

    # ----- reading ---------------------------------------------------------------------------

    def roots(self) -> list[Path]:
        return [Path(p) for p in self.state.settings.library_folders]

    def _stamp_now(self) -> tuple[object, ...]:
        db = library_path(self.state.settings)
        stats: list[object] = [tuple(str(r) for r in self.roots())]
        for name in (db, Path(f"{db}-wal"), self.duplicates_file()):
            try:
                st = os.stat(name)
                stats.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stats.append(None)
        return tuple(stats)

    def duplicates_file(self) -> Path:
        return duplicates_path(self.state.settings.resolved_cache_folder())

    @property
    def loading(self) -> bool:
        """True while the library is being read in the background."""
        return self._loading

    def reload(self) -> None:
        """Read the library again for the chosen folders (in a worker), then tell the views."""
        self._generation += 1
        generation = self._generation
        roots = self.roots()
        path = library_path(self.state.settings)
        stamp = self._stamp_now()
        dups = self.duplicates_file()
        if self.synchronous:
            self._apply(_read_library(generation, roots, path, stamp, dups))
            return
        self._set_loading(True)
        worker = Worker(lambda _w: _read_library(generation, roots, path, stamp, dups))
        worker.succeeded.connect(self._loaded)
        worker.failed.connect(lambda exc: self._failed(generation, exc))
        worker.start()

    def _loaded(self, result: object) -> None:
        if isinstance(result, _Loaded) and result.generation == self._generation:
            self._apply(result)  # a result of an older read is dropped: a newer one is coming

    def _failed(self, generation: int, error: object) -> None:
        """The read itself failed: keep what is shown, stop saying "Loading", try again later."""
        log.warning("Could not read the media library: %s", error)
        if generation != self._generation:
            return  # an older read; the newer one is still coming
        self._stamp = None  # so that reload_if_stale tries again
        self._set_loading(False)

    def _set_loading(self, loading: bool) -> None:
        if loading != self._loading:
            self._loading = loading
            self.loadingChanged.emit(loading)

    def _apply(self, result: _Loaded) -> None:
        self._stamp = result.stamp
        self._items = result.items
        self._index = result.index
        self._months = result.months
        self._order = result.order
        self._order_days = result.order_days
        self._ranked = result.ranked
        self._ranked_groups = result.groups
        self._places = result.places or {}
        self._duplicates = result.duplicates
        self._groups_cache = None
        self._by_path = None
        self._shown_idx = self._select(self._filter, None)
        self._shown_cache = self._located_cache = self._timeline_cache = self._groups_cache = None
        self._set_loading(False)
        self.itemsChanged.emit()
        self.changed.emit()

    def reload_if_stale(self) -> bool:
        """Reload only when the library file or the folders changed. True when it did."""
        if self._loading or self._stamp_now() == self._stamp:
            return False
        self.reload()
        return True

    # ----- what is there ---------------------------------------------------------------------

    def _select(self, flt: MediaFilter, among: Sequence[int] | None) -> Sequence[int]:
        """Positions that pass ``flt``; with no filter, all of them without building a list."""
        if flt.is_empty and among is None:
            return range(len(self._items))
        return self._index.select(flt, among)

    @property
    def all_items(self) -> list[MediaItem]:
        return self._items

    @property
    def shown(self) -> list[MediaItem]:
        """The items that pass the filter, in library order (oldest first)."""
        if self._shown_cache is None:
            items = self._items
            if len(self._shown_idx) == len(items):  # nothing filtered out: the same order
                self._shown_cache = list(items)
            else:
                self._shown_cache = [items[i] for i in self._shown_idx]
        return self._shown_cache

    @property
    def timeline_shown(self) -> list[MediaItem]:
        """The items that pass the filter in timeline order (see :func:`timeline_order`). The
        order was made once when the library was read; a filter only picks from it."""
        if self._timeline_cache is None:
            self._make_timeline()
        assert self._timeline_cache is not None
        return self._timeline_cache

    @property
    def timeline_groups(self) -> Groups:
        """The month and day headers of :attr:`timeline_shown`."""
        if self._groups_cache is None:
            self._make_timeline()
        assert self._groups_cache is not None
        return self._groups_cache

    def _make_timeline(self) -> None:
        items = self._items
        ranked, ranked_groups = self._ranked, self._ranked_groups
        if len(self._shown_idx) == len(items) and ranked is not None and ranked_groups is not None:
            self._timeline_cache, self._groups_cache = ranked, ranked_groups  # nothing filtered
            return
        keep = np.zeros(len(items), dtype=bool)
        keep[self._shown_idx] = True
        order = np.asarray(self._order, dtype=np.int64)
        picked = keep[order] if len(order) else keep[:0]
        self._timeline_cache = [items[i] for i in order[picked].tolist()]
        self._groups_cache = groups_from_days(self._order_days[picked])

    @property
    def filter(self) -> MediaFilter:
        return self._filter

    def set_filter(self, flt: MediaFilter) -> None:
        if flt == self._filter:
            return
        among = self._shown_idx if FilterIndex.narrows(self._filter, flt) else None
        self._filter = flt
        self._shown_idx = self._select(flt, among)
        self._shown_cache = self._located_cache = self._timeline_cache = self._groups_cache = None
        self.filterChanged.emit()
        self.changed.emit()

    def places(self) -> dict[str, list[str]]:
        """Every country that has a located item, with its regions (for the Place filter)."""
        return dict(self._places)

    # ----- duplicates (from the last Duplicate Finder result) --------------------------------

    @property
    def duplicate_index(self) -> DuplicateIndex:
        return self._duplicates

    def has_duplicate(self, item: MediaItem) -> bool:
        return self._duplicates.has(item)

    def copies_of(self, item: MediaItem) -> list[MediaItem]:
        """The other library items that the last Duplicate Finder result calls copies of this."""
        return self._duplicates.copies_of(item)

    def update_duplicates(self, plan: Plan) -> int:
        """Remember the groups of a Duplicate Finder plan and read the library again.

        Call it when that tool's scan is done. Other tools' plans change nothing (returns 0).
        Returns the number of groups kept (some may not be in the library).
        """
        count = LibraryDuplicates(self.duplicates_file()).update_from_plan(plan)
        if plan.tool == "duplicates":
            self.reload()
        return count

    def months(self) -> list[tuple[int, int]]:
        """Every (year, month) that has a dated item, newest first."""
        return list(self._months)

    def located(self) -> list[MediaItem]:
        if self._located_cache is None:
            self._located_cache = [i for i in self.shown if i.lat is not None and i.lon is not None]
        return self._located_cache

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
