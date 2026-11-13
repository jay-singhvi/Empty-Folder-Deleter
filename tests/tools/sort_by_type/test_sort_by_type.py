"""Sort by type: rule parsing, the plan, name clashes, safety rules, apply and undo.

Every tree is generated under ``tmp_path``. Nothing here reads or moves a real user folder.
"""

from __future__ import annotations

import builtins
import ctypes
import io
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.plan import ActionKind, Plan
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.sort_by_type import (
    DEFAULT_RULES,
    TOOL,
    SortByTypeOptions,
    parse_rule,
)
from various_tools.tools.sort_by_type import tool as sbt_tool

OFFLINE = 0x1000
DAY = 86400


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


def _opts(root: Path, **fields: Any) -> SortByTypeOptions:
    return SortByTypeOptions.model_validate({"roots": [root], **fields})


def _write(path: Path, text: str = "x", age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age_days:
        when = time.time() - age_days * DAY
        os.utime(path, (when, when))
    return path


def _moves(plan: Plan, base: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for action in plan.actions:
        assert action.src is not None and action.dst is not None
        result[action.src.relative_to(base).as_posix()] = action.dst.relative_to(base).as_posix()
    return result


def _files(base: Path) -> dict[str, str]:
    return {
        p.relative_to(base).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


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


# --- Rules -----------------------------------------------------------------------------------


def test_the_tool_is_registered_in_organize() -> None:
    tool = discover()["sort_by_type"]
    assert tool is TOOL and tool.category == "Organize"


def test_every_default_rule_parses_and_uses_a_safe_folder() -> None:
    assert SortByTypeOptions().rules == list(DEFAULT_RULES)
    folders: dict[str, str] = {}
    for line in DEFAULT_RULES:
        rule = parse_rule(line)
        for pattern in rule.patterns:
            folders[pattern] = rule.folder_text
    assert folders["*.pdf"] == "Documents/PDF"
    assert folders["*.docx"] == "Documents" and folders["*.txt"] == "Documents"
    assert folders["*.xlsx"] == "Spreadsheets" and folders["*.csv"] == "Spreadsheets"
    assert folders["*.heic"] == "Pictures" and folders["*.webp"] == "Pictures"
    assert folders["*.mkv"] == "Videos" and folders["*.flac"] == "Music"
    assert folders["*.7z"] == "Archives" and folders["*.msi"] == "Installers"


def test_parse_rule() -> None:
    rule = parse_rule(" *.JPG | *.png = Pictures\\Holiday/2024 ")
    assert rule.patterns == ("*.JPG", "*.png")
    assert rule.folder == ("Pictures", "Holiday", "2024")
    assert rule.matches("a.jpg") == "*.JPG" and rule.matches("B.PNG") == "*.png"
    assert rule.matches("a.gif") is None
    assert parse_rule("report*=Docs").matches("Report final.docx") == "report*"
    assert parse_rule("*.pdf=Docs/").folder == ("Docs",)
    assert parse_rule("*.pdf=Docs /PDF ").folder == ("Docs", "PDF")  # spaces around a name go


@pytest.mark.parametrize(
    "line",
    [
        "no equals sign",
        "*.pdf=",
        "=Docs",
        "*.pdf|=Docs",
        "*.pdf=../Docs",
        "*.pdf=Docs/../../x",
        "*.pdf=C:/Docs",
        "*.pdf=C:Docs",
        "*.pdf=/Docs",
        "*.pdf=\\\\server\\share",
        "*.pdf=Docs//PDF",
        "*.pdf=Docs/./PDF",
        "*.pdf=Do<cs",
        'x.pdf=Do"cs',
        "*.pdf=Docs?",
        "*.pdf=Docs.",
        "*.pdf=CON",
        "*.pdf=Docs/nul.txt",
        "sub/*.pdf=Docs",
        "*.pdf=" + "a" * 300,
    ],
)
def test_unsafe_or_malformed_rules_are_rejected(line: str) -> None:
    with pytest.raises(ValueError):
        parse_rule(line)
    with pytest.raises(ValidationError):
        SortByTypeOptions(rules=[line])


def test_option_validation() -> None:
    for bad in (
        {"unmatched": "delete"},
        {"clash_handling": "overwrite"},
        {"clash_suffix": ""},
        {"clash_suffix": "a/b"},
        {"clash_suffix": " x"},
        {"other_folder": ".."},
        {"other_folder": "C:\\Other"},
        {"min_age_days": -1},
        {"min_age_days": 40000},
        {"surprise": True},
    ):
        with pytest.raises(ValidationError):
            SortByTypeOptions.model_validate(bad)
    ok = SortByTypeOptions(rules=[], other_folder="Misc/Unsorted")
    assert ok.rules == [] and ok.other_folder == "Misc/Unsorted"


def test_defaults() -> None:
    opts = SortByTypeOptions()
    assert opts.recursive is False and opts.unmatched == "leave"
    assert opts.other_folder == "Other" and opts.clash_handling == "rename"
    assert opts.clash_suffix == "_dup" and opts.min_age_days == 0
    assert opts.skip_hidden_system is True and opts.skip_dot_files is True
    assert ".git" in opts.exclude_globs and "*.crdownload" in opts.exclude_globs


# --- Plan contents ---------------------------------------------------------------------------


def test_files_in_the_folder_are_sorted_by_the_default_rules(tmp_path: Path, root: Path) -> None:
    for name in (
        "cv.pdf",
        "letter.DOCX",
        "budget.xlsx",
        "IMG_1.JPG",
        "clip.mkv",
        "song.flac",
        "pack.zip",
        "setup.exe",
        "raw.CR2",
        "mystery.xyz",
    ):
        _write(root / name)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _moves(plan, root) == {
        "cv.pdf": "Documents/PDF/cv.pdf",
        "letter.DOCX": "Documents/letter.DOCX",
        "budget.xlsx": "Spreadsheets/budget.xlsx",
        "IMG_1.JPG": "Pictures/IMG_1.JPG",
        "clip.mkv": "Videos/clip.mkv",
        "song.flac": "Music/song.flac",
        "pack.zip": "Archives/pack.zip",
        "setup.exe": "Installers/setup.exe",
        "raw.CR2": "Pictures/RAW/raw.CR2",
    }
    assert all(a.kind is ActionKind.MOVE and a.selected for a in plan.actions)
    assert plan.roots == [root]
    assert plan.notes[0].startswith("Sort by type: 9 file(s) to move into 9 folder(s)")
    assert "1 match no rule and stay" in plan.notes[0]


def test_reason_and_group_name_the_rule_and_folder(tmp_path: Path, root: Path) -> None:
    _write(root / "cv.pdf")
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert plan.actions[0].reason == "*.pdf -> Documents/PDF"
    assert plan.actions[0].group_id == "Documents/PDF"


def test_the_first_matching_rule_wins(tmp_path: Path, root: Path) -> None:
    _write(root / "scan-2024.pdf")
    _write(root / "plain.pdf")
    rules = ["scan-*=Scans", "*.pdf=Documents/PDF"]
    plan = TOOL.scan(_opts(root, rules=rules), _ctx(tmp_path))
    assert _moves(plan, root) == {
        "scan-2024.pdf": "Scans/scan-2024.pdf",
        "plain.pdf": "Documents/PDF/plain.pdf",
    }
    swapped = TOOL.scan(_opts(root, rules=list(reversed(rules))), _ctx(tmp_path))
    assert _moves(swapped, root)["scan-2024.pdf"] == "Documents/PDF/scan-2024.pdf"


def test_custom_rules_replace_the_defaults(tmp_path: Path, root: Path) -> None:
    _write(root / "a.pdf")
    _write(root / "b.log")
    plan = TOOL.scan(_opts(root, rules=["*.log=Logs"]), _ctx(tmp_path))
    assert _moves(plan, root) == {"b.log": "Logs/b.log"}


def test_unmatched_files_stay_or_go_to_the_other_folder(tmp_path: Path, root: Path) -> None:
    _write(root / "a.pdf")
    _write(root / "weird.xyz")
    _write(root / "README")
    left = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert set(_moves(left, root)) == {"a.pdf"}
    other = TOOL.scan(_opts(root, unmatched="other"), _ctx(tmp_path))
    assert _moves(other, root) == {
        "a.pdf": "Documents/PDF/a.pdf",
        "weird.xyz": "Other/weird.xyz",
        "README": "Other/README",
    }
    named = TOOL.scan(_opts(root, unmatched="other", other_folder="Misc/Rest"), _ctx(tmp_path))
    assert _moves(named, root)["weird.xyz"] == "Misc/Rest/weird.xyz"


def test_subfolders_need_recursive(tmp_path: Path, root: Path) -> None:
    _write(root / "top.pdf")
    _write(root / "sub" / "deep" / "inner.pdf")
    flat = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert set(_moves(flat, root)) == {"top.pdf"}
    deep = TOOL.scan(_opts(root, recursive=True), _ctx(tmp_path))
    assert _moves(deep, root) == {
        "top.pdf": "Documents/PDF/top.pdf",
        "sub/deep/inner.pdf": "Documents/PDF/inner.pdf",
    }


def test_recursive_never_resorts_files_in_destination_folders(
    tmp_path: Path, root: Path, journal: Journal
) -> None:
    _write(root / "Documents" / "PDF" / "done.pdf")
    _write(root / "Documents" / "stray.pdf")  # inside a destination folder: left alone
    _write(root / "Documents" / "Taxes" / "t.pdf")
    _write(root / "Pictures" / "RAW" / "r.cr2")
    _write(root / "loose.pdf")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root, recursive=True), ctx)
    assert _moves(plan, root) == {"loose.pdf": "Documents/PDF/loose.pdf"}
    TOOL.apply(plan, ctx)
    second = TOOL.scan(_opts(root, recursive=True), ctx)
    assert second.actions == []
    assert second.notes[0].startswith("Sort by type: 0 file(s)")


def test_the_other_folder_is_also_never_entered(tmp_path: Path, root: Path) -> None:
    _write(root / "Other" / "kept.xyz")
    _write(root / "new.xyz")
    plan = TOOL.scan(_opts(root, recursive=True, unmatched="other"), _ctx(tmp_path))
    assert _moves(plan, root) == {"new.xyz": "Other/new.xyz"}


def test_destination_folders_are_matched_without_regard_to_case(tmp_path: Path, root: Path) -> None:
    _write(root / "documents" / "pdf" / "x.pdf")
    plan = TOOL.scan(_opts(root, recursive=True), _ctx(tmp_path))
    assert plan.actions == []


def test_skipped_kinds(tmp_path: Path, root: Path) -> None:
    _write(root / "ok.pdf")
    _write(root / ".hidden.pdf")
    _write(root / "partial.pdf.crdownload")
    _write(root / "temp.tmp")
    _write(root / "node_modules" / "pkg" / "readme.txt")
    _write(root / "fresh.pdf")
    hidden = _write(root / "attr.pdf")
    _set_hidden(hidden)
    plan = TOOL.scan(_opts(root, recursive=True), _ctx(tmp_path))
    assert set(_moves(plan, root)) == {"ok.pdf", "fresh.pdf"}
    assert "2 hidden or dot item(s) skipped" in plan.notes[0]
    everything = TOOL.scan(
        _opts(
            root,
            recursive=True,
            skip_hidden_system=False,
            skip_dot_files=False,
            exclude_globs=[],
            rules=["*.pdf=Docs"],
        ),
        _ctx(tmp_path),
    )
    assert set(_moves(everything, root)) == {
        "ok.pdf",
        ".hidden.pdf",
        "fresh.pdf",
        "attr.pdf",
    }


def test_hidden_folders_are_not_entered(tmp_path: Path, root: Path) -> None:
    _write(root / "secret" / "a.pdf")
    _set_hidden(root / "secret")
    plan = TOOL.scan(_opts(root, recursive=True), _ctx(tmp_path))
    assert plan.actions == []


def test_min_age_leaves_recent_files(tmp_path: Path, root: Path) -> None:
    _write(root / "old.pdf", age_days=40)
    _write(root / "new.pdf", age_days=1)
    plan = TOOL.scan(_opts(root, min_age_days=30), _ctx(tmp_path))
    assert set(_moves(plan, root)) == {"old.pdf"}
    assert "1 changed too recently" in plan.notes[0]


# --- Name clashes ----------------------------------------------------------------------------


def test_clashes_with_files_on_disk_and_inside_the_batch_get_numbered_names(
    tmp_path: Path, root: Path, journal: Journal
) -> None:
    _write(root / "Documents" / "PDF" / "a.pdf", "existing")
    _write(root / "one" / "a.pdf", "one")
    _write(root / "two" / "a.pdf", "two")
    _write(root / "a.pdf", "top")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root, recursive=True), ctx)
    destinations = sorted(a.dst.name for a in plan.actions if a.dst)
    assert destinations == ["a_dup1.pdf", "a_dup2.pdf", "a_dup3.pdf"]
    assert "3 renamed because the name was taken" in plan.notes[0]
    assert all("renamed to" in a.reason for a in plan.actions)

    result = TOOL.apply(plan, ctx)
    assert result.status == "done"
    folder = root / "Documents" / "PDF"
    assert sorted(p.name for p in folder.iterdir()) == [
        "a.pdf",
        "a_dup1.pdf",
        "a_dup2.pdf",
        "a_dup3.pdf",
    ]
    assert (folder / "a.pdf").read_text(encoding="utf-8") == "existing"
    assert sorted(p.read_text(encoding="utf-8") for p in folder.iterdir()) == [
        "existing",
        "one",
        "top",
        "two",
    ]


