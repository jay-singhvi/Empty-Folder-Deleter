"""Tool pages for the Phase 7 tools: scan -> apply -> undo through the GUI for each of them.

Same pattern as ``test_tool_pages.py``. Every tree is generated under ``tmp_path``. Recycle-Bin
calls (apply, the tools' own recycling through ``core/plan.py``, and undo) go to the fake in
``conftest.py``, an autouse fixture, so it covers this file too. Undo finds no Recycle Bin to
restore from. Dialog hooks are replaced (``dialogs``), so nothing blocks.

Nothing here needs ffmpeg or Ollama. ExifTool is needed to strip metadata, so the stripper has a
"program missing" test that runs everywhere and a full test that is skipped without ExifTool.
The temp cleaner reads ``%TEMP%`` and ``%LOCALAPPDATA%`` when no folder is chosen: its tests point
those variables at ``tmp_path``, so the real temp folder and browser caches are never looked at.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import random
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from various_tools.core.plan import ActionKind
from various_tools.gui.state import AppState
from various_tools.gui.tool_page import ToolPage
from various_tools.tools.disk_usage import FOLDER_SIZE_PREFIX, ROOT_SIZE_PREFIX, parse_size_note
from various_tools.tools.temp_cleaner import BROWSER_CACHE_PREFIX, parse_browser_cache_note

if TYPE_CHECKING:
    from conftest import Dialogs

ImageMaker = Callable[..., Path]
OpenTool = Callable[..., ToolPage]
PageStep = Callable[[ToolPage], None]

DAY = 86400
RESTORE_BY_HAND = "restore manually from the Recycle Bin"


def tree_snapshot(root: Path) -> list[tuple[str, bool, int, int]]:
    """Every entry with its size and modified time, to prove a scan changed nothing."""
    result: list[tuple[str, bool, int, int]] = []
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        result.append((str(path.relative_to(root)), path.is_dir(), stat.st_size, stat.st_mtime_ns))
    return result


def files_of(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def write(path: Path, data: bytes | str = b"x", age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    if age_days:
        age(path, age_days)
    return path


def age(path: Path, days: float) -> None:
    """Make ``path`` look ``days`` old: the modified time and (on Windows) the creation time.

    The temp cleaner judges age on the newer of the two, so a fresh file whose modified time was
    moved back still counts as new unless its creation time moves back too.
    """
    when = time.time() - days * DAY
    os.utime(path, (when, when))
    set_created(path, when)


def set_created(path: Path, when: float) -> None:
    """Set the Windows creation time (``st_birthtime``) of a file or folder. A no-op elsewhere."""
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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def undo_text(dialogs: Dialogs) -> str:
    title, text = dialogs.informs[-1]
    assert title == "Undo finished"
    return text


# ----- checksum -------------------------------------------------------------------------------


def test_checksum_create_then_verify_through_the_page(
    tmp_path: Path,
    open_tool: OpenTool,
    dialogs: Dialogs,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    folder = tmp_path / "payload"
    contents = {"a.txt": b"alpha", "b.bin": bytes(range(256)), "sub/c.txt": b"charlie"}
    for name, data in contents.items():
        write(folder / name, data)
    before = tree_snapshot(folder)
    manifest = folder / "SHA256SUMS.txt"

    page = open_tool("checksum", str(folder))
    scan(page)
    assert page.plan is not None
    assert [a.kind for a in page.plan.actions] == [ActionKind.WRITE]
    action = page.plan.actions[0]
    assert action.dst == manifest and action.selected
    assert "listing 3 file(s)" in action.reason
    assert "1 manifest(s) ready to write for 3 file(s)" in page.notes_view.toPlainText()
    assert tree_snapshot(folder) == before  # nothing is hashed or written by a scan
    assert not manifest.exists()
    assert page.apply_button.isEnabled()

    apply(page)
    assert "write: 1" in dialogs.confirms[0][1]
    assert page.result is not None and page.result.status == "done"
    assert "wrote 3 entries" in page.result.outcomes[0].detail
    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert sorted(line.split("  ", 1)[1].lstrip("*") for line in lines) == sorted(contents)
    for line in lines:
        digest, name = line.split("  ", 1)
        assert digest == sha256(contents[name.lstrip("*")])

    # The same page can now verify what it wrote. Verify is read-only, so it has no actions.
    page.form.update_values({"mode": "verify"})
    scan(page)
    assert page.plan is not None and page.plan.actions == []
    notes = page.notes_view.toPlainText()
    assert "Verify: OK - 3 OK, 0 mismatched, 0 missing" in notes
    assert not page.apply_button.isEnabled()
    assert page.apply() is None

    # Changing a file and adding one shows up as a mismatch and an extra file.
    (folder / "a.txt").write_bytes(b"ALPHA")
    write(folder / "new.txt", b"new")
    scan(page)
    notes = page.notes_view.toPlainText()
    assert "Verify: PROBLEMS FOUND - 2 OK, 1 mismatched" in notes
    assert "mismatch: a.txt" in notes and "extra: new.txt" in notes
    assert not page.apply_button.isEnabled()

    # The written manifest is unchanged, so Undo sends it to the Recycle Bin (the test fake).
    assert not page.undo_button.isHidden()
    undo(page)
    assert "undone (1 reverted)" in undo_text(dialogs)
    assert not manifest.exists()
    assert page.undo_button.isHidden()


def test_checksum_never_replaces_an_existing_manifest(
    tmp_path: Path, open_tool: OpenTool, scan: PageStep
) -> None:
    folder = tmp_path / "payload"
    write(folder / "a.txt", b"alpha")
    write(folder / "SHA256SUMS.txt", b"keep me")
    page = open_tool("checksum", str(folder))
    scan(page)
    assert page.plan is not None and len(page.plan.actions) == 1
    assert not page.plan.actions[0].selected  # left unchecked: it would replace a file
    assert "never replaced" in page.notes_view.toPlainText()
    assert not page.apply_button.isEnabled()
    assert (folder / "SHA256SUMS.txt").read_bytes() == b"keep me"


# ----- corrupt_media --------------------------------------------------------------------------


@pytest.fixture
def media_tree(tmp_path: Path, make_image: ImageMaker) -> Path:
    """Good pictures, a truncated JPEG, a file of random bytes and a zero-byte PNG."""
    make_image("media/good.jpg", seed=1, size=(160, 120))
    make_image("media/good.png", seed=2)
    make_image("media/sub/good2.jpg", seed=3, size=(160, 120))
    cut = make_image("media/cut.jpg", seed=4, size=(160, 120))
    cut.write_bytes(cut.read_bytes()[: cut.stat().st_size // 2])
    write(tmp_path / "media" / "noise.jpg", random.Random(7).randbytes(4096))
    write(tmp_path / "media" / "empty.png", b"")
    write(tmp_path / "media" / "notes.txt", "not media")
    return tmp_path / "media"


def test_corrupt_media_quarantine_scan_apply_undo(
    open_tool: OpenTool,
    dialogs: Dialogs,
    media_tree: Path,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    page = open_tool("corrupt_media", str(media_tree))
    page.form.update_values({"media": "images"})  # no ffprobe needed
    before = tree_snapshot(media_tree)
    original = files_of(media_tree)
    scan(page)
    assert page.plan is not None
    assert sorted(a.src.name for a in page.plan.actions if a.src) == [
        "cut.jpg",
        "empty.png",
        "noise.jpg",
    ]
    assert {a.kind for a in page.plan.actions} == {ActionKind.MOVE}
    assert all(not a.selected for a in page.plan.actions)  # nothing is ticked by default
    assert "Checked 6 file(s) (standard): 3 corrupt" in page.notes_view.toPlainText()
    assert page.table.totals_text().startswith("3 actions, 0 selected")
    assert not page.apply_button.isEnabled()
    assert tree_snapshot(media_tree) == before
    assert not (media_tree / "_quarantine").exists()

    assert page.apply() is None  # nothing ticked: it only says so
    assert dialogs.confirms == []
    assert dialogs.informs[-1][0] == "Apply"

    assert page.select_all(True) == 3  # the Select all button
    apply(page)
    assert "move: 3" in dialogs.confirms[0][1]
    assert sorted(p.name for p in (media_tree / "_quarantine").iterdir()) == [
        "cut.jpg",
        "empty.png",
        "noise.jpg",
    ]
    assert sorted(p.name for p in media_tree.iterdir() if p.is_file()) == [
        "good.jpg",
        "good.png",
        "notes.txt",
    ]
    assert page.result is not None and page.result.count("done") == 3

    undo(page)
    assert "undone (3 reverted)" in dialogs.informs[-1][1]
    assert files_of(media_tree) == original  # every file is back, byte for byte
    assert page.undo_button.isHidden()


def test_corrupt_media_only_the_ticked_file_is_trashed_and_trash_is_not_undone(
    open_tool: OpenTool,
    dialogs: Dialogs,
    trashed: list[str],
    media_tree: Path,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    page = open_tool("corrupt_media", str(media_tree))
    page.form.update_values({"media": "images", "action": "trash"})
    scan(page)
    assert page.plan is not None
    assert {a.kind for a in page.plan.actions} == {ActionKind.TRASH}
    assert all(a.dst is None for a in page.plan.actions)
    index = next(i for i, a in enumerate(page.plan.actions) if a.src and a.src.name == "cut.jpg")
    page.table.set_action_checked(index, True)
    apply(page)
    assert "Recycle Bin" in dialogs.confirms[0][1]
    assert [Path(p).name for p in trashed] == ["cut.jpg"]
    assert (media_tree / "noise.jpg").is_file() and (media_tree / "empty.png").is_file()
    undo(page)
    text = undo_text(dialogs)  # there is no Recycle Bin to restore from in tests
    assert "not undone (0 reverted)" in text and RESTORE_BY_HAND in text
    assert not (media_tree / "cut.jpg").exists()


# ----- disk_usage -----------------------------------------------------------------------------


def test_disk_usage_select_largest_files_apply_and_trash_is_not_undone(
    tmp_path: Path,
    open_tool: OpenTool,
    dialogs: Dialogs,
    trashed: list[str],
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    root = tmp_path / "usage"
    write(root / "big.bin", b"x" * 3000)
    write(root / "mid.bin", b"x" * 2000)
    write(root / "small.txt", b"x" * 10)
    write(root / "sub" / "nested.mp4", b"x" * 1500)
    before = tree_snapshot(root)

    page = open_tool("disk_usage", str(root))
    page.form.update_values({"top_files": 3, "top_folders": 5})
    scan(page)
    assert page.plan is not None
    assert [a.src.name for a in page.plan.actions if a.src] == ["big.bin", "mid.bin", "nested.mp4"]
    assert {a.kind for a in page.plan.actions} == {ActionKind.TRASH}
    assert all(not a.selected for a in page.plan.actions)  # the person picks what to remove
    assert page.table.totals_text().startswith("3 actions, 0 selected")
    assert not page.apply_button.isEnabled()
    assert tree_snapshot(root) == before

    # The folder totals arrive as notes, in the documented format.
    parsed = [parse_size_note(n) for n in page.plan.notes]
    sizes: dict[str, list[tuple[int, Path]]] = {"folder-size": [], "root-size": []}
    for item in parsed:
        if item is not None:
            sizes[item[0]].append((item[1], item[2]))
    assert (6510, root) in sizes["root-size"]
    assert (1500, root / "sub") in sizes["folder-size"]
    assert any(n.startswith(ROOT_SIZE_PREFIX) for n in page.plan.notes)
    assert any(n.startswith(FOLDER_SIZE_PREFIX) for n in page.plan.notes)

    assert page.select_all(True) == 3
    index = next(i for i, a in enumerate(page.plan.actions) if a.src and a.src.name == "mid.bin")
    page.table.set_action_checked(index, False)  # keep this one
    assert page.table.totals_text().startswith("3 actions, 2 selected")
    apply(page)
    assert "trash: 2" in dialogs.confirms[0][1]
    assert sorted(Path(p).name for p in trashed) == ["big.bin", "nested.mp4"]
    assert sorted(p.name for p in root.rglob("*") if p.is_file()) == ["mid.bin", "small.txt"]

    undo(page)
    text = undo_text(dialogs)
    assert "not undone (0 reverted)" in text
    assert text.count(RESTORE_BY_HAND) == 2
    assert sorted(p.name for p in root.rglob("*") if p.is_file()) == ["mid.bin", "small.txt"]


# ----- media_convert --------------------------------------------------------------------------


@pytest.fixture
def webp_tree(tmp_path: Path) -> Path:
    folder = tmp_path / "pics"
    folder.mkdir()
    for number, colour in ((1, (200, 10, 10)), (2, (10, 10, 200))):
        Image.new("RGB", (64, 48), colour).save(folder / f"pic{number}.webp", format="WEBP")
    write(folder / "notes.txt", "not a picture")
    return folder


def test_media_convert_webp_to_jpg_keeps_the_originals_and_undo_reports_manual(
    open_tool: OpenTool,
    dialogs: Dialogs,
    webp_tree: Path,
    trashed: list[str],
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    page = open_tool("media_convert", str(webp_tree))
    page.form.update_values({"convert_mov": False, "keep_metadata": False, "jpeg_quality": 80})
    before = tree_snapshot(webp_tree)
    original = files_of(webp_tree)
    scan(page)
    assert page.plan is not None
    assert [(a.src.name, a.dst.name) for a in page.plan.actions if a.src and a.dst] == [
        ("pic1.webp", "pic1.jpg"),
        ("pic2.webp", "pic2.jpg"),
    ]
    assert {a.kind for a in page.plan.actions} == {ActionKind.CONVERT}
    assert all(a.selected for a in page.plan.actions)
    assert all(a.dst and a.dst.parent == webp_tree / "converted" for a in page.plan.actions)
    assert all("WebP -> JPG" in a.reason for a in page.plan.actions)
    assert page.missing_banner.isHidden()  # pictures need no external program
    assert tree_snapshot(webp_tree) == before
    assert not (webp_tree / "converted").exists()

    apply(page)
    assert "convert: 2" in dialogs.confirms[0][1]
    assert page.result is not None and page.result.count("done") == 2
    out = webp_tree / "converted"
    assert sorted(p.name for p in out.iterdir()) == ["pic1.jpg", "pic2.jpg"]
    for name in ("pic1.jpg", "pic2.jpg"):
        with Image.open(out / name) as converted:
            assert converted.format == "JPEG" and converted.size == (64, 48)
    for name, data in original.items():
        assert (webp_tree / name).read_bytes() == data  # originals untouched
    assert trashed == []  # originals are kept by default: the Recycle Bin was not used

    undo(page)  # the unchanged outputs go to the Recycle Bin (the test fake); originals stay
    assert "undone (2 reverted)" in undo_text(dialogs)
    assert list(out.iterdir()) == []
    for name, data in original.items():
        assert (webp_tree / name).read_bytes() == data


def test_media_convert_can_recycle_the_original_after_the_output_is_verified(
    tmp_path: Path,
    open_tool: OpenTool,
    dialogs: Dialogs,
    webp_tree: Path,
    trashed: list[str],
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    page = open_tool("media_convert", str(webp_tree))
    page.form.update_values(
        {
            "convert_mov": False,
            "keep_metadata": False,
            "output": "next_to_original",
            "trash_originals": True,
        }
    )
    scan(page)
    assert page.plan is not None and len(page.plan.actions) == 2
    assert all("Recycle Bin" in a.reason for a in page.plan.actions)
    apply(page)
    assert sorted(Path(p).name for p in trashed) == ["pic1.webp", "pic2.webp"]
    assert sorted(p.name for p in webp_tree.iterdir()) == ["notes.txt", "pic1.jpg", "pic2.jpg"]
    undo(page)  # the recycled originals are journaled, but there is no Recycle Bin in tests
    assert RESTORE_BY_HAND in undo_text(dialogs)


# ----- metadata_strip -------------------------------------------------------------------------


GPS_IFD = 0x8825


def picture_with_gps(path: Path) -> Path:
    exif = Image.Exif()
    exif[0x010F] = "TestMake"
    ifd = exif.get_ifd(GPS_IFD)
    ifd[1], ifd[2] = "N", (37.0, 46.0, 29.7)
    ifd[3], ifd[4] = "W", (122.0, 25.0, 9.8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), (200, 10, 10)).save(path, format="JPEG", exif=bytes(exif.tobytes()))
    return path


def test_metadata_strip_without_exiftool_plans_nothing_and_shows_the_locate_banner(
    tmp_path: Path,
    open_tool: OpenTool,
    gui_state: AppState,
    monkeypatch: pytest.MonkeyPatch,
    scan: PageStep,
) -> None:
    folder = tmp_path / "pics"
    picture_with_gps(folder / "a.jpg")
    before = tree_snapshot(folder)
    monkeypatch.setattr(gui_state.deps, "exiftool", lambda: None)  # whatever this PC has
    page = open_tool("metadata_strip", str(folder))
    scan(page)
    assert page.plan is not None and page.plan.actions == []
    notes = page.notes_view.toPlainText()
    assert "ExifTool is needed" in notes and "winget install OliverBetz.ExifTool" in notes
    assert "Nothing was changed" in notes
    assert not page.missing_banner.isHidden()
    assert "exiftool" in page.missing_label.text()
    assert set(page.locate_buttons) == {"exiftool"}
    assert "Nothing found" in page.status_label.text()
    assert not page.apply_button.isEnabled()
    assert page.apply() is None
    assert tree_snapshot(folder) == before
    assert not (folder / "stripped").exists()


def test_metadata_strip_gps_scan_apply_undo(
    tmp_path: Path,
    open_tool: OpenTool,
    dialogs: Dialogs,
    exiftool_path: Path,
    trashed: list[str],
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    folder = tmp_path / "pics"
    source = picture_with_gps(folder / "gps.jpg")
    Image.new("RGB", (64, 48), (1, 2, 3)).save(folder / "clean.jpg", format="JPEG")
    before = tree_snapshot(folder)
    original = source.read_bytes()

    page = open_tool("metadata_strip", str(folder))
    scan(page)
    assert page.plan is not None
    assert [a.src.name for a in page.plan.actions if a.src] == ["gps.jpg"]
    action = page.plan.actions[0]
    assert action.kind is ActionKind.CONVERT and action.selected
    assert action.dst == folder / "stripped" / "gps.jpg"
    assert action.reason.startswith("remove GPS")
    assert tree_snapshot(folder) == before

    apply(page)
    assert page.result is not None and page.result.count("done") == 1
    assert source.read_bytes() == original  # the original is never changed
    assert trashed == []  # in-place mode is off: the Recycle Bin was not used
    with Image.open(folder / "stripped" / "gps.jpg") as stripped:
        assert not dict(stripped.getexif().get_ifd(GPS_IFD))
        assert stripped.size == (64, 48)
    undo(page)  # the unchanged output goes to the Recycle Bin (the test fake)
    assert "undone (1 reverted)" in undo_text(dialogs)
    assert not (folder / "stripped" / "gps.jpg").exists()
    assert source.read_bytes() == original


# ----- sort_by_type ---------------------------------------------------------------------------


def test_sort_by_type_scan_apply_undo(
    tmp_path: Path,
    open_tool: OpenTool,
    dialogs: Dialogs,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    root = tmp_path / "downloads"
    write(root / "cv.pdf", "cv")
    write(root / "photo.JPG", "photo")
    write(root / "notes" / "todo.txt", "todo")
    write(root / "notes" / "deep" / "a.pdf", "deep pdf")
    write(root / "keep.xyz", "unknown")
    before = tree_snapshot(root)
    original = files_of(root)

    page = open_tool("sort_by_type", str(root))
    page.form.update_values({"recursive": True})
    scan(page)
    assert page.plan is not None
    assert {a.kind for a in page.plan.actions} == {ActionKind.MOVE}
    assert all(a.selected for a in page.plan.actions)
    moves = {
        a.src.relative_to(root).as_posix(): a.dst.relative_to(root).as_posix()
        for a in page.plan.actions
        if a.src and a.dst
    }
    assert moves == {
        "cv.pdf": "Documents/PDF/cv.pdf",
        "photo.JPG": "Pictures/photo.JPG",
        "notes/todo.txt": "Documents/todo.txt",
        "notes/deep/a.pdf": "Documents/PDF/a.pdf",
    }
    assert "1 match no rule and stay" in page.notes_view.toPlainText()
    assert tree_snapshot(root) == before
    assert not (root / "Documents").exists()

    apply(page)
    assert "move: 4" in dialogs.confirms[0][1]
    assert files_of(root) == {
        "Documents/PDF/cv.pdf": b"cv",
        "Documents/PDF/a.pdf": b"deep pdf",
        "Documents/todo.txt": b"todo",
        "Pictures/photo.JPG": b"photo",
        "keep.xyz": b"unknown",
    }
    assert page.result is not None and page.result.count("done") == 4

    undo(page)
    assert "undone (4 reverted)" in dialogs.informs[-1][1]
    assert files_of(root) == original
    assert page.undo_button.isHidden()


def test_sort_by_type_custom_rules_and_the_other_folder(
    tmp_path: Path, open_tool: OpenTool, scan: PageStep, apply: PageStep
) -> None:
    root = tmp_path / "mixed"
    write(root / "a.pdf", "pdf")
    write(root / "b.png", "png")
    write(root / "c.xyz", "other")
    page = open_tool("sort_by_type", str(root))
    page.form.update_values(
        {
            "rules": ["*.pdf=Docs", "*.png|*.gif=Pics"],
            "unmatched": "other",
            "other_folder": "Misc",
        }
    )
    scan(page)
    apply(page)
    assert files_of(root) == {"Docs/a.pdf": b"pdf", "Pics/b.png": b"png", "Misc/c.xyz": b"other"}


# ----- temp_cleaner ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point TEMP, TMP, LOCALAPPDATA and APPDATA at ``tmp_path``, so the real ones are never read.

    The temp cleaner falls back to these when no folder is chosen and always looks for browser
    caches under ``%LOCALAPPDATA%``. Returns the fake ``%LOCALAPPDATA%``.
    """
    sandbox = tmp_path / "sandbox-env"
    for name in ("TEMP", "TMP", "APPDATA"):
        monkeypatch.setenv(name, str(sandbox / name))
    local = sandbox / "LOCALAPPDATA"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return local


