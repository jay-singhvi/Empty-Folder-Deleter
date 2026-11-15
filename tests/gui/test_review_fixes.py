# pyright: reportOptionalMemberAccess=false
"""GUI fixes from the review: closing during a run, a frozen plan while Apply runs, the rename
live preview during Apply, one undo rule for both pages, copy to clipboard, the keeper-size
lookup off the UI thread, drag-enter without disk access, and the Ollama address field.

Everything runs offscreen with the fakes from ``conftest.py``: no real Recycle Bin, registry or
user folder, and every dialog hook is replaced.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QGuiApplication
from pytestqt.qtbot import QtBot

from various_tools.cli import clipboard as clipboard_module
from various_tools.core import plan as plan_module
from various_tools.core.deps import DEFAULT_OLLAMA_URL
from various_tools.core.journal import Journal
from various_tools.gui import plan_edit
from various_tools.gui.main_window import CLOSE_QUESTION, MainWindow
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.undo_rules import UndoInfo, can_undo
from various_tools.gui.widgets import path_picker
from various_tools.gui.widgets.path_picker import PathEdit, PathListEdit
from various_tools.gui.workers import active_workers

if TYPE_CHECKING:
    from conftest import Dialogs

OpenTool = Callable[..., ToolPage]
PageStep = Callable[[ToolPage], None]
ImageMaker = Callable[..., Path]


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


@pytest.fixture
def empty_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "c").mkdir()
    (tree / "c" / "Thumbs.db").write_text("junk")
    (tree / "keep").mkdir()
    (tree / "keep" / "file.txt").write_text("data")
    return tree


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[threading.Event, threading.Event]]:
    """The first Recycle-Bin call of an apply blocks until ``release`` is set.

    Yields ``(entered, release)``. The release is always set at the end, so no thread hangs.
    """
    entered, release = threading.Event(), threading.Event()

    def slow_trash(path: object) -> None:
        entered.set()
        release.wait(30)
        text = str(path)
        if os.path.isdir(text):
            os.rmdir(text)
        else:
            os.remove(text)

    monkeypatch.setattr(plan_module, "send2trash", slow_trash)
    yield entered, release
    release.set()


# ----- 1: closing the window during a run -----------------------------------------------------


def test_closing_during_an_apply_asks_then_waits_for_the_worker(
    qtbot: QtBot,
    window: MainWindow,
    open_tool: OpenTool,
    dialogs: Dialogs,
    empty_tree: Path,
    scan: PageStep,
    gate: tuple[threading.Event, threading.Event],
    gui_state: AppState,
) -> None:
    entered, release = gate
    window.show()
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    worker = page.apply()
    assert worker is not None and worker.writes
    qtbot.waitUntil(entered.is_set, timeout=10000)

    # "No": everything keeps running and the window stays.
    dialogs.answers = [False]
    window.close()
    assert dialogs.confirms[-1] == ("Close Various Tools", CLOSE_QUESTION)
    assert window.isVisible() and not window.closing
    assert page.busy and not worker.cancel_event.is_set()

    # "Yes": the run is told to stop, but the window waits for the worker to finish.
    dialogs.answers = [True]
    window.close()
    assert window.isVisible() and window.closing
    assert worker.cancel_event.is_set()
    assert not page.scan_button.isEnabled() and page.scan() is None
    window.close()  # closing again while waiting asks nothing more
    assert len(dialogs.confirms) == 3

    release.set()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=20000)
    assert page.worker is None
    assert worker.isFinished()
    assert active_workers() == []
    with Journal(gui_state.settings.journal_path()) as journal:
        run = journal.runs(1)[0]
    assert run.status == "cancelled" and run.finished is not None  # the journal run is closed
    assert [title for title, _ in dialogs.informs] == [], "no message box while closing"


def test_closing_during_a_scan_cancels_without_asking(
    qtbot: QtBot,
    window: MainWindow,
    open_tool: OpenTool,
    dialogs: Dialogs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window.show()
    page = open_tool("empty_folders", str(tmp_path))
    started = threading.Event()

    def blocking_scan(_opts: Any, ctx: Any) -> Any:
        started.set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ctx.check_cancelled()
            time.sleep(0.01)
        raise RuntimeError("never cancelled")

    monkeypatch.setattr(page.tool, "scan", blocking_scan)
    assert page.scan() is not None
    qtbot.waitUntil(started.is_set, timeout=10000)
    window.close()
    assert dialogs.confirms == []
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=20000)
    assert page.worker is None and page.plan is None
    assert active_workers() == []


def test_closing_with_nothing_running_closes_at_once(window: MainWindow, dialogs: Dialogs) -> None:
    window.show()
    window.close()
    assert not window.isVisible()
    assert dialogs.confirms == []


# ----- 2: the plan cannot be edited while Apply runs ------------------------------------------


def test_plan_edits_during_an_apply_change_nothing(
    qtbot: QtBot,
    open_tool: OpenTool,
    empty_tree: Path,
    scan: PageStep,
    wait_idle: Callable[[ToolPage], None],
    gate: tuple[threading.Event, threading.Event],
) -> None:
    entered, release = gate
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    assert page.table.editable()
    assert page.apply() is not None
    qtbot.waitUntil(entered.is_set, timeout=10000)

    model = page.table.view.model()
    index = model.index(0, 0)
    assert not page.table.editable()
    assert not (model.flags(index) & Qt.ItemFlag.ItemIsUserCheckable)
    assert not model.setData(index, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    page.table.set_action_checked(0, False)
    assert all(a.selected for a in page.plan.actions)
    assert page.select_all(False) == 0
    assert not page.select_all_button.isEnabled() and not page.select_none_button.isEnabled()
    # Even a change made straight to the shown plan cannot reach the worker: it has a copy.
    for action in page.plan.actions:
        action.selected = False
    assert page.applied_plan is not page.plan

    release.set()
    wait_idle(page)
    assert page.result is not None and page.result.count("done") == 4
    assert names(empty_tree) == ["keep"]
    assert page.table.editable()


@pytest.fixture
def dup_tree(tmp_path: Path, make_image: ImageMaker) -> Path:
    root = tmp_path / "dups"
    first = make_image("dups/a.jpg", seed=1)
    shutil.copyfile(first, root / "b.jpg")
    make_image("dups/other.jpg", seed=2)
    return root


def test_apply_closes_the_compare_dialog_and_keeps_the_confirmed_keeper(
    open_tool: OpenTool,
    dup_tree: Path,
    scan: PageStep,
    wait_idle: Callable[[ToolPage], None],
) -> None:
    page = open_tool("duplicates", str(dup_tree))
    page.form.update_values({"use_cache": False, "media": "images"})
    scan(page)
    page.table.view.setCurrentIndex(page.table.view.model().index(0, 0))
    dialog = page.open_compare()
    assert dialog is not None and dialog.isVisible()
    plan = page.plan
    assert plan is not None
    action = plan.actions[0]
    assert action.src is not None and action.group_id is not None
    moved, group_id = action.src, action.group_id
    keeper = Path(group_id)
    assert page.apply() is not None
    assert not dialog.isVisible() and page.compare_dialog is None
    assert page.open_compare() is None  # not while it runs
    # Swapping the keeper of the shown plan now must not change what is applied.
    plan_edit.change_keeper(plan, group_id, moved)
    wait_idle(page)
    assert not moved.exists() and keeper.exists()


# ----- 3: no live preview during an apply ------------------------------------------------------


@pytest.fixture
def snap_tree(tmp_path: Path, make_image: ImageMaker) -> Path:
    root = tmp_path / "snaps"
    for number, second in ((1001, 5), (1002, 40)):
        path = make_image(f"snaps/Snapchat-{number}.jpg", seed=number)
        stamp = datetime(2024, 1, 1, 12, 34, second).timestamp()
        os.utime(path, (stamp, stamp))
    return root


def test_rename_preview_cannot_start_while_apply_is_confirmed(
    open_tool: OpenTool,
    snap_tree: Path,
    scan: PageStep,
    wait_idle: Callable[[ToolPage], None],
) -> None:
    page = open_tool("rename", str(snap_tree))
    page._preview_timer.stop()  # pyright: ignore[reportPrivateUsage]
    page.form.update_values({"preset": "snapchat"})
    scan(page)
    seen: list[str] = []

    def confirm(title: str, _text: str) -> bool:
        # While the question is open, a form change and the timer firing both do nothing.
        page._form_changed()  # pyright: ignore[reportPrivateUsage]
        seen.append(f"timer:{page._preview_timer.isActive()}")  # pyright: ignore[reportPrivateUsage]
        page._live_preview()  # pyright: ignore[reportPrivateUsage]
        seen.append(f"worker:{page.worker is not None}")
        seen.append(f"scan:{page.scan() is not None}")
        seen.append(f"form:{page.form.isEnabled()}")
        return True

    page.confirm = confirm
    page._preview_timer.start()  # pyright: ignore[reportPrivateUsage]  # a preview was pending
    worker = page.apply()
    assert seen == ["timer:False", "worker:False", "scan:False", "form:False"]
    assert worker is not None and page.worker is worker and page.busy
    page._live_preview()  # pyright: ignore[reportPrivateUsage]  # during the apply itself
    assert page.worker is worker
    wait_idle(page)
    assert page.result is not None and page.result.status == "done"
    assert not page._preview_timer.isActive()  # pyright: ignore[reportPrivateUsage]


# ----- 9: one undo rule for both pages ---------------------------------------------------------


def test_undo_rule() -> None:
    assert can_undo("done", {"move": 1})
    assert can_undo("partially undone", {"trash": 2})
    assert can_undo("cancelled", {"write": 1})
    assert can_undo("done", {"convert": 1, "delete": 3})
    assert not can_undo("done", {"delete": 3})
    assert not can_undo("undone", {"move": 1})
    assert not can_undo("done", {})
    info = UndoInfo(1, "done", {"move": 2, "trash": 1, "delete": 4})
    assert info.undoable and info.reversible == 3 and info.done == 7


def test_undo_in_history_refreshes_the_tool_page_and_back(
    qtbot: QtBot,
    window: MainWindow,
    open_tool: OpenTool,
    snap_tree: Path,
    scan: PageStep,
    dialogs: Dialogs,
) -> None:
    page = open_tool("rename", str(snap_tree))
    page._preview_timer.stop()  # pyright: ignore[reportPrivateUsage]
    page.form.update_values({"preset": "snapchat"})
    original = names(snap_tree)
    scan(page)
    assert page.apply() is not None
    qtbot.waitUntil(lambda: page.result is not None and page.worker is None, timeout=20000)
    assert not page.undo_button.isHidden()

    history = window.history_page
    qtbot.waitUntil(lambda: not history.loading and history.runs_view.rowCount() == 1)
    history.select_run(page.result.run_id)
    qtbot.waitUntil(lambda: not history.loading and history.undo_button.isEnabled())
    with qtbot.waitSignal(history.runUndone, timeout=20000):
        assert history.undo_selected() is not None
    assert names(snap_tree) == original
    # The tool page hears about it and drops its stale Undo button.
    qtbot.waitUntil(lambda: page.undo_button.isHidden(), timeout=20000)
    assert page.undo_last() is None
    assert len([t for t, _ in dialogs.confirms if t.startswith("Undo")]) == 1


def test_undo_on_the_tool_page_refreshes_history(
    qtbot: QtBot,
    window: MainWindow,
    open_tool: OpenTool,
    snap_tree: Path,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    page = open_tool("rename", str(snap_tree))
    page._preview_timer.stop()  # pyright: ignore[reportPrivateUsage]
    page.form.update_values({"preset": "snapchat"})
    scan(page)
    apply(page)
    history = window.history_page
    qtbot.waitUntil(lambda: not history.loading and history.runs_view.rowCount() == 1)
    history.select_run(page.result.run_id)
    qtbot.waitUntil(lambda: not history.loading and history.undo_button.isEnabled())
    with qtbot.waitSignal(page.runUndone, timeout=20000):
        undo(page)
    qtbot.waitUntil(
        lambda: not history.loading and history.runs_view.item(0, 4).text() == "undone",
        timeout=20000,
    )
    assert not history.undo_button.isEnabled()
    assert page.undo_button.isHidden()


# ----- 12: copy to clipboard -------------------------------------------------------------------


def _export_page(open_tool: OpenTool, tmp_path: Path, copy: bool) -> tuple[ToolPage, Path]:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text("print('hello')\n", encoding="utf-8")
    out = tmp_path / "exports" / "export.txt"
    out.parent.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    page = open_tool("project_export", str(project))
    page.form.update_values({"output_path": str(out), "copy_to_clipboard": copy})
    return page, out


def test_apply_copies_the_written_file_to_the_clipboard(
    qtbot: QtBot, open_tool: OpenTool, tmp_path: Path, scan: PageStep, apply: PageStep
) -> None:
    clipboard = QGuiApplication.clipboard()
    clipboard.setText("before")
    page, out = _export_page(open_tool, tmp_path, copy=True)
    scan(page)
    with qtbot.waitSignal(page.clipboardCopied, timeout=20000):
        apply(page)
    assert clipboard.text() == out.read_text(encoding="utf-8")
    assert "Copied export.txt to the clipboard" in page.status_label.text()


def test_no_copy_when_the_option_is_off_or_the_file_is_large(
    qtbot: QtBot,
    open_tool: OpenTool,
    tmp_path: Path,
    scan: PageStep,
    apply: PageStep,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clipboard = QGuiApplication.clipboard()
    clipboard.setText("before")
    page, _out = _export_page(open_tool, tmp_path, copy=False)
    scan(page)
    apply(page)
    qtbot.wait(200)
    assert clipboard.text() == "before"

    monkeypatch.setattr(clipboard_module, "MAX_CLIPBOARD_BYTES", 5)
    page.form.update_values(
        {"copy_to_clipboard": True, "output_path": str(tmp_path / "exports" / "second.txt")}
    )
    scan(page)
    apply(page)
    qtbot.waitUntil(lambda: "Not copied" in page.status_label.text(), timeout=20000)
    assert clipboard.text() == "before"


# ----- 15: no disk access on the UI thread -----------------------------------------------------


def test_keeper_sizes_are_read_in_a_worker(
    qtbot: QtBot,
    open_tool: OpenTool,
    dup_tree: Path,
    scan: PageStep,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from various_tools.core.media import thumbs

    page = open_tool("duplicates", str(dup_tree))
    page.form.update_values({"use_cache": False, "media": "images"})
    scan(page)
    threads: list[int] = []
    real = thumbs.file_facts

    def spy(path: Path) -> Any:
        threads.append(threading.get_ident())
        return real(path)

    monkeypatch.setattr(thumbs, "file_facts", spy)
    plan = page.plan
    assert plan is not None
    action = plan.actions[0]
    assert action.src is not None and action.group_id is not None
    new_id = plan_edit.change_keeper(plan, action.group_id, action.src)
    worker = page._keeper_changed(new_id)  # pyright: ignore[reportPrivateUsage]
    assert "Now keeping" in page.status_label.text()
    assert worker is not None
    qtbot.waitUntil(worker.isFinished, timeout=10000)
    qtbot.wait(50)
    assert threads and threading.get_ident() not in threads
    assert page.table.sizes()[0] == (dup_tree / action.src.name).stat().st_size


def _drag(mime: QMimeData) -> QDragEnterEvent:
    return QDragEnterEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def test_drag_enter_never_touches_the_disk(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    edit, folders = PathEdit("folder"), PathListEdit()
    qtbot.addWidget(edit)
    qtbot.addWidget(folders)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(folder))])
    web = QMimeData()
    web.setUrls([QUrl("https://example.com/x")])

    def no_disk(_path: object) -> bool:
        raise AssertionError("drag-enter touched the disk")

    with monkeypatch.context() as patch:
        patch.setattr(path_picker.os.path, "isdir", no_disk)
        patch.setattr(path_picker.os.path, "isfile", no_disk)
        for widget in (edit, folders):
            event = _drag(mime)
            widget.dragEnterEvent(event)
            assert event.isAccepted()
            ignored = _drag(web)
            widget.dragEnterEvent(ignored)
            assert not ignored.isAccepted()
    # The drop still checks: a folder goes in, a file does not.
    assert folders.accept_mime(mime) and folders.paths() == [str(folder)]
    a_file = tmp_path / "f.txt"
    a_file.write_text("x")
    file_mime = QMimeData()
    file_mime.setUrls([QUrl.fromLocalFile(str(a_file))])
    assert not edit.accept_mime(file_mime)


# ----- 14: the Ollama address in the external programs panel ----------------------------------


def test_ollama_row_has_an_address_field_that_saves_the_setting(
    qtbot: QtBot, window: MainWindow, gui_state: AppState
) -> None:
    page = window.settings_page
    panel = page.tools_panel
    row = panel.rows["ollama"]
    assert not row.url_edit.isHidden() and row.browse_button.isHidden()
    assert row.url_edit.text() == gui_state.settings.ollama_url

    assert panel.set_ollama_url("localhost:11434") is None
    assert "must start with http" in row.message_label.text()

    worker = panel.set_ollama_url("http://127.0.0.1:9/")
    assert worker is not None
    assert gui_state.settings.ollama_url == "http://127.0.0.1:9"  # saved at once
    assert page.form.raw_values()["ollama_url"] == "http://127.0.0.1:9"  # the form follows
    qtbot.waitUntil(lambda: "Saved, but" in row.message_label.text(), timeout=20000)
    assert row.reset_button.isEnabled()

    # Saving the form afterwards keeps the new address.
    assert page.save()
    assert gui_state.settings.ollama_url == "http://127.0.0.1:9"

    assert panel.reset("ollama") is not None
    assert gui_state.settings.ollama_url == DEFAULT_OLLAMA_URL
    assert row.url_edit.text() == DEFAULT_OLLAMA_URL
    assert page.form.raw_values()["ollama_url"] == DEFAULT_OLLAMA_URL
    qtbot.waitUntil(lambda: not row.reset_button.isEnabled(), timeout=20000)
    assert panel.reset("ollama") is None  # nothing left to reset
