"""fs.walk filters, reparse-point safety, collision-safe names and protected paths."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from various_tools.core import fs
from various_tools.core.fs import FileInfo, is_inside, protected_reason, unique_destination, walk


def _names(paths: list[FileInfo], root: Path) -> list[str]:
    return sorted(p.path.relative_to(root).as_posix() for p in paths)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "a" / "deep").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "a" / "one.jpg").write_bytes(b"1")
    (root / "a" / "deep" / "two.PNG").write_bytes(b"22")
    (root / "a" / "notes.txt").write_bytes(b"333")
    (root / "top.jpg").write_bytes(b"4444")
    (root / "node_modules" / "pkg" / "junk.jpg").write_bytes(b"5")
    (root / ".git" / "HEAD").write_bytes(b"6")
    return root


def test_walk_yields_every_file_sorted_and_not_the_root(tree: Path) -> None:
    found = list(walk(tree))
    assert all(isinstance(f, FileInfo) for f in found)
    assert _names(found, tree) == sorted(
        [
            "a/one.jpg",
            "a/deep/two.PNG",
            "a/notes.txt",
            "top.jpg",
            "node_modules/pkg/junk.jpg",
            ".git/HEAD",
        ]
    )


def test_walk_reports_size_and_mtime(tree: Path) -> None:
    info = next(f for f in walk(tree) if f.path.name == "two.PNG")
    assert info.size == 2
    assert info.mtime > 0
    assert info.is_reparse_point is False


def test_walk_extension_filter_is_case_insensitive(tree: Path) -> None:
    found = list(walk(tree, exts={".png", ".jpg"}))
    assert _names(found, tree) == sorted(
        ["a/one.jpg", "a/deep/two.PNG", "top.jpg", "node_modules/pkg/junk.jpg"]
    )


def test_walk_exclude_globs_skip_folders_and_files(tree: Path) -> None:
    found = list(walk(tree, exclude_globs=[".git", "node_modules"]))
    assert _names(found, tree) == ["a/deep/two.PNG", "a/notes.txt", "a/one.jpg", "top.jpg"]


def test_walk_include_globs_match_name_or_relative_path(tree: Path) -> None:
    by_name = list(walk(tree, include_globs=["one*"]))
    assert _names(by_name, tree) == ["a/one.jpg"]
    by_path = list(walk(tree, include_globs=["a/deep/*"]))
    assert _names(by_path, tree) == ["a/deep/two.PNG"]


def test_walk_non_recursive_stays_in_root(tree: Path) -> None:
    assert _names(list(walk(tree, recursive=False)), tree) == ["top.jpg"]


def test_walk_respects_gitignore_at_each_level(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
    (root / "sub" / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "keep.py").write_text("x", encoding="utf-8")
    (root / "errors.log").write_text("x", encoding="utf-8")
    (root / "build" / "out.bin").write_text("x", encoding="utf-8")
    (root / "sub" / "secret.txt").write_text("x", encoding="utf-8")
    (root / "sub" / "code.py").write_text("x", encoding="utf-8")

    found = list(walk(root, respect_gitignore=True))
    names = _names(found, root)
    assert "keep.py" in names
    assert "sub/code.py" in names
    assert "errors.log" not in names
    assert "build/out.bin" not in names
    assert "sub/secret.txt" not in names
    assert "errors.log" in _names(list(walk(root)), root)


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows feature")
def test_walk_never_follows_a_junction_out_of_root(tmp_path: Path) -> None:
    # A directory junction needs no special privilege, unlike a symlink.
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.jpg").write_bytes(b"x")
    (root / "inside.jpg").write_bytes(b"x")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction in this environment")
    assert _names(list(walk(root)), root) == ["inside.jpg"]
    # With the default setting off, the junction would be followed, so the guard is what matters.
    assert "link/secret.jpg" not in _names(list(walk(root, skip_reparse_points=True)), root)


def test_walk_skips_unreadable_folder_and_keeps_going(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scandir = os.scandir
    blocked = tree / "a"

    def fake_scandir(path: object = ".") -> object:
        if Path(os.fspath(path)) == blocked:  # type: ignore[arg-type]
            raise PermissionError("denied")
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(fs.os, "scandir", fake_scandir)
    names = _names(list(walk(tree)), tree)
    assert "top.jpg" in names
    assert not any(n.startswith("a/") for n in names)


def test_unique_destination_returns_free_name_untouched(tmp_path: Path) -> None:
    assert unique_destination(tmp_path, "photo.jpg") == tmp_path / "photo.jpg"


def test_unique_destination_adds_dup_suffix_past_existing_names(tmp_path: Path) -> None:
    (tmp_path / "photo.jpg").write_bytes(b"1")
    (tmp_path / "photo_dup1.jpg").write_bytes(b"2")
    assert unique_destination(tmp_path, "photo.jpg") == tmp_path / "photo_dup2.jpg"


def test_unique_destination_compares_names_case_insensitively(tmp_path: Path) -> None:
    (tmp_path / "Photo.JPG").write_bytes(b"1")
    assert unique_destination(tmp_path, "photo.jpg") == tmp_path / "photo_dup1.jpg"


def test_unique_destination_when_folder_does_not_exist(tmp_path: Path) -> None:
    assert unique_destination(tmp_path / "missing", "a.txt") == tmp_path / "missing" / "a.txt"


def test_protected_drive_root_and_user_profile(tmp_path: Path) -> None:
    env = {"USERPROFILE": str(tmp_path / "me")}
    assert protected_reason(Path(tmp_path.anchor), env) == "drive root"
    assert protected_reason(tmp_path / "me", env) == "user profile folder"
    assert protected_reason(tmp_path / "me" / "Docs", env) is None


def test_protected_windows_and_program_files(tmp_path: Path) -> None:
    env = {
        "SystemRoot": str(tmp_path / "Windows"),
        "ProgramFiles": str(tmp_path / "PF"),
        "ProgramFiles(x86)": str(tmp_path / "PF86"),
    }
    assert protected_reason(tmp_path / "Windows" / "Temp" / "x.tmp", env) == "Windows folder"
    assert protected_reason(tmp_path / "PF" / "app" / "x.exe", env) == "Program Files"
    assert protected_reason(tmp_path / "PF86", env) == "Program Files"
    assert protected_reason(tmp_path / "PFX" / "y", env) is None


def test_is_inside_matches_root_and_children_only(tmp_path: Path) -> None:
    assert is_inside(tmp_path, tmp_path)
    assert is_inside(tmp_path / "x" / "y", tmp_path)
    assert not is_inside(tmp_path.parent / (tmp_path.name + "2"), tmp_path)


def test_root_refusal_rejects_missing_and_protected_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from various_tools.core import fs

    assert fs.root_refusal(tmp_path) is None
    assert "Not a folder" in (fs.root_refusal(tmp_path / "missing") or "")
    monkeypatch.setattr(fs, "protected_reason", lambda _p, *_a, **_k: "Windows folder")
    assert "Protected folder refused" in (fs.root_refusal(tmp_path) or "")
    assert fs.root_refusal(tmp_path, allow_protected=True) is None


def test_walk_respects_gitignore_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (root / "sub" / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    for name in ("a.txt", "b.log", "sub/c.txt", "sub/secret.txt", "sub/d.log"):
        (root / name).write_text("x", encoding="utf-8")
    found = _names(list(walk(root, respect_gitignore=True)), root)
    assert found == [".gitignore", "a.txt", "sub/.gitignore", "sub/c.txt"]


def test_a_cloud_only_gitignore_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    ignore = root / ".gitignore"
    ignore.write_text("*.log\n", encoding="utf-8")
    (root / "b.log").write_text("x", encoding="utf-8")
    offline = 0x1000

    def attributes(path: Path) -> int | None:
        if Path(path).name == ".gitignore":
            return offline
        return 0 if os.path.lexists(path) else None

    monkeypatch.setattr("various_tools.core.onedrive.get_attributes", attributes)
    real_read_text = Path.read_text

    def no_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == ".gitignore":
            raise AssertionError("a cloud-only .gitignore was read")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", no_read)
    found = _names(list(walk(root, respect_gitignore=True)), root)
    # Its rules are not applied, so b.log is listed. That is the safe direction for a scan.
    assert "b.log" in found


def test_reparse_ancestor_finds_a_junction_on_the_way_up(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are a Windows feature")
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction in this environment")
    deep = root / "link" / "not" / "yet" / "file.txt"
    assert fs.reparse_ancestor(deep, root) == root / "link"
    assert fs.destination_refusal(deep, root) is not None
    assert fs.reparse_ancestor(root / "plain" / "file.txt", root) is None
    assert fs.destination_refusal(root / "plain" / "file.txt") is None


def test_rename_no_replace_never_replaces(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    target = tmp_path / "b.txt"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    with pytest.raises(FileExistsError):
        fs.rename_no_replace(source, target)
    assert target.read_text(encoding="utf-8") == "old"
    assert source.exists()