def test_temp_cleaner_scan_apply_undo_and_browser_caches_stay_read_only(
    tmp_path: Path,
    open_tool: OpenTool,
    dialogs: Dialogs,
    trashed: list[str],
    sandbox_env: Path,
    scan: PageStep,
    apply: PageStep,
    undo: PageStep,
) -> None:
    root = tmp_path / "scratch"
    write(root / "old1.tmp", b"1" * 100, age_days=10)
    write(root / "sub" / "old2.log", b"2" * 50, age_days=10)
    age(root / "sub", 10)  # the folder is old too, so it goes once it is empty
    write(root / "new.tmp", b"new")  # too recent: kept
    cache = sandbox_env / "Google" / "Chrome" / "User Data" / "Default" / "Cache"
    write(cache / "data_0", b"c" * 150, age_days=100)
    before = tree_snapshot(root)
    cache_before = tree_snapshot(cache)

    page = open_tool("temp_cleaner", str(root))
    scan(page)
    assert page.plan is not None
    assert {a.kind for a in page.plan.actions} == {ActionKind.TRASH}
    assert sorted(a.src.relative_to(root).as_posix() for a in page.plan.actions if a.src) == [
        "old1.tmp",
        "sub",
        "sub/old2.log",
    ]
    assert all(a.selected for a in page.plan.actions)  # checked by default, Recycle Bin only
    assert "1 too recent, kept" in page.notes_view.toPlainText()
    assert tree_snapshot(root) == before

    # The browser cache is a note, never an action, and it is the fake one under tmp_path.
    notes = [
        parse_browser_cache_note(n) for n in page.plan.notes if n.startswith(BROWSER_CACHE_PREFIX)
    ]
    assert notes == [("Chrome (Default) Cache", cache, 150)]
    assert not any(a.src and a.src.is_relative_to(cache) for a in page.plan.actions)

    apply(page)
    assert "trash: 3" in dialogs.confirms[0][1] and "Recycle Bin" in dialogs.confirms[0][1]
    assert sorted(Path(p).name for p in trashed) == ["old1.tmp", "old2.log", "sub"]
    assert trashed.index(str(root / "sub" / "old2.log")) < trashed.index(str(root / "sub"))
    assert sorted(p.name for p in root.iterdir()) == ["new.tmp"]
    assert tree_snapshot(cache) == cache_before

    undo(page)
    text = undo_text(dialogs)
    assert "not undone (0 reverted)" in text
    assert text.count(RESTORE_BY_HAND) == 3  # there is no Recycle Bin to restore from in tests
    assert sorted(p.name for p in root.iterdir()) == ["new.tmp"]


def test_temp_cleaner_with_no_folder_uses_the_sandboxed_temp_not_the_real_one(
    tmp_path: Path,
    open_tool: OpenTool,
    sandbox_env: Path,
    trashed: list[str],
    scan: PageStep,
    apply: PageStep,
) -> None:
    fake_temp = tmp_path / "sandbox-env" / "TEMP"
    write(fake_temp / "left-over.tmp", b"x", age_days=30)
    page = open_tool("temp_cleaner")
    assert page.form.raw_values()["roots"] == []  # nothing chosen: the default applies
    page.form.update_values({"list_browser_caches": False})
    scan(page)
    assert page.plan is not None
    assert str(fake_temp) in page.notes_view.toPlainText()
    assert page.plan.roots == [fake_temp]  # %LOCALAPPDATA%\Temp does not exist, so it is refused
    assert [a.src.name for a in page.plan.actions if a.src] == ["left-over.tmp"]
    assert not any("browser-cache" in n for n in page.plan.notes)
    apply(page)
    assert [Path(p) for p in trashed] == [fake_temp / "left-over.tmp"]
    assert not (fake_temp / "left-over.tmp").exists()
