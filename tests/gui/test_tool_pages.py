# pyright: reportOptionalMemberAccess=false
"""Tool pages: a smoke test per tool, then scan -> apply -> undo through the GUI for each tool.

Every tree is generated under ``tmp_path``. Recycle-Bin calls go to the fake in ``conftest.py``.
Dialog hooks are replaced (``dialogs``), so nothing blocks. Nothing needs ffmpeg, ExifTool or
Ollama.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import openpyxl
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from various_tools.core.deps import TOOL_SPECS
from various_tools.core.plan import ActionKind
from various_tools.core.report import EXPORT_FILTER
from various_tools.gui.main_window import MainWindow
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import (
    COMPARE_TOOLS,
    LIVE_PREVIEW_DELAY_MS,
    LIVE_PREVIEW_TOOLS,
    ToolPage,
    apply_summary,
    missing_programs,
    rememberable_fields,
    result_summary,
)
from various_tools.tools.base import discover

if TYPE_CHECKING:
    from conftest import Dialogs

ImageMaker = Callable[..., Path]
OpenTool = Callable[..., ToolPage]
PageStep = Callable[[ToolPage], None]

TOOL_IDS = sorted(discover())


def current_plan(page: ToolPage) -> Any:
    return page.plan


def stop_live_preview(page: ToolPage) -> None:
    """Rename starts a timed re-scan after every form change; tests that scan by hand stop it."""
    page._preview_timer.stop()  # pyright: ignore[reportPrivateUsage]


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def tree_snapshot(root: Path) -> list[tuple[str, bool]]:
    return sorted((str(p.relative_to(root)), p.is_dir()) for p in root.rglob("*"))


# ----- one smoke test per tool ----------------------------------------------------------------


@pytest.mark.parametrize("tool_id", TOOL_IDS)
def test_every_tool_page_builds(window: MainWindow, open_tool: OpenTool, tool_id: str) -> None:
    page = open_tool(tool_id)
    assert window.stack.currentWidget() is page
    assert window.tool_pages[tool_id] is page
    assert page.tool.id == tool_id
    assert page.form is not None
    assert page.form.model is page.tool.Options
    assert page.scan_button.isEnabled()
    assert not page.cancel_button.isEnabled()
    assert not page.apply_button.isEnabled()
    assert not page.export_button.isEnabled()
    assert page.undo_button.isHidden()
    assert page.compare_button.isHidden() is (tool_id not in COMPARE_TOOLS)
    assert page.table.totals_text() == "No plan"
    assert page.plan is None and page.worker is None and not page.busy
    stop_live_preview(page)


def test_live_preview_and_compare_sets_are_what_the_plan_says() -> None:
    assert {"rename"} == LIVE_PREVIEW_TOOLS
    assert {"duplicates"} == COMPARE_TOOLS
    assert set(LIVE_PREVIEW_TOOLS | COMPARE_TOOLS) <= set(TOOL_IDS)


def test_scan_with_invalid_options_does_not_start(open_tool: OpenTool) -> None:
    page = open_tool("project_export")  # the root folder is required
    assert page.scan() is None
    assert page.worker is None
    assert "not valid" in page.status_label.text()
    assert page.form.errors()["root"] == "This is required."
    assert page.plan is None


def test_apply_before_scan_does_nothing(open_tool: OpenTool, dialogs: Dialogs) -> None:
    page = open_tool("empty_folders")
    assert page.apply() is None
    assert page.undo_last() is None
    assert page.export("x.csv") is None
    assert dialogs.confirms == [] and dialogs.informs == []


def test_open_tool_with_folder_fills_the_first_folder_field(
    make_window: Callable[..., MainWindow], tmp_path: Path
) -> None:
    folder = tmp_path / "start"
    folder.mkdir()
    for tool_id, key, expected in (
        ("project_export", "root", str(folder)),
        ("duplicates", "roots", [str(folder)]),
        ("organize_by_date", "sources", [str(folder)]),
    ):
        window = make_window(tool=tool_id, folder=str(folder))
        page = window.tool_pages[tool_id]
        stop_live_preview(page)
        value = page.form.raw_values()[key]
        assert (str(value) if isinstance(expected, str) else [str(v) for v in value]) == expected


# ----- empty_folders: scan, apply, undo -------------------------------------------------------


@pytest.fixture
def empty_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "c").mkdir()
    (tree / "c" / "Thumbs.db").write_text("junk")
    (tree / "keep").mkdir()
    (tree / "keep" / "file.txt").write_text("data")
    return tree


def test_empty_folders_scan_apply_undo(
    window: MainWindow,
    open_tool: OpenTool,
    dialogs: Dialogs,
    trashed: list[str],
    empty_tree: Path,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
    gui_state: AppState,
    qtbot: QtBot,
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    before = tree_snapshot(empty_tree)
    scan(page)
    assert page.plan is not None
    assert len(page.plan.actions) == 4
    assert page.table.totals_text().startswith("4 actions, 4 selected")
    assert page.apply_button.isEnabled() and page.export_button.isEnabled()
    assert "Found 3 empty folder" in page.notes_view.toPlainText()
    assert tree_snapshot(empty_tree) == before  # scan never changes the disk
    assert not gui_state.settings.journal_path().exists()
    assert gui_state.open_journal() is None

    with qtbot.waitSignal(page.runApplied) as applied:
        apply(page)
    assert len(dialogs.confirms) == 1
    title, text = dialogs.confirms[0]
    assert title == f"Apply: {page.tool.name}"
    assert "trash: 4" in text and "Recycle Bin" in text and "PERMANENTLY" not in text
    assert sorted(Path(p).name for p in trashed) == ["Thumbs.db", "a", "b", "c"]
    assert names(empty_tree) == ["keep"]
    assert page.result is not None and page.result.status == "done"
    assert applied.args == [page.result.run_id]
    assert page.last_run_id == page.result.run_id
    assert dialogs.informs[-1][0] == "Apply finished"
    assert "4 done" in dialogs.informs[-1][1]
    assert not page.apply_button.isEnabled()
    assert not page.undo_button.isHidden() and page.undo_button.isEnabled()
    assert page.apply() is None  # a plan is applied once; scan again for a new one

    # History picked the run up (it reads the journal in a worker).
    history = window.history_page
    qtbot.waitUntil(lambda: not history.loading and history.runs_view.rowCount() == 1)
    assert history.runs_view.rowCount() == 1
    assert history.runs_view.item(0, 4).text() == "done"

    # Undo tries the Recycle Bin. Here there is none (the test fake), so nothing comes back and
    # the report says to restore by hand. The trash rows stay "done", so Undo stays offered.
    undo(page)
    assert len(dialogs.confirms) == 2 and dialogs.confirms[1][0] == "Undo"
    title, text = dialogs.informs[-1]
    assert title == "Undo finished"
    assert "not undone (0 reverted)" in text
    assert "restore manually from the Recycle Bin" in text
    assert page.last_run_id == page.result.run_id
    assert not page.undo_button.isHidden()
    assert names(empty_tree) == ["keep"]


def test_apply_declined_changes_nothing(
    open_tool: OpenTool,
    dialogs: Dialogs,
    trashed: list[str],
    empty_tree: Path,
    scan: PageStep,
    gui_state: AppState,
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    before = tree_snapshot(empty_tree)
    dialogs.default = False
    assert page.apply() is None
    assert len(dialogs.confirms) == 1
    assert trashed == []
    assert tree_snapshot(empty_tree) == before
    assert "Apply cancelled" in page.status_label.text()
    assert page.result is None and page.worker is None
    assert page.apply_button.isEnabled()  # still can apply
    assert not gui_state.settings.journal_path().exists()  # nothing journaled


def test_apply_with_nothing_checked_only_informs(
    open_tool: OpenTool, dialogs: Dialogs, trashed: list[str], empty_tree: Path, scan: PageStep
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    assert page.plan is not None
    for index in range(len(page.plan.actions)):
        page.table.set_action_checked(index, False)
    assert page.table.totals_text().startswith("4 actions, 0 selected")
    assert not page.apply_button.isEnabled()
    assert page.apply() is None
    assert dialogs.confirms == []
    assert dialogs.informs[-1][0] == "Apply"
    assert trashed == []


def test_unchecked_actions_are_left_alone(
    open_tool: OpenTool,
    dialogs: Dialogs,
    trashed: list[str],
    empty_tree: Path,
    scan: PageStep,
    apply: PageStep,
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    assert page.plan is not None
    index = next(i for i, a in enumerate(page.plan.actions) if a.src and a.src.name == "b")
    page.table.set_action_checked(index, False)  # also unchecks its parent "a"
    apply(page)
    assert sorted(names(empty_tree)) == ["a", "keep"]
    assert (empty_tree / "a" / "b").is_dir()
    assert sorted(Path(p).name for p in trashed) == ["Thumbs.db", "c"]


def test_permanent_delete_asks_twice_and_the_second_no_keeps_the_folder(
    make_state: Callable[..., AppState],
    make_window: Callable[..., MainWindow],
    dialogs: Dialogs,
    trashed: list[str],
    tmp_path: Path,
    scan: PageStep,
    apply: PageStep,
) -> None:
    state = make_state(delete_mode="permanent")
    window = make_window(state)
    folder = tmp_path / "perm"
    (folder / "gone").mkdir(parents=True)
    page = window.open_tool("empty_folders", str(folder))
    assert page is not None
    dialogs.attach(page)
    page.form.update_values({"delete_mode": "permanent"})  # a locked field: no prompt when loaded
    scan(page)
    assert page.plan is not None
    assert [a.kind for a in page.plan.actions] == [ActionKind.DELETE]

    dialogs.answers = [True, False]
    assert page.apply() is None
    assert [t for t, _ in dialogs.confirms] == [f"Apply: {page.tool.name}", "PERMANENT DELETE"]
    assert "PERMANENTLY" in dialogs.confirms[0][1]
    assert (folder / "gone").is_dir()  # the second "no" kept it
    assert not state.settings.journal_path().exists()

    dialogs.confirms.clear()
    dialogs.answers = [True, True]
    apply(page)
    assert [t for t, _ in dialogs.confirms] == [f"Apply: {page.tool.name}", "PERMANENT DELETE"]
    assert not (folder / "gone").exists()
    assert trashed == []  # permanent: the Recycle Bin was not used


def test_trash_mode_never_asks_for_the_permanent_confirmation(
    open_tool: OpenTool, dialogs: Dialogs, empty_tree: Path, scan: PageStep, apply: PageStep
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    apply(page)
    assert [t for t, _ in dialogs.confirms] == [f"Apply: {page.tool.name}"]


def test_permanent_option_without_the_app_setting_never_deletes(
    open_tool: OpenTool,
    dialogs: Dialogs,
    trashed: list[str],
    tmp_path: Path,
    scan: PageStep,
    apply: PageStep,
) -> None:
    folder = tmp_path / "safe"
    (folder / "x").mkdir(parents=True)
    page = open_tool("empty_folders", str(folder))
    page.form.update_values({"delete_mode": "permanent"})  # the app setting is still "trash"
    scan(page)
    assert page.plan is not None
    assert [a.kind for a in page.plan.actions] == [ActionKind.DELETE]
    apply(page)  # both confirmations answered yes; the engine still refuses
    assert page.result is not None
    assert [o.status for o in page.result.outcomes] == ["skipped"]
    assert "permanent delete is off" in page.result.outcomes[0].detail
    assert (folder / "x").is_dir()
    assert trashed == []


# ----- rename ---------------------------------------------------------------------------------


@pytest.fixture
def snap_tree(tmp_path: Path, make_image: ImageMaker) -> Path:
    root = tmp_path / "snaps"
    for number, second in ((1001, 5), (1002, 40)):
        path = make_image(f"snaps/Snapchat-{number}.jpg", seed=number)
        stamp = datetime(2024, 1, 1, 12, 34, second).timestamp()
        os.utime(path, (stamp, stamp))
    return root


def test_rename_scan_apply_undo(
    open_tool: OpenTool,
    dialogs: Dialogs,
    snap_tree: Path,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    page = open_tool("rename", str(snap_tree))
    stop_live_preview(page)
    page.form.update_values({"preset": "snapchat"})
    original = names(snap_tree)
    scan(page)
    assert page.plan is not None
    assert {a.kind for a in page.plan.actions} == {ActionKind.RENAME}
    assert len(page.plan.actions) == 2
    assert names(snap_tree) == original  # scan only previews
    assert page.table.view.model().headerData(1, Qt.Orientation.Horizontal) == "Folder"
    apply(page)
    renamed = names(snap_tree)
    assert len(renamed) == 2
    assert not set(renamed) & set(original)
    assert all(n.startswith("Snapchat-2024") for n in renamed)
    assert "Moves and renames can be undone" in dialogs.confirms[0][1]
    undo(page)
    assert names(snap_tree) == original
    assert "undone (2 reverted)" in dialogs.informs[-1][1]
    assert page.undo_button.isHidden()


def test_rename_live_preview_scans_after_a_form_change(
    qtbot: QtBot, open_tool: OpenTool, snap_tree: Path
) -> None:
    page = open_tool("rename", str(snap_tree))  # setting the folder is a form change
    page.form.update_values({"preset": "snapchat"})  # loading does not restart the timer
    assert page._preview_timer.isActive()  # pyright: ignore[reportPrivateUsage]
    assert page._preview_timer.interval() == LIVE_PREVIEW_DELAY_MS  # pyright: ignore[reportPrivateUsage]
    assert page.plan is None
    qtbot.waitUntil(lambda: page.plan is not None and page.worker is None, timeout=15000)
    first = current_plan(page)
    assert first is not None and len(first.actions) == 2
    assert not page.form.isHidden()
    assert all(a.dst.suffix == ".jpg" for a in first.actions if a.dst)

    case = page.form.editor("case")
    case.setCurrentText("upper")  # type: ignore[attr-defined]
    assert page._preview_timer.isActive()  # pyright: ignore[reportPrivateUsage]
    qtbot.waitUntil(lambda: page.plan is not first and page.worker is None, timeout=15000)
    second = current_plan(page)
    assert second is not None
    assert all(a.dst.suffix == ".JPG" for a in second.actions if a.dst)
    # The preview never changed the files.
    assert all(p.name.startswith("Snapchat-100") for p in snap_tree.iterdir())


def test_live_preview_waits_for_a_folder_and_other_tools_have_none(
    open_tool: OpenTool, tmp_path: Path
) -> None:
    page = open_tool("rename")
    page._live_preview()  # pyright: ignore[reportPrivateUsage]
    assert page.worker is None and page.plan is None  # no folder yet: nothing to preview
    other = open_tool("empty_folders", str(tmp_path))
    assert not other._preview_timer.isActive()  # pyright: ignore[reportPrivateUsage]
    other.form.update_values({"min_age_days": 2})
    other.form.editor("skip_hidden_system").toggle()  # type: ignore[attr-defined]
    assert not other._preview_timer.isActive()  # pyright: ignore[reportPrivateUsage]
    stop_live_preview(page)


# ----- duplicates -----------------------------------------------------------------------------


@pytest.fixture
def dup_tree(tmp_path: Path, make_image: ImageMaker) -> Path:
    root = tmp_path / "dups"
    first = make_image("dups/a.jpg", seed=1)
    shutil.copyfile(first, root / "b.jpg")
    make_image("dups/other.jpg", seed=2)
    return root


def prepare_duplicates(page: ToolPage) -> None:
    page.form.update_values({"use_cache": False, "media": "images"})


def test_duplicates_scan_apply_undo(
    open_tool: OpenTool,
    dialogs: Dialogs,
    dup_tree: Path,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    page = open_tool("duplicates", str(dup_tree))
    prepare_duplicates(page)
    scan(page)
    assert page.plan is not None
    assert len(page.plan.actions) == 1
    action = page.plan.actions[0]
    assert action.kind is ActionKind.MOVE
    assert action.dst is not None and action.dst.parent == dup_tree / "duplicate"
    assert page.table.view.model().rowCount() == 1  # one group
    apply(page)
    assert action.src is not None
    kept = ({"a.jpg", "b.jpg"} - {action.src.name}).pop()
    assert names(dup_tree) == sorted([kept, "duplicate", "other.jpg"])
    assert (dup_tree / "duplicate" / action.dst.name).is_file()
    assert not action.src.exists()
    undo(page)
    assert names(dup_tree) == ["a.jpg", "b.jpg", "duplicate", "other.jpg"]
    assert not any((dup_tree / "duplicate").iterdir())
    assert "undone (1 reverted)" in dialogs.informs[-1][1]


def test_duplicates_compare_view_changes_the_keeper_before_apply(
    qtbot: QtBot,
    open_tool: OpenTool,
    dup_tree: Path,
    scan: PageStep,
    apply: PageStep,
) -> None:
    page = open_tool("duplicates", str(dup_tree))
    prepare_duplicates(page)
    assert page.open_compare() is None  # no plan yet
    scan(page)
    assert not page.compare_button.isEnabled()  # no group chosen
    model = page.table.view.model()
    page.table.view.setCurrentIndex(model.index(0, 0))
    assert page.compare_button.isEnabled()
    dialog = page.open_compare()
    assert dialog is not None
    assert dialog.wait_for_workers(15000)  # type: ignore[attr-defined]
    assert page.plan is not None
    old_action = page.plan.actions[0]
    assert old_action.src is not None and old_action.group_id is not None
    old_keeper, moved = Path(old_action.group_id), old_action.src
    with qtbot.waitSignal(dialog.keeperChanged):  # type: ignore[attr-defined]
        dialog.keep(moved)  # type: ignore[attr-defined]
    assert f"Now keeping {moved.name}" in page.status_label.text()
    assert page.plan.actions[0].src == old_keeper
    dialog.close()  # type: ignore[attr-defined]
    apply(page)
    # The file that used to be kept is the one that moved.
    assert not old_keeper.exists()
    assert moved.exists()
    assert (dup_tree / "duplicate" / old_keeper.name).is_file()


def test_compare_is_closed_to_other_tools_and_after_apply(
    open_tool: OpenTool, dup_tree: Path, scan: PageStep, apply: PageStep
) -> None:
    page = open_tool("duplicates", str(dup_tree))
    prepare_duplicates(page)
    scan(page)
    page.table.view.setCurrentIndex(page.table.view.model().index(0, 0))
    apply(page)
    assert page.open_compare() is None
    assert not page.compare_button.isEnabled()


# ----- organize_by_date -----------------------------------------------------------------------


def test_organize_by_date_scan_apply_undo(
    tmp_path: Path,
    open_tool: OpenTool,
    make_image: ImageMaker,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    src = tmp_path / "unsorted"
    stamp = datetime(2024, 3, 5, 10, 0, 0).timestamp()
    for number in (1, 2):
        path = make_image(f"unsorted/photo{number}.jpg", seed=number)
        os.utime(path, (stamp, stamp))
    dest = tmp_path / "sorted"
    page = open_tool("organize_by_date", str(src))
    page.form.update_values(
        {
            "dest_root": str(dest),
            "date_priority": ["modified"],
            "catalog_path": str(tmp_path / "catalog.sqlite"),
        }
    )
    scan(page)
    assert page.plan is not None
    assert len(page.plan.actions) == 2
    assert {a.kind for a in page.plan.actions} == {ActionKind.MOVE}
    assert names(src) == ["photo1.jpg", "photo2.jpg"]
    assert not dest.exists()
    apply(page)
    assert names(src) == []
    moved = sorted(p for p in dest.rglob("*.jpg"))
    assert [p.name for p in moved] == ["photo1.jpg", "photo2.jpg"]
    assert all("2024" in str(p.relative_to(dest)) for p in moved)
    assert all("03" in p.parent.name for p in moved)
    undo(page)
    assert names(src) == ["photo1.jpg", "photo2.jpg"]
    assert not list(dest.rglob("*.jpg"))
    assert "undone" in page.status_label.text()


# ----- media_resize ---------------------------------------------------------------------------


def test_media_resize_scan_apply_undo(
    tmp_path: Path,
    open_tool: OpenTool,
    dialogs: Dialogs,
    make_image: ImageMaker,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    folder = tmp_path / "imgs"
    for number in (1, 2):
        make_image(f"imgs/pic{number}.jpg", seed=number, size=(96, 64))
    page = open_tool("media_resize", str(folder))
    page.form.update_values(
        {"media": "images", "mode": "percent", "percent": 50, "keep_metadata": False}
    )
    scan(page)
    assert page.plan is not None
    assert page.plan.actions
    assert {a.kind for a in page.plan.actions} <= {ActionKind.CONVERT, ActionKind.WRITE}
    assert names(folder) == ["pic1.jpg", "pic2.jpg"]  # preview wrote nothing
    apply(page)
    out = folder / "resized"
    assert names(out) == ["pic1.jpg", "pic2.jpg"]
    with Image.open(out / "pic1.jpg") as resized:
        assert resized.size == (48, 32)
    with Image.open(folder / "pic1.jpg") as original:
        assert original.size == (96, 64)  # originals untouched
    undo(page)
    # Undo recycles unchanged outputs (the Recycle Bin is the test fake); originals stay.
    text = dialogs.informs[-1][1]
    assert "undone (2 reverted)" in text
    assert not out.exists() or names(out) == []
    assert names(folder)[:2] == ["pic1.jpg", "pic2.jpg"]
    assert page.undo_button.isHidden()


# ----- project_export -------------------------------------------------------------------------


def test_project_export_scan_apply_undo(
    tmp_path: Path,
    open_tool: OpenTool,
    dialogs: Dialogs,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (project / "notes.md").write_text("# notes\n", encoding="utf-8")
    (tmp_path / "exports").mkdir()
    out = tmp_path / "exports" / "export.txt"
    page = open_tool("project_export", str(project))
    page.form.update_values({"output_path": str(out), "exts": [".py", ".md"]})
    scan(page)
    assert page.plan is not None
    assert [a.kind for a in page.plan.actions] == [ActionKind.WRITE], page.plan.notes
    assert not out.exists()
    apply(page)
    text = out.read_text(encoding="utf-8")
    assert "main.py" in text and "print('hello')" in text and "notes.md" in text
    undo(page)
    # The written export goes to the Recycle Bin (the test fake) because it is unchanged.
    assert "undone (1 reverted)" in dialogs.informs[-1][1]
    assert not out.exists()
    assert page.undo_button.isHidden()


# ----- cancel ---------------------------------------------------------------------------------


def test_cancel_during_a_scan_stops_it_and_leaves_the_page_usable(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    open_tool: OpenTool,
    tmp_path: Path,
    wait_idle: Callable[[ToolPage], None],
    trashed: list[str],
) -> None:
    page = open_tool("empty_folders", str(tmp_path))
    started = threading.Event()

    def blocking_scan(_opts: Any, ctx: Any) -> Any:
        started.set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ctx.check_cancelled()  # raises Cancelled once Cancel is clicked
            time.sleep(0.01)
        raise RuntimeError("the scan was never cancelled")

    monkeypatch.setattr(page.tool, "scan", blocking_scan)
    busy: list[bool] = []
    page.busyChanged.connect(busy.append)
    worker = page.scan()
    assert worker is not None
    qtbot.waitUntil(started.is_set, timeout=10000)
    assert page.busy
    assert page.cancel_button.isEnabled()
    assert not page.scan_button.isEnabled()
    assert page.scan() is None  # one job at a time
    assert page.apply() is None
    page.cancel_button.click()
    assert "Cancelling" in page.status_label.text()
    wait_idle(page)
    assert busy == [True, False]
    assert page.plan is None
    assert "Cancelled" in page.status_label.text()
    assert page.scan_button.isEnabled() and not page.cancel_button.isEnabled()
    assert page.form.isEnabled()
    assert trashed == []


def test_cancel_with_nothing_running_is_harmless(open_tool: OpenTool) -> None:
    page = open_tool("empty_folders")
    page.cancel()
    assert page.worker is None
    assert page.scan_button.isEnabled()


def test_a_failing_scan_is_reported_not_raised(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    open_tool: OpenTool,
    tmp_path: Path,
    wait_idle: Callable[[ToolPage], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = open_tool("empty_folders", str(tmp_path))

    def failing_scan(_opts: Any, _ctx: Any) -> Any:
        raise OSError("disk exploded")

    monkeypatch.setattr(page.tool, "scan", failing_scan)
    assert page.scan() is not None
    wait_idle(page)
    assert "disk exploded" in page.status_label.text()
    assert "Failed" in page.status_label.text()
    assert page.plan is None
    assert page.scan_button.isEnabled()


# ----- remembered options ---------------------------------------------------------------------


def test_remembered_folders_survive_a_new_page_and_a_new_state(
    make_window: Callable[..., MainWindow],
    open_tool: OpenTool,
    gui_state: AppState,
    empty_tree: Path,
    scan: PageStep,
    tmp_path: Path,
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)  # a scan remembers the folders and options
    assert gui_state.last_options("empty_folders")["roots"] == [str(empty_tree)]
    assert (tmp_path / "data" / "gui_state.json").is_file()

    fresh_window = make_window()
    fresh_page = fresh_window.open_tool("empty_folders")
    assert fresh_page is not None
    assert [str(r) for r in fresh_page.form.raw_values()["roots"]] == [str(empty_tree)]

    reloaded = AppState.load(gui_state.settings_path)
    assert reloaded.last_options("empty_folders")["roots"] == [str(empty_tree)]
    third = make_window(reloaded)
    third_page = third.open_tool("empty_folders")
    assert third_page is not None
    assert [str(r) for r in third_page.form.raw_values()["roots"]] == [str(empty_tree)]
    # Other tools are not affected.
    other = third.open_tool("duplicates")
    assert other is not None
    assert other.form.raw_values()["roots"] == []


def test_options_are_remembered_and_reset_keeps_the_folders(
    make_window: Callable[..., MainWindow],
    open_tool: OpenTool,
    empty_tree: Path,
    scan: PageStep,
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    page.form.update_values({"min_age_days": 7})
    scan(page)

    again = make_window().open_tool("empty_folders")
    assert again is not None
    assert again.form.raw_values()["min_age_days"] == 7  # remembered

    again.reset_options()
    values = again.form.raw_values()
    assert values["min_age_days"] == 0  # back to the default
    assert [str(r) for r in values["roots"]] == [str(empty_tree)]  # folders stay


def test_risky_options_are_never_remembered(
    make_window: Callable[..., MainWindow],
    open_tool: OpenTool,
    gui_state: AppState,
    empty_tree: Path,
    scan: PageStep,
) -> None:
    # A permanent delete always starts again from the Recycle Bin default.
    page = open_tool("empty_folders", str(empty_tree))
    page.form.update_values({"delete_mode": "permanent"})
    scan(page)
    assert "delete_mode" not in gui_state.last_options("empty_folders")
    again = make_window().open_tool("empty_folders")
    assert again is not None and again.form.raw_values()["delete_mode"] == "trash"

    # Options that replace or remove originals, and 🔒 locked options, are left out entirely.
    from various_tools.tools.media_convert import MediaConvertOptions
    from various_tools.tools.organize_by_date.tool import OrganizeByDateOptions

    convert = rememberable_fields(MediaConvertOptions)
    assert "trash_originals" not in convert and "jpeg_quality" in convert
    organize = rememberable_fields(OrganizeByDateOptions)
    assert "cloud_only" not in organize


def test_folders_saved_by_version_0_2_are_still_read(make_state: Callable[..., AppState]) -> None:
    state = make_state()
    path = state.settings.resolved_data_folder() / "gui_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"format": 1, "last_folders": {"empty_folders": {"roots": ["C:/x"]}}}', encoding="utf-8"
    )
    assert state.last_options("empty_folders") == {"roots": ["C:/x"]}


def test_remembering_can_be_switched_off(
    make_state: Callable[..., AppState],
    make_window: Callable[..., MainWindow],
    empty_tree: Path,
    scan: PageStep,
    tmp_path: Path,
) -> None:
    state = make_state(remember_last_folders=False)
    window = make_window(state)
    page = window.open_tool("empty_folders", str(empty_tree))
    assert page is not None
    scan(page)
    assert state.last_options("empty_folders") == {}
    assert not (tmp_path / "data" / "gui_state.json").exists()


# ----- missing programs -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("notes", "expected"),
    [
        ([], []),
        (["ffmpeg not found: videos skipped"], ["ffmpeg"]),
        (["ExifTool not found; dates come from file times"], ["exiftool"]),
        (["Needs ffprobe to read video lengths"], ["ffprobe"]),
        (["ffprobe not found", "ffmpeg not found"], ["ffmpeg", "ffprobe"]),  # TOOL_SPECS order
        (["ffmpeg is installed and fine"], []),  # mentioned but not missing
        (["something was not found"], []),  # missing, but no known program named
        (["ffmpegx not found"], []),  # whole words only
        (["The tool needs ollama at http://localhost:11434"], ["ollama"]),
        (["Found 3 files", "needs nothing"], []),
    ],
)
def test_missing_programs_parsing(notes: list[str], expected: list[str]) -> None:
    assert missing_programs(notes) == expected
    assert set(expected) <= set(TOOL_SPECS)


def test_missing_banner_and_locate_button(
    open_tool: OpenTool,
    tmp_path: Path,
    fake_exe: Callable[..., Path],
    gui_state: AppState,
    dialogs: Dialogs,
) -> None:
    page = open_tool("duplicates")
    assert page.missing_banner.isHidden()
    page._show_missing(["ffmpeg", "ollama"])  # pyright: ignore[reportPrivateUsage]
    assert not page.missing_banner.isHidden()
    assert "ffmpeg" in page.missing_label.text() and "ollama" in page.missing_label.text()
    assert set(page.locate_buttons) == {"ffmpeg"}  # ollama is an address, set in Settings
    exe = fake_exe(tmp_path / "bin", "ffmpeg", "ffmpeg version 9.9")
    assert page.locate("ffmpeg", str(exe)) is True
    assert gui_state.settings.external_tools["ffmpeg"] == str(exe)
    assert "Saved the ffmpeg location" in page.status_label.text()
    assert not page.locate_buttons["ffmpeg"].isEnabled()
    assert AppState.load(gui_state.settings_path).settings.external_tools["ffmpeg"] == str(exe)
    page._show_missing([])  # pyright: ignore[reportPrivateUsage]
    assert page.missing_banner.isHidden() and page.locate_buttons == {}


def test_locate_with_a_file_that_is_not_the_program_is_refused(
    open_tool: OpenTool,
    tmp_path: Path,
    gui_state: AppState,
    problems: list[tuple[str, str]],
) -> None:
    page = open_tool("duplicates")
    page._show_missing(["ffmpeg"])  # pyright: ignore[reportPrivateUsage]
    missing = tmp_path / "nowhere" / "ffmpeg.exe"
    assert page.locate("ffmpeg", str(missing)) is False
    assert "ffmpeg" not in gui_state.settings.external_tools
    assert problems and "Not saved" in problems[-1][1]
    assert page.locate_buttons["ffmpeg"].isEnabled()


def test_a_scan_note_about_a_missing_program_shows_the_banner(
    open_tool: OpenTool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scan: PageStep,
) -> None:
    from various_tools.core.plan import Plan

    page = open_tool("empty_folders", str(tmp_path))

    def scan_with_note(_opts: Any, _ctx: Any) -> Plan:
        return Plan(tool="empty_folders", roots=[tmp_path], notes=["ffprobe not found: skipped"])

    monkeypatch.setattr(page.tool, "scan", scan_with_note)
    scan(page)
    assert "ffprobe not found" in page.notes_view.toPlainText()
    assert not page.missing_banner.isHidden()
    assert "ffprobe" in page.locate_buttons
    assert "Nothing found" in page.status_label.text()


# ----- export ---------------------------------------------------------------------------------


def test_export_plan_to_csv_xlsx_and_html(
    open_tool: OpenTool,
    empty_tree: Path,
    scan: PageStep,
    wait_idle: PageStep,
    tmp_path: Path,
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    out = tmp_path / "out"
    out.mkdir()

    assert page.export(str(out / "plan.csv")) is not None
    wait_idle(page)
    csv_text = (out / "plan.csv").read_text(encoding="utf-8-sig")
    assert "Thumbs.db" in csv_text and "empty folder" in csv_text
    assert "Exported to" in page.status_label.text()

    assert page.export(str(out / "plan.xlsx")) is not None
    wait_idle(page)
    workbook = openpyxl.load_workbook(out / "plan.xlsx")
    cells = [str(c.value) for sheet in workbook for row in sheet.iter_rows() for c in row]
    assert any("Thumbs.db" in c for c in cells)

    assert page.export(str(out / "plan.html")) is not None
    wait_idle(page)
    html = (out / "plan.html").read_text(encoding="utf-8")
    assert "<html" in html.lower() and "Thumbs.db" in html


def test_export_after_apply_uses_the_result(
    open_tool: OpenTool,
    empty_tree: Path,
    scan: PageStep,
    apply: PageStep,
    wait_idle: PageStep,
    tmp_path: Path,
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    apply(page)
    target = tmp_path / "result.csv"
    assert page.export(str(target)) is not None
    wait_idle(page)
    text = target.read_text(encoding="utf-8-sig")
    assert "done" in text
    assert "Thumbs.db" in text


def test_export_through_the_file_dialog_hook(
    open_tool: OpenTool,
    dialogs: Dialogs,
    empty_tree: Path,
    scan: PageStep,
    wait_idle: PageStep,
    tmp_path: Path,
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    dialogs.export_choice = None  # the user cancelled the dialog
    assert page.export() is None
    target = tmp_path / "chosen.html"
    html_filter = next(f for f in EXPORT_FILTER.split(";;") if f.startswith("HTML"))
    dialogs.export_choice = (str(target), html_filter)
    assert page.export() is not None
    wait_idle(page)
    assert "<html" in target.read_text(encoding="utf-8").lower()


def test_export_with_an_unknown_suffix_informs(
    open_tool: OpenTool, dialogs: Dialogs, empty_tree: Path, scan: PageStep, tmp_path: Path
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    assert page.export(str(tmp_path / "plan.weird")) is None
    assert dialogs.informs[-1][0] == "Export"
    assert "cannot tell the export format" in dialogs.informs[-1][1]
    assert not (tmp_path / "plan.weird").exists()


# ----- small pure helpers ---------------------------------------------------------------------


def test_apply_summary_and_result_summary(
    open_tool: OpenTool, empty_tree: Path, scan: PageStep, apply: PageStep
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    scan(page)
    assert page.plan is not None
    text = apply_summary(page.plan, permanent_delete=False)
    assert text.startswith("Apply 4 selected action(s)?")
    assert "  trash: 4" in text and "PERMANENTLY" not in text
    assert "PERMANENTLY" in apply_summary(page.plan, permanent_delete=True)
    apply(page)
    assert page.result is not None
    assert result_summary(page.result) == (
        f"Run {page.result.run_id} done: 4 done, 0 skipped, 0 failed."
    )


def test_closing_the_window_cancels_a_running_scan(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    make_window: Callable[..., MainWindow],
    tmp_path: Path,
    dialogs: Dialogs,
) -> None:
    window = make_window()
    page = window.open_tool("empty_folders", str(tmp_path))
    assert page is not None
    dialogs.attach(page)
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
    qtbot.waitUntil(lambda: page.worker is None, timeout=15000)
    assert page.plan is None


def test_select_none_and_select_all_buttons(
    open_tool: OpenTool, dialogs: Dialogs, empty_tree: Path, scan: PageStep
) -> None:
    page = open_tool("empty_folders", str(empty_tree))
    assert page.select_all(False) == 0  # no plan yet
    scan(page)
    plan = current_plan(page)
    assert plan is not None
    assert page.apply_button.isEnabled()
    assert page.select_all(False) == 4
    assert all(not a.selected for a in plan.actions)
    assert "0 selected" in page.table.totals_text()
    assert not page.apply_button.isEnabled()
    assert page.apply() is None
    assert dialogs.confirms == []
    assert page.select_all(True) == 4
    assert all(a.selected for a in plan.actions)
    assert page.table.totals_text().startswith("4 actions, 4 selected")
    assert page.apply_button.isEnabled()
    page.select_none_button.click()
    assert not page.apply_button.isEnabled()
    page.select_all_button.click()
    assert page.apply_button.isEnabled()
