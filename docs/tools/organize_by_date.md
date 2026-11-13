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

| Option | Default | What it does |
|---|---|---|
| Folders | (required) | Folders to organize. |
| Recursive | off | Also organize subfolders. Off, as in the legacy script. |
| Destination | `OneDrive\Pictures\Camera Roll` | Where the year folders are created. |
| Folder template | `{year}/{year} - {month:02d}` | Only `{year}` and `{month}` may be used. The template must stay inside the destination. |
| Unknown-date folder | `Unknown Date` | Holds files with no usable date. |
| Date sources (advanced) | EXIF, video, ExifTool, modified | The order to try. Remove "modified" and undated files go to the unknown folder. |
| Minimum plausible year (advanced) | 1990 | Dates before this are treated as wrong (for example a camera clock reset to 1970). |
| Workers (advanced) | 1 | How many files are read at once while scanning. |
| Skip already processed | off | Skips files an earlier run already moved, before reading them. |
| Retry errors only | off | Scans only the files whose last run failed. |
| Catalog file (advanced) | in your user data folder | The SQLite catalog. |

## The media catalog

The catalog replaces the Excel master log. It has one row per file: its path, capture date, where
the date came from, camera make and model, size, dimensions, GPS, sync status, destination, and
its status (`planned`, `moved`, `organized`, `skipped` or `error`). It is a SQLite file, so it can
be written safely from several workers. It can be exported to CSV.

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
