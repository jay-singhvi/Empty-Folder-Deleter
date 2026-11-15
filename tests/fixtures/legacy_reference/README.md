# Legacy reference scripts (test fixtures)

Unchanged copies of the original standalone scripts and of the first Tkinter app
(`empty_folder_deleter.py`). The parity tests under `tests/tools/` run them on generated temp
trees and compare the result with the new tools. They are not part of the app and are never
shipped.

- Do not edit them. Fix behaviour in `src/`.
- Never run them against real folders.

| Script | Ported to |
|---|---|
| `empty_folder_deleter.py` | `tools/empty_folders` |
| `app.py` | `tools/media_resize` |
| `python-find_duplicates.py`, `find_duplicate_videos.py` | `tools/duplicates` |
| `python-extract_file_metadata-8.py` | `tools/organize_by_date` |
| `rename_snapchat.py` | `tools/rename` (Snapchat preset) |
| `python-v1-Extract_Project_Structure.py`, `python-v3-Extract_Project_Code.py` | `tools/project_export` |

The rest of the old `legacy/` folder (the old PyInstaller spec, the NSIS installer script and the
requirements file) was deleted in Phase 8; it is in the git history.
