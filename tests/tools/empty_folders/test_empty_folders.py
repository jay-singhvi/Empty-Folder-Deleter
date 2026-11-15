"""Empty Folders: parity with the original app, the §2.1 bug fixes, and the safety rules.

Every tree is generated under ``tmp_path``. Trash calls go to a fake that deletes for real inside
the temp folder, so nothing reaches the Recycle Bin and no user folder is touched.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from various_tools.core import plan as plan_module
from various_tools.core.context import Cancelled, RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.plan import Plan
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.empty_folders import (
    TOOL,
    EmptyFolders,
    EmptyFoldersOptions,
    set_folder_selected,
)

# The original Tkinter app, kept as a test fixture. Its find function is read from the source
# and run on its own, so tkinter is never imported.
LEGACY_APP = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "legacy_reference"
    / "empty_folder_deleter.py"
)
NO_EXTRAS: dict[str, object] = {
    "treat_as_empty": [],
    "exclude_globs": [],
    "skip_hidden_system": False,
}


@pytest.fixture
def sent_to_trash(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Trash calls are recorded and then carried out inside the temp folder only."""
    calls: list[str] = []

    def fake_send2trash(path: str) -> None:
        calls.append(path)
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.remove(path)

    monkeypatch.setattr(plan_module, "send2trash", fake_send2trash)
    return calls


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def _ctx(tmp_path: Path, journal: Journal | None, **settings: object) -> RunContext:
    base: dict[str, object] = {"onedrive_root": tmp_path}
    base.update(settings)
    return RunContext(
        settings=Settings.model_validate(base),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


def _opts(**fields: object) -> EmptyFoldersOptions:
    return EmptyFoldersOptions.model_validate(fields)


def _rel(path: Path | None, root: Path) -> str:
    assert path is not None
    return path.relative_to(root).as_posix()


def _tree_listing(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def _legacy_find_empty_folders() -> Callable[[str], list[str]]:
    """The original ``find_empty_folders`` method, compiled from the legacy source file."""
    source = LEGACY_APP.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(LEGACY_APP))
    method = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "find_empty_folders"
    )
    isolated = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace: dict[str, object] = {"os": os}
    exec(compile(isolated, str(LEGACY_APP), "exec"), namespace)  # noqa: S102 - legacy reference
    find = namespace["find_empty_folders"]
    assert callable(find)
    return lambda root: find(None, root)  # type: ignore[no-any-return]  # the method ignores self


def _legacy_delete_until_none_left(root: Path) -> tuple[set[str], int]:
    """Run the original app's loop: find, delete, rescan, until nothing is found."""
    find = _legacy_find_empty_folders()
    deleted: set[str] = set()
    passes = 0
    while True:
        passes += 1
        found = find(str(root))
        if not found:
            return deleted, passes
        for folder in found:
            os.rmdir(folder)
            deleted.add(Path(folder).relative_to(root).as_posix())


def _parity_tree(base: Path) -> Path:
    """Nested empty folders, a folder kept by a file, and a chain that needs two passes."""
    root = base / "root"
    for rel in ("a", "b/c/d", "b/e", "h/i"):
        (root / rel).mkdir(parents=True)
    (root / "b" / "keep.txt").write_text("kept", encoding="utf-8")
    (root / "f" / "g").mkdir(parents=True)
    (root / "f" / "g" / "x.txt").write_text("x", encoding="utf-8")
    (root / "h" / "j.txt").write_text("j", encoding="utf-8")
    return root


# --- Parity with the original app ------------------------------------------------------------


def test_plan_matches_the_legacy_app_on_the_same_tree(
    tmp_path: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    expected = {"a", "b/c/d", "b/c", "b/e", "h/i"}

    legacy_root = _parity_tree(tmp_path / "legacy")
    legacy_deleted, passes = _legacy_delete_until_none_left(legacy_root)
    assert legacy_deleted == expected
    assert passes >= 2, "the fixture must need more than one legacy pass to reach nested folders"

    new_root = _parity_tree(tmp_path / "new")
    plan = TOOL.scan(_opts(roots=[new_root], **NO_EXTRAS), _ctx(tmp_path, journal))
    assert {_rel(a.src, new_root) for a in plan.actions} == expected

    result = TOOL.apply(plan, _ctx(tmp_path, journal))
    assert result.status == "done"
    assert _tree_listing(new_root) == _tree_listing(legacy_root)


def test_one_scan_finds_nested_empty_folders_deepest_first(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "one" / "two" / "three").mkdir(parents=True)
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, None))
    assert [_rel(a.src, root) for a in plan.actions] == ["one/two/three", "one/two", "one"]


