# pyright: reportOptionalMemberAccess=false
"""App entry point, main window shell, sidebar, theme, Explorer menu layout and tool discovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from various_tools import __version__
from various_tools.gui import app as gui_app
from various_tools.gui import shell, theme
from various_tools.gui.icon import app_icon
from various_tools.gui.main_window import HISTORY_ID, SETTINGS_ID, MainWindow
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.tools import base
from various_tools.tools.base import discover

TOOL_IDS = {
    "checksum",
    "corrupt_media",
    "disk_usage",
    "duplicates",
    "empty_folders",
    "media_convert",
    "media_resize",
    "metadata_strip",
    "organize_by_date",
    "project_export",
    "rename",
    "sort_by_type",
    "temp_cleaner",
}


def _qapp() -> QApplication:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    return app


# ----- argument parsing -----------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    args, rest = gui_app.parse_args([])
    assert args.tool is None
    assert args.folder is None
    assert args.config is None
    assert rest == []


def test_parse_args_reads_tool_folder_and_config() -> None:
    args, rest = gui_app.parse_args(
        ["--tool", "empty_folders", "--folder", "C:\\x y", "--config", "s.toml"]
    )
    assert (args.tool, args.folder, args.config) == ("empty_folders", "C:\\x y", "s.toml")
    assert rest == []


def test_parse_args_passes_unknown_arguments_to_qt() -> None:
    args, rest = gui_app.parse_args(["--tool", "rename", "-style", "fusion", "--bogus"])
    assert args.tool == "rename"
    assert rest == ["-style", "fusion", "--bogus"]


# ----- window ---------------------------------------------------------------------------------


def test_make_window_title_and_icon(window: MainWindow) -> None:
    assert window.windowTitle() == f"Various Tools {__version__}"
    assert not window.windowIcon().isNull()
    assert not app_icon().isNull()
    assert app_icon().availableSizes()


def test_make_window_with_tool_and_folder_fills_the_first_folder_field(
    make_window: Callable[..., MainWindow], tmp_path: Path
) -> None:
    folder = tmp_path / "chosen"
    folder.mkdir()
    window = make_window(tool="empty_folders", folder=str(folder))
    page = window.tool_pages["empty_folders"]
    assert window.stack.currentWidget() is page
    assert page.form.raw_values()["roots"] == [str(folder)]
    assert window.sidebar.currentItem().data(0, Qt.ItemDataRole.UserRole) == "empty_folders"


def test_unknown_tool_is_reported_not_raised(
    make_window: Callable[..., MainWindow], tmp_path: Path
) -> None:
    window = make_window(tool="no_such_tool", folder=str(tmp_path))
    assert window.tool_pages == {}
    assert "Unknown tool" in window.statusBar().currentMessage()
    assert window.open_tool("no_such_tool") is None


def test_pages_are_created_on_first_use(window: MainWindow) -> None:
    assert window.tool_pages == {}
    first = window.tool_page("rename")
    assert window.tool_page("rename") is first
    assert isinstance(first, ToolPage)
    assert set(window.tool_pages) == {"rename"}


def test_history_and_settings_pages_are_reachable(window: MainWindow) -> None:
    assert window.show_page(HISTORY_ID) is window.history_page
    assert window.stack.currentWidget() is window.history_page
    assert window.show_page(SETTINGS_ID) is window.settings_page
    assert window.stack.currentWidget() is window.settings_page


def test_sidebar_lists_every_tool_by_category(window: MainWindow) -> None:
    ids = set(window.visible_tool_ids())
    assert ids >= TOOL_IDS
    assert {HISTORY_ID, SETTINGS_ID} <= set(window._tool_items)  # pyright: ignore[reportPrivateUsage]
    headers = [
        window.sidebar.topLevelItem(i).text(0) for i in range(window.sidebar.topLevelItemCount())
    ]
    assert headers[-1] == "App"
    for tool in window.state.tools.values():
        assert tool.category in headers


def test_sidebar_search_filters_tools(window: MainWindow) -> None:
    window.search_edit.setText("duplicate")
    shown = window.visible_tool_ids()
    assert "duplicates" in shown
    assert "empty_folders" not in shown
    assert "project_export" not in shown
    window.search_edit.setText("zzz-no-such-thing")
    assert window.visible_tool_ids() == []
    window.search_edit.setText("")
    assert set(window.visible_tool_ids()) >= TOOL_IDS
    # Matching is on the id too, ignoring case.
    window.search_edit.setText("EMPTY_FOLD")
    assert window.visible_tool_ids() == ["empty_folders"]


def test_sidebar_click_opens_the_page(window: MainWindow) -> None:
    item = window._tool_items["rename"]  # pyright: ignore[reportPrivateUsage]
    window.sidebar.setCurrentItem(item)
    assert window.stack.currentWidget() is window.tool_pages["rename"]


def test_window_close_shuts_down_pages(qtbot: QtBot, window: MainWindow) -> None:
    page = window.tool_page("empty_folders")
    window.show()
    qtbot.waitExposed(window)
    window.close()
    assert page.worker is None


# ----- main() ---------------------------------------------------------------------------------


MAIN_SCRIPT = """
import json, sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from various_tools.gui.app import main
from various_tools.gui.main_window import MainWindow

