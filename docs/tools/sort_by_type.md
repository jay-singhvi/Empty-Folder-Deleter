# Sort by Type

Moves files into sub-folders chosen by simple name rules, such as `*.pdf` into `Documents/PDF`.
Category: Organize.

## How it works

1. **Scan** reads file names and attributes only. It never opens a file and changes nothing on
   disk. The first rule that matches a file name decides where the file goes.
2. **Preview** lists every move with its rule and its destination, and the notes say what was left
   alone and why. Uncheck any move you do not want.
3. **Apply** moves the checked files. Each move is journaled, so **Undo** in History puts every
   file back where it was (the new, now empty, folders stay).

## Rules

A rule is one line: `PATTERN=FOLDER`.

```
*.pdf=Documents/PDF
*.jpg|*.jpeg|*.png=Pictures
```

- **Pattern:** matches the file *name* (not its path), without regard to letter case. `*` and `?`
  work, and `[abc]`. Join several patterns with `|` to share one folder.
- **First match wins.** Put specific rules before general ones: `scan-*=Scans` before
  `*.pdf=Documents/PDF`.
- **Folder:** a relative path inside the folder being sorted. Never absolute, no drive letter, no
  `..` or `.`, no `< > : " | ? *`, no name ending in a dot, no Windows device names (`CON`,
  `NUL`, `COM1` and so on). Use `/` or `\` between folders. A bad rule is refused with a message
  naming it.
- **A rule splits at its first `=` only**, so a folder may hold a comma:
  `*.pdf=Letters/Smith, John` is one rule.
- In the app the rules are a two-column table (Pattern(s), Folder). The field carries the hints
  `widget: "pairs"`, `pair_separator: "="` and `pair_headers: ["Pattern(s)", "Folder"]`; each row
  is stored as `<patterns>=<folder>`.
- On the command line, give the rules as `--rules "*.pdf=Docs,*.jpg|*.png=Pictures"`. Rules there
  are separated by commas, and the tool puts back a rule that a comma split: a piece without `=`
  that looks like a pattern (has `*`, `?` or `[`) joins the next rule as another pattern
  (`*.jpg,*.png=Pictures` becomes `*.jpg|*.png=Pictures`); any other piece joins the previous
  rule's folder as `, <piece>` (`*.pdf=Smith, John` stays one rule). Text given as one string is
  read one rule per line.

Default rules:

| Pattern | Folder |
|---|---|
| `*.pdf` | `Documents/PDF` |
| `*.doc` `*.docx` `*.odt` `*.txt` `*.rtf` `*.md` | `Documents` |
| `*.xls` `*.xlsx` `*.ods` `*.csv` | `Spreadsheets` |
| `*.ppt` `*.pptx` `*.odp` `*.key` | `Presentations` |
| `*.cr2` `*.cr3` `*.nef` `*.arw` `*.dng` `*.raf` `*.orf` `*.rw2` | `Pictures/RAW` |
| `*.jpg` `*.jpeg` `*.png` `*.gif` `*.webp` `*.bmp` `*.heic` `*.heif` `*.tif` `*.tiff` `*.svg` | `Pictures` |
| `*.mp4` `*.mov` `*.mkv` `*.avi` `*.wmv` `*.m4v` `*.webm` `*.mpg` `*.mpeg` | `Videos` |
| `*.mp3` `*.wav` `*.flac` `*.m4a` `*.aac` `*.ogg` `*.wma` `*.opus` | `Music` |
| `*.zip` `*.7z` `*.rar` `*.tar` `*.gz` `*.bz2` `*.xz` | `Archives` |
| `*.exe` `*.msi` `*.msix` `*.appx` | `Installers` |
| `*.iso` `*.img` | `Disk images` |

## Options

Nothing is a fixed constant: every value below has a default and a hint in the app. Options marked
*advanced* are behind the "Advanced" switch.

| Option | Default | What it does |
|---|---|---|
| Folders to sort | (none) | The folders whose files are sorted. Sub-folders are created inside each one. |
| Include sub-folders | off | Also sort files in sub-folders. Destination folders are never entered. |
| Sorting rules | the table above | The rules, one per line. |
| Files that match no rule | `leave` | `leave` keeps them where they are; `other` moves them to the folder below. |
| Folder for unmatched files (advanced) | `Other` | Used when the option above is `other`. A relative folder path. |
| Never move (advanced) | `.git`, `node_modules`, `*.crdownload`, `*.part`, `*.partial`, `*.tmp` | File or folder names or relative paths (patterns allowed) that are left alone. A skipped folder is skipped with everything in it. |
| Leave recent files (days, advanced) | 0 (0 to 36500) | Files changed within this many days are left alone. 0 sorts any age. |
| Skip hidden and system files (advanced) | on | Files and folders marked hidden or system are left alone. |
| Skip files that start with a dot (advanced) | on | Names such as `.gitignore` are left alone. |
| When the name is taken | `rename` | `rename` gives the moved file a number; `skip` leaves it where it is. |
| Name clash suffix (advanced) | `_dup` | The text before the number: `report_dup1.pdf`. |

Protected folders (the Windows folder, Program Files, drive roots, your profile folder) come from
the app settings, where the list can be changed.

## Name clashes

A name is "taken" when a file of that name (compared without regard to letter case) is already in
the destination folder, or another file in the same plan is going there. The scan settles every
clash up front, so the preview shows the real names. Existing files are never replaced.

If a name is taken only after the scan (a saved plan applied later, or another program), apply
checks every destination again before any move and follows the same options: with `rename` the
file gets the next free name with **your** clash suffix (never a name another move in the plan is
going to), and the journal records the name it really got; with `skip` it stays where it is and
is journaled as **skipped** ("the name is taken ... since the scan"). The engine's own `_dup`
fallback only covers a name taken in the moment between that check and the move.

## Sub-folders and destination folders

With sub-folders on, every destination folder named by the rules (and the "unmatched" folder) is
never entered. A file that is already sorted stays put, files inside a destination folder are not
re-sorted whatever their type, and a second run finds nothing to do.

## Safety

- The scan reads no file contents and writes nothing.
- Destinations are always inside the selected folder, and the selected folder is never moved.
- A destination that is a junction or symlink is never written through. The files are left in
  place, with a note. The same check runs again when you apply.
- A file or folder where a destination folder would go (for example a file named `Documents`)
  stops the files that need that folder, with a note.
- Files are never replaced, overwritten or deleted.
- **Cloud-only OneDrive files are skipped, not moved.** A move on one drive is a rename that reads
  nothing, but a move that fails falls back to copy-and-delete, which would download the file. The
  safer choice is to leave it, and the plan says "skipped: cloud-only".
- Junctions and symlinks inside the folder are never followed or moved. OneDrive files and folders are not links, even though Windows marks them as reparse points.
- Hidden, system and dot files, unfinished downloads and `.git` or `node_modules` are left alone by
  default.
- Every move is journaled and can be undone from History.

## Command line

```
various-tools sort_by_type scan D:\Downloads
various-tools sort_by_type apply D:\Downloads --yes
```

Add `--recursive`, `--unmatched other` or `--clash-handling skip` as needed.
