"""TimelinePage: folders, the Year > Month tree, updating the library and the summary line."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QTreeWidgetItem
from pytestqt.qtbot import QtBot

from various_tools.core.context import make_context
from various_tools.core.media.library import MediaLibrary, SyncReport, library_path
from various_tools.gui.state import AppState
from various_tools.gui.timeline_page import TimelinePage, summary_text

EXIF_IFD = 0x8769


def jpeg(path: Path, taken: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = Image.Exif()
    data.get_ifd(EXIF_IFD)[0x9003] = taken
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
    page.grid.load_thumbnail = lambda _p: None
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


def test_on_open_it_shows_what_the_library_has(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    root = tmp_path / "pics"
    jpeg(root / "a.jpg", "2023:05:01 10:00:00")
    jpeg(root / "b.jpg", "2023:05:20 10:00:00")
    jpeg(root / "c.jpg", "2024:01:02 10:00:00")
    state = make_state(library_folders=[str(root)])
    fill_library(state, [root])

    page = make_page(qtbot, state)

    assert tree_texts(page) == [("2024 (1)", ["January (1)"]), ("2023 (2)", ["May (2)"])]
    assert page.folders.paths() == [str(root)]
    # the newest month is shown at once
    assert [i.path.name for i in page.grid.items()] == ["c.jpg"]
    assert page.status_label.text() == "3 photos and 0 videos."


def test_choosing_a_month_shows_its_items(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    root = tmp_path / "pics"
    jpeg(root / "b.jpg", "2023:05:20 10:00:00")
    jpeg(root / "a.jpg", "2023:05:01 10:00:00")
    jpeg(root / "c.jpg", "2024:01:02 10:00:00")
    state = make_state(library_folders=[str(root)])
    fill_library(state, [root])
    page = make_page(qtbot, state)

    may = top(page, 1).child(0)
    assert may is not None
    page.months.setCurrentItem(may)
    assert [i.path.name for i in page.grid.items()] == ["a.jpg", "b.jpg"]

    page.months.setCurrentItem(top(page, 1))  # a whole year
    assert [i.path.name for i in page.grid.items()] == ["a.jpg", "b.jpg"]


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


def test_changing_folders_saves_them_in_settings(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    state = make_state()
    page = make_page(qtbot, state)
    folder = tmp_path / "Camera Roll"
    folder.mkdir()
    page.folders.add_paths([str(folder)])
    assert state.settings.library_folders == [folder]


def test_qt_repaint_method_is_not_hidden() -> None:
    from PySide6.QtWidgets import QWidget

    from various_tools.gui.timeline_page import TimelinePage

    assert "update" not in TimelinePage.__dict__
    assert TimelinePage.update is QWidget.update
