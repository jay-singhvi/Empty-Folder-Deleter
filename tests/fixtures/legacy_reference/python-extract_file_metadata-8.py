"""
Scan a folder of photos/files and move eligible ones into
"<year>\\<year> - <month>" folders under DEST_ROOT, based on date.

RULES
-----
1. The date used to decide where a file goes is the capture date whenever
   it's available: EXIF "Date Taken" for photos (JPEG/HEIC/TIFF/etc.), or
   the embedded "creation_time" tag for videos (MP4/MOV, via ffprobe).
2. If no capture date is available AND the file is fully on this device
   (not a OneDrive cloud-only placeholder), fall back to Date Modified.
3. If a capture date can't be read because the file is a OneDrive
   cloud-only placeholder (not downloaded), the file is NOT moved. No
   fallback to Date Modified in that case -- reading the capture date
   would require downloading it, which this script never does on its own.
4. Only files already available on the device get moved. Cloud-only files
   are left in place and logged as skipped -- unless you say yes to the
   interactive prompt this script shows at the end, which offers to
   download just those skipped files from OneDrive and then process them
   (using the same Date Taken / Date Modified rules) before saving the log.

SINGLE MASTER LOG
-----------------
All runs -- regardless of which source folder you point at -- update ONE
shared Excel file (upsert by file name) instead of writing a separate log
per folder:
    C:\\Users\\<you>\\OneDrive\\Pictures\\Photo_Metadata_Master.xlsx
The <you> part is resolved dynamically from %USERPROFILE%, not hardcoded.

SAFETY
------
Files are moved only when --move is supplied. Use --dry-run-move to preview
the move plan. Without either option, processing happens in place.

IMAGE RESIZING
--------------
Pass --resize PIXELS to downscale images proportionally until their shorter
side is PIXELS pixels. Images are never enlarged. Without --resize, images
are not resized. When no move option is supplied, resizing happens in place.
CR3 files are converted to JPEG in the same folder; the CR3 is removed only
after the JPEG is complete and its transferable metadata has been copied.

USAGE
-----
    python python-extract_file_metadata-8.py                         # metadata only, no moves
    python python-extract_file_metadata-8.py --move                  # actually moves files
    python python-extract_file_metadata-8.py "SOME\\FOLDER" --dry-run-move
    python python-extract_file_metadata-8.py "FOLDER1" "FOLDER2" --move
    python python-extract_file_metadata-8.py "SOME\\FOLDER" --recursive
    python python-extract_file_metadata-8.py "SOME\\FOLDER" --recursive --resize 1080
    python python-extract_file_metadata-8.py "SOME\\FOLDER" --recursive --resize 1080 --y
    python python-extract_file_metadata-8.py "SOME\\FOLDER" --recursive --resize 1080 --y --skip-processed
    python python-extract_file_metadata-8.py "SOME\\FOLDER" --recursive --resize 1080 --N
    python python-extract_file_metadata-8.py "SOME\\FOLDER" --recursive --resize 1080 --errors-only
    python python-extract_file_metadata-8.py "SOME\\FOLDER" --download-wait-seconds 10

Note: RECURSIVE_SOURCE_SCAN is off by default, so pointing this at a whole
year folder (e.g. ...\\DCIM\\2021) will process only files directly inside
that folder and will ignore its subfolders. Pass --recursive to include
subfolders for a specific run.

REQUIREMENTS
------------
    pip install pandas openpyxl Pillow pillow-heif rawpy

    For video capture dates (MP4/MOV embedded creation_time), ffmpeg must
    also be installed and its "ffprobe" executable available on PATH.
    Without pillow-heif, HEIC photos fall back to Date Modified. Without
    ffprobe, all videos fall back to Date Modified. Both are optional --
    the script still runs and logs which fallback was used per file.

    CR3 conversion additionally requires ExifTool on PATH. ExifTool copies
    EXIF, GPS, XMP, IPTC, ICC, Date Taken, camera, and lens metadata into the
    JPEG. RAW-only sensor data cannot be represented in JPEG.
"""

import os
import shutil
import subprocess
import json
import ctypes
import time
import argparse
import sys
import uuid
import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from datetime import datetime

import pandas as pd

try:
    from PIL import Image, ExifTags, ImageOps
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import pillow_heif
    pillow_heif.register_heif_opener()   # makes Image.open() work transparently on .heic/.heif
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

try:
    import rawpy
    RAWPY_AVAILABLE = True
except ImportError:
    RAWPY_AVAILABLE = False

FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
RAW_RESIZE_LOCK = threading.Lock()

# ---- CONFIG -------------------------------------------------------------
# Derive OneDrive paths from the current account instead of hard-coding a
# drive letter or user name.
USER_PROFILE = Path(os.environ.get("USERPROFILE", str(Path.home())))
PERSONAL_ONEDRIVE_ROOT = USER_PROFILE / "OneDrive"
ONEDRIVE_ROOT = (
    PERSONAL_ONEDRIVE_ROOT
    if PERSONAL_ONEDRIVE_ROOT.is_dir()
    else Path(os.environ.get("OneDrive", PERSONAL_ONEDRIVE_ROOT))
)
SOURCE_FOLDER = ONEDRIVE_ROOT / "Pictures" / "Samsung Gallery" / "DCIM" / "2026" / "01"
DEST_ROOT = ONEDRIVE_ROOT / "Pictures" / "Camera Roll"
RECURSIVE_SOURCE_SCAN = False   # True = also pull files from subfolders of each source folder

FOLDER_NAME_FORMAT = "{year} - {month:02d}"   # matches "2024 - 10" style
DOWNLOAD_WAIT_SECONDS = 30
JPEG_QUALITY = 95

# Single master log in the same resolved OneDrive tree.
MASTER_LOG_PATH = ONEDRIVE_ROOT / "Pictures" / "Photo_Metadata_Master.xlsx"
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp"}
RAW_EXTS = {".cr3"}
RESIZABLE_EXTS = IMAGE_EXTS | RAW_EXTS
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv"}

# Excel's XML format rejects these ASCII control characters even though they
# can legitimately appear in camera/EXIF text fields.
EXCEL_ILLEGAL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


PROCESSABLE_EXTS = IMAGE_EXTS | RAW_EXTS | VIDEO_EXTS
RUN_STATE: dict = {}
MIN_PLAUSIBLE_YEAR = 1990
UNKNOWN_DATE_FOLDER = "Unknown Date"


def is_plausible_date(value) -> bool:
    """Reject reset camera clocks / zero epochs (1970, 1904...) and far-future dates."""
    return (
        isinstance(value, datetime)
        and MIN_PLAUSIBLE_YEAR <= value.year <= datetime.now().year + 1
    )


