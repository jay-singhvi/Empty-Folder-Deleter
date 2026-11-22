# Organize photos and videos by date

Moves photos and videos into `<destination>\<year>\<year> - <month>\` folders, by when they were
taken. It replaces `python-extract_file_metadata-8.py`. Its Excel master log becomes a SQLite
catalog, which can be exported to Excel or CSV, and the old log can be imported once. Its
`--resize` option is **Also resize pictures**; CR3 conversion is in [Resize media](media_resize.md).

## How it works

1. **Scan** works out the date of each file and the folder it would go to. It writes nothing to
   your photos. The only thing it writes is the media catalog (a SQLite file in your user data
   folder), which records what it read.
2. **Preview** lists every move, with the reason: which date was used and where the file goes.
3. **Apply** moves the selected files. Each move is journaled, so it can be undone (the History
   page arrives in Phase 5).

## Where the date comes from

The sources are tried in order. A source is used only when its date is plausible (not before the
minimum year, and not in the far future):

| Step | Used for | Label in the plan |
|---|---|---|
| The date in the file name (*Use the date in the file name first*, on by default since 0.9) | Camera and WhatsApp names, see below | Date in the file name |
| EXIF capture time (Date Taken) | Photos (JPEG, HEIC, PNG, TIFF, and so on) | Date Taken (EXIF) |
| Creation time in the video | Videos (MP4, MOV, and so on), read with ffprobe | Date Taken (video metadata) |
| ExifTool capture time | RAW files (CR3) only | Date Taken (ExifTool) |
| Modified time | Any file, as the last resort | Date Modified (fallback) |

If none of them gives a plausible date, the file goes to the **Unknown Date** folder.

### Dates in file names

Editing, re-saving, converting or exporting a photo or video can change its embedded date (a
video edited in 2025 may say "created March 2025"), and copying it changes its modified time. The
name the camera or WhatsApp gave it keeps the day it was taken, so the name is tried first.
Recognised names include `20220624_005115.mp4` (Samsung), `IMG_20220624_005115.jpg`,
`VID_20220624_005115.mp4`, `PXL_20220624_005115123.jpg` (Pixel), `DJI_20220624005115_0001_D.JPG`
(DJI), `Screenshot_20220624-005115.png`, `IMG-20220624-WA0001.jpg` and `VID-20220624-WA0001.mp4`
(WhatsApp), `WhatsApp Image 2022-06-24 at 00.51.15.jpeg`, `photo_2022-06-24_00-51-15.jpg`
(Telegram) and `2022-06-24 00.51.15.jpg`. The date must be a real calendar date between the
earliest believable year and today. Names without a date, such as iPhone's `IMG_1234.HEIC` or
older DJI's `DJI_0001.JPG`, use the metadata as before. Turn the option off to go by the metadata
only.

To put right files an earlier version filed by the wrong date, scan the destination folder with
*Include subfolders* on: a file whose name gives another month is listed to move. *Skip already
processed files* does not skip such a file.

## Rules that always hold

- **Cloud-only OneDrive files are never read during a scan.** By default (*Cloud-only files:
  skip*) they are listed as "cloud-only, skipped and not read", nothing is downloaded, and they
  are not moved. Their date is not guessed from the modified time, because reading it would mean
  downloading the file. See [Cloud-only files](#cloud-only-files) for the download choices.
- **A file that is already in its place is not moved.**
- **Only files on this device are moved.** The source folders are never changed or deleted, and
  junctions and symlinks are not followed.
- **Protected folders are refused** as the destination (for example the Windows folder).
- **Name clashes are numbered.** If `IMG.jpg` is already in the destination, the new file becomes
  `IMG_dup1.jpg`. An existing file is never replaced.
- Dot-files and files that are not photos or videos are ignored.

## Options

Nothing is a fixed constant: every value below has a default and a hint in the app. Options marked
*advanced* are behind the "Advanced" switch. An option left empty uses the matching app setting.

| Option | Default | What it does |
|---|---|---|
| Folders to organize | (required) | Folders to organize. |
| Include subfolders | off | Also organize subfolders. Off, as in the legacy script. |
| Skip dot-files (advanced) | on | Ignore files whose name starts with a dot. |
| Photo file types (advanced) | jpg, jpeg, png, heic, heif, tif, tiff, bmp, webp | Extensions read for their EXIF date. Typed with or without the dot, stored in lower case. |
| RAW file types (advanced) | cr3 | Extensions of RAW photos, dated with ExifTool. |
| Video file types (advanced) | mp4, mov, m4v, 3gp, avi, mkv | Extensions dated with ffprobe. |
| Destination folder | OneDrive folder from Settings + the sub-path below | Where the year folders are created. |
| Default destination sub-path (advanced) | `Pictures/Camera Roll` | Used only when no destination is chosen. A relative path inside OneDrive, without `..`. |
| Folder template | `{year}/{year} - {month:02d}` | Only `{year}` and `{month}` may be used. The template must stay inside the destination. |
| Unknown-date folder name | the Settings value (`Unknown Date`) | Holds files with no usable date. |
| Name clash suffix (advanced) | `_dup` | Added, with a number, to a new file whose name is taken (`IMG_dup1.jpg`). Existing files are never replaced. |
| Use the date in the file name first | on | Tries the camera or WhatsApp date in the name before the sources below. |
| Date sources, in order (advanced) | EXIF, video, ExifTool, modified | The order to try. Remove "modified" and undated files go to the unknown folder. |
| Earliest believable year (advanced) | the Settings value (1990) | Dates before this are treated as wrong (for example a camera clock reset to 1970). 1990 is a hand-tuned default: change it only if you know why. |
| Worker threads (advanced) | the Settings value (CPU cores) | How many files are read at once while scanning. 1 to 256. |
| Skip already processed files | off | Skips files an earlier run already moved, before reading them. |
| Retry errors only | off | Scans only the files whose last run failed. |
| Catalog file (advanced) | `catalog.sqlite` in the app data folder (Settings) | The SQLite catalog. The export and import buttons always use the default file. |
| 🔒 Cloud-only files (`cloud_only`, advanced) | skip | `skip`, `ask` or `download`. See below. |
| Download wait (`download_wait_seconds`, advanced) | 30 | Seconds to wait for OneDrive to download one file (1 to 3600). |
| Keep downloaded files on this device (`keep_pinned`, advanced) | off | Off: the "always keep on this device" mark that a download sets is removed after the move, as the legacy script did. |
| Also resize pictures (`also_resize`, advanced) | off | Resize each picture in place before it is moved. See below. |
| Side to resize (`resize_mode`, advanced) | shorter side | Shorter side (the legacy rule) or longer side. |
| Side length (`resize_px`, advanced) | 1080 | Pixels (16 to 16384), as in Resize media. |
| JPEG quality (`resize_jpeg_quality`, advanced) | 85 | As in Resize media (the legacy script used 95). |

The ffprobe and ExifTool timeouts come from the app settings. The date rules (capture date first,
modified time only when plausible, cloud-only files never read during a scan) are fixed and not
options.

## Cloud-only files

| Choice | What happens |
|---|---|
| `skip` (default) | Not read, not downloaded, not moved. Listed as skipped, and in the plan's cloud-only list. |
| `ask` | Each file is a row, **not ticked**, that says it will be downloaded first. Ticking a row is your consent: Apply downloads that file only. |
| `download` | The same rows, already ticked. The option is 🔒 locked, because Apply then downloads every one of them. |

Nothing is downloaded during the scan. At Apply, for a ticked row only, the file is downloaded
(waiting up to *Download wait*), then its date and folder are worked out exactly as the scan
does, and it is moved without replacing anything. The move is journaled as its own row with the
real destination, so undo moves it back (the file stays downloaded). Unless *Keep downloaded
files on this device* is on, the pin the download set is removed again afterwards. A download that
fails or times out is a failed row with the reason, and the file stays where it is.

## Also resize pictures

The legacy `--resize` overwrote each picture. Here each JPEG, PNG, BMP, GIF, TIFF or WebP is
resized **in place, before it is moved**, with Resize media's pipeline: the resized copy is written
next to the original and checked, the original goes to the **Recycle Bin** as its own journaled
row, and the copy takes its name. Undo moves the file back and restores the original from the
Recycle Bin. Pictures are never enlarged (🔒), and motion photos, HDR JPEGs and animated images
are left as they are. Videos, RAW (CR3), HEIC and downloaded files are only moved. Unticking a
file's move also drops its resize.

The resize runs before the move, at the source, because the original must go to the Recycle Bin
from inside a selected folder (rule 4), and because a picture is then never resized at a
destination whose name changed at the last moment.

## The media catalog

The catalog replaces the Excel master log. It has one row per file: its path, capture date, where
the date came from, camera make and model, size, dimensions, GPS, sync status, destination, and
its status (`planned`, `moved`, `organized`, `skipped`, `error` or `imported`). It is a SQLite
file, so it can be written safely from several workers. A problem writing it never stops a run:
it is noted in the plan and logged.

Two buttons (and CLI subcommands) work on it:

- **Export catalog…** (`catalog-export`) writes the catalog to a new `.xlsx` (sheet `Catalog`)
  or `.csv` file. An existing file is never replaced. Text is always stored as text, so a file
  name starting with `=` never becomes a formula.
- **Import old master log…** (`catalog-import`) reads the `Metadata` sheet of the old
  `Photo_Metadata_Master.xlsx` once, using its column names (`CurrentFullPath`, `FileName`,
  `Action`, `DateTaken`, and so on). Paths the catalog already knows keep their catalog row,
  which is newer. Rows without a path are skipped. A workbook without that sheet and those columns
  is refused, and a cloud-only workbook is not read. Imported rows have no recorded modified time,
  so the next scan reads each file once instead of skipping it.

## Programs it needs

- **ffprobe** (part of ffmpeg) for the creation time of videos. Without it, videos use their
  modified time. Install with `winget install Gyan.FFmpeg`.
- **ExifTool** for the capture time of RAW (CR3) files. Without it, RAW files use their modified
  time. Install with `winget install OliverBetz.ExifTool`.

## Not in this version

- **CR3 conversion while organizing.** The legacy `--resize` also turned CR3 files into JPEGs.
  RAW files are only moved here; use [Resize media](media_resize.md) for the conversion.
- **Writing back to the Excel master log.** The catalog is the log now; export it when you need a
  workbook.