def test_legacy_app_would_delete_an_empty_root_but_the_plan_never_does(tmp_path: Path) -> None:
    root = tmp_path / "empty-root"
    root.mkdir()
    assert _legacy_find_empty_folders()(str(root)) == [str(root)]  # bug 2 in the original app
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("No empty folders" in note for note in plan.notes)


# --- Bug fixes from plan §2.1 ----------------------------------------------------------------


def test_junk_files_count_as_empty_and_go_before_their_folder(
    tmp_path: Path, sent_to_trash: list[str], journal: Journal
) -> None:
    root = tmp_path / "root"
    folder = root / "photos"
    folder.mkdir(parents=True)
    (folder / "Desktop.ini").write_text("x", encoding="utf-8")  # case does not matter
    (folder / "Thumbs.db").write_text("x", encoding="utf-8")
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, journal))

    assert [_rel(a.src, root) for a in plan.actions] == [
        "photos/Desktop.ini",
        "photos/Thumbs.db",
        "photos",
    ]
    assert all(a.kind == "trash" for a in plan.actions)
    assert plan.actions[-1].reason == "empty except for Desktop.ini, Thumbs.db"

    result = TOOL.apply(plan, _ctx(tmp_path, journal))
    assert result.status == "done"
    assert not folder.exists()
    assert len(sent_to_trash) == 3


def test_a_real_file_keeps_its_folder_and_every_parent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "outer" / "inner").mkdir(parents=True)
    (root / "outer" / "inner" / "notes.txt").write_text("keep me", encoding="utf-8")
    (root / "outer" / "Thumbs.db").write_text("junk", encoding="utf-8")
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, None))
    assert plan.actions == []


def test_excluded_entries_count_as_content(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "project" / "node_modules").mkdir(parents=True)
    (root / "project" / ".git").mkdir()
    (root / "elsewhere").mkdir()
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, None))
    assert [_rel(a.src, root) for a in plan.actions] == ["elsewhere"]


def test_hidden_and_system_folders_are_kept_by_default(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("hidden and system attributes are a Windows feature")
    root = tmp_path / "root"
    hidden = root / "parent" / "$hidden"
    hidden.mkdir(parents=True)
    subprocess.run(["attrib", "+h", "+s", str(hidden)], check=True)

    kept = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, None))
    assert kept.actions == []
    assert any("Hidden or system folder kept" in note for note in kept.notes)

    removed = TOOL.scan(_opts(roots=[root], skip_hidden_system=False), _ctx(tmp_path, None))
    assert {_rel(a.src, root) for a in removed.actions} == {"parent/$hidden", "parent"}


def test_min_age_keeps_recent_folders_and_their_parents(tmp_path: Path) -> None:
    root = tmp_path / "root"
    old = root / "old"
    recent = root / "recent" / "inside"
    old.mkdir(parents=True)
    recent.mkdir(parents=True)
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))  # only "old" is backdated; "recent" stays new

    plan = TOOL.scan(_opts(roots=[root], min_age_days=7), _ctx(tmp_path, None))
    assert [_rel(a.src, root) for a in plan.actions] == ["old"]


def test_junctions_are_never_followed_and_count_as_content(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are a Windows feature")
    root = tmp_path / "root"
    outside = tmp_path / "outside-empty"
    outside.mkdir()
    holder = root / "holder"
    holder.mkdir(parents=True)
    link = holder / "link"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False
    )
    if made.returncode != 0:
        pytest.skip("this account cannot create junctions")

    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, None))
    assert plan.actions == []  # the outside folder is not reached, and holder keeps the link
    assert outside.is_dir()


