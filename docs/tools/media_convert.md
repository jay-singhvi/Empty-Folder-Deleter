# Convert media

Turns HEIC pictures into JPG or PNG, WebP pictures into JPG or PNG, and MOV videos into MP4, so
that files from a phone or a web page open everywhere. It is a new tool (Phase 7), with no legacy
script. It shares its picture and video helpers with Resize media where they fit.

## How it works

1. **Scan** looks at each file and works out where the output goes. It writes nothing. Pictures are
   only opened far enough to read their size, videos are probed with ffprobe. The plan lists every
   conversion with a reason, for example `HEIC -> JPG, 4032x3024, 2.1 MB` or
   `MOV -> MP4 (remux, no re-encode), 12.4 s, 38.0 MB`.
2. **Preview** shows the plan. A file whose output already exists is listed too, with the reason,
   and it is not selected.
3. **Apply** writes the selected outputs. Each output is journaled.

## What is converted

| From | To | How |
|---|---|---|
| HEIC, HEIF | JPG or PNG (option) | Decoded with pillow-heif, turned upright, encoded with Pillow. |
| WebP (still) | JPG or PNG (option) | Decoded and encoded with Pillow. Animated WebP is left as it is. |
| MOV | MP4 | **Remux** (copy the streams, no quality loss) when MP4 can hold every stream, otherwise **re-encode** to H.264 or H.265. |

Transparency: a JPG cannot hold it, so transparent areas are painted onto a colour (white by
default, option `background_color`). A PNG output keeps the transparency.

### Videos: remux or re-encode

- **Remux** is used when every video stream is H.264, H.265 (HEVC), MPEG-4 or AV1 and every audio
  stream is AAC, MP3, AC-3, E-AC-3 or ALAC. It takes seconds and changes no pixel. An H.265 video
  gets the `hvc1` tag that Apple players need. A rotation stored in the file (phone video) is kept.
- **Re-encode** is used otherwise (ProRes, Motion JPEG, PCM sound and so on), or always when the
  option says so. Video becomes H.264 (or H.265) at the chosen CRF, in 4:2:0 8-bit so that it plays
  everywhere. Sound that MP4 can hold is copied, any other sound becomes AAC.
- Timecode and other data tracks that MP4 cannot hold are left out. Every picture and sound track
  is kept. Subtitles are not copied.

## Rules

- **An existing file is never overwritten.** Checked at scan (the row is listed but not selected)
  and again at apply (the action fails). Two files that would take the same output name are listed
  and only the first is selected.
- **Originals are kept** unless the advanced option `trash_originals` is on (see below).
- **Cloud-only OneDrive files are never read.** They are reported as skipped. This is checked at
  scan and again at apply.
- **Junctions and symlinks are not followed,** protected folders are refused, and the output path
  is checked again at apply so it cannot go through a junction or outside the chosen folders.
- **The output folder is never scanned,** so a second run does not convert its own output.
- **The output keeps the source's modified time.**
- **Missing programs disable only what needs them.** Without ffmpeg and ffprobe, MOV files are
  skipped with a note and pictures are still converted. Without ExifTool, pictures are converted
  and keep their EXIF tags and colour profile through Pillow, and the plan says that XMP and maker
  notes are lost. The notes show the install commands (`winget install Gyan.FFmpeg`,
  `winget install OliverBetz.ExifTool`).

## Recycling the originals (`trash_originals`, off by default)

When on, each original goes to the Recycle Bin after its output was written **and verified**:

- a picture is reopened from disk at its final name, decoded, and its size must match;
- a video is probed with ffprobe, and its length must be within the tolerance of the original.
  This check runs on the encoded file while it still has its temporary name, before it is renamed
  to the output name.

