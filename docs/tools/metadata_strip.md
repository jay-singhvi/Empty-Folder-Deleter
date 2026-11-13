# Strip metadata

Removes GPS, or all metadata, from pictures and videos before you share them. It is a new tool
(Phase 7), with no legacy script. The preview says what each file would lose.

## How it works

1. **Scan** reads the tags of every file with ExifTool and writes nothing. The plan lists each file
   that has something to remove, with a reason such as `remove GPS: position, altitude (6 tags)` or
   `remove all: 42 tags incl. GPS, camera serial, owner name`. A file with nothing to remove is not
   an action; it is counted in the summary ("N had nothing to remove").
2. **Preview** shows the plan. A file whose output already exists is listed too, with the reason,
   and it is not selected.
3. **Apply** writes the clean copies. For each one, ExifTool writes a new file (`-o`, never
   `-overwrite_original`), the tool reads it back and checks it, and only then is it renamed into
   place. Each output is journaled.

## What is removed

| Option value | Removes | Keeps |
|---|---|---|
| **GPS** (default) | Every tag whose name starts with GPS, in any group (EXIF, XMP and others), and the location tags of videos (`Keys:Location`, `UserData:LocationInformation`). | Camera, dates, everything else. |
| **All** | Every EXIF, XMP and IPTC tag, maker notes, the thumbnail, PNG text chunks (Software, Comment, Author), and for videos the `ItemList`, `Keys` and `UserData` tags. | The picture or video data. By default also the orientation tag and the ICC colour profile (two options, below). Always: the file's own structure (size, codec, container dates and track details of videos). |

Text place names (an IPTC "City", say) are not GPS. Use "All" to remove those.

## File types

| Type | GPS | All |
|---|---|---|
| JPEG, PNG, WebP, HEIC, HEIF | yes | yes |
| TIFF | yes | **no**: ExifTool cannot delete the main metadata block of a TIFF. The file is skipped with a note. |
| Video: MP4, MOV, M4V, 3GP, 3G2 | yes | yes (container dates stay) |

Other types are not offered, because ExifTool cannot edit them without rewriting the data.

## Rules

- **ExifTool does the work, and it never re-encodes.** The picture or video data is copied as it
  is, so quality is unchanged. Without ExifTool, nothing is planned and the note shows the install
  command (`winget install OliverBetz.ExifTool`). A Pillow re-encode would lose quality and is not
  offered.
- **The original is never changed** (unless you turn on "Replace the originals"). The clean copy
  goes to a new folder (`stripped` by default, inside each folder you chose), or next to the
  original with a suffix (`_stripped` by default).
- **An existing file is never overwritten.** Checked at scan (the row is listed but not selected)
  and again at apply (the action fails).
- **The copy is checked before it is kept.** The tool reads the copy back: no tag of the chosen
  kind may be left, the picture must have the same size, and a video must have the same length
  (ffprobe, within a tolerance) when ffprobe is installed. A copy that fails is deleted and the
  action fails; nothing else changes.
- **Cloud-only OneDrive files are never read.** They are reported as skipped. Checked at scan and
  again at apply.
- **Junctions and symlinks are not followed,** protected folders are refused, and the output path
  is checked again at apply.
- **The output folder is never scanned,** so a second run does not strip its own output.
- **The copy keeps the original's modified time.**
- **Non-ASCII file names work.** ExifTool arguments go through a UTF-8 argument file.

## Replacing the originals (`in_place`, off by default)

For when you do not want copies. It is recoverable, in this order:

1. ExifTool writes the stripped copy to a temporary name next to the original.
2. The copy is checked (see above).
3. The original goes to the **Recycle Bin** (never a permanent delete).
4. The copy is renamed to the original's name.

If any step before 3 fails, the original is untouched and the temporary file is deleted. If the
rename in step 4 fails after the original is already in the Recycle Bin, the copy is kept, and the
action's error says where it is and what to rename it to. The plan row says "in place: the original
goes to the Recycle Bin, then the stripped copy takes its name". **Undo does not restore it**;
restore the original from the Recycle Bin by hand.

## Options

Nothing is fixed in the code. Every value below is an option with a default and a hint (title and
description) in the GUI and in `--help`.

| Option | Default | What it does |
|---|---|---|
| Folders (`roots`) | (required) | Folders to clean. A drive root or a protected folder is refused. |
| Recursive | off | Also clean files in sub-folders. |
| What to clean (`media`) | images | `images`, `videos` or `both`. |
| Picture file types (`image_extensions`) | `.jpg .jpeg .png .webp .heic .heif .tif .tiff` | Only types ExifTool can edit are accepted. |
| Video file types (`video_extensions`) | `.mp4 .mov .m4v` | `.mp4 .mov .m4v .3gp .3g2` are accepted. |
| What to remove (`scope`) | gps | `gps` or `all`. |
| Keep picture orientation (`keep_orientation`) | on | With `all`: keep the orientation tag so rotated photos stay upright. |
| Keep colour profile (`keep_color_profile`) | on | With `all`: keep the ICC profile so colours look the same. |
| Where the clean copies go (`output`) | new folder | Or `suffix`. Ignored with "Replace the originals". |
| Output folder name | `stripped` | Used by the new-folder output. This tool has its own name; it does not use the "Resized output folder name" setting. |
| File name suffix (`suffix_tag`) | `_stripped` | Added before the extension by the suffix output. No `\ / : * ? " < > \|`, and no trailing dot or space. Files already ending in it are skipped. |
| Replace the originals (`in_place`) | off | See above. Advanced. |
| Ignore minor ExifTool errors (`ignore_minor_errors`) | off | Passes ExifTool's `-m`. The copy is still checked. Advanced. |
| Files read per ExifTool call (`read_batch_size`) | 50 | 1 to 500. The preview reads this many files per ExifTool run. Advanced. |
| Video duration tolerance (`duration_tolerance_s`) | 0.5 | Seconds. A cleaned video must match the original's length within this. Advanced. |

These app-wide settings also apply (Settings page): the protected folders, the ExifTool read and
write timeouts and the `ffprobe` timeout.

## Limits in this version

- **Undo cannot reverse a strip.** The output is a new file (or, in place, the original is in the
  Recycle Bin), and undo lists it as "not reversible by undo; restore manually". No file is lost.
- **Video container dates stay,** and so do track headers: they describe the file, and ExifTool's
  "remove all" keeps them.
- **TIFF cannot lose all metadata**, only GPS (see File types).
- **Timed GPS tracks** inside some action-camera videos are not read without ExifTool's embedded
  data option, so they are neither listed nor removed.
