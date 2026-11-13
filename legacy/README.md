# legacy/

These are **read-only reference copies** of the original standalone scripts (copied unchanged from
`%USERPROFILE%\OneDrive\Scripts\`). They define the behavior the new toolkit must reproduce.

- Do not edit them. If a fix is needed, make it in the new code under `src/` and note it in the PR.
- Do not run them against real folders during development. Use generated fixtures in a temp
  directory (see `AGENTS.md`).
- They are deleted in Phase 8, once every tool has passed its parity test.

## Where each script is ported

| Legacy file | Purpose (short) | New home |
|---|---|---|
| `app.py` | Streamlit image/video resolution reducer (ffmpeg) | `src/various_tools/tools/media_resize/` |
| `python-find_duplicates.py` | Image duplicate finder (SHA-256, pHash/dHash, EXIF, Ollama check) | `src/various_tools/tools/duplicates/images.py` |
| `find_duplicate_videos.py` | Video duplicate finder (SHA-256, duration, sampled-frame pHash) | `src/various_tools/tools/duplicates/videos.py` |
| `python-extract_file_metadata-8.py` | Organize photos/videos into `YYYY\YYYY - MM` folders | `src/various_tools/tools/organize_by_date/` |
| `rename_snapchat.py` | Rename `Snapchat-<digits>` files by modified time | `src/various_tools/tools/rename/` (Snapchat preset) |
| `python-v3-Extract_Project_Code.py` | Concatenate project source into one text file | `src/various_tools/tools/project_export/` |
| `python-v1-Extract_Project_Structure.py` | List project files with include/exclude filters | `src/various_tools/tools/project_export/` |
| `requirements-find_duplicates.txt` | Dependencies for the duplicate finder | `pyproject.toml` (Phase 1) |
| `empty_folder_deleter.spec` | PyInstaller one-file build of the old Tkinter app (with UPX) | `packaging/various_tools.spec` (Phase 8) |
| `installer.nsi` | Per-machine NSIS installer of the old app, with a "start on Windows startup" option | `packaging/installer.iss` (Phase 8, per-user, no startup option) |

The root-level `empty_folder_deleter.py` in this repo is ported to `src/various_tools/tools/empty_folders/`
(Phase 3.1). It is not a legacy copy here. Its old build files (`empty_folder_deleter.spec`,
`installer.nsi`) were moved here from the repository root in Phase 8; they are not copies from
OneDrive.

Source of truth for the mapping and the rules: `docs/MODERNIZATION_PLAN.md`, §2.2 and §4.
