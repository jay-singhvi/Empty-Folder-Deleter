"""Disk usage: filters, the size notes, safety rules and apply through the journal.

Every tree is generated under ``tmp_path``. Trash calls go to a fake that deletes inside the temp
folder, so nothing reaches the Recycle Bin and no user folder is touched.
"""

from __future__ import annotations

import builtins
import ctypes
import io
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.plan import ActionKind, Plan
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.disk_usage import (
    FOLDER_SIZE_PREFIX,
    ROOT_SIZE_PREFIX,
    TOOL,
    DiskUsageOptions,
    format_size,
    parse_size_note,
)
from various_tools.tools.disk_usage import tool as du_tool

OFFLINE = 0x1000
DAY = 86400
KB = 1024
MB = 1024 * 1024


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


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "root"
    folder.mkdir()
    return folder


def _ctx(tmp_path: Path, journal: Journal | None = None, **settings: Any) -> RunContext:
    values: dict[str, Any] = {"onedrive_root": tmp_path, **settings}
    return RunContext(
        settings=Settings.model_validate(values),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


def _opts(root: Path, **fields: Any) -> DiskUsageOptions:
    return DiskUsageOptions.model_validate({"roots": [root], **fields})


def _write(path: Path, size: int, age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_days:
        when = time.time() - age_days * DAY
        os.utime(path, (when, when))
    return path


def _rel(path: Path | None, base: Path) -> str:
    assert path is not None
    return path.relative_to(base).as_posix()


def _files(plan: Plan, base: Path) -> list[str]:
    return [_rel(a.src, base) for a in plan.actions]


def _folder_notes(plan: Plan, base: Path) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    for note in plan.notes:
        parsed = parse_size_note(note)
        if parsed is not None and parsed[0] == "folder-size":
            result.append((_rel(parsed[2], base), parsed[1]))
    return result


def _snapshot(base: Path) -> dict[str, tuple[bool, int, int]]:
    result: dict[str, tuple[bool, int, int]] = {}
    for path in [base, *base.rglob("*")]:
        st = path.stat()
        result[str(path.relative_to(base))] = (path.is_dir(), st.st_size, st.st_mtime_ns)
    return result


def _set_hidden(path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("hidden attributes are a Windows feature")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    assert kernel32.SetFileAttributesW(str(path), 0x2)


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are a Windows feature")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction in this environment")


def _sample_tree(root: Path) -> None:
    _write(root / "big.mkv", 5000, age_days=400)
    _write(root / "docs" / "report.pdf", 3000, age_days=10)
    _write(root / "docs" / "notes.txt", 100)
    _write(root / "media" / "clip.mp4", 4000, age_days=60)
    _write(root / "media" / "deep" / "song.mp3", 2000)
    _write(root / "empty.bin", 0)


# --- Options ---------------------------------------------------------------------------------


def test_the_tool_is_registered_in_clean_up() -> None:
    tool = discover()["disk_usage"]
    assert tool is TOOL and tool.category == "Clean up"


def test_defaults() -> None:
    opts = DiskUsageOptions()
    assert opts.recursive is True
    assert opts.top_files == 50 and opts.top_folders == 25
    assert opts.min_size_mb == 0 and opts.category == "any" and opts.file_types == []
    assert opts.older_than_days == 0 and opts.newer_than_days == 0
    assert opts.include_hidden is False and opts.exclude_globs == []


def test_file_types_are_normalised_and_checked() -> None:
    assert DiskUsageOptions(file_types=["MP4", ".Mkv", "mp4"]).file_types == [".mp4", ".mkv"]
    for bad in ("", ".", "a.b", "m p4"):
        with pytest.raises(ValidationError):
            DiskUsageOptions(file_types=[bad])


@pytest.mark.parametrize(
    "fields",
    [
        {"top_files": 0},
        {"top_files": 10001},
        {"top_folders": -1},
        {"min_size_mb": -1},
        {"older_than_days": -1},
        {"older_than_days": 36501},
        {"category": "everything"},
        {"nonsense": 1},
        {"older_than_days": 30, "newer_than_days": 10},
        {"older_than_days": 30, "newer_than_days": 30},
    ],
)
def test_bad_options_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DiskUsageOptions.model_validate(fields)


def test_an_age_window_is_accepted() -> None:
    opts = DiskUsageOptions(older_than_days=10, newer_than_days=30)
    assert (opts.older_than_days, opts.newer_than_days) == (10, 30)


def test_format_size_uses_windows_units() -> None:
    assert format_size(0) == "0 bytes"
    assert format_size(1023) == "1023 bytes"
    assert format_size(1024) == "1.0 KB"
    assert format_size(MB * 3 // 2) == "1.5 MB"
    assert format_size(5 * 1024**4) == "5.0 TB"
    assert format_size(2048 * 1024**4) == "2048.0 TB"


# --- Plan contents ---------------------------------------------------------------------------


def test_largest_files_come_first_and_none_is_checked(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _files(plan, root) == [
        "big.mkv",
        "media/clip.mp4",
        "docs/report.pdf",
        "media/deep/song.mp3",
        "docs/notes.txt",
    ]
    assert all(a.kind is ActionKind.TRASH and a.selected is False for a in plan.actions)
    assert plan.selected_actions() == []
    assert plan.roots == [root]


def test_reason_has_size_type_and_age(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    by_name = {a.src.name: a.reason for a in plan.actions if a.src}
    assert by_name["big.mkv"] == "4.9 KB, video (.mkv), modified 400 days ago"
    assert by_name["report.pdf"] == "2.9 KB, document (.pdf), modified 10 days ago"
    assert by_name["notes.txt"] == "100 bytes, document (.txt), modified today"


def test_unknown_types_say_other(tmp_path: Path, root: Path) -> None:
    _write(root / "data.xyz", 10)
    _write(root / "README", 10)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    reasons = {a.src.name: a.reason for a in plan.actions if a.src}
    assert "other (.xyz)" in reasons["data.xyz"]
    assert "other (no extension)" in reasons["README"]


def test_top_files_limits_the_list_and_ties_are_stable(tmp_path: Path, root: Path) -> None:
    for name in ("d.bin", "a.bin", "c.bin", "b.bin"):
        _write(root / name, 100)
    _write(root / "huge.bin", 900)
    plan = TOOL.scan(_opts(root, top_files=3), _ctx(tmp_path))
    assert _files(plan, root) == ["huge.bin", "a.bin", "b.bin"]
    again = TOOL.scan(_opts(root, top_files=3), _ctx(tmp_path))
    assert _files(again, root) == _files(plan, root)


def test_first_note_is_a_summary(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert plan.notes[0].startswith("Disk usage: 5 file(s)")
    assert "none checked" in plan.notes[0]
    assert parse_size_note(plan.notes[0]) is None


def test_folder_and_root_notes_are_parseable(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _folder_notes(plan, root) == [
        ("media", 6000),
        ("docs", 3100),
        ("media/deep", 2000),
    ]
    roots = [parse_size_note(n) for n in plan.notes if n.startswith(ROOT_SIZE_PREFIX)]
    assert roots == [("root-size", 5000 + 3100 + 6000, root)]
    assert any(n.startswith(FOLDER_SIZE_PREFIX) for n in plan.notes)


def test_size_note_parses_paths_with_spaces_and_rejects_other_notes() -> None:
    parsed = parse_size_note("folder-size: 12345 C:\\My Files\\Big Stuff")
    assert parsed == ("folder-size", 12345, Path("C:\\My Files\\Big Stuff"))
    assert parse_size_note("folder-size: abc C:\\x") is None
    assert parse_size_note("skipped: cloud-only: 3 file(s)") is None
    assert parse_size_note("folder-size: 5") is None


def test_folder_notes_handle_spaces_in_real_paths(tmp_path: Path, root: Path) -> None:
    _write(root / "My Videos" / "a b.mp4", 700)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _folder_notes(plan, root) == [("My Videos", 700)]


def test_top_folders_limit_and_zero(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    one = TOOL.scan(_opts(root, top_folders=1), _ctx(tmp_path))
    assert _folder_notes(one, root) == [("media", 6000)]
    none = TOOL.scan(_opts(root, top_folders=0), _ctx(tmp_path))
    assert _folder_notes(none, root) == []


def test_not_recursive_lists_only_the_top_files_and_no_folders(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root, recursive=False), _ctx(tmp_path))
    assert _files(plan, root) == ["big.mkv"]
    assert _folder_notes(plan, root) == []


def test_folder_totals_include_nested_folders(tmp_path: Path, root: Path) -> None:
    _write(root / "a" / "b" / "c" / "x.bin", 500)
    _write(root / "a" / "y.bin", 100)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _folder_notes(plan, root) == [("a", 600), ("a/b", 500), ("a/b/c", 500)]


def test_several_roots_and_nested_roots_are_not_double_counted(tmp_path: Path, root: Path) -> None:
    other = tmp_path / "other"
    _write(root / "inner" / "a.bin", 100)
    _write(other / "b.bin", 200)
    options = DiskUsageOptions(roots=[root, root / "inner", other, other])
    plan = TOOL.scan(options, _ctx(tmp_path))
    assert plan.roots == [root, other]
    assert sorted(a.src.name for a in plan.actions if a.src) == ["a.bin", "b.bin"]
    sizes = sorted(parsed[1] for n in plan.notes if (parsed := parse_size_note(n)) is not None)
    assert 300 not in sizes and 100 in sizes and 200 in sizes


# --- Filters ---------------------------------------------------------------------------------


def test_min_size_filters_files_and_folder_totals(tmp_path: Path, root: Path) -> None:
    _write(root / "dir" / "big.bin", 3 * KB)
    _write(root / "dir" / "small.bin", 200)
    plan = TOOL.scan(_opts(root, min_size_mb=2 * KB / MB), _ctx(tmp_path))
    assert _files(plan, root) == ["dir/big.bin"]
    assert _folder_notes(plan, root) == [("dir", 3 * KB)]


def test_a_category_counts_only_its_kind(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root, category="videos"), _ctx(tmp_path))
    assert _files(plan, root) == ["big.mkv", "media/clip.mp4"]
    assert _folder_notes(plan, root) == [("media", 4000)]
    assert "Counting only files of type videos" in plan.notes[0]


def test_file_types_and_category_are_added_together(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root, category="music", file_types=[".pdf"]), _ctx(tmp_path))
    assert sorted(_files(plan, root)) == ["docs/report.pdf", "media/deep/song.mp3"]


def test_file_types_alone_match_case_insensitively(tmp_path: Path, root: Path) -> None:
    _write(root / "A.MKV", 50)
    _write(root / "b.txt", 60)
    plan = TOOL.scan(_opts(root, file_types=["mkv"]), _ctx(tmp_path))
    assert _files(plan, root) == ["A.MKV"]


def test_older_than_days_keeps_only_old_files(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root, older_than_days=30), _ctx(tmp_path))
    assert _files(plan, root) == ["big.mkv", "media/clip.mp4"]
    assert _folder_notes(plan, root) == [("media", 4000)]


def test_newer_than_days_keeps_only_recent_files(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root, newer_than_days=30), _ctx(tmp_path))
    assert _files(plan, root) == [
        "docs/report.pdf",
        "media/deep/song.mp3",
        "docs/notes.txt",
    ]


def test_age_window(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root, older_than_days=5, newer_than_days=100), _ctx(tmp_path))
    assert _files(plan, root) == ["media/clip.mp4", "docs/report.pdf"]


def test_exclude_globs_skip_files_and_whole_folders(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root, exclude_globs=["media", "*.mkv"]), _ctx(tmp_path))
    assert _files(plan, root) == ["docs/report.pdf", "docs/notes.txt"]
    assert _folder_notes(plan, root) == [("docs", 3100)]


def test_hidden_files_and_folders_are_left_out_unless_asked(tmp_path: Path, root: Path) -> None:
    _write(root / "seen.bin", 100)
    hidden_file = _write(root / "pagefile.bin", 900)
    _write(root / "hidden dir" / "inside.bin", 800)
    _set_hidden(hidden_file)
    _set_hidden(root / "hidden dir")
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _files(plan, root) == ["seen.bin"]
    assert any("2 hidden or system item(s) left out" in n for n in plan.notes)
    shown = TOOL.scan(_opts(root, include_hidden=True), _ctx(tmp_path))
    assert _files(shown, root) == ["pagefile.bin", "hidden dir/inside.bin", "seen.bin"]


# --- Safety ----------------------------------------------------------------------------------


def test_scan_changes_nothing_and_opens_no_file(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sample_tree(root)
    before = _snapshot(root)
    real_open = builtins.open

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, str | Path) and str(file).startswith(str(root)):
            raise AssertionError(f"scan opened {file}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    TOOL.scan(_opts(root), _ctx(tmp_path))
    monkeypatch.undo()
    assert _snapshot(root) == before


def test_protected_roots_are_refused(tmp_path: Path, root: Path) -> None:
    guarded = tmp_path / "Guarded"
    _write(guarded / "a.bin", 10)
    options = DiskUsageOptions(roots=[guarded, Path(tmp_path.anchor), tmp_path / "missing"])
    plan = TOOL.scan(options, _ctx(tmp_path, protected_folders=[str(guarded)]))
    assert plan.roots == [] and plan.actions == []
    assert any("Protected folder refused (protected folder" in n for n in plan.notes)
    assert any("drive root" in n for n in plan.notes)
    assert any("Not a folder" in n for n in plan.notes)
    assert plan.notes[-1] == "No folders to scan."
    allowed = TOOL.scan(
        DiskUsageOptions(roots=[guarded]),
        _ctx(tmp_path, protected_folders=[str(guarded)], allow_protected_paths=True),
    )
    assert allowed.roots == [guarded] and len(allowed.actions) == 1


def test_a_junction_is_not_followed_or_counted(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "secret.bin", 9000)
    _write(root / "real.bin", 100)
    _junction(root / "link", outside)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _files(plan, root) == ["real.bin"]
    assert not any(str(outside) in (a.src or Path()).as_posix() for a in plan.actions)
    sizes = [parse_size_note(n) for n in plan.notes if n.startswith(ROOT_SIZE_PREFIX)]
    assert sizes == [("root-size", 100, root)]
    assert _folder_notes(plan, root) == []


def test_a_junction_as_the_root_is_refused(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "x.bin", 5)
    link = tmp_path / "linkroot"
    _junction(link, outside)
    plan = TOOL.scan(DiskUsageOptions(roots=[link]), _ctx(tmp_path))
    assert plan.actions == [] and any("Junction or symlink" in n for n in plan.notes)


def test_cloud_only_files_are_skipped_and_never_counted(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / "local.bin", 100)
    _write(root / "cloud.bin", 7000)

    def fake_attributes(path: Path, st: os.stat_result) -> int:
        return OFFLINE if path.name == "cloud.bin" else 0

    monkeypatch.setattr(du_tool, "_attributes", fake_attributes)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _files(plan, root) == ["local.bin"]
    assert any(n.startswith("skipped: cloud-only: 1 file(s)") for n in plan.notes)
    sizes = [parse_size_note(n) for n in plan.notes if n.startswith(ROOT_SIZE_PREFIX)]
    assert sizes == [("root-size", 100, root)]
    assert plan.cloud_only == [root / "cloud.bin"]  # collected for the download action, once


def test_a_onedrive_placeholder_that_shows_the_reparse_bit_is_not_a_link(
    tmp_path: Path, root: Path, cloud_placeholder: Callable[..., None]
) -> None:
    _write(root / "placeholder.bin", 500)
    _write(root / "folder" / "inside.bin", 300)
    cloud_placeholder("placeholder.bin", "folder")  # a placeholder file and a placeholder folder
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert sorted(_files(plan, root)) == ["folder/inside.bin", "placeholder.bin"]
    sizes = [parse_size_note(n) for n in plan.notes if n.startswith(ROOT_SIZE_PREFIX)]
    assert sizes == [("root-size", 800, root)]
    assert plan.cloud_only == []  # on the device, so it is counted like any other file


def test_cancelling_stops_the_scan(tmp_path: Path, root: Path) -> None:
    from various_tools.core.context import Cancelled

    _sample_tree(root)
    ctx = _ctx(tmp_path)
    ctx.cancel()
    with pytest.raises(Cancelled):
        TOOL.scan(_opts(root), ctx)


# --- Apply -----------------------------------------------------------------------------------


def test_apply_trashes_only_the_files_that_were_checked(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _sample_tree(root)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    for action in plan.actions:
        if action.src and action.src.name == "big.mkv":
            action.selected = True

    result = TOOL.apply(plan, ctx)
    assert result.status == "done" and result.count("done") == 1
    assert sent_to_trash == [str(root / "big.mkv")]
    assert not (root / "big.mkv").exists()
    assert (root / "media" / "clip.mp4").exists()
    recorded = journal.actions(result.run_id)
    assert [(a.kind, a.status, a.src) for a in recorded] == [
        ("trash", "done", str(root / "big.mkv"))
    ]
    run = journal.run(result.run_id)
    assert run is not None and run.tool == "disk_usage"


def test_applying_an_untouched_plan_changes_nothing(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _sample_tree(root)
    before = _snapshot(root)
    ctx = _ctx(tmp_path, journal)
    result = TOOL.apply(TOOL.scan(_opts(root), ctx), ctx)
    assert result.outcomes == [] and sent_to_trash == []
    assert _snapshot(root) == before


def test_a_saved_plan_can_be_applied_later(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _sample_tree(root)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    plan.actions[1].selected = True
    loaded = Plan.from_json(plan.to_json())
    assert loaded.notes == plan.notes
    result = TOOL.apply(loaded, ctx)
    assert result.count("done") == 1 and not (root / "media" / "clip.mp4").exists()


# --- The size tree (treemap data) ------------------------------------------------------------


def _tree_root(root: Path) -> None:
    """root 10000: top.bin 1000, a 8000 (a/x.bin 500, a/b 7500 (b/y.bin 7000, b/c/z.bin 500)),
    tiny 10, d 990 (d/e.bin 990)."""
    _write(root / "top.bin", 1000)
    _write(root / "a" / "x.bin", 500)
    _write(root / "a" / "b" / "y.bin", 7000)
    _write(root / "a" / "b" / "c" / "z.bin", 500)
    _write(root / "tiny" / "t.bin", 10)
    _write(root / "My Stuff, 2024" / "e.bin", 990)


def _tree_notes(plan: Plan, base: Path) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for note in plan.notes:
        parsed = du_tool.parse_tree_note(note)
        if parsed is not None:
            depth, size, path = parsed
            found.append((depth, size, "." if path == base else _rel(path, base)))
    return found


def test_tree_notes_list_each_folder_down_to_the_depth_and_share(
    tmp_path: Path, root: Path
) -> None:
    _tree_root(root)
    plan = TOOL.scan(_opts(root, tree_depth=2, tree_min_share_pct=1.0), _ctx(tmp_path))
    assert _tree_notes(plan, root) == [
        (0, 10000, "."),
        (1, 8000, "a"),
        (2, 7500, "a/b"),
        (1, 990, "My Stuff, 2024"),
    ]  # "tiny" is 0.1 % (under 1 %), "a/b/c" is depth 3
    assert all(parse_size_note(n) is None for n in plan.notes if n.startswith("tree-size: "))
    # The existing notes are unchanged.
    assert ("a", 8000) in _folder_notes(plan, root)
    assert [parse_size_note(n) for n in plan.notes if n.startswith(ROOT_SIZE_PREFIX)] == [
        ("root-size", 10000, root)
    ]


def test_tree_options_have_defaults_and_bounds() -> None:
    opts = DiskUsageOptions()
    assert opts.tree_depth == 3 and opts.tree_min_share_pct == 1.0
    for bad in ({"tree_depth": -1}, {"tree_depth": 33}, {"tree_min_share_pct": 101}):
        with pytest.raises(ValidationError):
            DiskUsageOptions.model_validate(bad)


def test_size_tree_rebuilds_the_hierarchy_largest_first(tmp_path: Path, root: Path) -> None:
    from various_tools.tools.disk_usage import SizeNode, size_tree

    _tree_root(root)
    plan = TOOL.scan(_opts(root, tree_depth=5, tree_min_share_pct=0.0), _ctx(tmp_path))
    tree = size_tree(plan)
    assert isinstance(tree, SizeNode)
    assert (tree.path, tree.bytes, tree.depth, tree.name) == (root, 10000, 0, "root")
    assert [c.name for c in tree.children] == ["a", "My Stuff, 2024", "tiny"]
    a = tree.children[0]
    assert [(c.name, c.bytes) for c in a.children] == [("b", 7500)]
    assert a.own_bytes == 500 and tree.own_bytes == 1000  # files directly inside
    c = a.children[0].children[0]
    assert (c.path, c.bytes, c.depth, c.children, c.own_bytes) == (
        root / "a" / "b" / "c",
        500,
        3,
        (),
        500,
    )
    # Round trip through a saved plan.
    assert size_tree(Plan.from_json(plan.to_json())) == tree


def test_the_remainder_includes_folders_too_small_to_list(tmp_path: Path, root: Path) -> None:
    from various_tools.tools.disk_usage import size_tree

    _tree_root(root)
    tree = size_tree(TOOL.scan(_opts(root, tree_depth=1, tree_min_share_pct=10.0), _ctx(tmp_path)))
    assert [c.name for c in tree.children] == ["a"]
    assert tree.own_bytes == 10000 - 8000  # top.bin, tiny and "My Stuff, 2024"
    assert tree.children[0].children == () and tree.children[0].own_bytes == 8000


def test_size_tree_with_several_roots_and_with_none(tmp_path: Path) -> None:
    from various_tools.tools.disk_usage import size_tree

    one, two = tmp_path / "one", tmp_path / "two"
    _write(one / "a.bin", 100)
    _write(two / "sub" / "b.bin", 300)
    plan = TOOL.scan(DiskUsageOptions(roots=[one, two]), _ctx(tmp_path))
    tree = size_tree(plan)
    assert tree.path is None and tree.bytes == 400 and tree.depth == -1
    assert [(c.path, c.bytes) for c in tree.children] == [(two, 300), (one, 100)]
    assert [c.name for c in tree.children[0].children] == ["sub"]
    empty = size_tree(TOOL.scan(DiskUsageOptions(roots=[]), _ctx(tmp_path)))
    assert empty.path is None and empty.bytes == 0 and empty.children == ()


def test_recursive_off_gives_the_root_only(tmp_path: Path, root: Path) -> None:
    from various_tools.tools.disk_usage import size_tree

    _tree_root(root)
    plan = TOOL.scan(_opts(root, recursive=False), _ctx(tmp_path))
    assert _tree_notes(plan, root) == [(0, 1000, ".")]
    assert size_tree(plan).children == ()


def test_cloud_only_files_stay_out_of_the_tree(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.tools.disk_usage import size_tree

    _write(root / "box" / "local.bin", 100)
    _write(root / "box" / "cloud.bin", 7000)
    _write(root / "cloudy" / "only.bin", 5000)
    monkeypatch.setattr(
        du_tool,
        "_attributes",
        lambda path, st: OFFLINE if path.name in ("cloud.bin", "only.bin") else 0,
    )
    tree = size_tree(TOOL.scan(_opts(root, tree_min_share_pct=0.0), _ctx(tmp_path)))
    assert tree.bytes == 100 and [(c.name, c.bytes) for c in tree.children] == [("box", 100)]


def test_parse_tree_note() -> None:
    assert du_tool.parse_tree_note("tree-size: 2 1234 C:\\My Files, old\\x") == (
        2,
        1234,
        Path("C:\\My Files, old\\x"),
    )
    assert du_tool.parse_tree_note("folder-size: 1 C:\\x") is None
    assert du_tool.parse_tree_note("tree-size: x 1 C:\\x") is None


def test_a_file_outside_the_roots_is_refused_at_apply(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    outside = _write(tmp_path / "outside" / "x.bin", 50)
    _write(root / "a.bin", 10)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    plan.actions[0].src = outside
    plan.actions[0].selected = True
    result = TOOL.apply(plan, ctx)
    assert result.count("skipped") == 1 and outside.exists() and sent_to_trash == []


def test_folders_are_never_trash_targets_in_a_plan(tmp_path: Path, root: Path) -> None:
    _sample_tree(root)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert all(a.src is not None and a.src.is_file() for a in plan.actions)
