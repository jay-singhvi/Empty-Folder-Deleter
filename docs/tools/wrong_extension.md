# Fix File Extensions

Finds files whose content does not match their extension, for example a PNG picture named
`photo.jpg`, and renames them to the right extension.

## How it works

1. **Scan** walks the selected folders. For each file with an extension it checks, it reads the
   first 64 bytes and compares them with known file signatures. It changes nothing on disk.
2. **Preview** lists every file that would be renamed, with the old and the new name.
3. **Apply** renames the checked files. Each rename is journaled, and you can undo it from History.

## What counts as wrong

A file is renamed only when its content is recognised and the extension does not fit it.

- Recognised: JPEG, PNG, GIF, WebP, BMP, TIFF, HEIC/HEIF, AVIF, PDF, ZIP, MP4/MOV/M4V, MKV/WebM, AVI,
  WAV, MP3, FLAC, Ogg, 7-Zip, RAR and gzip.
- Extensions that mean the same thing are left alone: `.jpg`, `.jpeg` and `.jfif`; `.tif` and
  `.tiff`; `.heic` and `.heif`; `.mp4`, `.m4v` and `.mov`; `.mkv` and `.webm`.
- ZIP-based documents (`.docx`, `.xlsx`, `.pptx`, `.odt` and similar) count as ZIP, so a real Word
  file is never "fixed" to `.zip`. A file that is not recognised is left alone.
- Files with no extension, or an extension this tool does not check (for example `.txt`, `.exe`),
  are never touched.
- When the new name is taken, the file gets a free name such as `photo_dup1.png`. Nothing is
  replaced.

## Options

| Option | Default | What it does |
|---|---|---|
| Folders to scan | (none) | The folders to look in, including the folders inside them. |
| Kinds of files to check | all | Photos, videos, audio, documents, archives. A file is checked when its current extension belongs to a chosen kind. |
| Never touch (advanced) | `.git`, `node_modules` | Names or paths that are left alone, with everything inside them. |

## Safety

- Files that are in OneDrive but not on this device (cloud-only) are never opened. They are
  skipped and the preview says "skipped: cloud-only". Download them first if you want them checked.
- The folder you select is never renamed, and junctions and symlinks are never followed.
- Renames never replace another file, and undo from History restores the old names.

## Command line

```
various-tools wrong_extension scan D:\Photos
various-tools wrong_extension apply D:\Photos --yes
```
