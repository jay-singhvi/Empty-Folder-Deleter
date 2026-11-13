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

- **Files are never enlarged** (🔒 locked option `never_upscale`, on by default). A file is
  resized only when the chosen rule makes it smaller. See the options for turning it off.
- **Originals are never changed or removed.** The output goes to a new folder (`resized` by
  default, inside each folder you chose), or next to the original with a suffix (`_resized` by
  default).
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

Nothing is fixed in the code. Every value below is an option with a default and a hint (title and
description) in the GUI and in `--help`. 🔒 marks a value that was a fixed rule of the original
tools. It is an advanced option, and the GUI warns before it is changed.

| Option | Default | What it does |
|---|---|---|
| Folders | (required) | Folders to process. A drive root or a protected folder is refused. |
| Recursive | off | Also process subfolders. Off, as in `app.py`. |
| Media | Images only | Images, videos, or both. |
| Picture file types (`image_extensions`) | `.jpg .jpeg .png .bmp .gif .tiff .webp` | Picture types to resize. Only types this tool can write are accepted. |
| Video file types (`video_extensions`) | `.avi .flv .m4v .mkv .mov .mp4 .webm .wmv` | Video types to resize. The output keeps the type. A type cannot be a picture type too. |
| Mode | Fit inside a box | See the table above. |
| Size | 1080p | The preset for the box. Choose Custom to set your own width and height (100 to 16384). |
| Shorter / longer side (px) | 1080 | Used by the side modes (16 to 16384). |
| Percentage | 50 | Used by the percentage mode (10 to 400; over 100 needs the lock below off). |
| 🔒 Never enlarge (`never_upscale`) | on | Files are never made bigger. Turned off, small files are enlarged: *fit inside a box* scales the file to the box (up or down, without the "both sides larger" rule), the side modes make the side exactly the target, and a percentage over 100 is allowed. Enlarging makes bigger files and softer pictures. |
| Video quality | High (CRF 18) | Medium is CRF 23, low is CRF 28. Lower CRF means higher quality. |
| Video CRF override (`video_crf`) | empty | Your own CRF, 0 to 51. Empty uses the video quality above. |
| Video encoding speed (`video_preset`) | medium | The x264 preset, `ultrafast` to `veryslow`. Slower makes smaller files at the same quality. |
| JPEG quality | 85 | 85 to 90 is the recommended range. |
| Optimise JPEG and PNG (`optimize_output`) | on | A slower encode that makes the files smaller, with the same picture. |
| Resize filter (`resample`) | lanczos | `lanczos`, `bicubic`, `bilinear`, `box`, `hamming` or `nearest`. |
| Keep metadata | on | Keeps the EXIF block. The orientation is reset, because the pixels are already turned. |
| Video size estimate factor (`estimate_compression`) | 0.8 | Preview only: expected video size is the pixel ratio times this. |
| Motion-photo scan size (`extras_scan_kb`) | 256 | How much of the start of each JPEG is searched for a motion-photo or HDR marker. |
| Output | New folder | Or a suffix next to the original. |
| Output folder name | the `default_output_folder` setting (`resized`) | Used by the new-folder output. |
| File name suffix (`suffix_tag`) | `_resized` | Added before the extension by the suffix output. No `\ / : * ? " < > \|`, and no trailing dot or space. Files already ending in it are skipped. |
| Convert RAW (CR3) to JPEG | off | Needs ExifTool to keep the metadata. See below. |

These app-wide settings also apply (Settings page): the resized output folder name, the protected
folders, the `ffprobe` timeout, the video encode timeout and the ExifTool write timeout.

## Programs it needs

- **ffmpeg and ffprobe** for videos. Without them, videos are skipped with a note, and images are
  still processed. The note shows the install command (`winget install Gyan.FFmpeg`).
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
