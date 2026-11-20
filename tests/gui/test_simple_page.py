"""The step-by-step tool page and the app-wide "Show all settings" switch."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pytestqt.qtbot import QtBot

from various_tools.gui.main_window import MainWindow
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage

Open = Callable[..., ToolPage]
Scan = Callable[[ToolPage], None]


def _two_empty_folders(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "keep").mkdir()
    (root / "keep" / "file.txt").write_text("x")
    return root


def test_page_reads_as_three_steps(open_tool: Open) -> None:
    page = open_tool("empty_folders")
    assert [label.text() for label in page.step_labels] == [
        "1  Choose where to look",
        "2  Look for empty folders",
        "3  Check the list, then confirm",
    ]
    assert page.scan_button.text() == "Look for empty folders"


def test_results_stay_hidden_until_a_scan(open_tool: Open, scan: Scan, tmp_path: Path) -> None:
    page = open_tool("empty_folders", str(_two_empty_folders(tmp_path)))
    assert not page.results.isVisibleTo(page)
    scan(page)
    assert page.results.isVisibleTo(page)


def test_buttons_and_summary_say_what_happens(open_tool: Open, scan: Scan, tmp_path: Path) -> None:
    page = open_tool("empty_folders", str(_two_empty_folders(tmp_path)))
    scan(page)
    assert page.apply_button.text() == "Move 2 to the Recycle Bin…"
    # Empty folders also removes junk files, so the summary does not call them all folders.
    assert page.status_label.text().startswith("Found 2 items to remove")


def test_results_get_the_room_after_a_scan(
    qtbot: QtBot, window: MainWindow, open_tool: Open, scan: Scan, tmp_path: Path
) -> None:
    root = tmp_path / "many"
    for i in range(30):
        (root / f"f{i:02}").mkdir(parents=True)
    page = open_tool("empty_folders", str(root))
    window.resize(1200, 820)
    window.show()
    scan(page)
    # At least ten rows of the list are visible, not two.
    row = page.table.view.sizeHintForRow(0)
    qtbot.waitUntil(lambda: page.table.view.viewport().height() >= 10 * row, timeout=3000)


def test_switch_off_hides_the_extra_controls(open_tool: Open, scan: Scan, tmp_path: Path) -> None:
    page = open_tool("empty_folders", str(_two_empty_folders(tmp_path)))
    scan(page)
    assert not page.show_all_check.isChecked()
    assert not page.table.kind_combo.isVisibleTo(page)
    assert page.select_all_button.isVisibleTo(page)  # always visible
    assert not page.form._advanced_button.isChecked()  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
    page.show_all_check.setChecked(True)
    assert page.table.kind_combo.isVisibleTo(page)
    assert page.select_all_button.isVisibleTo(page)
    assert page.form._advanced_button.isChecked()  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]


def test_switch_reaches_every_page_and_the_log(
    window: MainWindow, open_tool: Open, gui_state: AppState
) -> None:
    first = open_tool("empty_folders")
    second = open_tool("duplicates")
    assert not window.log_panel.isVisibleTo(window)
    first.show_all_check.setChecked(True)
    assert second.show_all_check.isChecked()
    assert window.log_panel.isVisibleTo(window)
    later = open_tool("rename")
    assert later.show_all_check.isChecked()
    assert gui_state.show_all_settings() is True
    later.show_all_check.setChecked(False)
    assert not first.show_all_check.isChecked()
    assert not window.log_panel.isVisibleTo(window)


def test_every_tool_has_plain_words(gui_state: AppState) -> None:
    for tool in gui_state.tools.values():
        label = getattr(tool, "scan_label", "")
        nouns = getattr(tool, "found_nouns", ())
        assert label and label != "Scan", tool.id
        assert len(nouns) == 2 and all(nouns), tool.id