def test_a_folder_that_cannot_be_read_stays_and_is_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    locked = root / "locked"
    locked.mkdir(parents=True)
    real_scandir = os.scandir

    def refusing_scandir(path: object) -> object:
        if str(path).endswith("locked"):
            raise PermissionError(13, "Access is denied")
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", refusing_scandir)
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, None))
    assert plan.actions == []
    assert any("Could not read" in note for note in plan.notes)


def test_a_failed_removal_is_reported_and_the_rest_still_runs(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "bad").mkdir(parents=True)
    (root / "good").mkdir()

    def flaky_trash(path: str) -> None:
        if "bad" in path:
            raise PermissionError(13, "Access is denied")
        os.rmdir(path)

    monkeypatch.setattr(plan_module, "send2trash", flaky_trash)
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, journal))
    result = TOOL.apply(plan, _ctx(tmp_path, journal))
    assert result.status == "partial"
    assert result.count("failed") == 1 and result.count("done") == 1
    assert (root / "bad").is_dir() and not (root / "good").exists()


def test_protected_root_is_refused_before_anything_is_listed(tmp_path: Path) -> None:
    drive = Path(tmp_path.anchor)
    plan = TOOL.scan(_opts(roots=[drive]), _ctx(tmp_path, None))
    assert plan.roots == []
    assert plan.actions == []
    assert any("Protected folder refused" in note for note in plan.notes)


def test_missing_roots_are_noted_and_nested_roots_are_scanned_once(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (inner / "empty").mkdir(parents=True)
    missing = tmp_path / "missing"
    plan = TOOL.scan(_opts(roots=[outer, inner, missing]), _ctx(tmp_path, None))
    assert plan.roots == [outer]
    assert [_rel(a.src, outer) for a in plan.actions] == ["inner/empty", "inner"]
    assert any("Not a folder" in note for note in plan.notes)


# --- Safety rules -----------------------------------------------------------------------------


def test_scan_never_changes_the_disk(tmp_path: Path) -> None:
    root = _parity_tree(tmp_path / "work")
    before = _tree_listing(root)
    TOOL.scan(_opts(roots=[root], **NO_EXTRAS), _ctx(tmp_path, None))
    assert _tree_listing(root) == before


def test_trash_is_the_default_and_permanent_needs_the_setting(
    tmp_path: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    root = tmp_path / "root"
    (root / "gone").mkdir(parents=True)
    default_plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, journal))
    assert default_plan.actions[0].kind == "trash"

    permanent_plan = TOOL.scan(
        _opts(roots=[root], delete_mode="permanent"), _ctx(tmp_path, journal)
    )
    assert permanent_plan.actions[0].kind == "delete"
    refused = TOOL.apply(permanent_plan, _ctx(tmp_path, journal))  # settings still say trash
    assert refused.count("skipped") == 1
    assert "Recycle Bin" in refused.outcomes[0].detail
    assert (root / "gone").is_dir()

    allowed = TOOL.apply(permanent_plan, _ctx(tmp_path, journal, delete_mode="permanent"))
    assert allowed.status == "done"
    assert not (root / "gone").exists()
    assert sent_to_trash == []


def test_unchecking_a_folder_keeps_it_and_its_parents(tmp_path: Path, journal: Journal) -> None:
    root = tmp_path / "root"
    (root / "a" / "b" / "c").mkdir(parents=True)
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, journal))
    assert [_rel(a.src, root) for a in plan.actions] == ["a/b/c", "a/b", "a"]

    set_folder_selected(plan, root / "a" / "b", False)
    assert [a.selected for a in plan.actions] == [True, False, False]

    result = TOOL.apply(plan, _ctx(tmp_path, journal))
    assert result.count("done") == 1
    assert (root / "a" / "b").is_dir() and (root / "a").is_dir()


def test_unchecking_a_folder_also_unchecks_junk_files_in_its_parents(
    tmp_path: Path, sent_to_trash: list[str], journal: Journal
) -> None:
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "Thumbs.db").write_text("x", encoding="utf-8")
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, journal))
    assert [_rel(a.src, root) for a in plan.actions] == ["a/b", "a/Thumbs.db", "a"]

    set_folder_selected(plan, root / "a" / "b", False)
    assert [a.selected for a in plan.actions] == [False, False, False]

    result = TOOL.apply(plan, _ctx(tmp_path, journal))
    assert result.outcomes == []
    assert (root / "a" / "Thumbs.db").exists() and not sent_to_trash


