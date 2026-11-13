# Various Tools

Various Tools is a collection of Windows utilities for tidying up files and media. It is being built as one installable app with a desktop GUI and a command-line interface. The plan is in [docs/MODERNIZATION_PLAN.md](docs/MODERNIZATION_PLAN.md).

This repository currently ships the original **Empty Folder Deleter** app. It is the first tool, and it is being rewritten as part of Various Tools. The description below covers that current app.

## Command line: `various-tools`

Every tool is available from the command line. The flags are generated from each tool's options, so the CLI, the GUI and the docs always agree. Run the commands with `uv run various-tools ...` from a source checkout (after `uv sync`), or `various-tools ...` once installed.

```
various-tools list                          # the tools: id, name, category, description
various-tools TOOL scan  [PATHS] [flags]    # preview. Changes nothing
various-tools TOOL apply [PATHS] [flags]    # the same, then apply (asks first)
various-tools history [RUN_ID]              # what was applied, and each run's actions
various-tools undo RUN_ID                   # move a run's files back
various-tools doctor                        # are ffmpeg, ffprobe, ExifTool and Ollama available?
various-tools presets list|save|delete      # named option sets
various-tools config show|get|set|unset     # settings
various-tools TOOL scan --help              # every flag, with its meaning, default and limits
```

### Preview first, then apply

`scan` never changes anything on disk. It prints a table of the actions (kind, source, destination, reason, whether it is selected) and the totals. Only `apply` changes files, and it shows the same table and asks for confirmation unless you pass `--yes`.

```
# 1. Preview, and save the plan so it can be read in an editor (or by an AI) first.
various-tools empty_folders scan "D:\Photos" --save-plan plan.json

# 2. Apply exactly that plan. Edit "selected" in the file to leave actions out.
various-tools empty_folders apply --plan plan.json

# Or skip the plan file: apply takes the same flags as scan.
various-tools rename apply "D:\Chats" --opt-preset snapchat

# Machine-readable output (only JSON on stdout), for scripts:
various-tools duplicates scan "D:\Photos" --media images --json > plan.json
```

Rules that always hold (the same as in the app):

- Deletes go to the Recycle Bin. Permanent delete needs the setting `delete_mode = permanent` and asks a second, explicit question (`--yes` skips both questions for scripts). Without `--yes`, a run with nobody to answer is refused.
- `various-tools undo RUN_ID` moves renamed and moved files back. Items sent to the Recycle Bin are restored from there.
- OneDrive cloud-only files are never read or downloaded; they are reported as skipped.
- `Ctrl+C` stops a running scan or apply at the next safe point and exits with code 130.

### Options, presets and settings

