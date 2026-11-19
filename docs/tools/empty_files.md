# Empty Files

Finds files that are exactly 0 bytes long and sends them to the Recycle Bin.

## How it works

1. **Scan** walks the selected folders and looks at each file's size in the folder listing. No file
   is opened, and nothing changes on disk.
2. **Preview** lists every empty file. Uncheck the ones you want to keep.
3. **Apply** sends the checked files to the Recycle Bin. Each one is journaled, and you can undo
   the run from History.

## Options

Every value has a default and a hint in the app. Options marked *advanced* are behind the
"Advanced" switch.

| Option | Default | What it does |
|---|---|---|
| Folders to scan | (none) | The folders to look in, including the folders inside them. |
| Empty on purpose | `__init__.py`, `.gitkeep`, `.keep`, `py.typed`, `.nojekyll` | Exact file names that are kept even when empty, because programs expect them to be empty. Names match without regard to case. |
| Minimum age in days | 0 (0 to 36500) | Files changed more recently than this stay. |
| Never touch (advanced) | `.git`, `node_modules` | Names or paths that are left alone, with everything inside them. |
| Keep hidden and system files (advanced) | on | Files marked hidden or system by Windows stay. |

Protected folders (the Windows folder, Program Files, drive roots, your profile folder) come from
the app settings, where the list can be changed.

## Safety

- Only files of exactly 0 bytes are listed. A file with any content is never listed.
- The folder you select is never touched, and junctions and symlinks are never followed.
- Files go to the Recycle Bin, never straight to permanent deletion. Undo from History puts them
  back from the Recycle Bin when it finds them there; an item it cannot find is listed to restore
  by hand.
- Empty folders are not removed here. Use Empty Folder Deleter for those.

## Command line

```
various-tools empty_files scan D:\Projects
various-tools empty_files apply D:\Projects --yes
```
