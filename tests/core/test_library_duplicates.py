"""The saved Duplicate Finder groups, matched to library items (stale entries ignored)."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from various_tools.core.media.library import MediaItem
from various_tools.core.media.library_duplicates import (
    LibraryDuplicates,
    duplicates_path,
    path_key,
)
from various_tools.core.plan import ActionKind, Plan, PlannedAction


def make(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def item_of(path: Path) -> MediaItem:
    st = path.stat()
    return MediaItem(
        path, "photo", st.st_size, st.st_mtime, datetime(2024, 1, 1), "camera",
        None, None, None, None, False,
    )  # fmt: skip


def plan_for(keeper: Path, dups: list[Path], tool: str = "duplicates") -> Plan:
    plan = Plan(tool=tool, roots=[keeper.parent])
    for d in dups:
        plan.actions.append(
            PlannedAction(ActionKind.TRASH, src=d, reason="same", group_id=str(keeper))
        )
    return plan


def test_groups_are_saved_and_reloaded(tmp_path: Path) -> None:
    a, b, c = (make(tmp_path / n, n.encode()) for n in ("a.jpg", "b.jpg", "c.jpg"))
    store = duplicates_path(tmp_path / "cache")
    dups = LibraryDuplicates(store)
    assert dups.update_from_plan(plan_for(a, [b, c])) == 1
    assert store.is_file()

    again = LibraryDuplicates(store)
    assert again.saved == dups.saved != ""
    items = {path_key(p): item_of(p) for p in (a, b, c)}
    index = again.index_for(items.get)
    assert len(index) == 1
    assert index.has(items[path_key(a)])
    assert {m.path.name for m in index.copies_of(items[path_key(b)])} == {"a.jpg", "c.jpg"}


def _plan_in(root: Path, keeper: Path, dups: list[Path]) -> Plan:
    plan = plan_for(keeper, dups)
    plan.roots = [root]
    return plan


def test_a_scan_outside_the_library_changes_nothing(tmp_path: Path) -> None:
    lib, other = tmp_path / "lib", tmp_path / "other"
    a, b = make(lib / "a.jpg", b"a"), make(lib / "b.jpg", b"b")
    x, y = make(other / "x.jpg", b"x"), make(other / "y.jpg", b"y")
    dups = LibraryDuplicates(duplicates_path(tmp_path / "cache"))
    assert dups.update_from_plan(_plan_in(lib, a, [b]), [lib]) == 1
    assert dups.update_from_plan(_plan_in(other, x, [y]), [lib]) == 0
    assert dups.changed is False
    assert [[Path(m.path).name for m in g] for g in dups.groups] == [["a.jpg", "b.jpg"]]
    assert [[Path(m.path).name for m in g] for g in LibraryDuplicates(dups.path).groups] == [
        ["a.jpg", "b.jpg"]
    ]


def test_a_scan_replaces_only_the_groups_under_its_roots(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    a, b = make(one / "a.jpg", b"a"), make(one / "b.jpg", b"b")
    c, d = make(two / "c.jpg", b"c"), make(two / "d.jpg", b"d")
    dups = LibraryDuplicates(duplicates_path(tmp_path / "cache"))
    libs = [one, two]
    dups.update_from_plan(_plan_in(one, a, [b]), libs)
    dups.update_from_plan(_plan_in(two, c, [d]), libs)
    assert len(dups.groups) == 2 and dups.changed is True
    dups.update_from_plan(_plan_in(two, c, [d]), libs)  # the same answer again
    assert len(dups.groups) == 2 and dups.changed is False
    empty = Plan(tool="duplicates", roots=[one])  # `one` has no duplicates any more
    dups.update_from_plan(empty, libs)
    assert [[Path(m.path).name for m in g] for g in dups.groups] == [["c.jpg", "d.jpg"]]
    assert dups.changed is True


def test_other_tools_plans_are_ignored(tmp_path: Path) -> None:
    a, b = make(tmp_path / "a.jpg", b"a"), make(tmp_path / "b.jpg", b"b")
    dups = LibraryDuplicates(duplicates_path(tmp_path / "cache"))
    assert dups.update_from_plan(plan_for(a, [b], tool="empty_folders")) == 0
    assert dups.groups == []


def test_changed_files_are_ignored(tmp_path: Path) -> None:
    a, b, c = (make(tmp_path / n, n.encode()) for n in ("a.jpg", "b.jpg", "c.jpg"))
    dups = LibraryDuplicates(duplicates_path(tmp_path / "cache"))
    dups.update_from_plan(plan_for(a, [b, c]))
    b.write_bytes(b"changed length")  # size changed
    st = c.stat()
    os.utime(c, (st.st_atime, st.st_mtime + 50))  # time changed
    items = {path_key(p): item_of(p) for p in (a, b, c)}
    # b and c are stale: a group of one is no group.
    assert len(dups.index_for(items.get)) == 0
    # Only the stale one drops out of a bigger group.
    d = make(tmp_path / "d.jpg", b"d")
    dups.update_from_plan(plan_for(a, [d, b]))
    b.write_bytes(b"changed again, longer")
    items = {path_key(p): item_of(p) for p in (a, b, d)}
    index = dups.index_for(items.get)
    assert [m.path.name for m in index.groups[0]] == ["a.jpg", "d.jpg"]
    assert not index.has(items[path_key(b)])


def test_members_missing_from_the_library_are_ignored(tmp_path: Path) -> None:
    a, b = make(tmp_path / "a.jpg", b"a"), make(tmp_path / "b.jpg", b"bb")
    dups = LibraryDuplicates(duplicates_path(tmp_path / "cache"))
    dups.update_from_plan(plan_for(a, [b]))
    assert len(dups.index_for({path_key(a): item_of(a)}.get)) == 0


def test_a_broken_file_is_an_empty_result(tmp_path: Path) -> None:
    store = duplicates_path(tmp_path)
    store.write_text("{not json", encoding="utf-8")
    assert LibraryDuplicates(store).groups == []
