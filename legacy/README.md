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
| `app.py` | Streamlit image/video resolution reducer (ffmpeg) | `src/tidykit/tools/media_resize/` |
| `python-find_duplicates.py` | Image duplicate finder (SHA-256, pHash/dHash, EXIF, Ollama check) | `src/tidykit/tools/duplicates/images.py` |
| `find_duplicate_videos.py` | Video duplicate finder (SHA-256, duration, sampled-frame pHash) | `src/tidykit/tools/duplicates/videos.py` |
| `python-extract_file_metadata-8.py` | Organize photos/videos into `YYYY\YYYY - MM` folders | `src/tidykit/tools/organize_by_date/` |
| `rename_snapchat.py` | Rename `Snapchat-<digits>` files by modified time | `src/tidykit/tools/rename/` (Snapchat preset) |
| `python-v3-Extract_Project_Code.py` | Concatenate project source into one text file | `src/tidykit/tools/project_export/` |
| `python-v1-Extract_Project_Structure.py` | List project files with include/exclude filters | `src/tidykit/tools/project_export/` |
| `requirements-find_duplicates.txt` | Dependencies for the duplicate finder | `pyproject.toml` (Phase 1) |

The root-level `empty_folder_deleter.py` in this repo is ported to `src/tidykit/tools/empty_folders/`
(Phase 3.1). It is not a legacy copy here.

Source of truth for the mapping and the rules: `docs/MODERNIZATION_PLAN.md`, §2.2 and §4.
