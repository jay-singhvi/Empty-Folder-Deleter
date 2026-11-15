# Packaging

How the Windows app is built (plan §4 Phase 8). Everything runs from the repository root.

| File | What it does |
|---|---|
| `various_tools.spec` | PyInstaller one-dir spec. Builds two programs into one folder: `Various Tools.exe` (the desktop app, no console) and `various-tools.exe` (the command line). No UPX. |
| `gui_entry.py`, `cli_entry.py` | The two entry scripts PyInstaller freezes. They only call `various_tools.gui.app:main` and `various_tools.cli.main:main`. |
| `make_version_info.py` | Writes the Windows version resources from the version in `pyproject.toml` (the only place the version lives). `--print-version` prints it. |
| `build.py` | Runs PyInstaller with the spec and writes the portable zip. |
| `smoke_test.py` | Checks the frozen build: the desktop app starts and stays up, `list`, `doctor`, and a scan, apply and undo of `empty_folders` on a generated temp folder. Never touches your real folders or settings. |
| `installer.iss` | Inno Setup 6 script: per-user installer. |
| `make_icon.py`, `icon.ico` | The app icon (`uv run python packaging/make_icon.py` redraws it). |

## Build the app

```
uv sync                                   # PyInstaller is in the dev group
uv run python packaging/build.py          # dist\Various Tools\ and the portable zip
uv run python packaging/smoke_test.py     # test the frozen build
```

Output:

- `dist\Various Tools\Various Tools.exe`: double-click to run the desktop app.
- `dist\Various Tools\various-tools.exe`: the command line (`various-tools.exe --help`).
- `dist\Various-Tools-<version>-portable.zip`: the same folder, zipped. Unzip anywhere and run.

ffmpeg, ffprobe and ExifTool are not bundled. The app finds them at run time (Settings >
External programs, or `various-tools doctor`) and shows the `winget install` command for a missing
one.

## Build the installer

Needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`).
One command builds the app, the portable zip and the installer, with the version from
`pyproject.toml`:

```
uv run python packaging/build.py --installer
```

Or, with the app already built, compile the script yourself:

```
iscc /DAppVersion=<version from pyproject.toml> packaging\installer.iss
```

This writes `dist\Various-Tools-<version>-setup.exe`. The installer:

- installs for the current user only, with no admin prompt, to
  `%LOCALAPPDATA%\Programs\Various Tools`;
- adds a Start menu shortcut, and optionally a desktop shortcut and the Explorer "Open with
  Various Tools" folder menu (the same menu as Settings > Desktop app > shell integration);
- has no "start with Windows" option;
- on uninstall, removes the Explorer menu and asks whether to keep settings, presets, history and
  caches (`%LOCALAPPDATA%\Various Tools`). A silent uninstall keeps them.

## Release

Push a tag `v<version>` that matches `pyproject.toml`. `.github/workflows/release.yml` runs the
checks, builds, smoke-tests the frozen build, compiles the installer and publishes a GitHub
Release with the installer and the portable zip. Installers and `*.exe` files are never committed.

## When freezing breaks

- A tool is missing from `various-tools.exe list`: tools are found through the package's entry
  points (the spec copies the package metadata) and, failing that, through the built-in
  `various_tools.tools` packages (the spec collects every submodule). A new tool package needs no
  spec change.
- A missing module at run time: add it to `hiddenimports` in the spec.
- Run `smoke_test.py` after every dependency upgrade.
