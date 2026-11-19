"""Fix File Extensions: the plan, name clashes, cloud-only files, families, apply and undo."""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from various_tools.core import onedrive
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.onedrive import SyncStatus
from various_tools.core.plan import ActionKind, Plan
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.wrong_extension import TOOL, WrongExtensionOptions

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32
ZIP = b"PK\x03\x04" + b"\x00" * 32
MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 32
SEVEN_Z = b"7z\xbc\xaf\x27\x1c\x00\x04" + b"\x00" * 32


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


def _opts(root: Path, **fields: Any) -> WrongExtensionOptions:
    return WrongExtensionOptions.model_validate({"roots": [root], **fields})


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _renames(plan: Plan, root: Path) -> dict[str, str]:
    return {
        a.src.relative_to(root).as_posix(): a.dst.relative_to(root).as_posix()
        for a in plan.actions
        if a.src is not None and a.dst is not None
    }


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_tool_is_registered_with_plain_words() -> None:
    assert discover()["wrong_extension"] is TOOL
    assert TOOL.category == "Organize"
    assert TOOL.scan_label == "Look for wrong extensions"
    assert TOOL.found_nouns == ("file to rename", "files to rename")


def test_a_png_named_jpg_is_renamed_to_png(tmp_path: Path, root: Path) -> None:
    _write(root / "photo.jpg", PNG)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _renames(plan, root) == {"photo.jpg": "photo.png"}
    assert plan.actions[0].kind is ActionKind.RENAME and plan.actions[0].selected
    assert "PNG" in plan.actions[0].reason.upper()


def test_matching_and_equivalent_extensions_are_left_alone(tmp_path: Path, root: Path) -> None:
    _write(root / "a.jpeg", JPEG)
    _write(root / "b.JPG", JPEG)
    _write(root / "c.jfif", JPEG)
    _write(root / "d.docx", ZIP)
    _write(root / "e.xlsx", ZIP)
    _write(root / "f.zip", ZIP)
    _write(root / "g.pdf", PDF)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert plan.actions == []


def test_unknown_content_is_left_alone(tmp_path: Path, root: Path) -> None:
    _write(root / "notes.jpg", b"this is just text, not a picture")
    _write(root / "empty.png", b"")
    _write(root / "no_extension", PNG)
    _write(root / "data.bin", PNG)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert plan.actions == []


def test_a_zip_named_docx_is_fine_but_a_pdf_named_docx_is_not(tmp_path: Path, root: Path) -> None:
    _write(root / "real.docx", ZIP)
    _write(root / "fake.docx", PDF)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _renames(plan, root) == {"fake.docx": "fake.pdf"}


def test_name_collision_gets_a_unique_name(tmp_path: Path, root: Path) -> None:
    _write(root / "photo.jpg", PNG)
    _write(root / "photo.png", PNG)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _renames(plan, root) == {"photo.jpg": "photo_dup1.png"}


def test_two_files_wanting_the_same_name_do_not_clash(tmp_path: Path, root: Path) -> None:
    _write(root / "a.jpg", PNG)
    _write(root / "a.gif", PNG)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    targets = list(_renames(plan, root).values())
    assert len(targets) == 2 and len({t.casefold() for t in targets}) == 2
    assert "a.png" in targets


def test_cloud_only_file_is_never_opened_and_is_reported(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = _write(root / "cloud.jpg", PNG)
    _write(root / "local.jpg", PNG)
    real_status = onedrive.get_sync_status
    monkeypatch.setattr(
        onedrive,
        "get_sync_status",
        lambda p: SyncStatus.CLOUD_ONLY if Path(p) == cloud else real_status(p),
    )
    real_open = builtins.open

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if str(file).replace("\\\\?\\", "").endswith("cloud.jpg"):
            raise AssertionError("a cloud-only file was opened")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _renames(plan, root) == {"local.jpg": "local.png"}
    assert any("skipped: cloud-only" in note for note in plan.notes)
    assert plan.cloud_only == [cloud]


def test_family_filter(tmp_path: Path, root: Path) -> None:
    _write(root / "pic.jpg", PNG)
    _write(root / "song.wav", MP3)
    _write(root / "doc.docx", PDF)
    _write(root / "pack.zip", SEVEN_Z)
    everything = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _renames(everything, root) == {
        "pic.jpg": "pic.png",
        "song.wav": "song.mp3",
        "doc.docx": "doc.pdf",
        "pack.zip": "pack.7z",
    }
    photos = TOOL.scan(_opts(root, families=["photos"]), _ctx(tmp_path))
    assert _renames(photos, root) == {"pic.jpg": "pic.png"}
    both = TOOL.scan(_opts(root, families=["audio", "archives"]), _ctx(tmp_path))
    assert _renames(both, root) == {"song.wav": "song.mp3", "pack.zip": "pack.7z"}


def test_never_touch_folders_are_skipped(tmp_path: Path, root: Path) -> None:
    _write(root / ".git" / "x.jpg", PNG)
    _write(root / "node_modules" / "y.jpg", PNG)
    _write(root / "src" / "z.jpg", PNG)
    plan = TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _renames(plan, root) == {"src/z.jpg": "src/z.png"}


def test_scan_changes_nothing_on_disk(tmp_path: Path, root: Path) -> None:
    _write(root / "a.jpg", PNG)
    before = _files(root)
    TOOL.scan(_opts(root), _ctx(tmp_path))
    assert _files(root) == before


def test_apply_renames_and_undo_restores(tmp_path: Path, root: Path, journal: Journal) -> None:
    _write(root / "a.jpg", PNG)
    _write(root / "sub" / "b.png", JPEG)
    _write(root / "ok.jpeg", JPEG)
    before = _files(root)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    result = TOOL.apply(plan, ctx)
    assert result.status == "done" and result.count("done") == 2
    assert _files(root) == {"a.png": PNG, "sub/b.jpg": JPEG, "ok.jpeg": JPEG}
    assert [a.kind for a in journal.actions(result.run_id)] == ["rename", "rename"]

    report = undo_run(journal, result.run_id)
    assert report.reverted == 2 and report.failed == ()
    assert _files(root) == before


def test_unchecked_renames_are_not_applied(tmp_path: Path, root: Path, journal: Journal) -> None:
    _write(root / "a.jpg", PNG)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(root), ctx)
    plan.actions[0].selected = False
    TOOL.apply(plan, ctx)
    assert _files(root) == {"a.jpg": PNG}


def test_protected_roots_are_refused(tmp_path: Path) -> None:
    plan = TOOL.scan(
        WrongExtensionOptions.model_validate({"roots": [Path(tmp_path.anchor)]}), _ctx(tmp_path)
    )
    assert plan.actions == [] and plan.roots == [] and plan.notes
