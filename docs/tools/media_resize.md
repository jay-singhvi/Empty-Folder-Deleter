# Resize media

Reduces the resolution of images and videos, for example to 1080p, so that they take less space.
It replaces `app.py` (the resolution reducer) and the resize and CR3 conversion in
`python-extract_file_metadata-8.py`.

## How it works

1. **Scan** looks at each file and works out its new size and where the output goes. It writes
   nothing. The plan lists every file, its size before and after, and the total space saved.
2. **Preview** shows the plan. A file whose output already exists is listed too, with the reason,
   and it is not selected.
3. **Apply** writes the selected outputs. Each output is journaled.

## Rules

- **Files are never enlarged.** A file is resized only when the chosen rule makes it smaller.
- **Originals are never changed or removed.** The output goes to a new folder (`resized` by
  default, inside each folder you chose), or next to the original with a `_resized` suffix.
- **An existing file is never overwritten.** If the output name is taken, the file is listed and
  left alone.
- **Cloud-only OneDrive files are never read.** They are reported as skipped.
- **Junctions and symlinks are not followed,** and protected folders are refused.
- **The output folder is never scanned,** so a second run does not resize its own output.

## Sizes

| Mode | What it does | Rule it follows |
|---|---|---|
| Fit inside a box | Fits the file inside a size box (a preset or custom). A file is resized only when **both** sides are larger than the box. | `app.py` |
| Shorter side | Sets the shorter side to a number of pixels. | `metadata-8` |
| Longer side | Sets the longer side to a number of pixels. | Same formula as shorter side |
| Percentage | Scales each side by a percentage (10 to 100). | `app.py` |

Presets for the box: 360p (640×360), 480p (854×480), 720p (1280×720), 1080p (1920×1080),
2K (2560×1440), 4K (3840×2160). Videos are made an even size, as `app.py` does.

## Options

| Option | Default | What it does |
|---|---|---|
| Folders | (required) | Folders to process. A drive root or a protected folder is refused. |
| Recursive | off | Also process subfolders. Off, as in `app.py`. |
| Media | Images only | Images, videos, or both. |
| Mode | Fit inside a box | See the table above. |
| Size | 1080p | The preset for the box. Choose Custom to set your own width and height. |
| Shorter / longer side (px) | 1080 | Used by the side modes. |
| Percentage | 50 | Used by the percentage mode. |
| Video quality | High (CRF 18) | Medium is CRF 23, low is CRF 28. Lower CRF means higher quality. |
| JPEG quality | 85 | 85 to 90 is the recommended range. |
| Keep metadata | on | Keeps the EXIF block. The orientation is reset, because the pixels are already turned. |
| Output | New folder | Or a `_resized` suffix next to the original. |
| Output folder name | `resized` | Used by the new-folder output. |
| Convert RAW (CR3) to JPEG | off | Needs ExifTool to keep the metadata. See below. |

## Programs it needs

- **ffmpeg and ffprobe** for videos. Without them, videos are skipped with a note, and images are
  still processed. Install with `winget install Gyan.FFmpeg`.
- **ExifTool** only for RAW conversion, to copy the metadata. Install with
  `winget install OliverBetz.ExifTool`.

## Limits in this version

- **Overwriting the originals is not offered.** The undo log cannot restore a replaced file, so
  the originals are always kept.
- **RAW originals are kept.** Metadata-8 removed each CR3 after converting it. Removing originals
  needs the same undo decision as the Duplicate Finder's trash action, so it is left for the owner.
- **Undo cannot reverse a resize.** The output is a new file, and undo lists it as "not reversible
  by undo; restore manually" instead of deleting it. No file is lost.
- **Animated images** (animated GIF or WebP) and **motion photos or HDR JPEGs** are left as they
  are, because resizing them would drop frames or embedded data.
- **HEIC and HEIF** are not resized in this version (the `app.py` list does not include them).
- **Rotated videos** are resized to the size the viewer shows, and this is tested with a 90 degree
  phone video.