def test_a_file_added_after_the_scan_keeps_the_junk_file_and_is_journaled_skipped(
    tmp_path: Path, sent_to_trash: list[str], journal: Journal
) -> None:
    root = tmp_path / "root"
    folder = root / "photos"
    folder.mkdir(parents=True)
    (folder / "Thumbs.db").write_text("x", encoding="utf-8")
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, journal))
    (folder / "new.txt").write_text("appeared", encoding="utf-8")

    result = TOOL.apply(plan, _ctx(tmp_path, journal))
    assert (folder / "Thumbs.db").exists() and (folder / "new.txt").exists()
    assert not sent_to_trash
    assert result.count("done") == 0
    junk = [o for o in result.outcomes if o.src == str(folder / "Thumbs.db")]
    assert [o.status for o in junk] == ["skipped"]
    assert "no longer empty" in junk[0].detail
    statuses = [a.status for a in journal.actions(result.run_id)]
    assert statuses.count("skipped") == 2  # the junk file, and the folder the engine refused


def test_a_file_added_deep_in_a_planned_tree_keeps_the_junk_files_above_it(
    tmp_path: Path, sent_to_trash: list[str], journal: Journal
) -> None:
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "Thumbs.db").write_text("x", encoding="utf-8")
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, journal))
    (root / "a" / "b" / "new.txt").write_text("appeared", encoding="utf-8")

    TOOL.apply(plan, _ctx(tmp_path, journal))
    assert (root / "a" / "Thumbs.db").exists()
    assert not sent_to_trash


def test_a_parent_selected_by_hand_is_still_refused_while_its_child_remains(
    tmp_path: Path, journal: Journal
) -> None:
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, journal))
    set_folder_selected(plan, root / "a" / "b", False)
    plan.actions[-1].selected = True  # a plan edited by hand, so a is checked again

    result = TOOL.apply(plan, _ctx(tmp_path, journal))
    assert result.count("skipped") == 1
    assert "not empty" in result.outcomes[0].detail
    assert (root / "a").is_dir()


def test_plan_json_round_trip_keeps_the_selection(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, None))
    set_folder_selected(plan, root / "a", False)
    restored = Plan.from_json(plan.to_json())
    assert [a.selected for a in restored.actions] == [a.selected for a in plan.actions]
    assert restored.tool == "empty_folders"


def test_groups_point_at_the_top_of_each_empty_tree(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "top" / "mid" / "leaf").mkdir(parents=True)
    (root / "other").mkdir()
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, None))
    groups = {_rel(a.src, root): a.group_id for a in plan.actions}
    assert groups["top/mid/leaf"] == str(root / "top")
    assert groups["top"] == str(root / "top")
    assert groups["other"] == str(root / "other")


def test_cancelling_stops_the_scan(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    ctx = _ctx(tmp_path, None)
    ctx.cancel()
    with pytest.raises(Cancelled):
        TOOL.scan(_opts(roots=[root]), ctx)


# --- Options and registration -----------------------------------------------------------------


def test_options_have_the_planned_defaults_and_reject_unknown_fields() -> None:
    defaults = EmptyFoldersOptions.model_validate({})
    assert defaults.treat_as_empty == ["desktop.ini", "Thumbs.db", ".DS_Store"]
    assert defaults.exclude_globs == [".git", "node_modules"]
    assert defaults.min_age_days == 0
    assert defaults.skip_hidden_system is True
    assert defaults.delete_mode == "trash"
    with pytest.raises(ValidationError):
        EmptyFoldersOptions.model_validate({"bogus": 1})


def test_the_tool_is_found_through_the_entry_point_group() -> None:
    tools = discover()
    assert "empty_folders" in tools
    assert isinstance(tools["empty_folders"], EmptyFolders)
    assert tools["empty_folders"].Options is EmptyFoldersOptions
