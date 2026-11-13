# Corrupt media finder

Lists pictures and videos that fail to open or decode, so that a broken file does not sit
unnoticed in a library or a backup. Corrupt files can be moved to a quarantine folder or sent to
the Recycle Bin, but only the ones you tick.

## How it works

1. **Scan** checks every picture and video in the chosen folders. It changes nothing. The plan has
   one action per corrupt file, and the reason says what is wrong with it.
2. **Preview** lists the corrupt files. **Every row starts unticked**, so applying right away does
   nothing. Tick the files you want to deal with.
3. **Apply** moves the ticked files to the quarantine folder (or sends them to the Recycle Bin).
   Each action is journaled.

## What counts as corrupt

| File | Corrupt when | Reason shown |
|---|---|---|
| Any | The file is zero bytes. | `empty file (0 bytes)` |
| Picture | Pillow cannot identify the format. | `not a readable picture (cannot identify the image format)` |
| Picture | Pillow cannot open or `verify()` it, or (depth *standard* and *deep*) cannot decode every pixel with `load()`. A truncated JPEG fails here. | `picture does not decode: <error>` |
| Picture (RAW, optional) | The RAW decoder cannot decode it. | `picture does not decode: <error>` |
| Video | ffprobe cannot read the file. | `ffprobe cannot read the video: <error>` |
| Video | ffprobe finds no video stream (for example an audio-only file named `.mp4`). | `the file has no video stream` |
| Video | The duration is zero, or missing when *Videos need a duration* is on. | `the video has zero duration` / `the video reports no duration` |
| Video (depth *deep*) | ffmpeg reports an error while decoding the whole file. | `the video does not decode cleanly: <error>` |

Pictures: JPEG, PNG, HEIC and HEIF (through pillow-heif), TIFF, BMP, WebP and GIF. Pillow's
`LOAD_TRUNCATED_IMAGES` switch is forced off while the tool runs, whatever another part of the
program set it to, and put back afterwards, so a cut-off picture is always corrupt. GIF and other
multi-frame pictures are decoded up to their first frame.

## Options

Nothing is a fixed constant: every value below has a default and a hint in the app. Options
marked *advanced* are behind the "Advanced" switch.

| Option | Default | What it does |
|---|---|---|
| Folders to check (`roots`) | (required) | A drive root or a protected folder is refused. Folders inside another chosen folder are scanned once. |
| Include sub-folders (`recursive`) | on | |
| What to check (`media`) | `both` | `images`, `videos` or `both`. |
| Picture file types (`image_extensions`, advanced) | `.bmp .gif .heic .heif .jpeg .jpg .png .tif .tiff .webp` | Lower-case with a leading dot. |
| Video file types (`video_extensions`, advanced) | `.3gp .avi .flv .m4v .mkv .mov .mp4 .mpeg .mpg .webm .wmv` | A type cannot be a picture type too. |
| Also check RAW photos (`include_raw`) | off | Fully decodes `.cr3` files, which is slow. |
| Never check (`exclude_globs`, advanced) | empty | Matching files and folders are skipped. |
| Include hidden files (`include_hidden`, advanced) | off | Hidden or system files and folders, and names that start with a dot, are skipped unless this is on. |
| How thoroughly to check (`check_depth`) | `standard` | `quick`: pictures are opened and verified (this does not find a truncated JPEG), videos are read with ffprobe. `standard`: pictures are decoded completely. `deep`: videos are also decoded end to end with ffmpeg, which can take as long as playing them. |
| Videos need a duration (`require_duration`, advanced) | on | Off for valid recordings that carry no duration (some `.webm` and `.mkv` files). |
| Deep decode timeout (`decode_timeout_s`, advanced) | empty: the `transcode_timeout_s` setting (3600 s) | A video that takes longer is skipped with a note, not called corrupt. |
| Files checked at once (`workers`, advanced) | empty: the `workers` setting | |
| What to do with corrupt files (`action`) | `quarantine` | `quarantine` moves files, `trash` sends them to the Recycle Bin. |
| Quarantine folder name (`quarantine_folder`) | `_quarantine` | A plain name, not a path. The folder sits inside each chosen folder. |

The `ffprobe` timeout setting (`ffprobe_timeout_s`) and the protected-folder settings also apply.

## Quarantine and undo

A quarantined file keeps its relative path: `D:\Photos\2024\IMG_1.jpg` goes to
`D:\Photos\_quarantine\2024\IMG_1.jpg`. If the name is already taken there, the file gets a
`_dup1` style suffix. The quarantine folder is **never scanned**, at any depth, so a second run
does not re-check quarantined files. **Undo from History moves the files back.**

A trashed file goes to the Recycle Bin. Various Tools cannot restore it, so restore it from the
Recycle Bin by hand.

## Safety

- **Nothing is ticked by default**, and a scan changes nothing.
- **Cloud-only OneDrive files are never opened.** They are reported as skipped.
- **A file that could not be checked is never offered for removal.** A timeout, a permission
  error, a picture over Pillow's size limit or a missing ffprobe only produces a note.
- **Junctions and symlinks are never followed.** Quarantine moves are refused when the quarantine
  folder is, or sits behind, a junction. The chosen folders themselves are never moved.
- **Protected folders are refused.**

## Programs it needs

**ffmpeg and ffprobe** for videos. Without ffprobe, videos are skipped with one note that shows
the install command (`winget install Gyan.FFmpeg`); only zero-byte videos can still be found. The
deep check also needs ffmpeg. Pictures are checked either way.

## Notes in the plan

The first note is the summary: `Checked <n> file(s) (<depth>): <n> corrupt, <n> skipped
(cloud-only), <n> could not be checked.`

| Note | Meaning |
|---|---|
| `skipped: cloud-only: <path>` | The file is cloud-only and was not opened. |
| `could not check: <path> (<reason>)` | The check could not be made (timeout, permission error, picture too large, program failed to run). The file is not offered for removal. |
| `Videos are not checked: ffprobe is needed. Install it with ...` | ffprobe is missing. |
| `The deep video check needs ffmpeg, so videos were only read with ffprobe. ...` | ffmpeg is missing. |

Paths are relative to the chosen folder, with `/`.

## Command line

```
various-tools corrupt_media scan D:\Photos --save-plan broken.json
```

Every row is unticked, so an `apply` of the saved plan does nothing until you open the file and
set `"selected": true` on the actions you want, then run
`various-tools corrupt_media apply --plan broken.json`.
