"""Link detection by reparse tag, safe moves, prefixed and 8.3 protected paths (review fixes)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from various_tools.core import fs
from various_tools.core.onedrive import CloudOnlyFileError

REPARSE = fs.FILE_ATTRIBUTE_REPARSE_POINT
DIRECTORY = fs.FILE_ATTRIBUTE_DIRECTORY


def _stat(mode: int, attributes: int = 0, tag: int = 0) -> os.stat_result:
    fake = SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_reparse_tag=tag)
    return fake  # type: ignore[return-value]


# ---- link detection ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "is_link"),
    [
        (fs.IO_REPARSE_TAG_SYMLINK, True),
        (fs.IO_REPARSE_TAG_MOUNT_POINT, True),
        (0x9000001A, False),  # IO_REPARSE_TAG_CLOUD
        (0x9000101A, False),  # IO_REPARSE_TAG_CLOUD_1
        (0x9000701A, False),  # IO_REPARSE_TAG_CLOUD_7 (OneDrive)
        (0x9000F01A, False),  # IO_REPARSE_TAG_CLOUD_F
        (0x80000013, True),  # some other tag: treated as a link, the safer side
        (0x9000001B, True),  # close to a cloud tag but not one
    ],
)
def test_reparse_tags_decide_what_is_a_link(tag: int, is_link: bool) -> None:
    assert fs.stat_is_link(_stat(stat.S_IFDIR, DIRECTORY | REPARSE, tag)) is is_link


def test_a_plain_folder_or_file_is_not_a_link() -> None:
    assert not fs.stat_is_link(_stat(stat.S_IFDIR, DIRECTORY))
    assert not fs.stat_is_link(_stat(stat.S_IFREG, 0x20))
    assert fs.stat_is_link(_stat(stat.S_IFLNK))
    assert fs.is_cloud_reparse_tag(0x9000301A)
    assert not fs.is_cloud_reparse_tag(fs.IO_REPARSE_TAG_SYMLINK)


def test_walk_lists_cloud_placeholders_but_skips_other_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "cloud_dir").mkdir(parents=True)
    (root / "cloud_dir" / "inside.jpg").write_bytes(b"1")
    (root / "linkish").mkdir()
    (root / "linkish" / "hidden.jpg").write_bytes(b"2")
    (root / "placeholder.jpg").write_bytes(b"3")
    tags = {"cloud_dir": 0x9000701A, "placeholder.jpg": 0x9000701A, "linkish": 0x80000013}
    _fake_tags(monkeypatch, tags)
    names = sorted(i.path.relative_to(root).as_posix() for i in fs.walk(root))
    assert names == ["cloud_dir/inside.jpg", "placeholder.jpg"]


class _TaggedEntry:
    """A directory entry whose stat reports a reparse tag, as a cloud placeholder's would."""

    def __init__(self, entry: os.DirEntry[str], tag: int) -> None:
        self._entry = entry
        self._tag = tag
        self.name = entry.name
        self.path = entry.path

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return self._entry.is_file(follow_symlinks=follow_symlinks)

    def is_symlink(self) -> bool:
        return False

    def is_junction(self) -> bool:
        return False

    def stat(self, *, follow_symlinks: bool = True) -> object:
        st = self._entry.stat(follow_symlinks=follow_symlinks)
        return SimpleNamespace(
            st_mode=st.st_mode,
            st_size=st.st_size,
            st_mtime=st.st_mtime,
            st_file_attributes=getattr(st, "st_file_attributes", 0) | REPARSE,
            st_reparse_tag=self._tag,
        )


class _Listing:
    def __init__(self, entries: list[object]) -> None:
        self._entries = entries

    def __enter__(self) -> list[object]:
        return self._entries

    def __exit__(self, *_exc: object) -> None:
        return None


def _fake_tags(monkeypatch: pytest.MonkeyPatch, tags: dict[str, int]) -> None:
    real_scandir = os.scandir

    def fake_scandir(path: str) -> _Listing:
        with real_scandir(path) as it:
            entries: list[object] = [
                _TaggedEntry(e, tags[e.name]) if e.name in tags else e for e in it
            ]
        return _Listing(entries)

    monkeypatch.setattr(fs.os, "scandir", fake_scandir)


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are a Windows feature")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if made.returncode != 0 or not link.exists():
        pytest.skip("could not create a junction in this environment")


