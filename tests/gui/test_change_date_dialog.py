"""The Change date dialog: preview of old and new, confirm, apply, library refresh, undo trail."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QDateTime
from pytestqt.qtbot import QtBot

from various_tools.core.context import make_context
from various_tools.core.media.library import MediaLibrary, library_path
from various_tools.gui.change_date_dialog import ChangeDateDialog
from various_tools.gui.state import AppState
from various_tools.gui.timeline_page import TimelinePage

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


def idle(dialog: ChangeDateDialog) -> bool:
    return dialog.worker is None and not dialog._timer.isActive()  # pyright: ignore[reportPrivateUsage]


def test_preview_lists_old_and_new_then_apply_changes_and_refreshes(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState], trashed: list[str]
) -> None:
    root = tmp_path / "pics"
    a = jpeg(root / "a.jpg", "2019:01:31 08:30:00")
    state = make_state(library_folders=[str(root)])
    fill_library(state, [root])
    page = TimelinePage(state)
    qtbot.addWidget(page)
    page.grid.load_thumbnail = lambda _p: None
    asked: list[str] = []
    page.confirm = lambda title, _t: asked.append(title) is None

    dialog = page.change_dates([a])
    assert dialog is not None
    qtbot.addWidget(dialog)
    dialog.mode_box.setCurrentIndex(dialog.mode_box.findData("month_year"))
    dialog.year_spin.setValue(2021)
    dialog.month_box.setCurrentIndex(dialog.month_box.findData(2))
    qtbot.waitUntil(lambda: idle(dialog), timeout=30000)
    assert dialog.preview_rows() == [("a.jpg", "2019-01-31 08:30", "2021-02-28 08:30")]
    assert dialog.can_apply()
    assert a.stat().st_size > 0 and trashed == []  # nothing changed yet

    done: list[object] = []
    dialog.datesChanged.connect(done.append)
    assert dialog.apply() is not None
    qtbot.waitUntil(lambda: dialog.worker is None, timeout=60000)
    assert asked == ["Change dates?"]
    assert len(done) == 1 and len(trashed) == 1
    # the original is recycled from a hidden holder folder next to it, under its own file name
    assert Path(trashed[0]).name == a.name
    assert Path(trashed[0]).parent.parent == a.parent
    assert Path(trashed[0]).parent.name.startswith(".setdate-")
    assert datetime.fromtimestamp(a.stat().st_mtime) == datetime(2021, 2, 28, 8, 30)
    assert "Changed 1 file" in dialog.status_label.text()
    # without ExifTool only the modified time changes, so the library may keep the camera date;
    # with it, the Timeline moves the photo. Either way the page was refreshed.
    assert len(page.grid.items()) == 1


def test_declining_the_question_changes_nothing(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState], trashed: list[str]
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg", "2019:01:31 08:30:00")
    dialog = ChangeDateDialog(make_state(), [a])
    qtbot.addWidget(dialog)
    dialog.confirm = lambda _t, _x: False
    dialog.exact_edit.setDateTime(QDateTime(2020, 1, 1, 9, 0, 0))
    qtbot.waitUntil(lambda: idle(dialog), timeout=30000)
    assert dialog.preview_rows() == [("a.jpg", "2019-01-31 08:30", "2020-01-01 09:00")]
    assert dialog.apply() is None and trashed == []


def test_apply_is_disabled_while_the_preview_is_out_of_date(
    qtbot: QtBot, tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    a = jpeg(tmp_path / "pics" / "a.jpg", "2019:01:31 08:30:00")
    dialog = ChangeDateDialog(make_state(), [a])
    qtbot.addWidget(dialog)
    dialog.exact_edit.setDateTime(QDateTime(2020, 1, 1, 9, 0, 0))
    assert not dialog.apply_button.isEnabled()  # just edited: the preview is being refreshed
    qtbot.waitUntil(lambda: idle(dialog), timeout=30000)
    assert dialog.apply_button.isEnabled()
    dialog.mode_box.setCurrentIndex(dialog.mode_box.findData("shift"))
    assert not dialog.apply_button.isEnabled()
    qtbot.waitUntil(lambda: idle(dialog), timeout=30000)
    assert "Choose how many" in dialog.notes_label.text()
    assert not dialog.can_apply()


def test_a_failed_library_refresh_still_spends_the_plan(
    qtbot: QtBot,
    tmp_path: Path,
    make_state: Callable[..., AppState],
    trashed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_self: MediaLibrary, _paths: object, _ctx: object) -> None:
        raise RuntimeError("library is locked")

    monkeypatch.setattr(MediaLibrary, "refresh_paths", broken)
    a = jpeg(tmp_path / "pics" / "a.jpg", "2019:01:31 08:30:00")
    dialog = ChangeDateDialog(make_state(), [a])
    qtbot.addWidget(dialog)
    dialog.confirm = lambda _t, _x: True
    dialog.exact_edit.setDateTime(QDateTime(2020, 1, 1, 9, 0, 0))
    qtbot.waitUntil(lambda: idle(dialog), timeout=30000)
    assert dialog.apply() is not None
    qtbot.waitUntil(lambda: dialog.worker is None, timeout=60000)
    assert "Changed 1 file" in dialog.status_label.text()
    assert "could not be refreshed" in dialog.status_label.text()
    assert dialog.plan is None and not dialog.can_apply()
    assert dialog.apply() is None  # the same plan can never be applied twice


def test_library_model_item_for_follows_a_reload(
    tmp_path: Path, make_state: Callable[..., AppState]
) -> None:
    from various_tools.gui.library_model import library_model

    root = tmp_path / "pics"
    a = jpeg(root / "a.jpg", "2019:01:31 08:30:00")
    state = make_state(library_folders=[str(root)])
    fill_library(state, [root])
    model = library_model(state)
    model.reload()
    found = model.item_for(a)
    assert found is not None and found.path == a
    assert model.item_for(root / "missing.jpg") is None
    b = jpeg(root / "b.jpg", "2020:01:01 08:30:00")
    fill_library(state, [root])
    model.reload()  # the index is rebuilt with the new items
    assert model.item_for(b) is not None
