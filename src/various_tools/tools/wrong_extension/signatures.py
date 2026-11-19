"""File signatures ("magic numbers") and the table of extensions that mean the same thing.

``detect`` is a pure function: it looks at the first bytes of a file and says which extension the
content calls for. Nothing here touches the disk.

Two extensions are *equivalent* when one container format is commonly saved under both, for
example ``.jpg``, ``.jpeg`` and ``.jfif``. A file whose extension is equivalent to what its content
calls for is never renamed. The ZIP-based document formats (``.docx``, ``.xlsx`` and so on) count
as ZIP, so a real ``.docx`` is never "fixed" to ``.zip``. The MP4, M4V and MOV extensions share one
group, because they are all the same ISO media container and players open any of them.
"""

from __future__ import annotations

from typing import Literal

Family = Literal["photos", "videos", "audio", "documents", "archives"]
FAMILIES: tuple[Family, ...] = ("photos", "videos", "audio", "documents", "archives")

# Each group lists the extensions that mean the same content. The first one is the name used
# when a file has to be renamed.
_GROUPS: tuple[tuple[Family, tuple[str, ...]], ...] = (
    ("photos", (".jpg", ".jpeg", ".jfif", ".jpe")),
    ("photos", (".png",)),
    ("photos", (".gif",)),
    ("photos", (".webp",)),
    ("photos", (".bmp", ".dib")),
    ("photos", (".tiff", ".tif")),
    ("photos", (".heic", ".heif")),
    ("photos", (".avif",)),
    ("videos", (".mp4", ".m4v", ".mov")),
    ("videos", (".mkv", ".webm")),
    ("videos", (".avi",)),
    ("audio", (".wav", ".wave")),
    ("audio", (".mp3",)),
    ("audio", (".flac",)),
    ("audio", (".ogg", ".oga", ".opus")),
    ("documents", (".pdf",)),
    (
        "documents",
        (
            ".docx",
            ".docm",
            ".dotx",
            ".xlsx",
            ".xlsm",
            ".xltx",
            ".pptx",
            ".pptm",
            ".potx",
            ".odt",
            ".ods",
            ".odp",
            ".epub",
        ),
    ),
    ("archives", (".zip",)),
    ("archives", (".7z",)),
    ("archives", (".rar",)),
    ("archives", (".gz", ".tgz")),
)

# The ZIP container is shared by the document formats above and by .zip itself.
_ZIP_BASED = frozenset(_GROUPS[16][1]) | {".zip"}

_GROUP_OF: dict[str, int] = {}
_FAMILY_OF: dict[str, Family] = {}
for _index, (_family, _extensions) in enumerate(_GROUPS):
    for _ext in _extensions:
        _GROUP_OF[_ext] = _index
        _FAMILY_OF[_ext] = _family
for _ext in _ZIP_BASED:
    _GROUP_OF[_ext] = _GROUP_OF[".zip"]

_HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"})
_AVIF_BRANDS = frozenset({b"avif", b"avis"})
_MP4_BRANDS = frozenset(
    {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"mmp4"}
)


def _normal(ext: str) -> str:
    return ext.lower()


def family_of(ext: str) -> Family | None:
    """The family a (checkable) extension belongs to, or ``None`` when it is not one we check."""
    return _FAMILY_OF.get(_normal(ext))


def is_equivalent(current: str, wanted: str) -> bool:
    """True when ``current`` is an acceptable extension for content that calls for ``wanted``."""
    a, b = _normal(current), _normal(wanted)
    if a == b:
        return True
    return a in _GROUP_OF and _GROUP_OF[a] == _GROUP_OF.get(b)


def _ftyp(head: bytes) -> str | None:
    brand = head[8:12]
    if brand in _HEIC_BRANDS:
        return ".heic"
    if brand in _AVIF_BRANDS:
        return ".avif"
    if brand == b"qt  ":
        return ".mov"
    if brand == b"M4V ":
        return ".m4v"
    if brand in _MP4_BRANDS:
        return ".mp4"
    return None


def _riff(head: bytes) -> str | None:
    kind = head[8:12]
    return {b"WEBP": ".webp", b"WAVE": ".wav", b"AVI ": ".avi"}.get(kind)


def _is_mp3_frame(head: bytes) -> bool:
    """An MPEG audio frame header: 11 sync bits, a real version, and layer I, II or III."""
    if len(head) < 4 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0:
        return False
    version, layer = (head[1] >> 3) & 0x03, (head[1] >> 1) & 0x03
    bitrate, rate = head[2] >> 4, (head[2] >> 2) & 0x03
    return version != 1 and layer != 0 and bitrate not in (0, 15) and rate != 3


def detect(head: bytes) -> str | None:
    """The extension (lower case, with a dot) that ``head`` calls for, or ``None`` if unknown.

    ``head`` is the first bytes of a file (64 are enough). Content that is not recognised gives
    ``None`` and such files are left alone.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head.startswith(b"RIFF") and len(head) >= 12:
        return _riff(head)
    if head.startswith(b"BM") and len(head) >= 14 and head[6:10] == b"\x00\x00\x00\x00":
        return ".bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    if head[4:8] == b"ftyp":
        return _ftyp(head)
    if head.startswith(b"%PDF-"):
        return ".pdf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return ".zip"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm" if b"webm" in head[:64] else ".mkv"
    if head.startswith(b"ID3") or _is_mp3_frame(head):
        return ".mp3"
    if head.startswith(b"fLaC"):
        return ".flac"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if head.startswith(b"\x1f\x8b"):
        return ".gz"
    return None