def test_walk_never_recurses_into_a_junction_even_when_asked_not_to_skip(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.jpg").write_bytes(b"x")
    _junction(root / "link", outside)
    names = [i.path.name for i in fs.walk(root, skip_reparse_points=False)]
    assert "secret.jpg" not in names


def test_a_junction_is_a_link_by_tag(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _junction(tmp_path / "link", outside)
    assert fs.is_link(tmp_path / "link")
    assert not fs.is_link(outside)
    assert not fs.is_link(tmp_path / "missing")


def test_a_symlinked_gitignore_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (root / "b.log").write_text("x", encoding="utf-8")
    real_is_link = fs.is_link
    monkeypatch.setattr(
        fs, "is_link", lambda p: Path(p).name == ".gitignore" or real_is_link(Path(p))
    )
    names = [i.path.name for i in fs.walk(root, respect_gitignore=True)]
    assert "b.log" in names


# ---- safe moves -------------------------------------------------------------------------


def test_move_on_the_same_volume_renames_and_never_replaces(tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    src.write_text("a", encoding="utf-8")
    fs.move_no_replace(src, tmp_path / "b.txt")
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "a"
    other = tmp_path / "c.txt"
    other.write_text("c", encoding="utf-8")
    with pytest.raises(FileExistsError):
        fs.move_no_replace(tmp_path / "b.txt", other)
    assert other.read_text(encoding="utf-8") == "c"


def test_move_across_volumes_copies_through_a_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "same_volume", lambda _a, _b: False)
    src = tmp_path / "src" / "photo.jpg"
    src.parent.mkdir()
    src.write_bytes(b"x" * 5000)
    dst = tmp_path / "dst" / "photo.jpg"
    dst.parent.mkdir()
    fs.move_no_replace(src, dst)
    assert dst.read_bytes() == b"x" * 5000
    assert not src.exists()
    assert [p.name for p in dst.parent.iterdir()] == ["photo.jpg"]


def test_move_across_volumes_refuses_a_cloud_only_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "same_volume", lambda _a, _b: False)
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"data")
    dst = tmp_path / "out" / "photo.jpg"
    dst.parent.mkdir()
    monkeypatch.setattr(
        "various_tools.core.onedrive.get_attributes",
        lambda p: 0x1000 if Path(p).name == "photo.jpg" else 0,
    )
    real_open = open

    def no_read(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        if "photo.jpg" in str(file) and "r" in mode:
            raise AssertionError("a cloud-only file was opened")
        return real_open(file, mode, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("builtins.open", no_read)
    with pytest.raises(CloudOnlyFileError):
        fs.move_no_replace(src, dst)
    assert src.exists()
    assert list(dst.parent.iterdir()) == []


def test_move_across_volumes_refuses_a_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "same_volume", lambda _a, _b: False)
    src = tmp_path / "folder"
    src.mkdir()
    (src / "f.txt").write_text("x", encoding="utf-8")
    with pytest.raises(OSError, match="another drive"):
        fs.move_no_replace(src, tmp_path / "elsewhere")
    assert (src / "f.txt").exists()


def test_move_across_volumes_never_replaces_a_destination_that_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "same_volume", lambda _a, _b: False)
    src = tmp_path / "a.txt"
    src.write_text("mine", encoding="utf-8")
    dst = tmp_path / "out" / "a.txt"
    dst.parent.mkdir()
    real_copy = fs.shutil.copyfileobj

    def copy_then_race(fsrc: object, fdst: object, length: int = 0) -> None:
        real_copy(fsrc, fdst)  # type: ignore[arg-type]
        dst.write_text("someone else", encoding="utf-8")  # appears mid-move

    monkeypatch.setattr(fs.shutil, "copyfileobj", copy_then_race)
    with pytest.raises(FileExistsError):
        fs.move_no_replace(src, dst)
    assert dst.read_text(encoding="utf-8") == "someone else"
    assert src.read_text(encoding="utf-8") == "mine"
    assert [p.name for p in dst.parent.iterdir()] == ["a.txt"]  # no .partial left


@pytest.mark.skipif(os.name != "nt", reason="Windows names are case-insensitive")
def test_a_case_only_rename_is_allowed(tmp_path: Path) -> None:
    src = tmp_path / "photo.JPG"
    src.write_bytes(b"x")
    dst = tmp_path / "photo.jpg"
    assert fs.is_case_only_rename(src, dst)
    fs.rename_no_replace(src, dst)
    assert [p.name for p in tmp_path.iterdir()] == ["photo.jpg"]
    other = tmp_path / "other.jpg"
    other.write_bytes(b"y")
    assert not fs.is_case_only_rename(other, dst)
    with pytest.raises(FileExistsError):
        fs.rename_no_replace(other, tmp_path / "PHOTO.jpg")


# ---- protected paths --------------------------------------------------------------------


def test_protected_paths_see_through_long_path_prefixes(tmp_path: Path) -> None:
    env = {"SystemRoot": str(tmp_path / "Windows"), "USERPROFILE": str(tmp_path / "me")}
    inside = tmp_path / "Windows" / "System32" / "x.dll"
    assert fs.protected_reason(Path("\\\\?\\" + str(inside)), env) == "Windows folder"
    assert fs.protected_reason(Path("\\\\?\\" + str(tmp_path / "me")), env) == (
        "user profile folder"
    )
    assert fs.protected_reason(Path("\\\\?\\" + tmp_path.anchor), env) == "drive root"
    unc = fs._norm("\\\\?\\UNC\\server\\share\\folder")  # pyright: ignore[reportPrivateUsage]
    assert unc == os.path.normcase("\\\\server\\share\\folder")
    assert fs.same_path(Path("\\\\?\\" + str(inside)), inside)


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names are a Windows feature")
def test_protected_paths_expand_short_names(tmp_path: Path) -> None:
    folder = tmp_path / "Program Files Long Name"
    (folder / "app").mkdir(parents=True)
    short = fs.short_path_name(folder)
    if short is None or "~" not in short:
        pytest.skip("8.3 names are turned off on this volume")
    env = {"ProgramFiles": str(folder)}
    assert fs.protected_reason(Path(short) / "app" / "new.exe", env) == "Program Files"


def test_root_refusal_sees_through_long_path_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "me"
    profile.mkdir()
    monkeypatch.setenv("USERPROFILE", str(profile))
    assert "Protected" in (fs.root_refusal(Path("\\\\?\\" + str(profile))) or "")
