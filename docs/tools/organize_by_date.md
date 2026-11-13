# Organize photos and videos by date

Moves photos and videos into `<destination>\<year>\<year> - <month>\` folders, by when they were
taken. It replaces `python-extract_file_metadata-8.py`, without its Excel master log and its
resize and CR3 options (those are covered by [Resize media](media_resize.md)).

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
| EXIF capture time (Date Taken) | Photos (JPEG, HEIC, PNG, TIFF, and so on) | Date Taken (EXIF) |
| Creation time in the video | Videos (MP4, MOV, and so on), read with ffprobe | Date Taken (video metadata) |
| ExifTool capture time | RAW files (CR3) only | Date Taken (ExifTool) |
| Modified time | Any file, as the last resort | Date Modified (fallback) |

If none of them gives a plausible date, the file goes to the **Unknown Date** folder.

## Rules that always hold

- **Cloud-only OneDrive files are never read and never moved.** They are listed as "cloud-only,
  skipped and not read", and nothing is downloaded. Their date is not guessed from the modified
  time, because reading it would mean downloading the file.
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
| Date sources, in order (advanced) | EXIF, video, ExifTool, modified | The order to try. Remove "modified" and undated files go to the unknown folder. |
| Earliest believable year (advanced) | the Settings value (1990) | Dates before this are treated as wrong (for example a camera clock reset to 1970). 1990 is a hand-tuned default: change it only if you know why. |
| Worker threads (advanced) | the Settings value (CPU cores) | How many files are read at once while scanning. 1 to 256. |
| Skip already processed files | off | Skips files an earlier run already moved, before reading them. |
| Retry errors only | off | Scans only the files whose last run failed. |
| Catalog file (advanced) | `catalog.sqlite` in the app data folder (Settings) | The SQLite catalog. |

The ffprobe and ExifTool timeouts come from the app settings. The date rules (capture date first,
modified time only when plausible, cloud-only files never read or moved) are fixed and not options.

## The media catalog

The catalog replaces the Excel master log. It has one row per file: its path, capture date, where
the date came from, camera make and model, size, dimensions, GPS, sync status, destination, and
its status (`planned`, `moved`, `organized`, `skipped` or `error`). It is a SQLite file, so it can
be written safely from several workers. A CSV export exists as a library function
(`Catalog.export_csv`) only; the tool does not expose it yet, and a button or option for it is
planned for a later phase.

Excel export and importing the old `Photo_Metadata_Master.xlsx` are not in this version.

## Programs it needs

- **ffprobe** (part of ffmpeg) for the creation time of videos. Without it, videos use their
  modified time. Install with `winget install Gyan.FFmpeg`.
- **ExifTool** for the capture time of RAW (CR3) files. Without it, RAW files use their modified
  time. Install with `winget install OliverBetz.ExifTool`.

## Not in this version

- **Downloading cloud-only files** from the scan. Every file that is not on the device is skipped.
  The download needs its own journaled action, which the engine does not have yet.
- **Resizing while organizing** (the legacy `--resize` option). Use [Resize media](media_resize.md)
  after organizing, or wait for a later release. Resizing in place would overwrite the originals.
- **Keeping the existing Excel master log**, and importing it.