app = QApplication(sys.argv[:1])  # main() reuses an existing QApplication
seen = {}

def finish():
    tops = QApplication.topLevelWidgets()
    windows = [w for w in tops if isinstance(w, MainWindow) and w.isVisible()]
    seen["count"] = len(windows)
    for w in windows:
        seen["welcome"] = w.stack.currentWidget() is w.welcome
        page = w.tool_pages.get("empty_folders")
        seen["roots"] = [str(r) for r in page.form.raw_values()["roots"]] if page else None
        w.close()
    app.quit()

QTimer.singleShot(500, finish)
code = main(sys.argv[1:])
print("RESULT " + json.dumps({"code": code, **seen}))
"""


def _run_main(args: list[str]) -> dict[str, object]:
    """Run ``main()`` in its own process: it starts and stops a real event loop, which must not
    leak into the other tests. A timeout means main() did not exit."""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    done = subprocess.run(
        [sys.executable, "-I", "-c", MAIN_SCRIPT, *args],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    line = next(line for line in done.stdout.splitlines() if line.startswith("RESULT "))
    return json.loads(line.removeprefix("RESULT "))


def test_main_starts_and_exits_cleanly(write_settings: Callable[..., Path], tmp_path: Path) -> None:
    config = write_settings()
    folder = tmp_path / "start"
    folder.mkdir()
    result = _run_main(
        ["--config", str(config), "--tool", "empty_folders", "--folder", str(folder)]
    )
    assert result["code"] == 0
    assert result["count"] == 1
    assert result["roots"] == [str(folder)]


def test_main_without_tool_shows_the_welcome_page(write_settings: Callable[..., Path]) -> None:
    result = _run_main(["--config", str(write_settings(theme="dark"))])
    assert result["code"] == 0
    assert result["welcome"] is True


# ----- theme ----------------------------------------------------------------------------------


@pytest.fixture
def restore_theme() -> Iterator[None]:
    app = _qapp()
    palette = QPalette(app.palette())
    style = app.style().objectName()
    yield
    app.setPalette(palette)
    app.setStyle(style)
    app.setProperty("variousToolsFollowSystem", False)


def _window_colour() -> QColor:
    return _qapp().palette().color(QPalette.ColorRole.Window)


@pytest.mark.usefixtures("restore_theme")
def test_apply_theme_dark_and_light() -> None:
    app = _qapp()
    assert theme.apply_theme(app, "dark") == "dark"
    assert _window_colour().lightness() < 100
    assert theme.apply_theme(app, "light") == "light"
    assert _window_colour().lightness() > 150
    assert app.style().objectName().lower() == "fusion"


@pytest.mark.usefixtures("restore_theme")
def test_apply_theme_system_and_unknown_follow_the_system() -> None:
    app = _qapp()
    expected = "dark" if app.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"
    assert theme.apply_theme(app, "system") == expected
    assert theme.apply_theme(app, "no-such-mode") == expected
    assert theme.MODES == ("system", "light", "dark")


@pytest.mark.usefixtures("restore_theme")
def test_apply_theme_can_run_repeatedly() -> None:
    app = _qapp()
    for mode in ("dark", "light", "system", "dark"):
        theme.apply_theme(app, mode)
    assert theme.apply_theme(app, "dark") == "dark"


# ----- Explorer menu layout (fake registry only) ----------------------------------------------


def test_shell_install_layout(registry: shell.FakeRegistry) -> None:
    shell.install(registry)
    assert shell.is_installed(registry)
    for menu_key in shell.MENU_KEYS:
        assert registry.get_value(menu_key, "MUIVerb") == shell.MENU_LABEL
        assert registry.get_value(menu_key, "SubCommands") == ""
        for tool_id, label in shell.SHELL_TOOLS:
            tool_key = f"{menu_key}\\shell\\{tool_id}"
            assert registry.get_value(tool_key, "MUIVerb") == label
            command = registry.get_value(f"{tool_key}\\command", "")
            assert command is not None
            assert f"--tool {tool_id}" in command
            assert command.endswith('--folder "%V\\."')
    # Only the per-user branch is ever written.
    assert all(w[1].lower().startswith("software\\classes\\") for w in registry.writes)


def test_shell_shell_tool_ids_are_registered_tools() -> None:
    assert {tool_id for tool_id, _ in shell.SHELL_TOOLS} <= set(discover())


def test_shell_install_twice_is_clean_and_uninstall_removes(registry: shell.FakeRegistry) -> None:
    shell.install(registry)
    first = {k: dict(v) for k, v in registry.keys.items()}
    shell.install(registry)
    assert registry.keys == first
    shell.uninstall(registry)
    assert not shell.is_installed(registry)
    assert not any(registry.has_key(key) for key in shell.MENU_KEYS)
    shell.uninstall(registry)  # nothing there: no error


def test_shell_set_enabled(registry: shell.FakeRegistry) -> None:
    shell.set_enabled(True, registry)
    assert shell.is_installed(registry)
    shell.set_enabled(False, registry)
    assert not shell.is_installed(registry)


def test_shell_is_installed_needs_both_places(registry: shell.FakeRegistry) -> None:
    registry.set_value(shell.MENU_KEY, "MUIVerb", "x")
    assert not shell.is_installed(registry)


def test_launch_command_from_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    python.write_text("")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(python))
    command = shell.launch_command("duplicates")
    assert command == f'"{python}" -m various_tools.gui --tool duplicates --folder "%V\\."'
    windowless = tmp_path / "pythonw.exe"
    windowless.write_text("")
    assert shell.launch_command("duplicates").startswith(f'"{windowless}"')


def test_launch_command_when_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "VariousTools.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert shell.launch_command("empty_folders") == (
        f'"{exe}" --tool empty_folders --folder "%V\\."'
    )
    registry = shell.FakeRegistry()
    shell.install(registry)
    assert registry.get_value(shell.MENU_KEY, "Icon") == str(exe)


def _windows_argv(command_line: str) -> list[str]:
    """Split a command line exactly as Windows does for a program (CommandLineToArgvW)."""
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32")
    kernel32 = ctypes.WinDLL("kernel32")
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    count = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(count))
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


@pytest.mark.skipif(sys.platform != "win32", reason="Explorer command lines are Windows-only")
@pytest.mark.parametrize(
    ("clicked", "expected"),
    [
        ("D:\\", "D:\\"),
        ("C:\\Users\\me\\My Photos", "C:\\Users\\me\\My Photos"),
        ("\\\\server\\share\\Some Folder", "\\\\server\\share\\Some Folder"),
    ],
)
def test_menu_command_passes_drive_roots_and_folders_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clicked: str, expected: str
) -> None:
    exe = tmp_path / "Various Tools.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    command = shell.launch_command("empty_folders").replace("%V", clicked)  # what Explorer does
    argv = _windows_argv(command)
    assert argv[0] == str(exe)
    args, rest = gui_app.parse_args(argv[1:])
    assert rest == [] and args.tool == "empty_folders"
    assert shell.clean_folder_arg(args.folder) == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Explorer command lines are Windows-only")
def test_an_old_menu_entry_for_a_drive_root_still_opens_the_root() -> None:
    # Older entries used "%V": for D:\ the backslash escaped the quote and D:" arrived.
    argv = _windows_argv('"app.exe" --tool duplicates --folder "D:\\"')
    args, _rest = gui_app.parse_args(argv[1:])
    assert args.folder == 'D:"'
    assert shell.clean_folder_arg(args.folder) == "D:\\"


def test_clean_folder_arg() -> None:
    assert shell.clean_folder_arg("D:") == "D:\\"
    assert shell.clean_folder_arg(' "E:\\" ') == "E:\\"
    assert shell.clean_folder_arg("C:\\Data\\.") == "C:\\Data"
    assert shell.clean_folder_arg("") == ""


def test_make_window_cleans_the_folder_argument(
    gui_state: AppState, registry: shell.FakeRegistry, qtbot: QtBot, tmp_path: Path
) -> None:
    folder = tmp_path / "start"
    folder.mkdir()
    window = gui_app.make_window(
        gui_state, tool="project_export", folder=f"{folder}\\.", shell_backend=registry
    )
    qtbot.addWidget(window)
    assert str(window.tool_pages["project_export"].form.raw_values()["root"]) == str(folder)


@pytest.mark.skipif(sys.platform != "win32", reason="the real backend refuses keys on win32 only")
def test_real_backend_refuses_keys_outside_software_classes() -> None:
    backend = shell.WinRegBackend()
    # The key check runs before any registry call, so these never reach the registry.
    for bad in ("Software\\Microsoft\\Windows\\x", "Environment", "Software\\Classes\\x\\"):
        with pytest.raises(OSError, match="Refusing"):
            backend.set_value(bad, "", "v")
        with pytest.raises(OSError, match="Refusing"):
            backend.delete_tree(bad)
        with pytest.raises(OSError, match="Refusing"):
            backend.get_value(bad, "")


# ----- tool discovery -------------------------------------------------------------------------


def test_builtin_entry_points_find_all_six_tools() -> None:
    entries = base._builtin_entry_points()  # pyright: ignore[reportPrivateUsage]
    assert {e.name for e in entries} == TOOL_IDS
    for entry in entries:
        assert entry.group == base.ENTRY_POINT_GROUP
        tool = base._load(entry)  # pyright: ignore[reportPrivateUsage]
        assert tool is not None
        assert tool.id == entry.name


def test_discover_matches_the_builtin_tools() -> None:
    assert set(discover()) == TOOL_IDS


def test_appstate_loads_tools_and_settings(gui_state: AppState) -> None:
    assert set(gui_state.tools) == TOOL_IDS
    assert gui_state.settings.ollama_probe_timeout_s == 0.2
    assert gui_state.settings_path.name == "settings.toml"
