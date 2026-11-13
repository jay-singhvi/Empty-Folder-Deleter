# Empty Folders

Finds folders that will be empty once their empty subfolders are removed, and removes them.
This is the Various Tools version of the original Empty Folder Deleter.

## How it works

1. **Scan** walks the selected folders once, from the bottom up. It changes nothing on disk.
2. **Preview** lists every folder that will go, deepest first. Folders that hold a real file, a
   reparse point (junction or symlink), an excluded entry, or a folder that is kept for another
   reason stay, and so do their parents.
3. **Apply** removes the selected items. Each removal is journaled, and you can see it in History.

## Options

Nothing is a fixed constant: every value below has a default and a hint in the app. Options marked
*advanced* are behind the "Advanced" switch.

| Option | Default | What it does |
|---|---|---|
| Folders to scan | (none) | The folders to scan. The folder itself is never removed. |
| Junk files that count as empty | `desktop.ini`, `Thumbs.db`, `.DS_Store` | Exact file names that do not count as content. They are removed with their folder. Names match without regard to case. |
| Never touch (advanced) | `.git`, `node_modules` | Entries that count as content and are never touched. A folder that holds one stays. |
| Minimum age in days (advanced) | 0 (0 to 36500) | Folders changed more recently than this stay. |
| Keep hidden and system folders (advanced) | on | Hidden or system folders stay, and so do their parents. |
| Delete mode (advanced, 🔒 locked) | `trash` (Recycle Bin) | `trash` sends items to the Recycle Bin. `permanent` erases them, but it also needs the app setting `delete_mode = permanent`, asks for confirmation, and cannot be undone. |

Protected folders (the Windows folder, Program Files, drive roots, your profile folder) come from
the app settings, where the list can be changed.

## Safety

- The folder you select is never removed, even when it is empty.
- Junctions and symlinks are never followed, so nothing outside the selected folder is touched.
- Removal goes to the Recycle Bin by default. Trashed items cannot be restored by Various Tools,
  so restore them from the Recycle Bin by hand.
- A folder that still has contents is refused at apply time, even if its selection was changed
  by hand.
- Unchecking a folder in the preview also unchecks the removal of every folder above it.

## Command line

Not available yet. The command line arrives in Phase 4.