def test_clash_names_are_compared_without_regard_to_case(tmp_path: Path, root: Path) -> None:
    _write(root / "Documents" / "PDF" / "REPORT.PDF")
    _write(root / "report.pdf")
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert plan.actions[0].dst == root / "Documents" / "PDF" / "report_dup1.pdf"


def test_the_clash_suffix_is_an_option(tmp_path: Path, root: Path) -> None:
    _write(root / "Documents" / "PDF" / "a.pdf")
    _write(root / "a.pdf")
    plan = TOOL.scan(_opts(root, clash_suffix="_copy"), _ctx(tmp_path))
    assert plan.actions[0].dst is not None and plan.actions[0].dst.name == "a_copy1.pdf"


def test_skip_leaves_files_whose_name_is_taken(tmp_path: Path, root: Path) -> None:
    _write(root / "Documents" / "PDF" / "a.pdf")
    _write(root / "a.pdf")
    _write(root / "one" / "b.pdf")
    _write(root / "two" / "b.pdf")
    plan = TOOL.scan(_opts(root, recursive=True, clash_handling="skip"), _ctx(tmp_path))
    assert _moves(plan, root) == {"one/b.pdf": "Documents/PDF/b.pdf"}
    assert "2 left because the name is taken" in plan.notes[0]
    assert any("Left in place, name is taken" in n for n in plan.notes)


