"""Temp cleaner: default roots, age and pattern rules, empty folders, browser caches, safety.

Every tree is generated under ``tmp_path``. The environment variables that name the real temp and
browser folders are redirected for every test, and every scan passes its roots or a fake
environment, so the real ``%TEMP%``, browser caches and home folder are never touched. Trash calls
go to a fake that deletes inside the temp folder, not the Recycle Bin.
"""

from __future__ import annotations

import builtins
import ctypes
import io
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
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
from various_tools.tools.temp_cleaner import (
    BROWSER_CACHE_PREFIX,
    TOOL,
    BrowserCache,
    TempCleaner,
    TempCleanerOptions,
    default_temp_roots,
    find_browser_caches,
    parse_browser_cache_note,
)
from various_tools.tools.temp_cleaner import tool as tc_tool

OFFLINE = 0x1000
DAY = 86400


@pytest.fixture(autouse=True)
def _sandbox_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever a test forgets to override points at an empty folder, never at the real ones."""
    sandbox = tmp_path / "sandbox-env"
    for name in ("TEMP", "TMP", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(name, str(sandbox / name))


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


def _opts(root: Path, **fields: Any) -> TempCleanerOptions:
    values: dict[str, Any] = {"roots": [root], "list_browser_caches": False}
    values.update(fields)
    return TempCleanerOptions.model_validate(values)


def _tool(tmp_path: Path) -> TempCleaner:
    """A tool whose environment is empty, so no browser cache is found."""
    return TempCleaner(environ={})


def _write(path: Path, age_days: float = 10.0, size: int = 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    _age(path, age_days)
    return path


def _age(path: Path, days: float, *, created_days: float | None = None) -> None:
    """Set the modified time, and the creation time (``days`` unless ``created_days``)."""
    when = time.time() - days * DAY
    os.utime(path, (when, when))
    _set_created(path, time.time() - (days if created_days is None else created_days) * DAY)


def _set_created(path: Path, when: float) -> None:
    """Set the Windows creation time (``st_birthtime``). A no-op elsewhere."""
    if sys.platform != "win32":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.SetFileTime.argtypes = [ctypes.c_void_p] * 4
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    # FILE_WRITE_ATTRIBUTES, share all, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS (folders too).
    handle = kernel32.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x02000000, None)
    assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    try:
        filetime = ctypes.c_uint64(int((when + 11644473600) * 10_000_000))
        assert kernel32.SetFileTime(handle, ctypes.byref(filetime), None, None)
    finally:
        kernel32.CloseHandle(handle)


def _rel(path: Path | None, base: Path) -> str:
    assert path is not None
    return path.relative_to(base).as_posix()


def _srcs(plan: Plan, base: Path) -> list[str]:
    return [_rel(a.src, base) for a in plan.actions]


def _listing(base: Path) -> set[str]:
    return {p.relative_to(base).as_posix() for p in base.rglob("*")}


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


def _browsers(local: Path) -> dict[str, Path]:
    """A fake %LOCALAPPDATA% holding Chrome, Edge and Firefox cache folders."""
    folders = {
        "chrome": local / "Google" / "Chrome" / "User Data" / "Default" / "Cache",
        "chrome2": local / "Google" / "Chrome" / "User Data" / "Profile 1" / "Cache",
        "chrome_code": local / "Google" / "Chrome" / "User Data" / "Default" / "Code Cache",
        "edge": local / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache",
        "firefox": local / "Mozilla" / "Firefox" / "Profiles" / "abc.default" / "cache2",
    }
    for name, folder in folders.items():
        _write(folder / "data_0", age_days=100, size=100)
        _write(folder / "sub" / "data_1", age_days=100, size=50)
        assert name
    # Not caches: they must never be listed.
    _write(local / "Google" / "Chrome" / "User Data" / "Default" / "Cookies", age_days=0)
    _write(local / "Mozilla" / "Firefox" / "Profiles" / "abc.default" / "prefs.js", age_days=0)
    return folders


# --- Default roots (pure) --------------------------------------------------------------------


def test_the_tool_is_registered_in_clean_up() -> None:
    tool = discover()["temp_cleaner"]
    assert tool is TOOL and tool.category == "Clean up"


def test_default_roots_come_from_the_environment_mapping(tmp_path: Path) -> None:
    temp, tmp, local = tmp_path / "t1", tmp_path / "t2", tmp_path / "local"
    environ = {"TEMP": str(temp), "TMP": str(tmp), "LOCALAPPDATA": str(local)}
    assert default_temp_roots(environ) == [temp, tmp, local / "Temp"]


def test_default_roots_ignore_case_repeats_blanks_and_relative_paths(tmp_path: Path) -> None:
    temp = tmp_path / "T"
    environ = {"temp": str(temp), "Tmp": str(temp).lower(), "LocalAppData": "  ", "OTHER": "x"}
    assert default_temp_roots(environ) == [temp]
    assert default_temp_roots({"TEMP": "relative\\folder"}) == []
    assert default_temp_roots({}) == []


def test_default_roots_touch_no_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> Any:
        raise AssertionError("default_temp_roots touched the disk")

    monkeypatch.setattr(Path, "exists", refuse)
    monkeypatch.setattr(Path, "is_dir", refuse)
    monkeypatch.setattr(os, "scandir", refuse)
    monkeypatch.setattr(os, "listdir", refuse)
    monkeypatch.setattr(os, "stat", refuse)
    environ: Mapping[str, str] = {"TEMP": str(tmp_path / "a"), "LOCALAPPDATA": str(tmp_path)}
    assert default_temp_roots(environ) == [tmp_path / "a", tmp_path / "Temp"]


def test_an_empty_roots_option_uses_the_default_from_the_environment(
    tmp_path: Path, journal: Journal
) -> None:
    fake_temp = tmp_path / "fake-temp"
    _write(fake_temp / "old.tmp")
    _write(fake_temp / "new.tmp", age_days=1)
    tool = TempCleaner(environ={"TEMP": str(fake_temp)})
    plan = tool.scan(TempCleanerOptions(list_browser_caches=False), _ctx(tmp_path))
    assert plan.roots == [fake_temp]
    assert _srcs(plan, fake_temp) == ["old.tmp"]
    assert any("the temp folder is used" in n for n in plan.notes)


def test_no_roots_and_no_environment_scans_nothing(tmp_path: Path) -> None:
    plan = TempCleaner(environ={}).scan(
        TempCleanerOptions(list_browser_caches=False), _ctx(tmp_path)
    )
    assert plan.roots == [] and plan.actions == []
    assert any("no TEMP, TMP or LOCALAPPDATA" in n for n in plan.notes)
    assert plan.notes[-1] == "No folders to clean."


def test_the_real_tool_reads_the_environment_at_scan_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_temp = tmp_path / "from-os-environ"
    _write(fake_temp / "old.tmp")
    monkeypatch.setenv("TEMP", str(fake_temp))
    plan = TOOL.scan(TempCleanerOptions(list_browser_caches=False), _ctx(tmp_path))
    assert plan.roots == [fake_temp] and _srcs(plan, fake_temp) == ["old.tmp"]


# --- Options ---------------------------------------------------------------------------------


def test_defaults() -> None:
    opts = TempCleanerOptions()
    assert opts.roots == [] and opts.recursive is True and opts.min_age_days == 7
    assert opts.include_patterns == ["*"] and opts.exclude_patterns == []
    assert opts.skip_hidden_system is True and opts.remove_empty_folders is True
    assert opts.list_browser_caches is True and opts.measure_browser_caches is True


@pytest.mark.parametrize(
    "fields",
    [
        {"min_age_days": -1},
        {"min_age_days": 36501},
        {"include_patterns": []},
        {"recursive": "maybe"},
        {"unknown_option": 1},
    ],
)
def test_bad_options_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TempCleanerOptions.model_validate(fields)


# --- Plan contents ---------------------------------------------------------------------------


def test_only_files_older_than_the_limit_are_planned_and_all_are_checked(
    tmp_path: Path, root: Path
) -> None:
    _write(root / "old.tmp", age_days=30)
    _write(root / "week.tmp", age_days=8)
    _write(root / "young.tmp", age_days=2)
    _write(root / "sub" / "deep.log", age_days=9)
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    assert sorted(_srcs(plan, root)) == ["old.tmp", "sub/deep.log", "week.tmp"]
    assert all(a.kind is ActionKind.TRASH and a.selected for a in plan.actions)
    assert plan.roots == [root]
    assert plan.notes[0].startswith("Temp cleaner: 3 file(s)")
    assert "1 too recent, kept" in plan.notes[0] and "Recycle Bin" in plan.notes[0]


def test_the_age_limit_is_an_option(tmp_path: Path, root: Path) -> None:
    _write(root / "a.tmp", age_days=2)
    _write(root / "b.tmp", age_days=0.01)
    tool = _tool(tmp_path)
    assert _srcs(tool.scan(_opts(root, min_age_days=1), _ctx(tmp_path)), root) == ["a.tmp"]
    assert _srcs(tool.scan(_opts(root, min_age_days=30), _ctx(tmp_path)), root) == []
    everything = tool.scan(_opts(root, min_age_days=0), _ctx(tmp_path))
    assert sorted(_srcs(everything, root)) == ["a.tmp", "b.tmp"]


def test_reason_has_size_and_age(tmp_path: Path, root: Path) -> None:
    _write(root / "a.tmp", age_days=12, size=2048)
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    assert plan.actions[0].reason == "2.0 KB, modified 12 days ago"


def test_include_and_exclude_patterns(tmp_path: Path, root: Path) -> None:
    for name in ("a.tmp", "b.LOG", "c.txt", "keep.tmp"):
        _write(root / name)
    _write(root / "sub" / "d.tmp")
    tool = _tool(tmp_path)
    only = tool.scan(_opts(root, include_patterns=["*.tmp", "*.log"]), _ctx(tmp_path))
    assert sorted(_srcs(only, root)) == ["a.tmp", "b.LOG", "keep.tmp", "sub/d.tmp"]
    both = tool.scan(
        _opts(root, include_patterns=["*.tmp"], exclude_patterns=["keep.*", "sub"]), _ctx(tmp_path)
    )
    assert _srcs(both, root) == ["a.tmp"]
    by_path = tool.scan(_opts(root, include_patterns=["sub/*"]), _ctx(tmp_path))
    assert _srcs(by_path, root) == ["sub/d.tmp"]


def test_not_recursive_stays_in_the_top_folder(tmp_path: Path, root: Path) -> None:
    _write(root / "top.tmp")
    _write(root / "sub" / "deep.tmp")
    plan = _tool(tmp_path).scan(_opts(root, recursive=False), _ctx(tmp_path))
    assert _srcs(plan, root) == ["top.tmp"]


def test_hidden_and_system_files_are_kept_unless_asked(tmp_path: Path, root: Path) -> None:
    _write(root / "seen.tmp")
    _set_hidden(_write(root / "hidden.tmp"))
    _write(root / "hdir" / "inside.tmp")
    _set_hidden(root / "hdir")
    tool = _tool(tmp_path)
    plan = tool.scan(_opts(root), _ctx(tmp_path))
    assert _srcs(plan, root) == ["seen.tmp"]
    assert "2 hidden or system, kept" in plan.notes[0]
    all_of_it = tool.scan(_opts(root, skip_hidden_system=False), _ctx(tmp_path))
    assert sorted(_srcs(all_of_it, root)) == ["hdir/inside.tmp", "hidden.tmp", "seen.tmp"]


def test_group_is_the_first_folder_under_the_root(tmp_path: Path, root: Path) -> None:
    _write(root / "top.tmp")
    _write(root / "sub" / "x" / "deep.tmp")
    plan = _tool(tmp_path).scan(_opts(root, remove_empty_folders=False), _ctx(tmp_path))
    groups = {a.src.name: a.group_id for a in plan.actions if a.src}
    assert groups == {"top.tmp": None, "deep.tmp": str(root / "sub")}


# --- Empty folders ---------------------------------------------------------------------------


def _folder_tree(root: Path) -> None:
    _write(root / "a" / "b" / "deep.tmp")
    _write(root / "a" / "keep.log", age_days=1)
    _write(root / "c" / "old.tmp")
    (root / "empty_old").mkdir()
    (root / "fresh_empty").mkdir()
    # Folders are aged last, because creating a file changes its folder's time.
    for folder in ("a/b", "a", "c", "empty_old"):
        _age(root / folder, 10)


def test_folders_left_empty_follow_their_files_deepest_first(tmp_path: Path, root: Path) -> None:
    _folder_tree(root)
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    srcs = _srcs(plan, root)
    assert sorted(srcs) == ["a/b", "a/b/deep.tmp", "c", "c/old.tmp", "empty_old"]
    assert srcs.index("a/b/deep.tmp") < srcs.index("a/b")
    assert srcs.index("c/old.tmp") < srcs.index("c")
    reasons = {_rel(a.src, root): a.reason for a in plan.actions}
    assert reasons["a/b"] == "folder left empty by this clean-up"
    assert reasons["empty_old"] == "empty folder"
    assert (
        plan.notes[0].startswith("Temp cleaner: 2 file(s)") and "3 empty folder(s)" in plan.notes[0]
    )
    assert all(a.src != root for a in plan.actions)


def test_a_folder_with_a_kept_item_keeps_every_folder_above_it(tmp_path: Path, root: Path) -> None:
    _write(root / "top" / "mid" / "bottom" / "gone.tmp")
    _write(root / "top" / "mid" / "bottom" / "stay.keep")
    for folder in ("top/mid/bottom", "top/mid", "top"):
        _age(root / folder, 10)
    plan = _tool(tmp_path).scan(_opts(root, include_patterns=["*.tmp"]), _ctx(tmp_path))
    assert _srcs(plan, root) == ["top/mid/bottom/gone.tmp"]


@pytest.mark.parametrize("blocker", ["young", "excluded", "link", "hidden"])
def test_each_kind_of_kept_item_keeps_its_folder(tmp_path: Path, root: Path, blocker: str) -> None:
    folder = root / "box"
    _write(folder / "gone.tmp")
    if blocker == "young":
        _write(folder / "new.tmp", age_days=1)
    elif blocker == "excluded":
        _write(folder / "precious.tmp")
    elif blocker == "link":
        outside = tmp_path / "outside"
        outside.mkdir()
        _junction(folder / "link", outside)
    else:
        _set_hidden(_write(folder / "secret.tmp"))
    _age(folder, 10)
    plan = _tool(tmp_path).scan(_opts(root, exclude_patterns=["precious.tmp"]), _ctx(tmp_path))
    assert "box" not in _srcs(plan, root)
    assert "box/gone.tmp" in _srcs(plan, root)


def test_a_recently_changed_folder_is_kept_even_if_all_its_files_are_old(
    tmp_path: Path, root: Path
) -> None:
    _write(root / "box" / "old.tmp")
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))  # "box" itself is brand new
    assert _srcs(plan, root) == ["box/old.tmp"]


def test_folder_removal_can_be_turned_off(tmp_path: Path, root: Path) -> None:
    _folder_tree(root)
    plan = _tool(tmp_path).scan(_opts(root, remove_empty_folders=False), _ctx(tmp_path))
    assert sorted(_srcs(plan, root)) == ["a/b/deep.tmp", "c/old.tmp"]
    assert "empty folder(s)" not in plan.notes[0]


def test_the_chosen_folder_is_never_removed_even_when_it_ends_up_empty(
    tmp_path: Path, root: Path
) -> None:
    _write(root / "only.tmp")
    _age(root, 10)
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    assert _srcs(plan, root) == ["only.tmp"]


# --- Browser caches --------------------------------------------------------------------------


def test_find_browser_caches_lists_known_cache_folders_only(tmp_path: Path) -> None:
    local = tmp_path / "local"
    folders = _browsers(local)
    found = find_browser_caches({"LOCALAPPDATA": str(local)})
    assert [c.name for c in found] == [
        "Chrome (Default) Cache",
        "Chrome (Default) Code Cache",
        "Chrome (Profile 1) Cache",
        "Edge (Default) Cache",
        "Firefox (abc.default) cache2",
    ]
    assert {c.path for c in found} == set(folders.values())
    assert find_browser_caches({}) == []
    assert find_browser_caches({"LOCALAPPDATA": str(tmp_path / "nothing-here")}) == []


def test_caches_are_listed_as_read_only_notes_with_sizes_and_never_as_actions(
    tmp_path: Path, root: Path
) -> None:
    local = tmp_path / "local"
    folders = _browsers(local)
    _write(root / "old.tmp")
    tool = TempCleaner(environ={"LOCALAPPDATA": str(local)})
    plan = tool.scan(_opts(root, list_browser_caches=True), _ctx(tmp_path))
    assert _srcs(plan, root) == ["old.tmp"]
    notes = [n for n in plan.notes if n.startswith(BROWSER_CACHE_PREFIX)]
    assert len(notes) == 5
    parsed = [parse_browser_cache_note(n) for n in notes]
    assert ("Chrome (Default) Cache", folders["chrome"], 150) in parsed
    assert ("Firefox (abc.default) cache2", folders["firefox"], 150) in parsed
    assert (
        notes[0] == f"browser-cache (read-only): Chrome (Default) Cache | {folders['chrome']} | 150"
    )


def test_cache_sizes_can_be_left_unmeasured(tmp_path: Path, root: Path) -> None:
    local = tmp_path / "local"
    _browsers(local)
    tool = TempCleaner(environ={"LOCALAPPDATA": str(local)})
    plan = tool.scan(
        _opts(root, measure_browser_caches=False, list_browser_caches=True), _ctx(tmp_path)
    )
    parsed = [parse_browser_cache_note(n) for n in plan.notes if n.startswith(BROWSER_CACHE_PREFIX)]
    assert len(parsed) == 5 and all(p is not None and p[2] is None for p in parsed)
    assert any(n.endswith("| unknown") for n in plan.notes)


def test_listing_caches_can_be_turned_off(tmp_path: Path, root: Path) -> None:
    local = tmp_path / "local"
    _browsers(local)
    tool = TempCleaner(environ={"LOCALAPPDATA": str(local)})
    plan = tool.scan(_opts(root, list_browser_caches=False), _ctx(tmp_path))
    assert not any(BROWSER_CACHE_PREFIX in n or "browser cache" in n for n in plan.notes)


def test_no_caches_found_says_so(tmp_path: Path, root: Path) -> None:
    plan = TempCleaner(environ={}).scan(_opts(root, list_browser_caches=True), _ctx(tmp_path))
    assert any("No browser cache folders found" in n for n in plan.notes)


def test_a_cache_inside_a_chosen_folder_is_skipped_never_trashed(
    tmp_path: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    local = tmp_path / "local"
    folders = _browsers(local)
    chosen = local / "Google"  # holds the three Chrome caches
    _write(chosen / "old.tmp")
    _write(chosen / "other" / "old2.tmp")
    tool = TempCleaner(environ={"LOCALAPPDATA": str(local)})
    ctx = _ctx(tmp_path, journal)
    plan = tool.scan(
        TempCleanerOptions(roots=[chosen], list_browser_caches=False, min_age_days=1), ctx
    )
    assert sorted(_srcs(plan, chosen)) == ["old.tmp", "other/old2.tmp"]
    assert "3 browser cache folder(s) skipped" in plan.notes[0]
    result = tool.apply(plan, ctx)
    assert result.count("done") == 2
    for folder in folders.values():
        assert (folder / "data_0").exists()


def test_a_browser_cache_cannot_be_chosen_as_a_folder_to_clean(
    tmp_path: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    local = tmp_path / "local"
    folders = _browsers(local)
    tool = TempCleaner(environ={"LOCALAPPDATA": str(local)})
    chosen = [folders["chrome"], folders["edge"] / "sub"]
    plan = tool.scan(TempCleanerOptions(roots=chosen, list_browser_caches=False), _ctx(tmp_path))
    assert plan.actions == [] and plan.roots == []
    assert sum("Browser caches are listed, never cleaned" in n for n in plan.notes) == 2


# --- Safety ----------------------------------------------------------------------------------


def test_scan_changes_nothing_and_opens_no_file(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folder_tree(root)
    local = tmp_path / "local"
    _browsers(local)
    before = _snapshot(root)
    before_local = _snapshot(local)
    real_open = builtins.open

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, str | Path) and str(file).startswith(str(tmp_path / "root")):
            raise AssertionError(f"scan opened {file}")
        if isinstance(file, str | Path) and str(file).startswith(str(local)):
            raise AssertionError(f"scan opened {file}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    tool = TempCleaner(environ={"LOCALAPPDATA": str(local)})
    tool.scan(_opts(root, list_browser_caches=True), _ctx(tmp_path))
    monkeypatch.undo()
    assert _snapshot(root) == before and _snapshot(local) == before_local


def test_protected_roots_are_refused(tmp_path: Path) -> None:
    guarded = tmp_path / "Guarded"
    _write(guarded / "a.tmp")
    options = TempCleanerOptions(
        roots=[guarded, Path(tmp_path.anchor), tmp_path / "missing"], list_browser_caches=False
    )
    plan = TempCleaner(environ={}).scan(options, _ctx(tmp_path, protected_folders=[str(guarded)]))
    assert plan.roots == [] and plan.actions == []
    assert any("Protected folder refused" in n for n in plan.notes)
    assert any("drive root" in n for n in plan.notes)
    assert any("Not a folder" in n for n in plan.notes)


def test_a_junction_is_not_followed_and_is_not_removed(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "secret.tmp")
    _write(root / "real.tmp")
    _junction(root / "link", outside)
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    assert _srcs(plan, root) == ["real.tmp"]


def test_a_junction_as_a_root_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "secret.tmp")
    link = tmp_path / "linkroot"
    _junction(link, outside)
    plan = _tool(tmp_path).scan(
        TempCleanerOptions(roots=[link], list_browser_caches=False), _ctx(tmp_path)
    )
    assert plan.actions == [] and any("Junction or symlink" in n for n in plan.notes)


def test_cloud_only_files_are_skipped_and_never_read(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / "local.tmp")
    _write(root / "box" / "cloud.tmp")
    _age(root / "box", 10)

    def fake_attributes(path: Path, st: os.stat_result) -> int:
        return OFFLINE if path.name == "cloud.tmp" else 0

    monkeypatch.setattr(tc_tool, "_attributes", fake_attributes)
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    assert _srcs(plan, root) == ["local.tmp"]  # the cloud-only file keeps its folder too
    assert f"skipped: cloud-only: {root / 'box' / 'cloud.tmp'}" in plan.notes
    assert "1 cloud-only, skipped and not read" in plan.notes[0]
    assert plan.cloud_only == [root / "box" / "cloud.tmp"]  # for the download action, once


def test_a_onedrive_placeholder_that_shows_the_reparse_bit_is_not_a_link(
    tmp_path: Path,
    root: Path,
    journal: Journal,
    sent_to_trash: list[str],
    cloud_placeholder: Callable[..., None],
) -> None:
    _write(root / "placeholder.tmp")
    _write(root / "folder" / "inside.tmp")
    _age(root / "folder", 10)
    cloud_placeholder("placeholder.tmp", "folder")  # a placeholder file and a placeholder folder
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    plan = tool.scan(_opts(root), ctx)
    assert _srcs(plan, root) == ["folder/inside.tmp", "folder", "placeholder.tmp"]
    assert plan.cloud_only == []  # on the device, so it is an ordinary file
    # Apply checks every file again, and must not take the placeholder for a junction either.
    result = tool.apply(plan, ctx)
    assert result.count("done") == 3, [o.detail for o in result.outcomes]
    assert _listing(root) == set()


def test_nested_roots_are_collapsed(tmp_path: Path, root: Path) -> None:
    _write(root / "inner" / "a.tmp")
    options = TempCleanerOptions(roots=[root, root / "inner", root], list_browser_caches=False)
    plan = _tool(tmp_path).scan(options, _ctx(tmp_path))
    assert plan.roots == [root] and _srcs(plan, root) == ["inner/a.tmp"]


def test_only_the_chosen_folder_is_ever_planned(tmp_path: Path, root: Path) -> None:
    sibling = _write(tmp_path / "sibling.tmp")
    _write(root / "a.tmp")
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    assert all(a.src is not None and a.src.is_relative_to(root) for a in plan.actions)
    assert sibling.exists()


def test_cancelling_stops_the_scan(tmp_path: Path, root: Path) -> None:
    from various_tools.core.context import Cancelled

    _write(root / "a.tmp")
    ctx = _ctx(tmp_path)
    ctx.cancel()
    with pytest.raises(Cancelled):
        _tool(tmp_path).scan(_opts(root), ctx)


# --- Apply -----------------------------------------------------------------------------------


def test_apply_trashes_files_then_the_folders_they_left_empty(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _folder_tree(root)
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    result = tool.apply(tool.scan(_opts(root), ctx), ctx)
    assert result.status == "done" and result.count("done") == 5
    assert _listing(root) == {"a", "a/keep.log", "fresh_empty"}
    assert sent_to_trash.index(str(root / "a" / "b" / "deep.tmp")) < sent_to_trash.index(
        str(root / "a" / "b")
    )
    run = journal.run(result.run_id)
    assert run is not None and run.tool == "temp_cleaner"
    assert {a.kind for a in journal.actions(result.run_id)} == {"trash"}


def test_unchecked_items_are_not_removed(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _write(root / "a.tmp")
    _write(root / "b.tmp")
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    plan = tool.scan(_opts(root), ctx)
    plan.actions[0].selected = False
    result = tool.apply(plan, ctx)
    assert result.count("done") == 1 and len(sent_to_trash) == 1
    assert len(list(root.iterdir())) == 1


def test_a_locked_file_fails_on_its_own_and_the_rest_carry_on(
    tmp_path: Path, root: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / "a.tmp")
    _write(root / "locked.tmp")
    _write(root / "z.tmp")

    def fake_send2trash(path: str) -> None:
        if path.endswith("locked.tmp"):
            raise PermissionError("in use by another process")
        os.remove(path)

    monkeypatch.setattr(plan_module, "send2trash", fake_send2trash)
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    result = tool.apply(tool.scan(_opts(root), ctx), ctx)
    assert result.status == "partial"
    assert result.count("done") == 2 and result.count("failed") == 1
    assert _listing(root) == {"locked.tmp"}


def test_a_folder_that_still_has_a_locked_file_is_not_removed(
    tmp_path: Path, root: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / "box" / "locked.tmp")
    _write(root / "box" / "free.tmp")
    _age(root / "box", 10)

    def fake_send2trash(path: str) -> None:
        if path.endswith("locked.tmp"):
            raise PermissionError("in use")
        os.remove(path) if os.path.isfile(path) else os.rmdir(path)

    monkeypatch.setattr(plan_module, "send2trash", fake_send2trash)
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    plan = tool.scan(_opts(root), ctx)
    assert "box" in _srcs(plan, root)
    result = tool.apply(plan, ctx)
    assert result.count("failed") == 1 and result.count("skipped") == 1
    assert (root / "box" / "locked.tmp").exists()


def test_a_saved_plan_can_be_applied_later(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _write(root / "a.tmp")
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    loaded = Plan.from_json(tool.scan(_opts(root), ctx).to_json())
    assert tool.apply(loaded, ctx).count("done") == 1 and not (root / "a.tmp").exists()


def test_a_file_outside_the_roots_is_refused_at_apply(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    outside = _write(tmp_path / "outside.tmp")
    _write(root / "a.tmp")
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    plan = tool.scan(_opts(root), ctx)
    plan.actions[0].src = outside
    result = tool.apply(plan, ctx)
    assert result.count("skipped") == 1 and outside.exists() and sent_to_trash == []


def test_parse_browser_cache_note() -> None:
    note = f"{BROWSER_CACHE_PREFIX}Chrome (Default) Cache | C:\\Users\\Me Too\\Cache | 1234"
    assert parse_browser_cache_note(note) == (
        "Chrome (Default) Cache",
        Path("C:\\Users\\Me Too\\Cache"),
        1234,
    )
    unknown = parse_browser_cache_note(f"{BROWSER_CACHE_PREFIX}Edge | C:\\x | unknown")
    assert unknown == ("Edge", Path("C:\\x"), None)
    assert parse_browser_cache_note("folder-size: 1 C:\\x") is None
    assert parse_browser_cache_note(f"{BROWSER_CACHE_PREFIX}broken") is None
    assert BrowserCache(name="n", path=Path("p")).name == "n"


# --- Age: newest of modified and created, checked again at apply --------------------------------


def _short_name(path: Path) -> Path:
    """The 8.3 short form of ``path``, or skip when the volume does not make short names."""
    if sys.platform != "win32":
        pytest.skip("8.3 short names are a Windows feature")
    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.WinDLL("kernel32").GetShortPathNameW(str(path), buffer, 1024)
    if not length or os.path.normcase(buffer.value) == os.path.normcase(str(path)):
        pytest.skip("this volume does not create 8.3 short names")
    return Path(buffer.value)


windows_times = pytest.mark.skipif(
    sys.platform != "win32", reason="creation times are set through Win32"
)


@windows_times
def test_a_file_unpacked_with_an_old_modified_time_is_recent_by_its_creation_time(
    tmp_path: Path, root: Path
) -> None:
    # An installer unpacking an archive keeps the archive's old modified times.
    _write(root / "unpacked.dll", age_days=400)
    _age(root / "unpacked.dll", 400, created_days=0.01)
    _write(root / "really_old.tmp", age_days=30)
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    assert _srcs(plan, root) == ["really_old.tmp"]
    assert "1 too recent, kept" in plan.notes[0]


@windows_times
def test_a_folder_created_recently_is_kept_even_with_an_old_modified_time(
    tmp_path: Path, root: Path
) -> None:
    (root / "fresh").mkdir()
    _age(root / "fresh", 30, created_days=0.01)
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    assert _srcs(plan, root) == []


@windows_times
def test_reason_uses_the_newer_of_modified_and_created(tmp_path: Path, root: Path) -> None:
    _write(root / "a.tmp", age_days=40)
    _age(root / "a.tmp", 40, created_days=12)
    plan = _tool(tmp_path).scan(_opts(root), _ctx(tmp_path))
    assert plan.actions[0].reason == "5 bytes, modified 12 days ago"


def test_a_file_changed_after_the_scan_is_skipped_at_apply_and_journaled(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _write(root / "changed.tmp")
    _write(root / "still_old.tmp")
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    saved = tool.scan(_opts(root), ctx).to_json()
    (root / "changed.tmp").write_bytes(b"new content")  # modified now, a week after the scan
    result = tool.apply(Plan.from_json(saved), ctx)
    assert (root / "changed.tmp").exists() and not (root / "still_old.tmp").exists()
    assert sent_to_trash == [str(root / "still_old.tmp")]
    assert result.count("done") == 1 and result.count("skipped") == 1
    skipped = [o for o in result.outcomes if o.status == "skipped"]
    assert skipped[0].src == str(root / "changed.tmp")
    assert "changed since the scan" in skipped[0].detail
    rows = {a.src: a for a in journal.actions(result.run_id)}
    assert rows[str(root / "changed.tmp")].status == "skipped"
    assert "changed since the scan" in (rows[str(root / "changed.tmp")].error or "")


@windows_times
def test_a_file_recreated_after_the_scan_is_skipped_at_apply(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _write(root / "setup.tmp", age_days=30)
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    plan = tool.scan(_opts(root), ctx)
    # A program deletes it and unpacks a new copy that keeps the old modified time.
    (root / "setup.tmp").unlink()
    _write(root / "setup.tmp", age_days=30)
    _age(root / "setup.tmp", 30, created_days=0)
    result = tool.apply(plan, ctx)
    assert result.count("skipped") == 1 and sent_to_trash == []
    assert (root / "setup.tmp").exists()


def test_a_file_that_no_longer_matches_is_skipped_at_apply(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _write(root / "a.tmp")
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    plan = tool.scan(_opts(root), ctx)
    (root / "a.tmp").unlink()
    (root / "a.tmp").mkdir()  # now a folder with something in it
    _write(root / "a.tmp" / "inside.txt", age_days=30)
    result = tool.apply(plan, ctx)
    assert result.count("skipped") == 1 and sent_to_trash == []
    assert (root / "a.tmp" / "inside.txt").exists()


def test_cloud_only_at_apply_time_is_skipped_and_never_trashed(
    tmp_path: Path,
    root: Path,
    journal: Journal,
    sent_to_trash: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(root / "a.tmp")
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    plan = tool.scan(_opts(root), ctx)
    monkeypatch.setattr(tc_tool, "_attributes", lambda path, st: OFFLINE)
    result = tool.apply(plan, ctx)
    assert result.count("skipped") == 1 and sent_to_trash == []
    assert "cloud-only" in result.outcomes[0].detail


def test_the_age_limit_at_apply_comes_from_the_saved_options(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _write(root / "a.tmp", age_days=3)
    ctx = _ctx(tmp_path, journal)
    tool = _tool(tmp_path)
    plan = tool.scan(_opts(root, min_age_days=2), ctx)
    assert tool.apply(plan, ctx).count("done") == 1 and sent_to_trash == [str(root / "a.tmp")]


# --- Roots: the same folder by another name, and folders that must never be cleaned -------------


def test_the_short_and_long_names_of_one_folder_are_one_root(tmp_path: Path) -> None:
    long_root = tmp_path / "Long Temp Folder Name"
    _write(long_root / "old.tmp")
    _write(long_root / "inner folder" / "deep.tmp")
    short = _short_name(long_root)
    plan = TempCleaner(environ={}).scan(
        TempCleanerOptions(
            roots=[short, long_root, _short_name(long_root / "inner folder")],
            list_browser_caches=False,
            remove_empty_folders=False,
        ),
        _ctx(tmp_path),
    )
    assert len(plan.roots) == 1
    names = sorted(a.src.name for a in plan.actions if a.src)
    assert names == ["deep.tmp", "old.tmp"]  # each file planned once


def test_temp_given_as_a_short_name_and_localappdata_temp_are_one_default_root(
    tmp_path: Path,
) -> None:
    local = tmp_path / "Local App Data"
    temp = local / "Temp"
    _write(temp / "old.tmp")
    environ = {"TEMP": str(_short_name(temp)), "TMP": str(temp), "LOCALAPPDATA": str(local)}
    plan = TempCleaner(environ=environ).scan(
        TempCleanerOptions(list_browser_caches=False), _ctx(tmp_path)
    )
    assert len(plan.roots) == 1 and [a.src.name for a in plan.actions if a.src] == ["old.tmp"]


@pytest.mark.parametrize("variable", ["LOCALAPPDATA", "APPDATA", "PROGRAMDATA"])
def test_app_data_folders_themselves_are_refused_as_roots(tmp_path: Path, variable: str) -> None:
    folder = tmp_path / variable.lower()
    _write(folder / "SomeApp" / "settings.db", age_days=30)
    tool = TempCleaner(environ={variable: str(folder)})
    plan = tool.scan(TempCleanerOptions(roots=[folder], list_browser_caches=False), _ctx(tmp_path))
    assert plan.roots == [] and plan.actions == []
    assert any(n.startswith("Refused") and f"%{variable}%" in n for n in plan.notes)
    assert (folder / "SomeApp" / "settings.db").exists()


def test_a_folder_that_holds_the_temp_folder_is_refused(tmp_path: Path) -> None:
    profile_appdata = tmp_path / "AppData"
    temp = profile_appdata / "Local" / "Temp"
    _write(temp / "old.tmp")
    _write(profile_appdata / "Roaming" / "App" / "data.db")
    tool = TempCleaner(environ={"TEMP": str(temp)})
    for chosen in (profile_appdata, profile_appdata / "Local"):
        plan = tool.scan(
            TempCleanerOptions(roots=[chosen], list_browser_caches=False), _ctx(tmp_path)
        )
        assert plan.roots == [] and plan.actions == []
        assert any("holds the temp folder" in n for n in plan.notes)


def test_a_folder_that_holds_a_short_named_temp_folder_is_refused(tmp_path: Path) -> None:
    holder = tmp_path / "Profile Folder"
    temp = holder / "Temp Files"
    _write(temp / "old.tmp")
    tool = TempCleaner(environ={"TEMP": str(_short_name(temp))})
    plan = tool.scan(TempCleanerOptions(roots=[holder], list_browser_caches=False), _ctx(tmp_path))
    assert plan.roots == [] and any("holds the temp folder" in n for n in plan.notes)


def test_the_temp_folders_themselves_stay_allowed(tmp_path: Path) -> None:
    local = tmp_path / "local"
    temp, tmp = local / "Temp", tmp_path / "tmp-var"
    _write(temp / "a.tmp")
    _write(tmp / "b.tmp")
    _write(local / "Temp" / "sub" / "c.tmp")
    tool = TempCleaner(environ={"TEMP": str(temp), "TMP": str(tmp), "LOCALAPPDATA": str(local)})
    plan = tool.scan(
        TempCleanerOptions(roots=[temp, tmp, temp / "sub"], list_browser_caches=False),
        _ctx(tmp_path),
    )
    assert plan.roots == [temp, tmp]
    assert sorted(a.src.name for a in plan.actions if a.src) == ["a.tmp", "b.tmp", "c.tmp"]


# --- Apply re-checks every plan root (a saved or edited plan) -----------------------------------


def _plan_for(root: Path, tmp_path: Path, ctx: RunContext) -> Plan:
    """A plan scanned for ``root`` with an empty environment (so nothing is refused yet)."""
    return TempCleaner(environ={}).scan(_opts(root), ctx)


@pytest.mark.parametrize("variable", ["LOCALAPPDATA", "APPDATA", "PROGRAMDATA"])
def test_apply_refuses_a_plan_whose_root_is_an_app_data_folder(
    tmp_path: Path, journal: Journal, sent_to_trash: list[str], variable: str
) -> None:
    folder = tmp_path / variable.lower()
    settings_db = _write(folder / "SomeApp" / "settings.db", age_days=30)
    ctx = _ctx(tmp_path, journal)
    plan = Plan.from_json(_plan_for(folder, tmp_path, ctx).to_json())  # saved, then loaded
    assert plan.selected_actions(), "the edited plan must have something to refuse"
    tool = TempCleaner(environ={variable: str(folder)})
    result = tool.apply(plan, ctx)
    assert sent_to_trash == [] and settings_db.exists()
    assert result.count("done") == 0
    assert result.count("skipped") == len(plan.selected_actions())
    assert all(f"%{variable}%" in (o.detail or "") for o in result.outcomes)
    rows = journal.actions(result.run_id)
    assert rows and all(r.status == "skipped" for r in rows)


def test_apply_refuses_a_plan_root_that_holds_the_temp_folder(
    tmp_path: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    profile_appdata = tmp_path / "AppData"
    temp = profile_appdata / "Local" / "Temp"
    _write(temp / "old.tmp")
    data = _write(profile_appdata / "Roaming" / "App" / "data.db")
    ctx = _ctx(tmp_path, journal)
    plan = _plan_for(profile_appdata, tmp_path, ctx)
    result = TempCleaner(environ={"TEMP": str(temp)}).apply(plan, ctx)
    assert sent_to_trash == [] and data.exists()
    assert result.count("done") == 0
    assert all("holds the temp folder" in (o.detail or "") for o in result.outcomes)


def test_apply_refuses_a_plan_root_inside_a_browser_cache(
    tmp_path: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    local = tmp_path / "local"
    caches = _browsers(local)
    ctx = _ctx(tmp_path, journal)
    plan = _plan_for(caches["chrome"], tmp_path, ctx)
    assert plan.selected_actions()
    result = TempCleaner(environ={"LOCALAPPDATA": str(local)}).apply(plan, ctx)
    assert sent_to_trash == [] and (caches["chrome"] / "data_0").exists()
    assert result.count("done") == 0
    assert all("browser cache" in (o.detail or "").casefold() for o in result.outcomes)


def test_apply_refuses_rows_inside_a_browser_cache_under_an_allowed_root(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    local = tmp_path / "local"
    caches = _browsers(local)
    user_data = local / "Google" / "Chrome" / "User Data"  # holds caches, is not one
    ordinary = _write(user_data / "old.tmp")
    ctx = _ctx(tmp_path, journal)
    plan = _plan_for(user_data, tmp_path, ctx)  # an empty environment: the caches are planned
    assert any(a.src is not None and is_under(a.src, caches["chrome"]) for a in plan.actions)
    result = TempCleaner(environ={"LOCALAPPDATA": str(local)}).apply(plan, ctx)
    assert sent_to_trash == [str(ordinary)]
    assert (caches["chrome"] / "data_0").exists() and (caches["chrome2"] / "data_0").exists()
    assert any("browser cache" in (o.detail or "") for o in result.outcomes)


def is_under(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
    except ValueError:
        return False
    return True


def test_apply_still_cleans_an_allowed_root(
    tmp_path: Path, root: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    _write(root / "a.tmp")
    ctx = _ctx(tmp_path, journal)
    plan = _plan_for(root, tmp_path, ctx)
    other = tmp_path / "elsewhere"
    result = TempCleaner(environ={"LOCALAPPDATA": str(other)}).apply(plan, ctx)
    assert result.count("done") == 1 and not (root / "a.tmp").exists()
