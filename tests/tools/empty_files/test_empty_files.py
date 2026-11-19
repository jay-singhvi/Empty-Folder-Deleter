"""Empty Files: finds 0-byte files, keeps the on-purpose ones, and obeys the safety rules.

Every tree lives under ``tmp_path``. Trash calls go to a fake Recycle Bin inside the temp folder.
"""

from __future__ import annotations

import builtins
import os
import struct
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.plan import ActionKind, Plan
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.empty_files import TOOL, EmptyFilesOptions

_EPOCH_AS_FILETIME = 116444736000000000


class FakeBin:
    """Stands in for the Recycle Bin: trashing writes a Windows-style ``$I``/``$R`` pair."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.trashed: list[Path] = []

    def trash(self, path: Path | str) -> None:
        path = Path(path)
        self.folder.mkdir(parents=True, exist_ok=True)
        content = path.read_bytes()
        key = uuid.uuid4().hex[:6].upper()
        item = self.folder / f"$R{key}{path.suffix}"
        text = str(path) + "\x00"
        when = int(datetime.now(UTC).timestamp() * 10_000_000) + _EPOCH_AS_FILETIME
        header = struct.pack("<qqqi", 2, len(content), when, len(text))
        (self.folder / f"$I{key}{path.suffix}").write_bytes(header + text.encode("utf-16-le"))
        path.replace(item)
        self.trashed.append(path)

    def locate(self, _path: Path) -> Path | None:
        return self.folder


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBin:
    bin_ = FakeBin(tmp_path / "fake-bin")
    monkeypatch.setattr(plan_module, "send2trash", bin_.trash)
    return bin_


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


def _ctx(tmp_path: Path, journal: Journal | None = None) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


def _opts(root: Path, **fields: Any) -> EmptyFilesOptions:
    return EmptyFilesOptions.model_validate({"roots": [root], **fields})


def _touch(path: Path, content: bytes = b"", age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if age_days:
        then = time.time() - age_days * 86400
        os.utime(path, (then, then))
    return path


def _names(plan: Plan, root: Path) -> set[str]:
    return {a.src.relative_to(root).as_posix() for a in plan.actions if a.src is not None}


def _listing(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def test_tool_is_registered_with_plain_words() -> None:
    assert discover()["empty_files"] is TOOL
    assert TOOL.category == "Clean up"
    assert TOOL.scan_label == "Look for empty files"
    assert TOOL.found_nouns == ("empty file", "empty files")


def test_finds_zero_byte_files_and_ignores_the_rest(tmp_path: Path, root: Path) -> None:
    _touch(root / "empty.txt")
    _touch(root / "sub" / "deep" / "also_empty.log")
    _touch(root / "full.txt", b"data")
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _names(plan, root) == {"empty.txt", "sub/deep/also_empty.log"}
    assert all(a.kind is ActionKind.TRASH and a.selected for a in plan.actions)


def test_keep_names_are_left_alone_regardless_of_case(tmp_path: Path, root: Path) -> None:
    for name in ("__init__.py", ".gitkeep", ".keep", "py.typed", ".nojekyll", "PY.TYPED"):
        _touch(root / "pkg" / name)
    _touch(root / "pkg" / "other.py")
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _names(plan, root) == {"pkg/other.py"}


def test_keep_names_can_be_changed(tmp_path: Path, root: Path) -> None:
    _touch(root / "__init__.py")
    plan = TOOL.scan(_opts(root, keep_names=[]), _ctx(tmp_path))
    assert _names(plan, root) == {"__init__.py"}


def test_never_touch_folders_are_skipped(tmp_path: Path, root: Path) -> None:
    _touch(root / ".git" / "empty_ref")
    _touch(root / "node_modules" / "pkg" / "empty.js")
    _touch(root / "src" / "empty.txt")
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _names(plan, root) == {"src/empty.txt"}


def test_minimum_age_keeps_recent_files(tmp_path: Path, root: Path) -> None:
    _touch(root / "old.txt", age_days=30)
    _touch(root / "new.txt")
    plan = TOOL.scan(_opts(root, min_age_days=7), _ctx(tmp_path))
    assert _names(plan, root) == {"old.txt"}


def test_hidden_and_system_files_are_skipped_by_default(tmp_path: Path, root: Path) -> None:
    if os.name != "nt":
        pytest.skip("hidden and system attributes are a Windows feature")
    import ctypes

    hidden = _touch(root / "hidden.txt")
    system = _touch(root / "system.txt")
    _touch(root / "plain.txt")
    set_attributes = ctypes.windll.kernel32.SetFileAttributesW  # type: ignore[attr-defined]
    assert set_attributes(str(hidden), 0x02)
    assert set_attributes(str(system), 0x04)
    try:
        skipped = TOOL.scan(_opts(root), _ctx(tmp_path))
        assert _names(skipped, root) == {"plain.txt"}
        included = TOOL.scan(_opts(root, skip_hidden_system=False), _ctx(tmp_path))
        assert _names(included, root) == {"hidden.txt", "system.txt", "plain.txt"}
    finally:
        set_attributes(str(hidden), 0x80)
        set_attributes(str(system), 0x80)


def test_scan_changes_nothing_on_disk(tmp_path: Path, root: Path) -> None:
    _touch(root / "a.txt")
    _touch(root / "b" / "c.txt", b"x")
    before = _listing(root)
    mtimes = {p: p.stat().st_mtime_ns for p in root.rglob("*")}
    TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _listing(root) == before
    assert {p: p.stat().st_mtime_ns for p in root.rglob("*")} == mtimes


def test_scan_never_opens_files(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch(root / "a.txt")

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a file was opened")

    monkeypatch.setattr(builtins, "open", refuse)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _names(plan, root) == {"a.txt"}


def test_apply_trashes_and_undo_restores(
    tmp_path: Path, root: Path, journal: Journal, fake_bin: FakeBin
) -> None:
    _touch(root / "a.txt")
    _touch(root / "sub" / "b.txt")
    _touch(root / "keep.txt", b"data")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    result = TOOL.apply(plan, ctx)
    assert result.status == "done" and result.count("done") == 2
    assert _listing(root) == {"keep.txt", "sub"}
    assert len(fake_bin.trashed) == 2
    assert {a.kind for a in journal.actions(result.run_id)} == {"trash"}

    report = undo_run(journal, result.run_id, locate_bin=fake_bin.locate)
    assert report.reverted == 2, report.skipped
    assert _listing(root) == {"a.txt", "sub", "sub/b.txt", "keep.txt"}
    assert (root / "a.txt").stat().st_size == 0


def test_unchecked_files_are_not_trashed(
    tmp_path: Path, root: Path, journal: Journal, fake_bin: FakeBin
) -> None:
    _touch(root / "a.txt")
    _touch(root / "b.txt")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    plan.actions[0].selected = False
    TOOL.apply(plan, ctx)
    assert len(_listing(root)) == 1 and len(fake_bin.trashed) == 1


def test_a_root_that_is_an_empty_file_is_not_scanned(tmp_path: Path) -> None:
    empty = _touch(tmp_path / "rootfile")
    plan = TOOL.scan(EmptyFilesOptions.model_validate({"roots": [empty]}), _ctx(tmp_path))
    assert plan.actions == [] and plan.roots == [] and empty.exists()


def test_protected_roots_are_refused(tmp_path: Path) -> None:
    drive_root = Path(tmp_path.anchor)
    plan = TOOL.scan(EmptyFilesOptions.model_validate({"roots": [drive_root]}), _ctx(tmp_path))
    assert plan.actions == [] and plan.roots == []
    assert plan.notes
