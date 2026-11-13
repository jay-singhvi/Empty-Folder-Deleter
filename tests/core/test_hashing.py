"""Exact (SHA-256) groups, perceptual hashes and band keys."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from various_tools.core import hashing
from various_tools.core.fs import FileInfo
from various_tools.core.hashing import band_keys, dhash, group_exact, hamming, phash, sha256
from various_tools.core.onedrive import CloudOnlyFileError

OFFLINE = 0x1000
ImageMaker = Callable[..., Path]


def _info(path: Path, attributes: int = 0x20) -> FileInfo:
    st = path.stat()
    return FileInfo(path=path, size=st.st_size, mtime=st.st_mtime, attributes=attributes)


def test_sha256_matches_hashlib_on_a_multi_chunk_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = bytes(range(256)) * 9
    target = tmp_path / "data.bin"
    target.write_bytes(data)
    assert sha256(target, chunk_size=7) == hashlib.sha256(data).hexdigest()  # several reads
    assert sha256(target) == hashlib.sha256(data).hexdigest()


def test_sha256_refuses_cloud_only_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "cloud.bin"
    target.write_bytes(b"x")
    monkeypatch.setattr("various_tools.core.onedrive.get_attributes", lambda _p: OFFLINE)
    with pytest.raises(CloudOnlyFileError):
        sha256(target)


def test_group_exact_finds_identical_copies_and_ignores_unique_sizes(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    original = tmp_path / "a" / "photo.jpg"
    copy = tmp_path / "b" / "photo copy.jpg"
    other_same_size = tmp_path / "b" / "different.jpg"
    lone = tmp_path / "a" / "lone.jpg"
    original.write_bytes(b"AAAA")
    copy.write_bytes(b"AAAA")
    other_same_size.write_bytes(b"BBBB")
    lone.write_bytes(b"CCCCCC")

    result = group_exact([_info(p) for p in (original, copy, other_same_size, lone)])
    assert result.groups == [sorted([original, copy])]
    assert result.skipped_cloud_only == []
    assert result.errors == []


def test_group_exact_never_hashes_cloud_only_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_a = tmp_path / "local_a.jpg"
    local_b = tmp_path / "local_b.jpg"
    cloud = tmp_path / "cloud.jpg"
    for path in (local_a, local_b, cloud):
        path.write_bytes(b"SAME")

    hashed: list[Path] = []
    real_sha = hashing.sha256

    def tracking_sha(path: Path, chunk_size: int = hashing.CHUNK_SIZE) -> str:
        hashed.append(path)
        return real_sha(path, chunk_size)

    monkeypatch.setattr(hashing, "sha256", tracking_sha)
    infos = [_info(local_a), _info(local_b), _info(cloud, attributes=OFFLINE)]
    result = group_exact(infos)

    assert cloud not in hashed
    assert result.skipped_cloud_only == [cloud]
    assert result.groups == [sorted([local_a, local_b])]


def test_group_exact_reports_read_errors_without_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"SAME")
    b.write_bytes(b"SAME")

    def broken(path: Path, chunk_size: int = 0) -> str:
        raise PermissionError(f"locked: {path.name}")

    monkeypatch.setattr(hashing, "sha256", broken)
    result = group_exact([_info(a), _info(b)])
    assert result.groups == []
    assert {p for p, _ in result.errors} == {a, b}


def test_phash_and_dhash_are_stable_and_256_bits(make_image: ImageMaker) -> None:
    path = make_image("pic.png", seed=3, size=(120, 90))
    with Image.open(path) as img:
        first = phash(img.convert("RGB"))
        again = phash(img.convert("RGB"))
        d = dhash(img.convert("RGB"))
    assert first == again
    assert 0 <= first < 2**256
    assert 0 <= d < 2**256


def test_similar_images_are_closer_than_unrelated_ones(make_image: ImageMaker) -> None:
    base = make_image("base.png", seed=5, size=(128, 96))
    brighter = base.parent / "brighter.png"
    with Image.open(base) as img:
        # One 256-entry table per band (RGB has three bands), each brightening by 4 levels.
        table = [min(255, i + 4) for i in range(256)] * 3
        img.point(table).save(brighter)
    other = make_image("other.png", seed=99, size=(128, 96))
    with Image.open(base) as a, Image.open(brighter) as b, Image.open(other) as c:
        close = hamming(phash(a.convert("RGB")), phash(b.convert("RGB")))
        far = hamming(phash(a.convert("RGB")), phash(c.convert("RGB")))
    assert close < far


def test_hamming_counts_differing_bits() -> None:
    assert hamming(0b1011, 0b0001) == 2
    assert hamming(7, 7) == 0


def test_band_keys_split_256_bits_into_bands() -> None:
    # For 3 bands the edges are 0, 85, 170, 256. Bit 0 is in band 0, bit 255 is in band 2.
    value = (1 << 255) | 1
    keys = band_keys(value, bands=3)
    assert [index for index, _ in keys] == [0, 1, 2]
    assert keys[0] == (0, 1)
    assert keys[1] == (1, 0)
    assert keys[2] == (2, 1 << (255 - 170))


def test_band_keys_guarantee_a_shared_band_within_bands_minus_one_flips() -> None:
    bands = 4  # tolerates 3 flipped bits: at least one of 4 bands is untouched
    base = 0x0123456789ABCDEF_FEDCBA9876543210_0F0F0F0F0F0F0F0F_F0F0F0F0F0F0F0F0
    for flips in ([0, 1, 2], [10, 100, 200], [63, 127, 191]):
        other = base
        for bit in flips:
            other ^= 1 << bit
        shared = set(band_keys(base, bands)) & set(band_keys(other, bands))
        assert shared, f"no shared band for flips {flips}"


def test_band_keys_rejects_zero_bands() -> None:
    with pytest.raises(ValueError):
        band_keys(5, bands=0)