def parse_exif_datetime(value):
    """Parse an EXIF date string; return None for blank/zeroed/garbled values."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    text = str(value).replace("\x00", "").strip()[:19]
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def get_exiftool_date_taken(filepath: Path):
    """Read the capture date of files Pillow cannot open (CR3) using ExifTool."""
    exiftool = find_exiftool()
    if not exiftool:
        return None
    try:
        result = subprocess.run(
            [exiftool, "-s3", "-d", "%Y:%m:%d %H:%M:%S",
             "-DateTimeOriginal", "-CreateDate", str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    for line in result.stdout.splitlines():
        parsed = parse_exif_datetime(line)
        if parsed:
            return parsed
    return None


def has_embedded_extras(filepath: Path) -> bool:
    """Detect Motion Photo / Ultra HDR JPEGs, which Pillow cannot re-save intact."""
    markers = (b"MotionPhoto", b"MicroVideo", b"hdrgm")
    try:
        with open(filepath, "rb") as f:
            head = f.read(262144)
        return any(marker in head for marker in markers)
    except OSError:
        return False


def _path_key_or_none(value):
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    if not str(value).strip():
        return None
    return normalized_path_key(value)


def make_error_row(fpath: Path, exc: Exception) -> dict:
    """One unreadable/locked file must not abort the whole run."""
    return {
        "FileName": fpath.name,
        "Extension": fpath.suffix.lower(),
        "FolderPath": str(fpath.parent),
        "CurrentFullPath": str(fpath),
        "OriginalFullPath": str(fpath),
        "Action": f"Error: {exc}",
        "ResizeAction": "Skipped - file unavailable",
        "LastChecked": datetime.now(),
    }


def process_and_move(fpath: Path, resize_target, move_requested: bool, dry_run: bool) -> dict:
    """Single code path for inspect -> (move) -> (resize); never raises."""
    try:
        row = process_file(fpath)
        if move_requested:
            row = execute_move(row, dry_run)
        elif row["Action"] in ("Pending", "Already organized (no move needed)"):
            row["DestinationPath"] = str(fpath)
            row["Action"] = "Processed in place (no move)"
        if dry_run:
            # A dry run must never modify, convert or delete image files.
            row["ResizeAction"] = (
                "Skipped - dry run" if resize_target is not None else "Not applicable"
            )
            row["ResizedWidth"] = None
            row["ResizedHeight"] = None
            return row
        return resize_image_if_needed(row, resize_target)
    except Exception as e:
        return make_error_row(fpath, e)


def unpin_file(filepath: Path):
    """Undo the 'always keep on this device' pin that force_download applies."""
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["attrib", "-P", "+U", str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def safe_input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        print("\n(no interactive input available - answering 'no')")
        return "n"


def flush_log_on_abort():
    """Save whatever was processed if the run crashed or was interrupted."""
    if not RUN_STATE or RUN_STATE.get("saved") or not RUN_STATE.get("new_rows"):
        return
    try:
        merged = upsert_rows(
            RUN_STATE["master_df"],
            [*RUN_STATE["checkpoint_rows"], *RUN_STATE["new_rows"]],
        )
        save_master_log(merged)
        RUN_STATE["saved"] = True
        print(f"\nMaster log saved after an interrupted/failed run: {MASTER_LOG_PATH}")
    except Exception as e:
        print(f"\nCould not save master log after abort: {e}")


def get_sync_status(filepath: Path) -> str:
    """Cloud-only vs local check via Windows attributes -- never downloads."""
    if os.name != "nt":
        return "Unknown (non-Windows)"
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(filepath))
        if attrs == -1:
            return "Unknown"
        recall_flags = FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        if attrs & recall_flags or attrs & FILE_ATTRIBUTE_OFFLINE:
            return "Cloud-only (not downloaded)"
        if attrs & FILE_ATTRIBUTE_PINNED:
            return "Always available (pinned)"
        return "Local"
    except Exception:
        return "Unknown"


def is_on_device(sync_status: str) -> bool:
    return sync_status in ("Local", "Always available (pinned)")


def is_regular_file_no_hydrate(filepath: Path) -> bool:
    """
    Confirm a path exists and is a regular (non-directory) file WITHOUT
    triggering OneDrive hydration.

    Path.is_file() calls os.stat(follow_symlinks=True), which opens a handle
    to the file. On a cloud-only placeholder that handle-open can block on the
    OneDrive filter driver's metadata callback and hang indefinitely if the
    sync engine is throttled / re-authenticating. GetFileAttributesW reads the
    placeholder's cached attributes and never recalls the file, so it returns
    immediately regardless of sync state.
    """
    if os.name != "nt":
        return filepath.is_file()
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(filepath))
    if attrs == -1:            # INVALID_FILE_ATTRIBUTES -> does not exist
        return False
    return not (attrs & FILE_ATTRIBUTE_DIRECTORY)


def _extract_exif_tags(img) -> dict:
    """
    Read a merged dict of EXIF tags using Pillow's modern getexif() API,
    including the Exif sub-IFD (where DateTimeOriginal actually lives) and
    the GPS IFD. This works across JPEG, TIFF, PNG, and HEIC (via
    pillow-heif) -- unlike the legacy _getexif() method, which only
    reliably merges the sub-IFD for native JPEG/TIFF files.
    """
    try:
        exif = img.getexif()
    except Exception:
        return {}
    if not exif:
        return {}

    tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}

    try:
        exif_ifd = exif.get_ifd(0x8769)  # Exif sub-IFD: DateTimeOriginal, etc.
        tags.update({ExifTags.TAGS.get(k, k): v for k, v in exif_ifd.items()})
    except Exception:
        pass

    try:
        gps_ifd = exif.get_ifd(0x8825)  # GPS sub-IFD
        if gps_ifd:
            tags["GPSInfo"] = gps_ifd
    except Exception:
        pass

    return tags


def get_exif_data(filepath: Path) -> dict:
    """Full EXIF pull -- only ever called for files already on device."""
    info = {
        "DateTaken": None, "CameraMake": None, "CameraModel": None,
        "ImageWidth": None, "ImageHeight": None,
        "GPSLatitude": None, "GPSLongitude": None,
    }
    if not (PIL_AVAILABLE and filepath.suffix.lower() in IMAGE_EXTS):
        return info
    try:
        with Image.open(filepath) as img:
            info["ImageWidth"], info["ImageHeight"] = img.size
            tags = _extract_exif_tags(img)
            if not tags:
                return info
            # Capture-date tags only. "DateTime" is the last-modified stamp
            # written by editors, so it is intentionally not used here.
            for date_tag in ("DateTimeOriginal", "DateTimeDigitized"):
                parsed_date = parse_exif_datetime(tags.get(date_tag))
                if parsed_date:
                    info["DateTaken"] = parsed_date
                    break
            info["CameraMake"] = tags.get("Make")
            info["CameraModel"] = tags.get("Model")
            gps_info = tags.get("GPSInfo")
            if gps_info:
                info["GPSLatitude"], info["GPSLongitude"] = _convert_gps(gps_info)
    except Exception:
        pass
    return info


def _convert_gps(gps_info: dict):
    try:
        def to_deg(value):
            d, m, s = value
            return d + (m / 60.0) + (s / 3600.0)
        lat = to_deg(gps_info[2])
        if gps_info.get(1) == "S":
            lat = -lat
        lon = to_deg(gps_info[4])
        if gps_info.get(3) == "W":
            lon = -lon
        return round(lat, 6), round(lon, 6)
    except Exception:
        return None, None


def get_video_date_taken(filepath: Path):
    """
    Read the embedded 'creation_time' tag from a video container (MP4/MOV
    box metadata) via ffprobe -- the video equivalent of EXIF
    DateTimeOriginal. Requires ffmpeg/ffprobe on PATH. Returns a naive
    local datetime, or None if unavailable/unreadable.
    """
    if filepath.suffix.lower() not in VIDEO_EXTS:
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_entries", "format_tags=creation_time:stream_tags=creation_time",
                str(filepath),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not result.stdout:
            return None

        data = json.loads(result.stdout)
        raw = data.get("format", {}).get("tags", {}).get("creation_time")
        if not raw:
            for stream in data.get("streams", []):
                raw = stream.get("tags", {}).get("creation_time")
                if raw:
                    break
        if not raw:
            return None

        # creation_time is typically UTC ISO 8601, e.g. "2026-03-15T14:30:22.000000Z"
        dt_utc = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt_utc.astimezone().replace(tzinfo=None)
    except FileNotFoundError:
        return None   # ffprobe not installed
    except Exception:
        return None


def unique_destination(dest_folder: Path, filename: str) -> Path:
    candidate = dest_folder / filename
    if not candidate.exists():
        return candidate
    stem, ext = Path(filename).stem, Path(filename).suffix
    counter = 1
    while True:
        candidate = dest_folder / f"{stem} ({counter}){ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def process_file(fpath: Path) -> dict:
    stat = fpath.stat()
    sync_status = get_sync_status(fpath)
    on_device = is_on_device(sync_status)

    exif = get_exif_data(fpath) if on_device else {
        "DateTaken": None, "CameraMake": None, "CameraModel": None,
        "ImageWidth": None, "ImageHeight": None,
        "GPSLatitude": None, "GPSLongitude": None,
    }
    if on_device and exif["DateTaken"] is None and fpath.suffix.lower() in RAW_EXTS:
        exif["DateTaken"] = get_exiftool_date_taken(fpath)
    date_modified = datetime.fromtimestamp(stat.st_mtime)

    row = {
        "FileName": fpath.name,
        "Extension": fpath.suffix.lower(),
        "FolderPath": str(fpath.parent),
        "CurrentFullPath": str(fpath),
        "OriginalFullPath": str(fpath),
        "SizeBytes": stat.st_size,
        "SizeMB": round(stat.st_size / (1024 * 1024), 3),
        "CreatedDate": datetime.fromtimestamp(stat.st_ctime),
        "DateModified": date_modified,
        "SyncStatus": sync_status,
        "DateTaken": exif["DateTaken"],
        "CameraMake": exif["CameraMake"],
        "CameraModel": exif["CameraModel"],
        "ImageWidth": exif["ImageWidth"],
        "ImageHeight": exif["ImageHeight"],
        "GPSLatitude": exif["GPSLatitude"],
        "GPSLongitude": exif["GPSLongitude"],
        "DateUsed": None,
        "DateSource": None,
        "DestinationPath": None,
        "ConvertedFromFileName": None,
        "Action": None,
        "LastChecked": datetime.now(),
    }

    if not on_device:
        row["Action"] = "Skipped - cloud-only (not on device)"
        return row

    video_date = get_video_date_taken(fpath) if fpath.suffix.lower() in VIDEO_EXTS else None

    unknown_date = False
    if is_plausible_date(exif["DateTaken"]):
        row["DateTaken"] = exif["DateTaken"]
        row["DateUsed"] = exif["DateTaken"]
        row["DateSource"] = "Date Taken (EXIF)"
    elif is_plausible_date(video_date):
        row["DateTaken"] = video_date
        row["DateUsed"] = video_date
        row["DateSource"] = "Date Taken (video metadata)"
    elif is_plausible_date(date_modified):
        row["DateUsed"] = date_modified
        row["DateSource"] = "Date Modified (fallback)"
    else:
        row["DateUsed"] = date_modified
        row["DateSource"] = "Date Modified (implausible - unknown date)"
        unknown_date = True

    if unknown_date:
        dest_folder = Path(DEST_ROOT) / UNKNOWN_DATE_FOLDER
    else:
        folder_name = FOLDER_NAME_FORMAT.format(year=row["DateUsed"].year, month=row["DateUsed"].month)
        dest_folder = Path(DEST_ROOT) / str(row["DateUsed"].year) / folder_name
    candidate = dest_folder / fpath.name

    try:
        already_there = candidate.resolve() == fpath.resolve()
    except Exception:
        already_there = str(candidate) == str(fpath)

    if already_there:
        row["DestinationPath"] = str(fpath)
        row["Action"] = "Already organized (no move needed)"
        return row

    row["DestinationPath"] = str(unique_destination(dest_folder, fpath.name))
    row["Action"] = "Pending"
    return row


def execute_move(row: dict, dry_run: bool) -> dict:
    if row["Action"] != "Pending":
        return row  # already skipped (cloud-only)

    src = Path(row["CurrentFullPath"])
    dest = Path(row["DestinationPath"])

    if dry_run:
        row["Action"] = "Would move (dry run)"
        return row

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        row["CurrentFullPath"] = str(dest)
        row["FolderPath"] = str(dest.parent)
        row["Action"] = "Moved"
    except Exception as e:
        row["Action"] = f"Error: {e}"

    return row


def find_exiftool():
    """Return the ExifTool executable path, if it is available on PATH."""
    for name in ("exiftool", "exiftool.exe"):
        executable = shutil.which(name)
        if executable:
            return executable
    known_locations = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ExifTool" / "ExifTool.exe",
        Path(os.environ.get("ProgramFiles", "")) / "ExifTool" / "ExifTool.exe",
    ]
    for executable in known_locations:
        if executable.is_file():
            return str(executable)
    return None


def convert_cr3_to_jpeg(row: dict, target_short_side_px: int) -> dict:
    """Convert CR3 to JPEG, copy writable metadata, then remove the CR3."""
    source = Path(row["CurrentFullPath"])
    source_filename = source.name
    # RAW+JPEG pairs are common: never collide with an existing same-named JPEG.
    target = unique_destination(source.parent, source.with_suffix(".jpg").name)
    temp_path = target.with_name(
        f".{target.stem}.cr3-convert-{uuid.uuid4().hex}{target.suffix}"
    )

    if not RAWPY_AVAILABLE:
        row["ResizeAction"] = "Error: CR3 conversion requires rawpy (python -m pip install rawpy)"
        return row
    if not PIL_AVAILABLE:
        row["ResizeAction"] = "Error: CR3 conversion requires Pillow"
        return row

    exiftool = find_exiftool()
    if not exiftool:
        row["ResizeAction"] = "Error: CR3 metadata preservation requires ExifTool on PATH"
        return row
    if target.exists():
        row["ResizeAction"] = f"Error: CR3 JPEG destination already exists: {target}"
        return row

    try:
        original_stat = source.stat()
        with rawpy.imread(str(source)) as raw:
            # Keep the sensor orientation and copy the CR3 Orientation tag;
            # viewers will display the JPEG exactly as they displayed the RAW.
            rgb = raw.postprocess(
                use_camera_wb=True,
                output_bps=8,
                user_flip=0,
            )

        image = Image.fromarray(rgb)
        width, height = image.size
        short_side = min(width, height)
        if short_side > target_short_side_px:
            scale = target_short_side_px / short_side
            new_size = (round(width * scale), round(height * scale))
            image = image.resize(new_size, Image.Resampling.LANCZOS)
        else:
            new_size = (width, height)

        image.save(
            temp_path,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
            subsampling=0,
        )

        # Copy all metadata that JPEG can represent, retaining original groups.
        # -unsafe includes normally excluded EXIF tags such as maker-note blocks.
        metadata_result = subprocess.run(
            [
                exiftool,
                "-overwrite_original",
                "-TagsFromFile", str(source),
                "-all:all",
                "-unsafe",
                # The copied RAW dimension tags would otherwise disagree with
                # the resized JPEG's real pixel size.
                f"-ExifIFD:ExifImageWidth={new_size[0]}",
                f"-ExifIFD:ExifImageHeight={new_size[1]}",
                str(temp_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if metadata_result.returncode != 0:
            detail = (metadata_result.stderr or metadata_result.stdout or "").strip()
            raise RuntimeError(f"ExifTool metadata copy failed: {detail}")

        os.replace(temp_path, target)
        os.utime(
            target,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

        # Delete the RAW only after the converted JPEG and metadata are safely
        # in their final location.
        source.unlink()

        row["FileName"] = target.name
        row["ConvertedFromFileName"] = source_filename
        row["Extension"] = target.suffix.lower()
        row["CurrentFullPath"] = str(target)
        row["FolderPath"] = str(target.parent)
        row["DestinationPath"] = str(target)
        row["SizeBytes"] = target.stat().st_size
        row["SizeMB"] = round(row["SizeBytes"] / (1024 * 1024), 3)
        row["ImageWidth"], row["ImageHeight"] = new_size
        row["ResizedWidth"], row["ResizedHeight"] = new_size
        row["ResizeAction"] = (
            f"Converted CR3 {width}x{height} to JPEG {new_size[0]}x{new_size[1]}; metadata preserved"
        )
    except Exception as e:
        row["ResizeAction"] = f"Error: CR3 conversion failed - {e}"
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass

    return row


def resize_image_if_needed(row: dict, target_short_side_px: int | None) -> dict:
    """Optionally downscale proportionally to the requested shorter-side size."""
    row["ResizeAction"] = "Not applicable"
    row["ResizedWidth"] = None
    row["ResizedHeight"] = None

    if target_short_side_px is None or row["Extension"] not in RESIZABLE_EXTS:
        return row
    if str(row["Action"]).startswith(("Skipped", "Error")):
        row["ResizeAction"] = "Skipped - file unavailable"
        return row
    if row["Extension"] in RAW_EXTS:
        # RAW decoding and ExifTool metadata copying are intentionally
        # serialized because each conversion can use substantial memory.
        with RAW_RESIZE_LOCK:
            return convert_cr3_to_jpeg(row, target_short_side_px)
    if not PIL_AVAILABLE:
        row["ResizeAction"] = "Skipped - Pillow not installed"
        return row

    filepath = Path(row["CurrentFullPath"])
    if filepath.suffix.lower() in (".jpg", ".jpeg") and has_embedded_extras(filepath):
        row["ResizeAction"] = (
            "Left as-is - Motion Photo / HDR gain map (resizing would drop embedded data)"
        )
        return row
    temp_path = filepath.with_name(
        f".{filepath.stem}.resize-{uuid.uuid4().hex}{filepath.suffix}"
    )

    try:
        with Image.open(filepath) as original:
            if original.format == "MPO" or getattr(original, "is_animated", False):
                row["ResizeAction"] = (
                    "Left as-is - multi-picture or animated image (resizing would drop frames/data)"
                )
                return row
            displayed = ImageOps.exif_transpose(original)
            width, height = displayed.size
            row["ImageWidth"], row["ImageHeight"] = width, height

            short_side = min(width, height)
            if short_side <= target_short_side_px:
                row["ResizeAction"] = "No resize needed"
                row["ResizedWidth"], row["ResizedHeight"] = width, height
                return row

            scale = target_short_side_px / short_side
            new_size = (round(width * scale), round(height * scale))
            row["ResizedWidth"], row["ResizedHeight"] = new_size

            resized = displayed.resize(new_size, Image.Resampling.LANCZOS)
            image_format = original.format
            save_kwargs = {}
            # exif_transpose removes the orientation flag after physically
            # rotating the pixels, preventing a second rotation by viewers.
            for key in ("exif", "icc_profile", "dpi"):
                if displayed.info.get(key) is not None:
                    save_kwargs[key] = displayed.info[key]

            if image_format == "JPEG":
                if resized.mode not in ("RGB", "L"):
                    resized = resized.convert("RGB")
                save_kwargs.update(quality=JPEG_QUALITY, optimize=True)
            elif image_format == "PNG":
                save_kwargs["optimize"] = True
            elif image_format in ("HEIF", "HEIC", "WEBP"):
                save_kwargs["quality"] = JPEG_QUALITY

            original_stat = filepath.stat()
            resized.save(temp_path, format=image_format, **save_kwargs)

        os.replace(temp_path, filepath)
        # Replacing the image would normally assign the newly written file a
        # new Date Modified. Restore the original timestamps exactly.
        os.utime(
            filepath,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        row["SizeBytes"] = filepath.stat().st_size
        row["SizeMB"] = round(row["SizeBytes"] / (1024 * 1024), 3)
        row["ImageWidth"], row["ImageHeight"] = new_size
        row["ResizeAction"] = (
            f"Resized {width}x{height} to {new_size[0]}x{new_size[1]}"
        )
    except Exception as e:
        row["ResizeAction"] = f"Error: {e}"
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass

    return row


def force_download(filepath: Path, wait_seconds: int = DOWNLOAD_WAIT_SECONDS) -> tuple[bool, str]:
    """
    Force OneDrive to fully download a cloud-only placeholder.

    First ask Windows/OneDrive to pin and hydrate it. If that does not make
    the file local, fall back to reading bytes, which also triggers recall.
    """
    if not filepath.exists():
        return False, "file no longer exists at logged path"

    attrib_detail = None
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["attrib", "+P", "-U", str(filepath)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                attrib_detail = f"attrib failed: {detail or f'exit code {result.returncode}'}"
            else:
                deadline = time.monotonic() + wait_seconds
                while time.monotonic() < deadline:
                    if is_on_device(get_sync_status(filepath)):
                        return True, "downloaded"
                    time.sleep(1)
                attrib_detail = f"attrib did not finish download within {wait_seconds}s"
        except subprocess.TimeoutExpired:
            attrib_detail = "attrib timed out"
        except Exception as e:
            attrib_detail = f"attrib failed: {e}"

    try:
        with open(filepath, "rb") as f:
            while f.read(1024 * 1024):
                pass
        return True, "downloaded"
    except Exception as e:
        read_detail = f"read failed: {e}"
        if attrib_detail:
            return False, f"{attrib_detail}; {read_detail}"
        return False, read_detail


def process_file_in_place(fpath: Path, resize_target: int | None) -> dict:
    """Extract metadata and optionally resize one file without moving it."""
    row = process_file(fpath)
    if row["Action"] in ("Pending", "Already organized (no move needed)"):
        row["DestinationPath"] = str(fpath)
        row["Action"] = "Processed in place (no move)"
    return resize_image_if_needed(row, resize_target)


def download_and_process_file_in_place(
    fpath: Path,
    wait_seconds: int,
    resize_target: int | None,
    move_requested: bool = False,
    dry_run: bool = False,
    keep_pinned: bool = False,
) -> tuple[bool, str, dict | None]:
    """Hydrate and process one cloud file (honouring --move); thread-safe."""
    downloaded, detail = force_download(fpath, wait_seconds)
    if not downloaded:
        return False, detail, None
    row = process_and_move(fpath, resize_target, move_requested, dry_run)
    if not keep_pinned:
        unpin_file(Path(row["CurrentFullPath"]))
    return True, detail, row


def bounded_thread_results(items, worker, max_workers: int):
    """Yield completed worker futures while keeping at most max_workers active."""
    item_iterator = iter(items)
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="photo-worker")
    pending = {}

    def submit_next():
        try:
            item = next(item_iterator)
        except StopIteration:
            return False
        pending[executor.submit(worker, item)] = item
        return True

    for _ in range(max_workers):
        if not submit_next():
            break

    try:
        while pending:
            completed_futures, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed_futures:
                item = pending.pop(future)
                submit_next()
                yield item, future
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def load_master_log() -> pd.DataFrame:
    if MASTER_LOG_PATH.exists():
        return pd.read_excel(MASTER_LOG_PATH, sheet_name="Metadata")
    return pd.DataFrame()


def checkpoint_path_for(args) -> Path:
    """Return a resume-journal path specific to the requested operation."""
    if args.move:
        mode = "move"
    elif args.dry_run_move:
        mode = "dry-run-move"
    else:
        mode = "in-place"
    resize = f"resize-{args.resize}" if args.resize is not None else "no-resize"
    # Kept on the local disk: appending + fsyncing inside a OneDrive folder
    # makes the sync client re-upload the journal after every file.
    journal_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PhotoMetadata"
    return journal_dir / f"{MASTER_LOG_PATH.stem}.{mode}.{resize}.resume.jsonl"


def _checkpoint_json_default(value):
    """Preserve date values in the append-only JSON resume journal."""
    if isinstance(value, (datetime, pd.Timestamp)):
        return {"__photo_metadata_datetime__": value.isoformat()}
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _checkpoint_json_hook(value):
    marker = value.get("__photo_metadata_datetime__")
    if marker is not None and len(value) == 1:
        return datetime.fromisoformat(marker)
    return value


def load_checkpoint_rows(checkpoint_path: Path) -> list[dict]:
    """Load complete records, ignoring only a torn final line after shutdown."""
    if not checkpoint_path.exists():
        return []

    rows = []
    with open(checkpoint_path, "r", encoding="utf-8") as journal:
        for line_number, line in enumerate(journal, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line, object_hook=_checkpoint_json_hook)
                if isinstance(record, dict):
                    rows.append(record)
            except (json.JSONDecodeError, ValueError):
                print(
                    f"Note: ignored incomplete resume-journal line {line_number}: "
                    f"{checkpoint_path}"
                )
    return rows


def append_checkpoint_row(checkpoint_path: Path, row: dict):
    """Durably record one completed file without rewriting the Excel workbook."""
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        row,
        default=_checkpoint_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with open(checkpoint_path, "a", encoding="utf-8", newline="\n") as journal:
        journal.write(payload + "\n")
        journal.flush()
        os.fsync(journal.fileno())


def row_completed_successfully(row, resize_target: int | None) -> bool:
    """Return whether a logged row fully completed the requested processing."""
    raw_action = row.get("Action", "")
    action = "" if raw_action is None or pd.isna(raw_action) else str(raw_action)
    if not action or action.startswith(("Error", "Skipped", "Pending", "Would move")):
        return False

    if resize_target is None:
        return True
    # Videos and other non-image files are never resized, so they are complete.
    if str(row.get("Extension", "")).lower() not in RESIZABLE_EXTS:
        return True

    raw_resize_action = row.get("ResizeAction", "")
    resize_action = (
        ""
        if raw_resize_action is None or pd.isna(raw_resize_action)
        else str(raw_resize_action)
    )
    if resize_action.startswith("Left as-is"):
        return True
    if not resize_action.startswith(("Resized", "No resize needed", "Converted CR3")):
        return False

    # Do not skip a file when the new target is smaller than its logged final
    # dimensions. A smaller/equal logged image is already safe because this
    # script never enlarges images.
    try:
        width = float(row.get("ResizedWidth"))
        height = float(row.get("ResizedHeight"))
        return min(width, height) <= resize_target
    except (TypeError, ValueError):
        return False


def row_has_processing_error(row) -> bool:
    """Return whether metadata/move or resize processing recorded an error."""
    return str(row.get("Action", "")).startswith("Error") or str(
        row.get("ResizeAction", "")
    ).startswith("Error")


def format_progress(completed: int, total: int, errors: int = 0) -> str:
    """Return a compact console progress bar with completion and queue counts."""
    total = max(total, 0)
    completed = min(max(completed, 0), total)
    pending = total - completed
    ratio = completed / total if total else 1.0
    bar_width = 24
    filled = round(ratio * bar_width)
    bar = "#" * filled + "-" * (bar_width - filled)
    return (
        f"[{bar}] {completed:,}/{total:,} complete ({ratio:6.2%}) | "
        f"{pending:,} pending | {errors:,} errors"
    )


def processed_path_keys(master_df: pd.DataFrame, checkpoint_rows: list[dict], resize_target) -> set[str]:
    """Build the successful-path set used before any image content is opened."""
    keys = set()
    if not master_df.empty:
        for row in master_df.to_dict(orient="records"):
            if row_completed_successfully(row, resize_target):
                path = row.get("CurrentFullPath")
                if path is not None and str(path).strip():
                    keys.add(normalized_path_key(path))
    for row in checkpoint_rows:
        if row_completed_successfully(row, resize_target):
            path = row.get("CurrentFullPath")
            if path is not None and str(path).strip():
                keys.add(normalized_path_key(path))
    return keys


def normalized_path_key(path) -> str:
    """Normalize a path for reliable case-insensitive matching on Windows."""
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def error_paths_from_log(master_df: pd.DataFrame) -> set[str]:
    """Return current paths whose latest logged processing or resize failed."""
    if master_df.empty or "CurrentFullPath" not in master_df.columns:
        return set()

    error_mask = pd.Series(False, index=master_df.index)
    for column in ("Action", "ResizeAction"):
        if column in master_df.columns:
            error_mask |= master_df[column].fillna("").astype(str).str.startswith("Error")

    paths = master_df.loc[error_mask, "CurrentFullPath"].dropna()
    return {normalized_path_key(path) for path in paths if str(path).strip()}


def upsert_rows(master_df: pd.DataFrame, new_rows: list) -> pd.DataFrame:
    if not new_rows:
        return master_df
    new_df = pd.DataFrame(new_rows)
    if master_df.empty:
        return new_df
    # Keyed on full path (current OR original), so same-named files in
    # different folders keep separate rows, while a file that was moved or
    # converted still replaces its earlier log row.
    replacement_keys = set()
    for column in ("CurrentFullPath", "OriginalFullPath"):
        if column in new_df.columns:
            replacement_keys.update(
                key for key in new_df[column].map(_path_key_or_none) if key
            )
    stale = pd.Series(False, index=master_df.index)
    for column in ("CurrentFullPath", "OriginalFullPath"):
        if column in master_df.columns:
            stale |= master_df[column].map(_path_key_or_none).isin(replacement_keys)
    master_df = master_df[~stale]
    if master_df.empty:
        return new_df

    columns = list(dict.fromkeys([*master_df.columns, *new_df.columns]))
    frames = [
        df.reindex(columns=columns).dropna(axis=1, how="all")
        for df in (master_df, new_df)
        if not df.empty
    ]
    return pd.concat(frames, ignore_index=True).reindex(columns=columns)


def sanitize_excel_value(value):
    """Remove characters Excel cannot store and enforce its cell text limit."""
    if not isinstance(value, str):
        return value
    return EXCEL_ILLEGAL_CHARACTER_RE.sub("", value)[:32767]


def save_master_log(df: pd.DataFrame):
    # Sanitize at the export boundary so this covers both newly extracted
    # metadata and values loaded from an existing master workbook.
    export_df = df.copy()
    text_columns = [
        column
        for column in export_df.columns
        if pd.api.types.is_object_dtype(export_df[column].dtype)
        or pd.api.types.is_string_dtype(export_df[column].dtype)
    ]
    for column in text_columns:
        export_df[column] = export_df[column].map(sanitize_excel_value)

    MASTER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = MASTER_LOG_PATH.with_name(
        f".{MASTER_LOG_PATH.stem}.{uuid.uuid4().hex}.tmp{MASTER_LOG_PATH.suffix}"
    )
    try:
        with pd.ExcelWriter(temp_path, engine="openpyxl") as writer:
            export_df.to_excel(writer, index=False, sheet_name="Metadata")
            worksheet = writer.sheets["Metadata"]
            for i, col in enumerate(export_df.columns, start=1):
                # pandas 3 preserves missing values as floats/NA when a column
                # is converted to strings, so call str() on each value before
                # measuring it instead of passing the raw value to len().
                data_max_len = max(
                    (len(str(value)) for value in export_df[col]),
                    default=0,
                )
                max_len = min(
                    max(data_max_len, len(str(col))) + 2,
                    255,
                )
                worksheet.column_dimensions[
                    worksheet.cell(row=1, column=i).column_letter
                ].width = max_len
        # Replace only after the entire workbook was written successfully.
        try:
            os.replace(temp_path, MASTER_LOG_PATH)
        except PermissionError:
            # Usually: the workbook is open in Excel. Keep the data anyway.
            fallback = MASTER_LOG_PATH.with_name(
                f"{MASTER_LOG_PATH.stem}.{datetime.now():%Y%m%d-%H%M%S}.recovery"
                f"{MASTER_LOG_PATH.suffix}"
            )
            os.replace(temp_path, fallback)
            print(
                f"\nWARNING: could not overwrite {MASTER_LOG_PATH} (open in Excel?). "
                f"Log saved to {fallback} instead."
            )
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass


def ffprobe_available() -> bool:
    try:
        result = subprocess.run(
            ["ffprobe", "-version"], capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except Exception:
        return False


def repair_windows_trailing_backslash_args(argv):
    """
    Windows native argument parsing treats a backslash before a closing quote
    as escaping that quote. This repairs the common command:
        "C:\\some folder\\" --move --recursive
    which otherwise arrives as one folder argument containing the flags.
    """
    repaired = []
    changed = False
    for arg in argv:
        if '" --' not in arg:
            repaired.append(arg)
            continue

        folder, flag_text = arg.split('" --', 1)
        repaired.append(folder)
        repaired.extend(("--" + flag_text).split())
        changed = True

    return repaired, changed


def parse_args():
    parser = argparse.ArgumentParser(
        description="Move photo/video files into year/month folders based on metadata date."
    )
    parser.add_argument(
        "folders",
        nargs="*",
        help="Source folder(s) to scan. Defaults to SOURCE_FOLDER in the script.",
    )
    move_group = parser.add_mutually_exclusive_group()
    move_group.add_argument(
        "--move",
        action="store_true",
        help="Move files into date folders.",
    )
    move_group.add_argument(
        "--dry-run-move",
        action="store_true",
        help="Preview date-folder moves without moving files.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan files in each source folder and all subfolders.",
    )
    parser.add_argument(
        "--download-wait-seconds",
        type=int,
        default=DOWNLOAD_WAIT_SECONDS,
        help=(
            "Seconds to wait per cloud-only file after asking OneDrive to download it. "
            f"Default: {DOWNLOAD_WAIT_SECONDS}."
        ),
    )
    parser.add_argument(
        "--resize",
        type=int,
        metavar="PIXELS",
        help=(
            "Downscale images proportionally so their shorter side is PIXELS. "
            "CR3 files are converted to metadata-preserving JPEGs. "
            "Images are not resized or converted when this option is omitted."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="COUNT",
        help=(
            "Process up to COUNT images concurrently for in-place resize runs. "
            "Use 2 for a conservative OneDrive/large-photo pipeline. Default: 1."
        ),
    )
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Process only files whose latest master-log Action or ResizeAction is an error.",
    )
    parser.add_argument(
        "--skip-processed",
        action="store_true",
        help=(
            "Resume safely by skipping successfully processed files before opening them. "
            "Each success is journaled immediately so an interrupted run can continue."
        ),
    )
    parser.add_argument(
        "--keep-pinned",
        action="store_true",
        help=(
            "Leave downloaded cloud files pinned ('always keep on this device'). "
            "By default they are unpinned again after processing."
        ),
    )
    download_prompt_group = parser.add_mutually_exclusive_group()
    download_prompt_group.add_argument(
        "--y",
        dest="download_cloud_files",
        action="store_true",
        help=(
            "Automatically download and process cloud-only files without prompting. "
            "Failed downloads are not retried interactively."
        ),
    )
    download_prompt_group.add_argument(
        "--N",
        dest="download_cloud_files",
        action="store_false",
        help="Leave cloud-only files skipped without prompting.",
    )
    parser.set_defaults(download_cloud_files=None)

    argv, repaired_shell_quoting = repair_windows_trailing_backslash_args(sys.argv[1:])
    args = parser.parse_args(argv)
    args.repaired_shell_quoting = repaired_shell_quoting
    if args.download_wait_seconds < 0:
        parser.error("--download-wait-seconds must be 0 or greater")
    if args.resize is not None and args.resize <= 0:
        parser.error("--resize must be greater than 0")
    if args.workers <= 0:
        parser.error("--workers must be greater than 0")
    if args.workers > 1 and (
        args.resize is None or args.move or args.dry_run_move
    ):
        parser.error("--workers greater than 1 requires --resize without a move option")
    return args


def run():
    args = parse_args()

    sources = [Path(a) for a in args.folders] if args.folders else [Path(SOURCE_FOLDER)]
    move_requested = args.move or args.dry_run_move
    resize_in_place_only = args.resize is not None and not move_requested
    recursive_scan = args.recursive or RECURSIVE_SOURCE_SCAN

    if args.move:
        print("Move operation: LIVE (files will be moved)")
    elif args.dry_run_move:
        print("Move operation: DRY RUN (moves will only be logged)")
    else:
        print("Move operation: disabled (all processing stays in place)")
    print(f"Scan: {'recursive (includes subfolders)' if recursive_scan else 'top-level only (ignores subfolders)'}")
    print(f"OneDrive download wait: {args.download_wait_seconds}s per cloud-only file")
    print(
        f"Image resize: LIVE, shorter side to {args.resize}px"
        if args.resize is not None
        else "Image resize: disabled (use --resize PIXELS to enable)"
    )
    if resize_in_place_only:
        print("Operation: resize images in place; videos ignored")
        print(
            f"Workers: {args.workers} "
            + ("(sequential)" if args.workers == 1 else "(bounded parallel processing)")
        )

    master_df = load_master_log()
    checkpoint_path = None
    checkpoint_rows = []
    already_processed_paths = set()
    if args.skip_processed:
        checkpoint_path = checkpoint_path_for(args)
        checkpoint_rows = load_checkpoint_rows(checkpoint_path)
        already_processed_paths = processed_path_keys(
            master_df, checkpoint_rows, args.resize
        )
        print(
            f"Resume filter: {len(already_processed_paths)} successfully processed "
            "file(s) will be skipped before opening"
        )
        if checkpoint_rows:
            print(
                f"Recovered {len(checkpoint_rows)} completed file(s) from the "
                "previous interrupted run"
            )

    error_paths = None
    if args.errors_only:
        error_paths = error_paths_from_log(master_df)
        print(f"Error retry filter: {len(error_paths)} file(s) found in the master log")
        if not error_paths:
            print(f"No errored files to process. Master log: {MASTER_LOG_PATH}")
            return
    if args.repaired_shell_quoting:
        print('Note: repaired a quoted folder path ending in "\\" so command flags were parsed correctly.')

    if not HEIF_AVAILABLE:
        if args.resize is not None:
            print("Note: pillow-heif not installed -- HEIC photos cannot be resized. "
                  "Run: pip install pillow-heif")
        else:
            print("Note: pillow-heif not installed -- HEIC photos will fall back to Date Modified "
                  "instead of Date Taken. Run: pip install pillow-heif")
    if args.resize is not None and not RAWPY_AVAILABLE:
        print("Note: rawpy not installed -- CR3 files cannot be converted. "
              "Run: python -m pip install rawpy")
    if args.resize is not None and not find_exiftool():
        print("Note: ExifTool not found on PATH -- CR3 metadata-preserving conversion is disabled.")
    if not resize_in_place_only and not ffprobe_available():
        print("Note: ffprobe (ffmpeg) not found on PATH -- videos will fall back to Date Modified "
              "instead of their embedded creation time. Install ffmpeg to enable this.")

    new_rows = []
    RUN_STATE.update(
        master_df=master_df,
        new_rows=new_rows,
        checkpoint_rows=checkpoint_rows,
        saved=False,
    )
    skipped_processed = 0
    work_queue = []
    seen_paths = set()
    for source in sources:
        if not source.exists() or not source.is_dir():
            print(f"\nSource folder not found, skipping: {source}")
            continue

        source = source.resolve()   # normalize relative paths to absolute before scanning
        print(f"\nBuilding queue: {source}")
        walker = os.walk(source) if recursive_scan else [(str(source), [], os.listdir(source))]

        queued_here = 0
        for root, _, files in walker:
            for name in files:
                fpath = Path(root) / name
                # Only media files; skips desktop.ini, sidecars, the master log,
                # resume journals and hidden temp files from interrupted resizes.
                if name.startswith(".") or fpath.suffix.lower() not in PROCESSABLE_EXTS:
                    continue
                if resize_in_place_only and fpath.suffix.lower() not in RESIZABLE_EXTS:
                    continue
                path_key = normalized_path_key(fpath)
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                if (
                    already_processed_paths
                    and path_key in already_processed_paths
                ):
                    skipped_processed += 1
                    continue
                if not is_regular_file_no_hydrate(fpath):
                    continue
                if error_paths is not None and path_key not in error_paths:
                    continue
                work_queue.append(fpath)
                queued_here += 1

        print(f"  Queued {queued_here:,} file(s)")

    progress_total = skipped_processed + len(work_queue)
    completed_indices = set()
    error_indices = set()
    print(f"\nQueue ready: {progress_total:,} file(s) in the selected folders")
    if args.skip_processed:
        print(f"Already complete: {skipped_processed:,} file(s)")
    print(f"TOTAL progress: {format_progress(skipped_processed, progress_total)}")

    def record_row(row):
        row_index = len(new_rows)
        new_rows.append(row)
        if row_completed_successfully(row, args.resize):
            completed_indices.add(row_index)
            if checkpoint_path is not None:
                append_checkpoint_row(checkpoint_path, row)
                already_processed_paths.add(normalized_path_key(row["CurrentFullPath"]))
        elif row_has_processing_error(row):
            error_indices.add(row_index)
        return row_index

    try:
        if args.workers > 1:
            worker = lambda path: process_and_move(path, args.resize, False, False)
            for fpath, future in bounded_thread_results(
                work_queue, worker, args.workers
            ):
                row = future.result()
                record_row(row)
                display_name = str(fpath)
                if len(display_name) > 80:
                    display_name = "..." + display_name[-77:]
                print(
                    "\r  TOTAL "
                    + format_progress(
                        skipped_processed + len(completed_indices),
                        progress_total,
                        len(error_indices),
                    )
                    + f" | Finished: {display_name:<80}",
                    end="",
                    flush=True,
                )
        else:
            for fpath in work_queue:
                display_name = str(fpath)
                if len(display_name) > 80:
                    display_name = "..." + display_name[-77:]
                current_progress = format_progress(
                    skipped_processed + len(completed_indices),
                    progress_total,
                    len(error_indices),
                )
                print(
                    f"\r  TOTAL {current_progress} | Current: {display_name:<80}",
                    end="",
                    flush=True,
                )
                row = process_and_move(fpath, args.resize, move_requested, args.dry_run_move)
                record_row(row)
    except KeyboardInterrupt:
        print("\nProcessing interrupted; no new files will be scheduled.")
        print(
            "TOTAL progress at interruption: "
            + format_progress(
                skipped_processed + len(completed_indices),
                progress_total,
                len(error_indices),
            )
        )
        raise

    print(
        "\rTOTAL progress after initial pass: "
        + format_progress(
            skipped_processed + len(completed_indices),
            progress_total,
            len(error_indices),
        )
        + " " * 90
    )

    print(f"\nInspected {len(new_rows):,} queued file(s) across {len(sources)} folder(s)")
    if args.skip_processed:
        print(f"Skipped {skipped_processed:,} previously processed file(s) before opening")

    skipped_indices = [i for i, r in enumerate(new_rows) if str(r["Action"]).startswith("Skipped")]

    if skipped_indices:
        print(f"\nCloud download queue: {len(skipped_indices):,} file(s)")
        print(
            "TOTAL progress: "
            + format_progress(
                skipped_processed + len(completed_indices),
                progress_total,
                len(error_indices),
            )
        )
        print(f"{len(skipped_indices):,} file(s) are cloud-only and were not processed:")
        preview = skipped_indices[:10]
        for i in preview:
            print(f"  - {new_rows[i]['FileName']}")
        if len(skipped_indices) > len(preview):
            print(f"  ... and {len(skipped_indices) - len(preview)} more")

        if args.dry_run_move:
            answer = "n"
            print("\nDry run: cloud-only files will not be downloaded.")
        elif args.download_cloud_files is None:
            answer = safe_input("\nDownload these from OneDrive now and process them? [y/N]: ").strip().lower()
        else:
            answer = "y" if args.download_cloud_files else "n"
            print(
                "\nCloud-only action supplied by argument: "
                + ("download and process (--y)" if answer == "y" else "leave skipped (--N)")
            )

        if answer == "y":
            retry_indices = skipped_indices
            retry_pass = 1
            while retry_indices:
                failed_download_indices = []
                if retry_pass > 1:
                    print(f"\nRetry pass {retry_pass}: {len(retry_indices)} failed file(s)")

                print(
                    f"Processing {len(retry_indices):,} cloud file(s) with "
                    f"{args.workers} worker(s)..."
                )
                worker = lambda index: download_and_process_file_in_place(
                    Path(new_rows[index]["CurrentFullPath"]),
                    args.download_wait_seconds,
                    args.resize,
                    args.move,
                    args.dry_run_move,
                    args.keep_pinned,
                )
                interrupted = False
                try:
                    for i, future in bounded_thread_results(
                        retry_indices, worker, args.workers
                    ):
                        file_name = new_rows[i]["FileName"]
                        try:
                            downloaded, detail, refreshed = future.result()
                        except Exception as e:
                            downloaded, detail, refreshed = False, f"worker error: {e}", None

                        completed_indices.discard(i)
                        error_indices.discard(i)
                        if downloaded and refreshed is not None:
                            new_rows[i] = refreshed
                            if row_completed_successfully(refreshed, args.resize):
                                completed_indices.add(i)
                                if checkpoint_path is not None:
                                    append_checkpoint_row(checkpoint_path, refreshed)
                                    already_processed_paths.add(
                                        normalized_path_key(refreshed["CurrentFullPath"])
                                    )
                            elif row_has_processing_error(refreshed):
                                error_indices.add(i)
                            result = (
                                refreshed.get("ResizeAction")
                                if args.resize is not None
                                else refreshed.get("Action")
                            )
                            print(f"Finished: {file_name} -> {result or 'done'}")
                        else:
                            new_rows[i]["Action"] = f"Error: download failed - {detail}"
                            error_indices.add(i)
                            failed_download_indices.append(i)
                            print(f"FAILED: {file_name} ({detail})")

                        print(
                            "  TOTAL progress: "
                            + format_progress(
                                skipped_processed + len(completed_indices),
                                progress_total,
                                len(error_indices),
                            )
                        )
                except KeyboardInterrupt:
                    interrupted = True
                    print("\nParallel processing interrupted; no new files will be scheduled.")
                    print(
                        "TOTAL progress at interruption: "
                        + format_progress(
                            skipped_processed + len(completed_indices),
                            progress_total,
                            len(error_indices),
                        )
                    )

                if interrupted:
                    failed_download_indices = []
                    break

                if not failed_download_indices:
                    break

                if args.download_cloud_files is not None:
                    print(
                        f"\n{len(failed_download_indices)} download(s) failed; "
                        "non-interactive argument mode will not retry."
                    )
                    break

                retry_answer = safe_input(
                    f"\n{len(failed_download_indices)} download(s) failed. Retry failed files? [y/N]: "
                ).strip().lower()
                if retry_answer != "y":
                    break
                retry_indices = failed_download_indices
                retry_pass += 1
        else:
            print("Leaving cloud-only files as skipped.")

    print(
        "\nFINAL TOTAL progress: "
        + format_progress(
            skipped_processed + len(completed_indices),
            progress_total,
            len(error_indices),
        )
    )

    master_df = upsert_rows(master_df, [*checkpoint_rows, *new_rows])
    save_master_log(master_df)
    RUN_STATE["saved"] = True
    if checkpoint_path is not None and checkpoint_path.exists():
        try:
            checkpoint_path.unlink()
        except OSError as e:
            print(f"Note: could not remove completed resume journal {checkpoint_path}: {e}")

    moved = sum(1 for r in new_rows if r["Action"] == "Moved")
    would_move = sum(1 for r in new_rows if str(r["Action"]).startswith("Would move"))
    already = sum(1 for r in new_rows if str(r["Action"]).startswith("Already organized"))
    skipped = sum(1 for r in new_rows if str(r["Action"]).startswith("Skipped"))
    resized = sum(1 for r in new_rows if str(r.get("ResizeAction", "")).startswith("Resized"))
    errors = sum(
        1 for r in new_rows
        if str(r["Action"]).startswith("Error")
        or str(r.get("ResizeAction", "")).startswith("Error")
    )

    print(f"Master log updated: {MASTER_LOG_PATH}")
    print(f"  Moved: {moved} | Would move (dry run): {would_move} | Already organized: {already} | "
          f"Skipped (cloud-only): {skipped} | Errors: {errors}")
    print(f"  Resized: {resized}")
    if args.dry_run_move:
        print("Re-run with --move once you've reviewed the log to actually move files.")


def main():
    try:
        run()
    finally:
        flush_log_on_abort()


if __name__ == "__main__":
    main()