def test_many_clashes_do_not_flood_the_notes(tmp_path: Path, root: Path) -> None:
    _write(root / "Documents" / "PDF" / "a.pdf")
    for index in range(15):
        _write(root / f"d{index:02d}" / "a.pdf")
    plan = TOOL.scan(_opts(root, recursive=True, clash_handling="skip"), _ctx(tmp_path))
    assert plan.actions == []
    taken = [n for n in plan.notes if n.startswith("Left in place, name is taken")]
    assert len(taken) == 10 and plan.notes[-1] == "... and 5 more name clashes"


# --- Safety ----------------------------------------------------------------------------------


def test_scan_changes_nothing_and_opens_no_file(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / "a.pdf")
    _write(root / "sub" / "b.jpg")
    before = _snapshot(root)
    real_open = builtins.open

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, str | Path) and str(file).startswith(str(root)):
            raise AssertionError(f"scan opened {file}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    TOOL.scan(_opts(root, recursive=True), _ctx(tmp_path))
    monkeypatch.undo()
    assert _snapshot(root) == before
    assert not (root / "Documents").exists()


def test_protected_roots_are_refused(tmp_path: Path, root: Path) -> None:
    guarded = tmp_path / "Guarded"
    _write(guarded / "a.pdf")
    options = SortByTypeOptions(roots=[guarded, Path(tmp_path.anchor), tmp_path / "missing"])
    plan = TOOL.scan(options, _ctx(tmp_path, protected_folders=[str(guarded)]))
    assert plan.roots == [] and plan.actions == []
    assert any("Protected folder refused" in n for n in plan.notes)
    assert any("drive root" in n for n in plan.notes)
    assert any("Not a folder" in n for n in plan.notes)
    assert plan.notes[-1] == "No folders to sort."


def test_a_junction_inside_the_folder_is_not_followed(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "secret.pdf")
    _write(root / "real.pdf")
    _junction(root / "link", outside)
    plan = TOOL.scan(_opts(root, recursive=True), _ctx(tmp_path))
    assert set(_moves(plan, root)) == {"real.pdf"}


def test_a_junction_as_a_destination_is_never_written_through(
    tmp_path: Path, root: Path, journal: Journal
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(root / "a.pdf")
    _junction(root / "Documents", outside)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    assert plan.actions == []
    assert any("junction or symlink" in n and "a.pdf" in n for n in plan.notes)
    assert "1 left because the destination is blocked" in plan.notes[0]
    assert list(outside.iterdir()) == [] and (root / "a.pdf").exists()


def test_the_engine_refuses_a_destination_that_became_a_junction_after_the_scan(
    tmp_path: Path, root: Path, journal: Journal
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(root / "a.pdf")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    _junction(root / "Documents", outside)
    result = TOOL.apply(plan, ctx)
    assert result.count("skipped") == 1 and list(outside.iterdir()) == []
    assert (root / "a.pdf").exists()


def test_a_file_in_the_way_of_a_destination_folder_is_reported(tmp_path: Path, root: Path) -> None:
    _write(root / "Documents")  # a file, where the folder would go
    _write(root / "a.pdf")
    _write(root / "b.jpg")
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert set(_moves(plan, root)) == {"b.jpg"}
    assert any("a file named 'Documents'" in n for n in plan.notes)


def test_cloud_only_files_are_skipped_not_moved(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / "local.pdf")
    _write(root / "cloud.pdf")

    def fake_attributes(path: Path, st: os.stat_result) -> int:
        return OFFLINE if path.name == "cloud.pdf" else 0

    monkeypatch.setattr(sbt_tool, "_attributes", fake_attributes)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert set(_moves(plan, root)) == {"local.pdf"}
    assert any(n == f"skipped: cloud-only: {root / 'cloud.pdf'}" for n in plan.notes)
    assert "1 cloud-only, skipped and not read" in plan.notes[0]


def test_nested_and_repeated_roots_are_collapsed(tmp_path: Path, root: Path) -> None:
    _write(root / "inner" / "a.pdf")
    options = SortByTypeOptions(roots=[root, root / "inner", root], recursive=True)
    plan = TOOL.scan(options, _ctx(tmp_path))
    assert plan.roots == [root]
    assert _moves(plan, root) == {"inner/a.pdf": "Documents/PDF/a.pdf"}


def test_each_root_is_sorted_inside_itself(tmp_path: Path, root: Path) -> None:
    other = tmp_path / "other"
    _write(root / "a.pdf")
    _write(other / "b.pdf")
    plan = TOOL.scan(SortByTypeOptions(roots=[root, other]), _ctx(tmp_path))
    destinations = {a.src.name: a.dst for a in plan.actions if a.src and a.dst}
    assert destinations == {
        "a.pdf": root / "Documents" / "PDF" / "a.pdf",
        "b.pdf": other / "Documents" / "PDF" / "b.pdf",
    }


def test_the_selected_folder_is_never_moved(tmp_path: Path, root: Path) -> None:
    folder = root / "Documents.pdf"
    folder.mkdir()
    _write(folder / "inner.txt")
    plan = TOOL.scan(_opts(root, recursive=True), _ctx(tmp_path))
    assert all(a.src != root and a.src != folder for a in plan.actions)
    assert set(_moves(plan, root)) == {"Documents.pdf/inner.txt"}


# --- Apply and undo --------------------------------------------------------------------------


def test_apply_moves_files_and_undo_restores_the_original_layout(
    tmp_path: Path, root: Path, journal: Journal
) -> None:
    _write(root / "cv.pdf", "cv")
    _write(root / "photo.jpg", "photo")
    _write(root / "notes" / "todo.txt", "todo")
    _write(root / "notes" / "deep" / "a.pdf", "deep pdf")
    _write(root / "keep.xyz", "unknown")
    before = _files(root)
    ctx = _ctx(tmp_path, journal)

    plan = TOOL.scan(_opts(root, recursive=True), ctx)
    result = TOOL.apply(plan, ctx)
    assert result.status == "done" and result.count("done") == 4
    assert _files(root) == {
        "Documents/PDF/cv.pdf": "cv",
        "Documents/PDF/a.pdf": "deep pdf",
        "Documents/todo.txt": "todo",
        "Pictures/photo.jpg": "photo",
        "keep.xyz": "unknown",
    }
    run = journal.run(result.run_id)
    assert run is not None and run.tool == "sort_by_type"
    assert [a.kind for a in journal.actions(result.run_id)] == ["move"] * 4

    report = undo_run(journal, result.run_id)
    assert report.reverted == 4 and report.skipped == () and report.failed == ()
    assert _files(root) == before


def test_unchecked_moves_are_not_applied(tmp_path: Path, root: Path, journal: Journal) -> None:
    _write(root / "a.pdf")
    _write(root / "b.pdf")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    plan.actions[0].selected = False
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1
    assert (root / "a.pdf").exists() and (root / "Documents" / "PDF" / "b.pdf").exists()


def test_a_name_taken_after_the_scan_is_never_overwritten(
    tmp_path: Path, root: Path, journal: Journal
) -> None:
    _write(root / "a.pdf", "mine")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    _write(root / "Documents" / "PDF" / "a.pdf", "arrived later")
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1
    assert (root / "Documents" / "PDF" / "a.pdf").read_text(encoding="utf-8") == "arrived later"
    assert (root / "Documents" / "PDF" / "a_dup1.pdf").read_text(encoding="utf-8") == "mine"


def test_a_saved_plan_can_be_applied_later(tmp_path: Path, root: Path, journal: Journal) -> None:
    _write(root / "a.pdf")
    ctx = _ctx(tmp_path, journal)
    loaded = Plan.from_json(TOOL.scan(_opts(root), ctx).to_json())
    result = TOOL.apply(loaded, ctx)
    assert result.count("done") == 1 and (root / "Documents" / "PDF" / "a.pdf").exists()


def test_a_junction_as_the_selected_folder_is_refused(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "x.pdf")
    link = tmp_path / "linkroot"
    _junction(link, outside)
    try:
        plan = TOOL.scan(_opts(link), _ctx(tmp_path))
        assert plan.actions == [] and plan.roots == []
    finally:
        os.rmdir(link)  # removes the junction only, never its target
    assert (outside / "x.pdf").exists()