If verification fails, the original is not touched and the action is marked failed in History. A
picture output is kept for you to look at. A video that fails is never given the output name: only
the tool's own temporary file is removed, so no broken `photo.mp4` is left behind and the next scan
offers the conversion again (instead of saying the output already exists). The video check runs
whether or not `trash_originals` is on. If the Recycle Bin refuses, the original stays and the action
fails too (the output exists). The plan row says "original goes to the Recycle Bin after the
output is verified", and so does the journal entry. Only the Recycle Bin is used, never a
permanent delete.

## Options

Nothing is fixed in the code. Every value below is an option with a default and a hint (title and
description) in the GUI and in `--help`.

| Option | Default | What it does |
|---|---|---|
| Folders (`roots`) | (required) | Folders to convert. A drive root or a protected folder is refused. |
| Recursive | off | Also convert files in sub-folders. |
| Convert HEIC and HEIF (`convert_heic`) | on | Turn the conversion on or off. |
| HEIC becomes (`heic_target`) | jpg | `jpg` or `png`. |
| Convert WebP (`convert_webp`) | on | Turn the conversion on or off. |
| WebP becomes (`webp_target`) | jpg | `jpg` or `png`. |
| Convert MOV to MP4 (`convert_mov`) | on | Turn the conversion on or off. Needs ffmpeg and ffprobe. |
| JPEG quality | 90 | 1 to 100. |
| Background for transparent areas (`background_color`) | `#ffffff` | `#RRGGBB`, used when a transparent picture becomes a JPG. |
| Keep metadata | on | Keep EXIF, the colour profile and, for videos, the tags. The orientation is reset because the pixels are turned. With ExifTool, XMP and maker notes are carried over too. |
| Optimise JPEG and PNG (`optimize_output`) | on | A slower encode that makes the files smaller, with the same picture. |
| JPEG file extension (`jpeg_extension`) | jpg | `jpg` or `jpeg`. |
| How to convert videos (`video_mode`) | auto | `auto`, `remux` (skip what MP4 cannot hold) or `reencode`. |
| Re-encode codec (`reencode_codec`) | h264 | `h264` or `h265`. |
| Re-encode CRF (`video_crf`) | empty | 0 to 51. Empty uses 18 for H.264 and 22 for H.265. |
| Re-encode speed (`video_preset`) | medium | `ultrafast` to `veryslow`. Slower makes smaller files at the same quality. |
| AAC audio bitrate (`audio_bitrate_kbps`) | 192 | 32 to 512. Used only when sound has to be turned into AAC. |
| Make MP4 start fast (`faststart`) | on | Puts the index at the start of each MP4. |
| Video duration tolerance (`duration_tolerance_s`) | 0.5 | Seconds. A converted video counts as verified within this of the original. |
| Where the converted files go (`output`) | new folder | Or `next_to_original`: same name, new extension (`photo.heic` becomes `photo.jpg`). |
| Output folder name | `converted` | Used by the new-folder output. This tool has its own name; it does not use the "Resized output folder name" setting. |
| Move originals to the Recycle Bin (`trash_originals`) | off | See above. Advanced. |

These app-wide settings also apply (Settings page): the protected folders, the `ffprobe` timeout,
the video encode timeout and the ExifTool write timeout.

## Limits in this version

- **Undo of a conversion** sends the output to the Recycle Bin, but only when it is unchanged
  since the run (same size and modified time). An output you edited is left alone and listed for
  manual removal. An original recycled by `trash_originals` has its own History row, and undo
  puts it back from the Recycle Bin when it finds it there; otherwise it is listed to restore by
  hand. Nothing is ever deleted for good.
- **Files that are not on this device** are listed in the plan's cloud-only list, for the optional
  download action. They are never read.
- **Animated WebP and multi-image HEIC files** are left as they are.
- **10-bit and HDR video** that has to be re-encoded becomes 8-bit 4:2:0 (a remux keeps it as it
  is).
- **MOV to MP4 only.** Other video types are not converted by this tool (Resize media handles
  resizing).
- **ExifTool and file names:** all ExifTool calls pass their arguments through a UTF-8 argument
  file, so names with accents, Chinese or emoji work.
