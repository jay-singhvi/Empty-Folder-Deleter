"""``detect`` and the table of equivalent extensions, with headers built here."""

from __future__ import annotations

import pytest

from various_tools.tools.wrong_extension.signatures import detect, family_of, is_equivalent

RIFF = b"RIFF\x24\x00\x00\x00"


def ftyp(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00" + brand + b"isom"


EBML = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\x82"

CASES: list[tuple[str, bytes, str]] = [
    ("jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF", ".jpg"),
    ("png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, ".png"),
    ("gif87", b"GIF87a" + b"\x00" * 10, ".gif"),
    ("gif89", b"GIF89a" + b"\x00" * 10, ".gif"),
    ("webp", RIFF + b"WEBPVP8 ", ".webp"),
    ("wav", RIFF + b"WAVEfmt ", ".wav"),
    ("avi", RIFF + b"AVI LIST", ".avi"),
    ("bmp", b"BM" + b"\x36\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\x36\x00\x00\x00", ".bmp"),
    ("tiff_le", b"II*\x00\x08\x00\x00\x00", ".tiff"),
    ("tiff_be", b"MM\x00*\x00\x00\x00\x08", ".tiff"),
    ("heic", ftyp(b"heic"), ".heic"),
    ("heix", ftyp(b"heix"), ".heic"),
    ("avif", ftyp(b"avif"), ".avif"),
    ("mp4", ftyp(b"isom"), ".mp4"),
    ("mp42", ftyp(b"mp42"), ".mp4"),
    ("m4v", ftyp(b"M4V "), ".m4v"),
    ("mov", ftyp(b"qt  "), ".mov"),
    ("pdf", b"%PDF-1.7\n%\xe2\xe3", ".pdf"),
    ("zip", b"PK\x03\x04\x14\x00\x00\x00", ".zip"),
    ("mkv", EBML + b"\x88matroska", ".mkv"),
    ("webm", EBML + b"\x84webm", ".webm"),
    ("mp3_frame", b"\xff\xfb\x90\x64\x00\x00\x00\x00", ".mp3"),
    ("flac", b"fLaC\x00\x00\x00\x22", ".flac"),
    ("ogg", b"OggS\x00\x02" + b"\x00" * 10, ".ogg"),
    ("7z", b"7z\xbc\xaf\x27\x1c\x00\x04", ".7z"),
    ("rar4", b"Rar!\x1a\x07\x00", ".rar"),
    ("rar5", b"Rar!\x1a\x07\x01\x00", ".rar"),
    ("gz", b"\x1f\x8b\x08\x00\x00\x00\x00\x00", ".gz"),
]


@pytest.mark.parametrize(("label", "head", "expected"), CASES, ids=[c[0] for c in CASES])
def test_detect_every_signature(label: str, head: bytes, expected: str) -> None:
    assert detect(head.ljust(64, b"\x00")[:64]) == expected
    assert detect(head) == expected  # a short head works too


@pytest.mark.parametrize(
    "head",
    [b"", b"\x00" * 64, b"plain text here", b"BM", b"RIFF\x00\x00\x00\x00ZZZZ", ftyp(b"zzzz")],
)
def test_unknown_content_gives_none(head: bytes) -> None:
    assert detect(head) is None


def ftyp_brands(major: bytes, *compatible: bytes) -> bytes:
    body = major + b"\x00\x00\x00\x00" + b"".join(compatible)
    return (8 + len(body)).to_bytes(4, "big") + b"ftyp" + body


def test_mif1_with_avif_compatible_brand_is_avif() -> None:
    head = ftyp_brands(b"mif1", b"mif1", b"avif", b"miaf")
    assert detect(head.ljust(64, b"\x00")) == ".avif"


def test_mif1_with_heic_compatible_brand_is_heic() -> None:
    head = ftyp_brands(b"mif1", b"mif1", b"heic")
    assert detect(head.ljust(64, b"\x00")) == ".heic"


def test_mif1_alone_is_unknown() -> None:
    assert detect(ftyp_brands(b"mif1", b"mif1").ljust(64, b"\x00")) is None


def test_id3_header_is_not_decided() -> None:
    assert detect(b"ID3\x03\x00\x00\x00\x00\x00\x00".ljust(64, b"\x00")) is None


def test_utf16_bom_text_is_not_mp3() -> None:
    assert detect(b"\xff\xfe" + "hello world".encode("utf-16-le")) is None


def test_mp3_frame_still_detected() -> None:
    assert detect(b"\xff\xfb\x90\x64\x00\x00\x00\x00") == ".mp3"


def test_equivalent_extensions() -> None:
    for a, b in [
        (".jpg", ".jpeg"),
        (".jpg", ".jfif"),
        (".tif", ".tiff"),
        (".heic", ".heif"),
        (".mp4", ".m4v"),
        (".mp4", ".mov"),
        (".zip", ".docx"),
        (".zip", ".xlsx"),
        (".zip", ".pptx"),
        (".zip", ".odt"),
        (".JPG", ".jpeg"),
        (".png", ".png"),
    ]:
        assert is_equivalent(a, b), (a, b)
    for a, b in [(".jpg", ".png"), (".png", ".gif"), (".heic", ".jpg"), (".pdf", ".zip")]:
        assert not is_equivalent(a, b), (a, b)


def test_families() -> None:
    assert family_of(".JPEG") == "photos"
    assert family_of(".mkv") == "videos"
    assert family_of(".flac") == "audio"
    assert family_of(".pdf") == "documents"
    assert family_of(".docx") == "documents"
    assert family_of(".7z") == "archives"
    assert family_of(".txt") is None
