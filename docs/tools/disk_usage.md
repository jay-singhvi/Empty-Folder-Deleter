# Disk Usage

Finds the largest files and folders under the folders you choose, with filters for size, type and
age. You can send the files you pick to the Recycle Bin. Category: Clean up.

## How it works

1. **Scan** reads file names, sizes and dates only. It never opens a file and changes nothing on
   disk, so OneDrive files that are not downloaded are not touched.
2. **Preview** lists the largest files, each with its size, type and age, and **none of them is
   checked**. The plan notes carry the folder totals (see "Plan note format").
3. **Apply** sends the files you checked to the Recycle Bin. Each one is journaled and shows in
   History. Restore them from the Recycle Bin by hand; Various Tools cannot undo a trash.

Folders are measured but never offered for removal: a folder with contents cannot be trashed by
the engine. Open the folder, pick its files, or use Empty Folder Deleter.

## Options

Nothing is a fixed constant: every value below has a default and a hint in the app. Options marked
*advanced* are behind the "Advanced" switch.

| Option | Default | What it does |
|---|---|---|
| Folders to measure | (none) | The folders to look in. A drive root or protected folder is refused. |
| Include sub-folders | on | Look inside sub-folders. When off, only files directly in each folder are listed and no folder totals are made. |
| Largest files to list | 50 (1 to 10000) | How many of the largest files are offered for removal. |
| Largest folders to report | 25 (0 to 10000) | How many folders get a `folder-size:` note. 0 turns the folder report off. |
| Minimum file size (MB) | 0 (0 to 10,000,000) | Ignore smaller files (1 MB is 1,048,576 bytes). Empty files are always ignored. |
| File kind | any | Count only pictures, videos, music, documents, archives, installers or disk images. |
| File types | (none) | Count only these extensions, for example `.mp4`. Added to the file kind. |
| Not changed for (days) | 0 (0 to 36500) | Count only files last modified at least this many days ago. 0 means any age. |
| Changed within (days, advanced) | 0 (0 to 36500) | Count only files modified within this many days. Must be larger than "Not changed for" when both are set. |
| Never count (advanced) | (none) | Names or relative paths (patterns allowed) that are skipped, with everything inside a skipped folder. |
| Include hidden and system files (advanced) | off | Also count files and folders that Windows marks hidden or system. |

The filters choose which files are counted, for the file list **and** for every folder total. A
scan of "videos older than 365 days" therefore shows the folders that hold the most old video.
File kinds are fixed sets of extensions (pictures: jpg, png, heic, RAW formats and so on). A file
type that is in no kind is shown as "other".

## Plan note format

The first note is always a human summary. The other size notes have a fixed format, so a front end
(for example a treemap) can parse them. Sizes are plain integers in bytes. The path is last and
may contain spaces.

```
root-size: <bytes> <path>        one per scanned folder, largest first
folder-size: <bytes> <path>      the top "Largest folders to report" folders, largest first
```

Parse them with `^(folder-size|root-size): (\d+) (.+)$`, or call
`various_tools.tools.disk_usage.parse_size_note(note)`, which returns `(kind, bytes, Path)` or
`None` for any other note.

- A folder's size is the recursive total of the counted files inside it, so a parent is always at
  least as large as its children, and the same bytes appear at several levels.
- The scanned folders themselves are never in the `folder-size:` list; their totals are in
  `root-size:`.
- Ties are ordered by path, so two scans of the same tree give the same notes.
- Other notes: `skipped: cloud-only: ...` and a count of hidden items left out.

## Safety

- The scan reads no file contents and writes nothing.
- Files are only ever sent to the Recycle Bin, never permanently deleted, and never without a check
  mark.
- Cloud-only OneDrive files are not counted and not offered. They take no space on this device, and
  deleting one would also delete it from the cloud. They are reported as "skipped: cloud-only".
- Junctions and symlinks are never followed or counted, so nothing outside the chosen folder is
  measured or removed.
- Protected folders (the Windows folder, Program Files, drive roots, your profile folder) are
  refused. The list is in the app settings.
- Hidden and system files are left out unless you ask for them.

## Command line

```
various-tools disk_usage scan D:\Downloads --top-files 20 --category videos --older-than-days 180
```

Add `--json` for a machine-readable plan. Nothing is checked by default, so applying from the
command line needs a saved plan (`--save-plan plan.json`) in which the files to remove have
`"selected": true`; then run `various-tools disk_usage apply --plan plan.json`.
