"""Tool-page features keyed on optional tool attributes, never on a tool id.

* ``plan_view = "tree"`` opens the plan in the folder tree;
* ``size_tree(plan)`` adds a "Size map" tab; clicking selects rows, "Tick files" checks them;
* ``extras`` adds one button per extra, run in a worker on a file the person picks;
* ``Plan.cloud_only`` shows "Download these N files…", which asks first (AGENTS.md rule 3);
* a ``choices_from`` option is filled from ``core/choices`` in a worker.

Every tool here is a fake built in the test; trees live under ``tmp_path``. ``force_download`` is
replaced, so nothing ever talks to OneDrive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pytest
from pydantic import Field
from pytestqt.qtbot import QtBot

from various_tools.core import choices as choices_module
from various_tools.core import onedrive as onedrive_module
from various_tools.core.context import RunContext
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult, apply_plan
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.gui.widgets.plan_table import FOLDER_TREE, GROUPED
from various_tools.gui.widgets.schema_form import DynamicChoiceEdit
from various_tools.tools.base import Category, ToolOptions
from various_tools.tools.disk_usage import SizeNode

if TYPE_CHECKING:
    from conftest import Dialogs


class FakeOptions(ToolOptions):
    roots: list[Path] = Field(
        default_factory=list,
        title="Folders",
        description="x",
        json_schema_extra={"widget": "folders"},
    )
    model: str = Field(
        default="m:1", title="Model", description="x", json_schema_extra={"choices_from": "fake"}
    )


@dataclass
class FakeExtra:
    id: str
    label: str
    description: str
    path_kind: Literal["save_file", "open_file"]
    file_filter: str
    run: Callable[[Path, RunContext], str]


@dataclass
class FakeTool:
    """A tool whose plan trashes every file under the chosen folder."""

    id: str = "fake_capabilities"
    name: str = "Fake"
    description: str = "A fake tool."
    category: Category = "Clean up"
    Options: type[ToolOptions] = FakeOptions
    cloud_only: list[Path] = field(default_factory=list[Path])

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan:
        assert isinstance(opts, FakeOptions)
        plan = Plan(tool=self.id, roots=list(opts.roots), cloud_only=list(self.cloud_only))
        for root in opts.roots:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    plan.actions.append(
                        PlannedAction(ActionKind.TRASH, path, reason="fake", selected=False)
                    )
        return plan

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult:
        return apply_plan(plan, ctx)


def make_page(qtbot: QtBot, state: AppState, dialogs: Dialogs, tool: Any, root: Path) -> ToolPage:
    page = ToolPage(state, tool)
    qtbot.addWidget(page)
    dialogs.attach(page)
    page.form.set_values({"roots": [str(root)]})
    return page


def fake_tree(root: Path) -> Path:
    (root / "big" / "inner").mkdir(parents=True)
    (root / "small").mkdir()
    (root / "big" / "a.bin").write_bytes(b"a" * 600)
    (root / "big" / "inner" / "b.bin").write_bytes(b"b" * 300)
    (root / "small" / "c.bin").write_bytes(b"c" * 100)
    return root


# ----- plan view ---------------------------------------------------------------------------------


def test_plan_view_attribute_picks_the_tree(
    qtbot: QtBot, gui_state: AppState, dialogs: Dialogs, tmp_path: Path
) -> None:
    plain = make_page(qtbot, gui_state, dialogs, FakeTool(), tmp_path)
    assert plain.table.view_mode() == GROUPED
    tree_tool = FakeTool()
    tree_tool.plan_view = "tree"  # type: ignore[attr-defined]
    tree = make_page(qtbot, gui_state, dialogs, tree_tool, tmp_path)
    assert tree.table.view_mode() == FOLDER_TREE


def test_empty_folders_opens_in_the_tree_and_keeps_the_hook(
    open_tool: Callable[..., ToolPage],
    scan: Callable[[ToolPage], None],
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    page = open_tool("empty_folders", str(root))
    assert page.table.view_mode() == FOLDER_TREE
    scan(page)
    assert page.plan is not None
    b = next(i for i, a in enumerate(page.plan.actions) if a.src and a.src.name == "b")
    page.table.set_action_checked(b, False)
    assert {a.src.name: a.selected for a in page.plan.actions if a.src} == {"a": False, "b": False}
    page.table.set_view_mode(GROUPED)  # the person can switch
    assert page.table.view_mode() == GROUPED


# ----- size map ----------------------------------------------------------------------------------


def size_tool() -> FakeTool:
    tool = FakeTool()

    def size_tree(plan: Plan) -> SizeNode:
        root = plan.roots[0]
        inner = SizeNode("inner", root / "big" / "inner", 300, 2)
        big = SizeNode("big", root / "big", 900, 1, (inner,))
        small = SizeNode("small", root / "small", 100, 1)
        return SizeNode(root.name, root, 1000, 0, (big, small))

    tool.size_tree = size_tree  # type: ignore[attr-defined]
    return tool


def test_size_map_tab_only_for_tools_with_a_size_tree(
    qtbot: QtBot, gui_state: AppState, dialogs: Dialogs, tmp_path: Path
) -> None:
    assert make_page(qtbot, gui_state, dialogs, FakeTool(), tmp_path).size_map is None
    page = make_page(qtbot, gui_state, dialogs, size_tool(), tmp_path)
    assert page.size_map is not None
    tabs = page.results_tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Plan", "Size map"]


def test_size_map_selects_rows_ticks_files_and_apply_trashes_them(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    trashed: list[str],
    tmp_path: Path,
    scan: Callable[[ToolPage], None],
    apply: Callable[[ToolPage], None],
) -> None:
    root = fake_tree(tmp_path / "root")
    page = make_page(qtbot, gui_state, dialogs, size_tool(), root)
    page.resize(1000, 800)
    page.show()
    scan(page)
    view = page.size_map
    assert view is not None and page.plan is not None
    assert [t.name for t in view.canvas.tiles()] == ["big", "small"]
    assert not any(a.selected for a in page.plan.actions)

    view.canvas.choose(0)  # "big": zoom in, select its rows in the plan table
    assert view.current_path() == root / "big"
    selected = {page.plan.actions[i].src.name for i in page.table.selected_action_indices()}  # type: ignore[union-attr]
    assert selected == {"a.bin", "b.bin"}
    assert not any(a.selected for a in page.plan.actions)  # selecting is not ticking

    view.tick_button.click()
    ticked = {a.src.name for a in page.plan.actions if a.selected and a.src}
    assert ticked == {"a.bin", "b.bin"}
    assert page.apply_button.isEnabled()
    assert trashed == []  # nothing happens before Apply

    apply(page)
    assert "trash: 2" in dialogs.confirms[-1][1]  # the normal confirmation
    assert sorted(Path(p).name for p in trashed) == ["a.bin", "b.bin"]
    assert (root / "small" / "c.bin").exists()
    assert page.tick_folder(root) == 0  # applied: nothing more to tick


def test_disk_usage_page_has_the_size_map(
    open_tool: Callable[..., ToolPage],
    scan: Callable[[ToolPage], None],
    tmp_path: Path,
) -> None:
    root = fake_tree(tmp_path / "usage")
    page = open_tool("disk_usage", str(root))
    assert page.size_map is not None
    page.form.update_values({"min_size_mb": 0, "tree_min_share_pct": 0})
    scan(page)
    assert page.size_map.canvas.root() is not None
    names = {t.name for t in page.size_map.canvas.tiles()}
    assert {"big", "small"} <= names
    assert page.tick_folder(root / "small") == 1
    assert [a.src.name for a in page.plan.actions if a.selected and a.src] == ["c.bin"]  # type: ignore[union-attr]


# ----- tool extras -------------------------------------------------------------------------------


def extra_tool(calls: list[tuple[Path, object]], error: str | None = None) -> FakeTool:
    tool = FakeTool()

    def run(path: Path, ctx: RunContext) -> str:
        calls.append((path, ctx))
        if error:
            raise ValueError(error)
        return f"Wrote {path.name}"

    tool.extras = (  # type: ignore[attr-defined]
        FakeExtra("catalog-export", "Export catalog", "Save it.", "save_file", "CSV (*.csv)", run),
    )
    return tool


def test_extra_button_runs_in_a_worker_and_shows_the_result(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    tmp_path: Path,
    wait_idle: Callable[[ToolPage], None],
) -> None:
    calls: list[tuple[Path, object]] = []
    page = make_page(qtbot, gui_state, dialogs, extra_tool(calls), tmp_path)
    button = page.extra_buttons["catalog-export"]
    assert button.text() == "Export catalog…" and button.toolTip() == "Save it."

    dialogs.extra_path = None  # the dialog was cancelled: nothing runs
    button.click()
    assert dialogs.extras_asked == ["catalog-export"] and calls == []

    dialogs.extra_path = str(tmp_path / "out.csv")
    button.click()
    wait_idle(page)
    assert [c[0] for c in calls] == [tmp_path / "out.csv"]
    assert isinstance(calls[0][1], RunContext)
    assert dialogs.informs[-1] == ("Export catalog", "Wrote out.csv")
    assert "Wrote out.csv" in page.status_label.text()


def test_extra_value_error_is_shown_as_a_plain_message(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    tmp_path: Path,
    wait_idle: Callable[[ToolPage], None],
) -> None:
    calls: list[tuple[Path, object]] = []
    page = make_page(
        qtbot, gui_state, dialogs, extra_tool(calls, "That is not a catalog."), tmp_path
    )
    assert page.run_extra(page.extras[0], str(tmp_path / "x.csv")) is not None
    wait_idle(page)
    qtbot.waitUntil(lambda: bool(dialogs.informs), timeout=5000)
    assert dialogs.informs[-1] == ("Export catalog", "That is not a catalog.")
    assert "That is not a catalog." in page.status_label.text()


def test_a_tool_without_extras_has_no_extra_buttons(
    qtbot: QtBot, gui_state: AppState, dialogs: Dialogs, tmp_path: Path
) -> None:
    assert make_page(qtbot, gui_state, dialogs, FakeTool(), tmp_path).extra_buttons == {}


# ----- cloud-only files ---------------------------------------------------------------------------


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int | None]]:
    calls: list[tuple[Path, int | None]] = []

    def fake(path: Path, wait_seconds: int | None = None) -> tuple[bool, str]:
        calls.append((path, wait_seconds))
        return (path.name != "bad.jpg", "downloaded" if path.name != "bad.jpg" else "timed out")

    monkeypatch.setattr(onedrive_module, "force_download", fake)
    return calls


def test_cloud_only_banner_asks_before_downloading(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    downloads: list[tuple[Path, int | None]],
    tmp_path: Path,
    scan: Callable[[ToolPage], None],
    wait_idle: Callable[[ToolPage], None],
) -> None:
    tool = FakeTool(cloud_only=[tmp_path / "a.jpg", tmp_path / "bad.jpg"])
    page = make_page(qtbot, gui_state, dialogs, tool, tmp_path)
    page.show()
    assert not page.cloud_banner.isVisible()
    scan(page)
    assert page.cloud_banner.isVisible()
    assert "2 files are cloud-only and were skipped" in page.cloud_label.text()
    assert page.download_button.text() == "Download these 2 files…"

    dialogs.answers = [False]
    page.download_button.click()
    assert downloads == []  # no download without the confirmation
    assert "2" in dialogs.confirms[-1][1]

    dialogs.answers = [True, False]  # download, then do not scan again
    worker = page.download_cloud_only()
    assert worker is not None
    wait_idle(page)
    assert [p.name for p, _w in downloads] == ["a.jpg", "bad.jpg"]
    assert "Downloaded 1 of 2 file(s)" in page.status_label.text()
    assert "Scan again" in dialogs.confirms[-1][1]


def test_download_then_scan_again(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    downloads: list[tuple[Path, int | None]],
    tmp_path: Path,
    scan: Callable[[ToolPage], None],
) -> None:
    tool = FakeTool(cloud_only=[tmp_path / "a.jpg"])
    page = make_page(qtbot, gui_state, dialogs, tool, tmp_path)
    scan(page)
    first = page.plan
    dialogs.answers = [True, True]
    assert page.download_cloud_only() is not None
    qtbot.waitUntil(lambda: page.plan is not first and page.worker is None, timeout=10000)
    assert len(downloads) == 1


def test_download_can_be_cancelled(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scan: Callable[[ToolPage], None],
    wait_idle: Callable[[ToolPage], None],
) -> None:
    calls: list[Path] = []
    page_ref: list[ToolPage] = []

    def slow(path: Path, wait_seconds: int | None = None) -> tuple[bool, str]:
        calls.append(path)
        page_ref[0].worker.cancel()  # type: ignore[union-attr]  # the person presses Cancel
        return True, "downloaded"

    monkeypatch.setattr(onedrive_module, "force_download", slow)
    tool = FakeTool(cloud_only=[tmp_path / f"{n}.jpg" for n in range(5)])
    page = make_page(qtbot, gui_state, dialogs, tool, tmp_path)
    page_ref.append(page)
    scan(page)
    dialogs.answers = [True]
    page.download_cloud_only()
    wait_idle(page)
    assert len(calls) == 1
    assert "Cancelled" in page.status_label.text()


# ----- run-time choices ---------------------------------------------------------------------------


def test_choices_from_field_is_filled_by_a_worker(
    qtbot: QtBot,
    gui_state: AppState,
    dialogs: Dialogs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[object] = []

    def source(ctx: RunContext) -> list[str]:
        seen.append(ctx)
        return ["first:1", "second:2"]

    monkeypatch.setitem(choices_module._SOURCES, "fake", source)  # pyright: ignore[reportPrivateUsage]
    page = make_page(qtbot, gui_state, dialogs, FakeTool(), tmp_path)
    page.show()
    edit = page.form.editor("model")
    assert isinstance(edit, DynamicChoiceEdit)
    qtbot.waitUntil(lambda: edit.combo.count() == 2, timeout=5000)
    assert isinstance(seen[0], RunContext)
    assert edit.text() == "m:1"
