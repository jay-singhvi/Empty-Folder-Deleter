# Checksum and verify

Writes a checksum manifest for a folder, or checks a folder against one. Use it to confirm that a
backup copy matches the original, or that a download or an archive has not changed. The manifest
uses the `sha256sum` text format, so other programs can read it.

## How it works

**Create** (the default)

1. **Scan** lists the files that would go into the manifest. It hashes nothing and changes
   nothing, so the preview is quick. The plan has one `write` action per chosen folder, whose
   output path is the manifest, for example `D:\Photos\SHA256SUMS.txt`.
2. **Apply** lists and hashes the folder at that moment and writes the manifest. The manifest
   describes the folder as it is when you press Apply, not as it was at scan time, and the file
   count in the preview is a guide. The action is journaled. The files are read once, because the
   scan does not hash them.

**Verify**

1. **Scan** reads the manifest, hashes each listed file again and reports the result in the plan
   notes. It changes nothing, and the plan has no actions, so there is nothing to apply.

## Manifest format

One line per file: `<hex digest>  <relative/posix/path>` (two spaces), UTF-8 without a byte-order
mark, `\n` line ends, sorted by path. `sha256sum -c SHA256SUMS.txt` accepts it. Paths are relative
to the folder that holds the manifest and use `/`.

Reading is more forgiving: a `*` before the path (binary mode), CRLF line ends, a UTF-8 byte-order
mark, `\` as a separator, blank lines and `#` comment lines are accepted. A manifest that is not
UTF-8 is refused.

## Options

Nothing is a fixed constant: every value below has a default and a hint in the app. Options
marked *advanced* are behind the "Advanced" switch.

| Option | Default | What it does |
|---|---|---|
| What to do (`mode`) | `create` | `create` writes a manifest. `verify` checks one. |
| Folders (`roots`) | (required) | Each folder gets its own manifest, with paths relative to it. A drive root or a protected folder is refused. |
| Hash algorithm (`algorithm`) | `sha256` | `sha256`, `sha1`, `md5` or `sha512`. The last three exist so that manifests from other programs can be checked. Verify must use the algorithm the manifest was written with. |
| Manifest file name (`manifest_name`) | empty: `SHA256SUMS.txt` (`SHA1SUMS.txt`, `MD5SUMS.txt`, `SHA512SUMS.txt` for the others) | A plain name inside each folder, not a path. |
| Manifest to verify (`manifest_file`) | empty | Verify only. Checks this manifest instead of the one inside the folder, for example one made from the original, against a backup copy. Needs exactly one folder. |
| Include sub-folders (`recursive`) | on | Also list files in sub-folders. |
| Only these files (`include_globs`) | empty | When given, only matching files are listed (by name or relative path, patterns allowed). |
| Never include (`exclude_globs`) | empty | Matching files and folders are left out, for example `.git` or `*.tmp`. |
| Include hidden files (`include_hidden`) | off | Hidden or system files and folders, and names that start with a dot, are left out unless this is on. |
| Report new files (`report_extra`) | on | Verify only. Lists files in the folder that the manifest does not mention. |
| If a file cannot be read (`unreadable_files`, advanced) | `fail` | Create only. `fail` writes no manifest, so one is never short of files without you knowing. `skip` leaves the file out and logs it. |
| Read size (KB) (`read_chunk_kb`, advanced) | empty: the `hash_chunk_kb` setting (1024) | Speed and memory only. Never changes a digest. |

The include, exclude and hidden rules decide which files a create lists, and which files verify
reports as new. Verify always checks every file the manifest lists, whatever those rules say.

## Safety

- **An existing manifest is never replaced.** The scan leaves that action unticked and says why.
  If one appears later, apply fails for that action and the file is untouched. The new manifest is
  written to a temporary file in the same folder and renamed into place without replacing.
- **Cloud-only OneDrive files are never read.** They are reported as skipped and a manifest never
  lists them. Downloading them is a separate decision.
- **Junctions and symlinks are never followed.** They are not listed, and a manifest line whose
  path goes through one is reported as unreadable.
- **A manifest path that is absolute or contains `..` is refused** and reported as an invalid
  line. Nothing outside the chosen folder is read.
- **Protected folders are refused**, and apply checks the output path again: the manifest must be
  inside the selected folder, directly in it, and not behind a junction.
- The manifest is one new file. **Undo sends it to the Recycle Bin** when it is unchanged since the
  run (same size and modified time). A manifest you edited is left alone and listed for manual
  removal.
- Cloud-only files (and a cloud-only manifest when verifying) are also listed in the plan's
  cloud-only list, for the optional download action.
- **Long paths work.** Files, and the manifest itself, are opened through `core/fs.long_path()`,
  so a path past 260 characters is hashed and verified even when Windows long-path support is off.

## Verify notes

The first note is the summary. After it come one `root:` line per folder, followed by that
folder's problems. Files that match are only counted. Paths are relative to the folder, with `/`.

| Note | Meaning |
|---|---|
| `Verify: OK - 120 OK, 0 mismatched, 0 missing, 0 unreadable, 0 skipped (cloud-only), 3 extra, 0 invalid line(s), 0 manifest(s) not usable.` | The summary. It says `OK` when nothing is mismatched, missing, unreadable, skipped, invalid or unusable. Extra files alone do not make it fail. Otherwise it says `PROBLEMS FOUND`. |
| `root: <folder> (manifest <name>, <n> entries)` | Starts the notes for one folder. |
| `mismatch: <path>` | The file is there but its digest differs from the manifest. |
| `missing: <path>` | The manifest lists it, but it is not on disk. |
| `extra: <path>` | It is on disk (and passes the filters) but the manifest does not list it. Only with *Report new files*. |
| `unreadable: <path> (<reason>)` | The file could not be read, is not a regular file, or its path goes through a junction or symlink. |
| `skipped: cloud-only: <path>` | The file is cloud-only, so it was not read. |
| `invalid: line <n>: <reason>` | A manifest line could not be used: not `<digest>  <path>`, a digest of the wrong length for the algorithm, or a path that is absolute or escapes the folder. |
| `Manifest not found: <path>` | There is no manifest to check against. |
| `Could not read the manifest <path>: <reason>` | The manifest could not be read or is not UTF-8. |

Notes from a create scan: `Create: <n> manifest(s) ready to write for <n> file(s) (<size>), ...`,
`skipped: cloud-only: <path>`, `Manifest already exists, never replaced: <path> ...` and
`No files to list in <folder>; no manifest planned.`

## Command line

```
various-tools checksum scan D:\Photos                       # preview a manifest
various-tools checksum apply D:\Photos --yes                # write SHA256SUMS.txt
various-tools checksum scan D:\Backup --mode verify --manifest-file D:\Photos\SHA256SUMS.txt
```
