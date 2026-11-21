"""The library is read in a worker; pages say "Loading your photos…" meanwhile; filters are fast."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from tests.gui.test_map_page import _two_places  # noqa: PLC2701

from various_tools.core.media.library import MediaFilter, MediaLibrary, library_path
from various_tools.gui import library_model as lm
from various_tools.gui.library_model import LOADING_TEXT, LibraryModel, library_model
from various_tools.gui.map_page import MapPage
from various_tools.gui.state import AppState
from various_tools.gui.timeline_page import TimelinePage

pytestmark = pytest.mark.usefixtures("_library_async")


@pytest.fixture
def _library_async(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LibraryModel, "synchronous", False)


class Gate:
    """Holds the worker's read until the test lets it go, and records which thread read."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.release = threading.Event()
        self.threads: list[int] = []
        real = lm._read_library  # pyright: ignore[reportPrivateUsage]

        def slow(*args: object, **kwargs: object) -> object:
            self.threads.append(threading.get_ident())
            assert self.release.wait(20)
            return real(*args, **kwargs)  # pyright: ignore[reportArgumentType]

        monkeypatch.setattr(lm, "_read_library", slow)


def test_the_library_is_read_off_the_gui_thread(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_places(gui_state, tmp_path)
    gate = Gate(monkeypatch)
    model = LibraryModel(gui_state)
    assert model.loading
    assert model.all_items == []
    gate.release.set()
    qtbot.waitUntil(lambda: not model.loading, timeout=10000)
    assert len(model.all_items) == 3
    assert gate.threads and gate.threads[0] != threading.get_ident()


def test_both_pages_say_loading_until_the_read_ends(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_places(gui_state, tmp_path)
    gate = Gate(monkeypatch)
    timeline = TimelinePage(gui_state)
    qtbot.addWidget(timeline)
    mapp = MapPage(gui_state)
    qtbot.addWidget(mapp)
    assert timeline.status_label.text() == LOADING_TEXT
    assert mapp.status_label.text() == LOADING_TEXT
    gate.release.set()
    qtbot.waitUntil(lambda: not timeline.model.loading, timeout=10000)
    assert timeline.status_label.text().startswith("3 photos")
    assert mapp.status_label.text().startswith("2 of 3")


def test_only_the_newest_read_is_used(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _two_places(gui_state, tmp_path)
    gate = Gate(monkeypatch)
    model = LibraryModel(gui_state)  # read 1 (waits at the gate)
    gui_state.update_settings(library_folders=[str(root / "nothing")])
    model.reload()  # read 2 for a folder that has no rows
    seen: list[int] = []
    model.itemsChanged.connect(lambda: seen.append(len(model.all_items)))
    gate.release.set()
    qtbot.waitUntil(lambda: not model.loading, timeout=10000)
    qtbot.wait(100)
    assert seen == [0]
    assert model.all_items == []


def test_the_update_report_still_shows_after_the_library_is_read_again(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    root = _two_places(gui_state, tmp_path)
    page = TimelinePage(gui_state)
    qtbot.addWidget(page)
    qtbot.waitUntil(lambda: not page.model.loading, timeout=10000)
    page.grid.load_thumbnail = lambda _p: None  # type: ignore[method-assign]
    worker = page.update_library()
    assert worker is not None
    qtbot.waitUntil(lambda: page.worker is None and not page.model.loading, timeout=20000)
    assert "Updated:" in page.status_label.text()
    assert root.is_dir()


def test_filtering_two_hundred_thousand_items_does_not_freeze_the_window(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path
) -> None:
    from datetime import datetime, timedelta

    db = library_path(gui_state.settings)
    root = tmp_path / "big"
    gui_state.update_settings(library_folders=[str(root)])
    start = datetime(2015, 1, 1)
    base = str(root)
    rows = []
    for i in range(200_000):
        path = rf"{base}\{2015 + i % 9}\Album {i % 400}\IMG_{i:06d}.jpg"
        placed = i % 3 == 0
        rows.append(
            (
                os.path.normcase(path), path, "video" if i % 9 == 0 else "photo", 1, 1.0,
                (start + timedelta(minutes=i * 7)).isoformat(), "camera",
                10.0 if placed else None, 20.0 if placed else None, None, None, 0,
            )
        )  # fmt: skip
    with MediaLibrary(db) as lib:
        lib._conn.executemany(  # pyright: ignore[reportPrivateUsage]
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        lib._conn.commit()  # pyright: ignore[reportPrivateUsage]

    model = library_model(gui_state)
    qtbot.waitUntil(lambda: not model.loading, timeout=60000)
    assert len(model.all_items) == 200_000

    worst = 0.0
    typed = ""
    for letter in "album 12":
        typed += letter
        t0 = time.perf_counter()
        model.set_filter(MediaFilter(text=typed))
        _ = model.shown
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.25, f"a keystroke took {worst * 1000:.0f} ms"
    assert 0 < len(model.shown) < 200_000


def test_a_failed_read_keeps_the_items_and_lets_a_later_reload_work(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_places(gui_state, tmp_path)
    model = LibraryModel(gui_state)
    qtbot.waitUntil(lambda: not model.loading, timeout=10000)
    assert len(model.all_items) == 3
    real = lm._read_library  # pyright: ignore[reportPrivateUsage]

    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the read blew up")

    monkeypatch.setattr(lm, "_read_library", broken)
    model.reload()
    assert model.loading
    qtbot.waitUntil(lambda: not model.loading, timeout=10000)
    assert len(model.all_items) == 3  # the previous items stay
    monkeypatch.setattr(lm, "_read_library", real)
    assert model.reload_if_stale() is True  # not stuck "loading", and the failed read is retried
    qtbot.waitUntil(lambda: not model.loading, timeout=10000)
    assert len(model.all_items) == 3


def test_the_timeline_order_is_made_once_and_not_again_per_keystroke(
    qtbot: QtBot, gui_state: AppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.gui.timeline_page import timeline_order

    _two_places(gui_state, tmp_path)
    model = LibraryModel(gui_state)
    qtbot.waitUntil(lambda: not model.loading, timeout=10000)

    def boom(_items: object) -> object:
        raise AssertionError("must not sort again")

    monkeypatch.setattr(lm, "timeline_order", boom)
    assert model.timeline_shown == timeline_order(model.shown)
    model.set_filter(MediaFilter(text="a"))
    assert model.timeline_shown == timeline_order(model.shown)
    model.set_filter(MediaFilter())
    assert model.timeline_shown == timeline_order(model.shown)
