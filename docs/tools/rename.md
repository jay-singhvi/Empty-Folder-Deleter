# Rename

Gives a batch of files one consistent name pattern, for example `Snapchat-20240101-12_34-001.jpg`.
It replaces the original `rename_snapchat.py` script, and the Snapchat behavior is a built-in
preset that gives the same names.

## How it works

1. **Scan** finds the files whose names match, dates each one, numbers them and works out the new
   names. It changes nothing.
2. **Preview** lists every old → new name. A file whose new name is already taken is listed too,
   with the reason, and it is not selected.
3. **Apply** renames the selected files. Each rename is journaled, so it can be undone from History.

## Presets

| Preset | Matches | New name | Date from |
|---|---|---|---|
| Snapchat export | `Snapchat-<digits>.jpg/jpeg/png/mp4/mov/mkv` | `Snapchat-YYYYMMDD-HH_MM-NNN.ext` | modified time, numbered per minute |
| WhatsApp image | `IMG-YYYYMMDD-WA<digits>.jpg/jpeg/png/webp` | `WhatsApp-YYYYMMDD-NNN.ext` | the date in the name, numbered per day |
| Screenshot | `Screenshot_YYYYMMDD_HHMMSS.png/jpg/jpeg/mp4` | `Screenshot-YYYYMMDD-HH_MM-NNN.ext` | the date and time in the name, numbered per minute |

Any option you set overrides the preset. Choose **Custom** to write your own match and template.

## Options

Nothing is a fixed constant: every value below has a default and a hint in the app. Options marked
*advanced* are behind the "Advanced" switch. The built-in presets above are the ready-made choices.

| Option | Default | What it does |
|---|---|---|
| Folders | (required) | Folders whose files are renamed. A drive root or a protected folder is refused. |
| Include subfolders (advanced) | off | Also renames files in subfolders. Off, as in the original script. |
| Preset | Custom | A starting point for the match, template, date source, counter and case. |
| Match | (from preset) | Which file names to rename: a glob such as `IMG_*.jpg`, or a regular expression when the match mode is regex. Matching ignores letter case. |
| Match mode (advanced) | glob, or the preset's | `glob` or `regex`. |
| Name template | (from preset) | The new name. Placeholders: `{date:%Y%m%d}`, `{time:%H_%M}` (needs a time of day), `{counter:03}`, `{name}` (the original name without its extension) and `{ext}` (the extension, required so the file keeps its type). |
| Date source | modified time | `capture`: EXIF, then ExifTool, then ffprobe for videos. `modified` and `created` come from the file system. `filename`: a date in the name. |
| Counter restarts | per minute | The number restarts for each minute, each day, or runs across the whole batch. |
| Extension case (advanced) | keep | The letter case of the extension: keep, lower or upper. The rest of the name is never changed. |
| Earliest believable year (advanced) | the app setting (1990) | Capture dates and dates in names before this year, or in the future, are treated as wrong. 1990 is a hand-tuned default. 1900 to 9999. |
| Filename date pattern (advanced) | `YYYYMMDD` with an optional `HHMMSS` | The regular expression that finds a date in a name when the date source is `filename`. It must compile and have 3 groups (year, month, day) or 6 (and hour, minute, second). |

The ExifTool and ffprobe timeouts used for the `capture` date source come from the app settings.

## Safety

- The preview changes nothing on disk.
- A name that is taken, by a file on disk or by another file in the batch, is never overwritten.
  Those rows are listed and not selected. Apply checks again before it renames anything.
- OneDrive cloud-only files are never read. A capture date is not taken from them, and they are
  listed as skipped. Renaming by modified or created time changes only the name.
- Junctions and symlinks are not followed, and the selected folder itself is never renamed.
- Every rename is journaled and can be undone from History.

## Command line

Not available yet. The command line arrives in Phase 4.
