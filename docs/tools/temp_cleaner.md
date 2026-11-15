# Temp Cleaner

Removes old files from your temp folder (or folders you choose) to the Recycle Bin, and lists
browser caches without touching them. Category: Clean up.

## How it works

1. **Scan** reads file names, sizes, dates and attributes only. It never opens a file and changes
   nothing on disk.
2. **Preview** lists every file that is older than the limit and matches the patterns, then the
   folders that will be left empty (deepest first, after their files). Everything is checked by
   default, and you can uncheck anything. Notes list the browser caches.
3. **Apply** checks every file again, then sends the checked items to the Recycle Bin. Each one
   is journaled and shows in History. A file that is in use fails on its own and the rest carry
   on. Undo from History puts trashed items back from the Recycle Bin when it finds them there (same path, same time, same size); an item it cannot find is listed to restore by hand.

A plan can be saved and applied days later, so apply does not trust the scan: each file must
still be a plain local file that matches the patterns, is not hidden (when those are kept), and has
not changed (modified or created) within the age limit, counted from the moment of apply. A file
that no longer qualifies (for example one created since the scan) is kept and journaled as
**skipped**, with the reason. Folders are not re-aged at apply (trashing their files changes their
time); the engine still refuses any folder that is not empty.

Apply also checks every folder of the plan again with the same refusals as the scan (see *Which
folders*): an app data folder (`%APPDATA%`, `%LOCALAPPDATA%`, `%PROGRAMDATA%`), a folder that holds
the temp folder, or a folder inside a browser cache. A saved or edited plan with such a folder
removes nothing under it: every row there is journaled as **skipped** with the reason. A row
inside a browser cache folder is skipped too, whatever folder the plan names.

## Which folders

If you choose no folder, the tool uses your temp folder: the folders named by the `TEMP` and `TMP`
variables, and `%LOCALAPPDATA%\Temp`, without repeats. The plan says which folders it picked. The
default is worked out from the environment variables alone (`default_temp_roots(environ)`), so it
is easy to test and never touches the disk until the scan itself.

Folders are compared on their real long names. `%TEMP%` is often an 8.3 short path
(`C:\Users\NAME~1\AppData\Local\Temp`) while `%LOCALAPPDATA%\Temp` is the long form of the same
folder: they count as one folder, and a folder inside another chosen folder drops out, so no file
is planned twice. The plan uses the long names.

Some folders are always refused, with a note, because the default rules (every file not changed
for a week) would trash program settings and data in them:

- `%APPDATA%`, `%LOCALAPPDATA%` and `%PROGRAMDATA%` themselves;
- any folder that holds the temp folder, such as `%USERPROFILE%\AppData` or
  `%USERPROFILE%\AppData\Local`.

`%TEMP%`, `%TMP%`, `%LOCALAPPDATA%\Temp` and folders inside them stay allowed.

## Options

Nothing is a fixed constant: every value below has a default and a hint in the app. Options marked
*advanced* are behind the "Advanced" switch.

| Option | Default | What it does |
|---|---|---|
| Folders to clean | (empty = your temp folder) | The folders to remove old files from. A drive root or protected folder is refused. The folders themselves are never removed. |
| Include sub-folders | on | Also clean sub-folders. When off, no folder is removed either. |
| Older than (days) | 7 (0 to 36500) | Only files that have not changed for more than this many days are removed. A file's age is the newer of its modified time and its creation time, because an installer that unpacks files into the temp folder gives them the archive's old modified times. 0 removes files of any age, including ones in use right now. |
| Only these files | `*` | File names or relative paths (patterns allowed) that may be removed. Matched without regard to letter case. |
| Never remove | (none) | Files or folders (patterns allowed) that are always kept. A kept folder stays with everything in it. |
| Keep hidden and system files (advanced) | on | Hidden or system files and folders are kept, and so are the folders that hold them. |
| Remove folders left empty | on | Also remove a folder when everything in it is going, or it is already empty, and it has not changed for the number of days above. |
| List browser caches | on | Add a read-only note for each browser cache folder found. |
| Measure browser cache sizes (advanced) | on | Add up each cache's size from file sizes only. When off, the size shows as `unknown`. |

Protected folders (the Windows folder, Program Files, drive roots, your profile folder) come from
the app settings, where the list can be changed.

## Empty folders

A folder is removed only when **every** item in it is going too: its files are planned for
removal and its sub-folders are removed. A junction or symlink, a hidden or system item, an
excluded item, a file that is too recent, a file that does not match the patterns, and a
cloud-only file each keep their folder, and the folders above it. A folder that changed within the
age limit is kept, so a folder that a program just emptied is left until it has been quiet.

Folders go to the Recycle Bin after their files, deepest first. The engine checks again at apply
time that a folder is empty, so if a file could not be removed (for example it is in use), its
folder is skipped instead of being trashed with the file in it.

## Browser caches

Chrome, Edge and Firefox cache folders that exist on this PC are listed as notes, for you to see
how big they are:

```
browser-cache (read-only): <name> | <path> | <bytes or unknown>
```

For example `browser-cache (read-only): Chrome (Default) Cache | C:\Users\Me\AppData\Local\Google\Chrome\User Data\Default\Cache | 734003200`.
The size is an integer number of bytes, or `unknown` when sizes are not measured or the folder
cannot be read. Parse a note with `various_tools.tools.temp_cleaner.parse_browser_cache_note(note)`,
which gives `(name, Path, bytes or None)`.

They are **only listed**: browsers manage their own caches, and clearing one by hand can hurt a
running browser. No action is ever planned for them. Where they are looked for (under
`%LOCALAPPDATA%`):

| Browser | Folders |
|---|---|
| Chrome | `Google\Chrome\User Data\<profile>\Cache`, `Code Cache`, `GPUCache` |
| Edge | `Microsoft\Edge\User Data\<profile>\Cache`, `Code Cache`, `GPUCache` |
| Firefox | `Mozilla\Firefox\Profiles\<profile>\cache2` |

If one of these folders is inside a folder you chose, it is skipped and counted in the summary. If
you choose a browser cache itself as the folder to clean, it is refused.

## Safety

- The scan reads no file contents and writes nothing.
- Nothing outside the chosen folders is touched, and the folders themselves are never removed.
- Everything goes to the Recycle Bin. Nothing is ever deleted permanently.
- Junctions and symlinks are never followed, and are never removed. OneDrive files and folders are not links, even though Windows marks them as reparse points.
- Cloud-only OneDrive files are skipped and never read. They are reported as
  "skipped: cloud-only".
- Protected folders are refused, and a dry run (the preview) is always the first step.
- Files younger than the age limit are kept, and hidden or system files are kept by default.
- Browser caches are never cleaned.

## Command line

```
various-tools temp_cleaner scan
various-tools temp_cleaner scan --min-age-days 30 --include-patterns "*.tmp,*.log"
various-tools temp_cleaner apply --yes
```

With no folder given, the scan uses your temp folder. Add `--roots D:\Scratch` to clean another
folder instead.
