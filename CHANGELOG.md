# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Phase 1: Skeleton & tooling (2026-11-12)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 1.

#### Added
- `pyproject.toml`: uv project with a hatchling build and `src/` layout. Dependency groups `gui`,
  `media` and `dev`; `uv sync` installs all three. Version `0.1.0` is the only version field.
- `uv.lock`, and `.python-version` pinned to **3.14**.
- `src/various_tools/` skeleton: `core/`, `tools/`, `cli/`, `gui/`. `__version__` is read from
  package metadata.
- `various-tools` CLI (Typer) with a `version` command, and `various-tools-gui`, which opens a
  placeholder window. Qt is imported only inside the GUI package.
- Tests: version, CLI and GUI smoke tests. Qt runs offscreen, so no display is needed.
- pytest uses `--import-mode=importlib`, so test files with the same name in different folders
  (for example `tests/core/test_x.py` and `tests/tools/y/test_x.py`) can coexist. Guarded by
  `tests/core/test_layout.py` and `tests/gui/test_layout.py`.
- Quality tooling: ruff (lint and format), pyright (basic, strict on `core/`), pytest, and
  pre-commit (ruff and pyright hooks).
- `.github/workflows/ci.yml`: lint, format check, type check and tests on `windows-latest`.

#### Changed
- `.gitignore`: ignores `.claude/`, `.ruff_cache/` and `.pyright/`.

#### Decisions (not covered by the plan)
- **Python 3.14, not 3.13.** Python 3.15 fails: `pyyaml` (a pre-commit dependency) has no 3.15
  wheel. Python 3.14 installs every dependency from wheels.
- ruff skips `legacy/` and the root `empty_folder_deleter.py` until Phase 3 ports them, and it
  formats only Python files. The pre-commit ruff hooks are limited to Python for the same reason.
- CI does not install ffmpeg yet, because Phase 1 has no media tests. Phase 2 adds it.

### Phase 0: Prep (2026-11-11)

Plan reference: `docs/MODERNIZATION_PLAN.md`, §4 Phase 0.

#### Added
- `legacy/`: read-only, byte-identical copies of the 8 standalone scripts from the OneDrive
  `Scripts` folder, with `legacy/README.md` mapping each one to its planned new home.
- `AGENTS.md` (stack, layout, commands, 🔒 safety rules) and `CLAUDE.md` (a copy of it).
- `CHANGELOG.md`.
- `*.exe` to `.gitignore`, so binaries are no longer committed.
- `docs/MODERNIZATION_PLAN.md` committed to the repo.

#### Changed
- Product name confirmed as **Various Tools** (package `various_tools`, CLI `various-tools`).
  The name is applied in the plan, the legacy README and the README. The `TidyKit` placeholder
  is gone.
- README: the title is now Various Tools, with the current app described as the first tool.
  Placeholder owner names are replaced with `jay-singhvi`, and the nonexistent
  `requirements.txt` step is removed.

#### Removed
- `releases/EmptyFolderDeleterSetup.exe` is no longer tracked by git. The file is still on disk.
  Installers will be published through GitHub Releases. The binary remains in git history.
