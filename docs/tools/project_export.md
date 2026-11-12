# Project Export

Writes a folder's file list and file contents into one text file. Use it to give an AI assistant
the context of a project. It replaces the original `Extract_Project_Structure` and
`Extract_Project_Code` scripts.

## How it works

1. **Scan** lists the files that pass the filters and reads them, to measure the export. It writes
   nothing.
2. **Preview** shows how many files will be written, the estimated token count, and anything that
   was skipped, with the reason.
3. **Apply** writes the export file. The write is journaled, and the file is never written over an
   existing one. If the name is taken, the export gets a free name such as `Project-File-Content_dup1.txt`.

## Options

| Option | Default | What it does |
|---|---|---|
| Root | (required) | The folder to export. It is never written to, and a drive root or a protected folder is refused. |
| Include | (all) | Only files whose name or relative path matches one of these globs. |
| Exclude | `.git`, `.venv`, `__pycache__`, `node_modules`, `dist`, `build` | Files and folders that match are left out, with everything inside them. |
| Extensions | (all) | Only files with these extensions, for example `py`, `ipynb`. |
| Respect `.gitignore` | on | Files that a `.gitignore` in the folder or its subfolders ignores are left out. |
| Mode | both | `tree` lists the files as an indented tree. `contents` writes the file text. `both` writes the tree, then the contents. |
| Output format | txt | `txt` uses separator lines. `md` writes headings and fenced code blocks. |
| Output file | `Project-File-Content.txt` in the root | Where the export is written. The output file is never part of the export itself. |
| Strip comments | on | Python comments are removed. String literals are kept, including triple-quoted strings and `#` inside a string. |
| Include markdown cells | on | Notebook markdown cells are included. |
| Include notebook outputs | off | Printed output and results of notebook cells are included. They can contain secrets, so they are off by default. |
| Maximum file size (KB) | (no limit) | Larger files are skipped and listed in the preview. |

## Safety

- The export never changes a file in the folder you select.
- OneDrive cloud-only files are never read. They are listed as skipped, and nothing is downloaded.
- Junctions and symlinks are never followed.
- An existing export file is never overwritten.

## Command line

Not available yet. The command line arrives in Phase 4.
