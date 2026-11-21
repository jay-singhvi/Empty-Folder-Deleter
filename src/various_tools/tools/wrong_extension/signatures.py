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
    ("audio", (".mp2",)),
    ("audio", (".mp1",)),
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
_ZIP_BASED = frozenset(_GROUPS[18][1]) | {".zip"}

_GROUP_OF: dict[str, int] = {}
_FAMILY_OF: dict[str, Family] = {}
for _index, (_family, _extensions) in enumerate(_GROUPS):
    for _ext in _extensions:
        _GROUP_OF[_ext] = _index
        _FAMILY_OF[_ext] = _family
for _ext in _ZIP_BASED:
    _GROUP_OF[_ext] = _GROUP_OF[".zip"]

_HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"})
_HEIF_GENERIC_BRANDS = frozenset({b"mif1", b"msf1"})
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


def _heif_by_compatible_brands(head: bytes) -> str | None:
    """``mif1``/``msf1`` name only the container; the compatible brands say AVIF or HEIC."""
    size = min(int.from_bytes(head[0:4], "big"), len(head))
    brands = {head[i : i + 4] for i in range(16, size - 3, 4)}
    if brands & _AVIF_BRANDS:
        return ".avif"
    if brands & _HEIC_BRANDS:
        return ".heic"
    return None


def _ftyp(head: bytes) -> str | None:
    brand = head[8:12]
    if brand in _HEIF_GENERIC_BRANDS:
        return _heif_by_compatible_brands(head)
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


# MPEG audio bitrates in kbit/s, indexed by the 4-bit bitrate field (0 and 15 are not usable).
_BITRATES_V1: dict[int, tuple[int, ...]] = {  # key: layer bits (3 = I, 2 = II, 1 = III)
    3: (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    2: (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    1: (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
}
_BITRATES_V2: dict[int, tuple[int, ...]] = {  # MPEG-2 and 2.5
    3: (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    2: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    1: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
_MPEG_AUDIO_BY_LAYER = {3: ".mp1", 2: ".mp2", 1: ".mp3"}


def mpeg_frame_length(header: bytes) -> int | None:
    """The length in bytes of the MPEG audio frame that starts with ``header``, or ``None``.

    ``None`` when the 4 bytes are not a valid frame header: no sync bits, the reserved version or
    layer, a free-format or bad bitrate, or the reserved sample rate. Layer I frames count in
    4-byte slots (``(12 * bitrate / rate + padding) * 4``); layer II is ``144 * bitrate / rate +
    padding``, and layer III is the same for MPEG-1 and ``72 * ...`` for MPEG-2 and 2.5.
    """
    if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version, layer = (header[1] >> 3) & 0x03, (header[1] >> 1) & 0x03
    bitrate_index, rate_index = header[2] >> 4, (header[2] >> 2) & 0x03
    padding = (header[2] >> 1) & 0x01
    if version == 1 or layer == 0 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    table = _BITRATES_V1 if version == 3 else _BITRATES_V2
    bitrate = table[layer][bitrate_index - 1] * 1000
    rate = _RATES[version][rate_index]
    if layer == 3:
        return (12 * bitrate // rate + padding) * 4
    factor = 72 if layer == 1 and version != 3 else 144
    return factor * bitrate // rate + padding


def _mpeg_audio(head: bytes) -> str | None:
    """``.mp1``, ``.mp2`` or ``.mp3`` for a stream of MPEG audio frames that starts at byte 0.

    Layer I and II (MP1, MP2) need two valid frame headers one frame length apart, with the same
    version and layer: a header alone is too weak (the first bytes of other formats can look
    like one). An ID3 tag in front never decides anything. Layer III (MP3) keeps its single
    header test.
    """
    length = mpeg_frame_length(head[:4])
    if length is None:
        return None
    layer = (head[1] >> 1) & 0x03
    if layer == 1:
        return ".mp3"
    second = head[length : length + 4]
    if mpeg_frame_length(second) is None or second[1] & 0x1E != head[1] & 0x1E:
        return None
    return _MPEG_AUDIO_BY_LAYER[layer]


def detect(head: bytes) -> str | None:
    """The extension (lower case, with a dot) that ``head`` calls for, or ``None`` if unknown.

    ``head`` is the first bytes of a file (the tool reads 4096; two MPEG audio frames need that
    much). Content that is not recognised gives ``None`` and such files are left alone.
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
    mpeg_audio = _mpeg_audio(head)
    if mpeg_audio is not None:
        return mpeg_audio
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