Flags are written as `--field-name`. A switch is `--flag/--no-flag`. A list is repeated or comma-separated (`--treat-as-empty a.tmp,b.tmp`). A dictionary is repeated as `KEY=VALUE`. Flags you leave out keep their default, and an empty value (`--match ""`) puts a "use the app setting" option back to its default. Options marked "Advanced" in `--help` are tuned values; the locked ones (marked with a padlock) are safety values or thresholds tuned on real data, so leave them alone unless you know why. If a tool option has the same name as a command flag (for example rename's `preset`), it is written `--opt-preset`.

```
# Save a set of options once, reuse it by name. Flags you type override the preset.
various-tools presets save duplicates "Camera roll" --media images --action trash
various-tools duplicates scan "D:\Camera Roll" --preset "Camera roll"
various-tools presets list
various-tools presets delete duplicates "Camera roll"

# Settings live in the per-user config folder (the path is shown by: various-tools config show).
various-tools config show
various-tools config set workers 4
various-tools config set external_tools.exiftool.path "C:\Program Files\Exiftool\exiftool.exe"
various-tools config unset external_tools.exiftool.path
```

Global options go before the command and apply to that run only; nothing is saved:

```
various-tools --config D:\portable\settings.toml --ffmpeg D:\tools\ffmpeg.exe duplicates scan D:\Videos --media videos
```

`--config FILE` (or the `VARIOUS_TOOLS_CONFIG` environment variable) chooses the settings file, and `--exiftool`, `--ffmpeg`, `--ffprobe` and `--ollama-url` override one program for one run.

Exit codes: `0` done, `1` failure, refusal, partial apply or a declined question, `2` usage or validation error (bad flag, bad value, unknown name), `130` cancelled with `Ctrl+C`.

### Old script to new command

Every documented use of the original scripts has a command. All of them preview by default; the old `--move` becomes `apply`.

| Original script and usage | New command |
|---|---|
| `empty_folder_deleter.py` (Tkinter: pick a folder, Start, review, confirm) | `various-tools empty_folders scan FOLDER`, then `various-tools empty_folders apply FOLDER`. Junk files and exclusions: `--treat-as-empty`, `--exclude-globs`; age: `--min-age-days`; permanent delete: `--delete-mode permanent` (also needs the setting). |
| `app.py` (Streamlit resolution reducer: preset, percentage or custom box, JPEG quality, video CRF, dry run, new folder or overwrite) | `various-tools media_resize scan FOLDER` then `apply`. Size presets: `--mode fit_box --size 720p` (`360p`, `480p`, `720p`, `1080p`, `2k`, `4k`); custom box: `--size custom --max-width W --max-height H`; percentage: `--mode percent --percent 50`; shorter side: `--mode shorter_side --target-px 1080`; JPEG quality: `--jpeg-quality 85`; video: `--media videos` or `both`, `--video-quality high\|medium\|low`, `--video-crf N`; output: `--output new_folder --output-folder-name resized` or `--output suffix --suffix-tag _resized`. The dry-run checkbox is `scan`. |
| `python-find_duplicates.py` (image duplicates; `--move`) | `various-tools duplicates scan FOLDER... --media images` (the input-list file becomes the folder arguments), then `various-tools duplicates apply FOLDER... --media images` for `--move`. Exact copies only: `--exact-only`. Trash instead of the `duplicate` folder: `--action trash`. Thresholds: `--phash-max`, `--dhash-max`, `--aspect-tol`, `--pixel-block-max`, `--burst-phash-max`. |
| `python-find_duplicates.py --verify --model M` (Ollama check of borderline pairs) | `various-tools duplicates scan FOLDER... --media images --verify-with-ollama --ollama-model M`; Ollama address: `--ollama-url URL` (before the command) or `config set ollama_url URL`. |
| `python-find_duplicates.py --explain A B` | `various-tools duplicates explain A B` (the same checks table and a verdict; `--phash-max`, `--dhash-max`, `--aspect-tol`, `--pixel-block-max` to experiment). |
| `find_duplicate_videos.py` (video duplicates; `--move`) | `various-tools duplicates scan FOLDER... --media videos`, then `apply` for `--move`. Thresholds: `--dur-tol-s`, `--dur-tol-pct`, `--frame-mean-max`, `--frame-worst-max`, `--sample-at 0.1,0.5,0.9`. Needs ffmpeg and ffprobe (`various-tools doctor`). |
| `python-extract_file_metadata-8.py` (organize by date; `--move`, `--dry-run-move`, `--recursive`, `--workers`, `--errors-only`, `--skip-processed`) | `various-tools organize_by_date scan SOURCE...` for `--dry-run-move`, `apply` for `--move`. `--recursive`, `--workers N`, `--errors-only`, `--skip-processed` keep their names. Destination: `--dest-root`; folder layout: `--folder-template "{year}/{year} - {month:02d}"`; date sources: `--date-priority`. Resize and CR3 conversion are now `media_resize` (`--convert-raw-to-jpeg`). Cloud-only OneDrive files are always skipped, so `--y`, `--N`, `--download-wait-seconds` and `--keep-pinned` have no equivalent. |
| `rename_snapchat.py` | `various-tools rename scan FOLDER --opt-preset snapchat`, then `apply`. Other built-in presets: `whatsapp`, `screenshot`. Your own pattern: `--match "IMG_*.jpg" --template "Trip-{date:%Y%m%d}-{counter:03}{ext}"`. Name clashes are listed and never overwritten. |
| `python-v3-Extract_Project_Code.py` (project source into one text file) | `various-tools project_export scan FOLDER --mode contents`, then `apply`. `--exts .py,.ipynb`, `--exclude-globs`, `--strip-comments/--no-strip-comments`, `--include-markdown-cells`, `--include-outputs`, `--output-format txt\|md`, `--output-path FILE`. |
| `python-v1-Extract_Project_Structure.py` (list project files with filters) | `various-tools project_export scan FOLDER --mode tree --exts .py,.md --exclude-globs .git,.venv`, then `apply` to write the listing. |

## Empty Folder Deleter (current app)

Empty Folder Deleter helps you clean up your file system by identifying and removing empty folders. It is useful for keeping a tidy directory structure and reclaiming disk space.

### Features

- **User-friendly GUI**: Easy-to-use interface for selecting the root directory to scan.
- **Recursive Scanning**: Scans subdirectories to find all empty folders within a given directory.
- **Safe Deletion**: Confirms with the user before deleting any folders.
- **Detailed Logging**: Provides a log of all actions taken, including successful deletions and any errors encountered.
- **Windows Integration**: Option to start the application on Windows startup.

## Installation

### Prerequisites

- Windows operating system
- Python 3.6 or higher (if running from source)

### Option 1: Install from Executable

1. Download the latest `EmptyFolderDeleterSetup.exe` from the [Releases](https://github.com/jay-singhvi/Empty-Folder-Deleter/releases) page.
2. Run the installer and follow the on-screen instructions.
3. Once installed, you can run Empty Folder Deleter from the Start menu.

### Option 2: Run from Source

1. Clone this repository:
   ```
   git clone https://github.com/jay-singhvi/Empty-Folder-Deleter.git
   ```
2. Navigate to the project directory:
   ```
   cd Empty-Folder-Deleter
   ```
3. No third-party packages are needed. The app uses only the Python standard library (Tkinter ships with Python).
4. Run the application:
   ```
   python empty_folder_deleter.py
   ```

## Usage

1. Launch the Empty Folder Deleter application.
2. Click the "Browse" button to select the root directory you want to scan for empty folders.
3. Click the "Start" button to begin the scanning process.
4. Review the list of empty folders found.
5. Confirm the deletion when prompted.
6. Check the log for details on the operation's success.

## Building from Source

To build the executable from source:

1. Install PyInstaller:
   ```
   pip install pyinstaller
   ```
2. Run PyInstaller with the spec file:
   ```
   pyinstaller empty_folder_deleter.spec
   ```
3. The executable will be created in the `dist` folder.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. Developer guidance (stack, layout, commands and safety rules) is in [AGENTS.md](AGENTS.md).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Thanks to all contribution provided by Claude 3.5 Sonnet on helping with error resolution to improve this tool and help convert this app to an actual windows installer.
- Icon provided by [Python libraries].

## Support

If you encounter any problems or have any suggestions, please [open an issue](https://github.com/jay-singhvi/Empty-Folder-Deleter/issues) on GitHub.

---

Developed with ❤️ by Jay Singhvi
