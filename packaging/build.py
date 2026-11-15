"""Build the frozen app and the portable zip. Run from anywhere: ``python packaging/build.py``.

``--installer`` also compiles the Inno Setup installer (needs Inno Setup 6:
``winget install JRSoftware.InnoSetup``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent
ROOT = PACKAGING.parent
sys.path.insert(0, str(PACKAGING))

from make_version_info import read_version  # noqa: E402

APP_FOLDER = "Various Tools"


def run_pyinstaller() -> int:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(PACKAGING / "various_tools.spec"),
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
    ]
    sys.stdout.write("Running: " + " ".join(command) + "\n")
    sys.stdout.flush()
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def make_zip(app_dir: Path, zip_path: Path) -> None:
    """Zip ``app_dir`` so that the archive holds the single folder ``Various Tools/``."""
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(app_dir.rglob("*")):
            if path.is_file():
                archive.write(path, Path(APP_FOLDER) / path.relative_to(app_dir))


def find_iscc() -> Path | None:
    """The Inno Setup compiler: on PATH, or in its per-user or per-machine install folder."""
    on_path = shutil.which("iscc")
    if on_path:
        return Path(on_path)
    for variable in ("LOCALAPPDATA", "ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(variable)
        if not base:
            continue
        folder = Path(base) / "Programs" if variable == "LOCALAPPDATA" else Path(base)
        candidate = folder / "Inno Setup 6" / "ISCC.exe"
        if candidate.is_file():
            return candidate
    return None


def make_installer(version: str) -> Path | None:
    """Compile ``installer.iss``. Returns the setup file, or None when it could not be built."""
    iscc = find_iscc()
    if iscc is None:
        sys.stderr.write(
            "Inno Setup 6 was not found. Install it with: winget install JRSoftware.InnoSetup\n"
        )
        return None
    command = [str(iscc), f"/DAppVersion={version}", str(PACKAGING / "installer.iss")]
    sys.stdout.write("Running: " + " ".join(command) + "\n")
    sys.stdout.flush()
    if subprocess.run(command, cwd=ROOT, check=False).returncode != 0:
        return None
    setup = ROOT / "dist" / f"Various-Tools-{version}-setup.exe"
    return setup if setup.is_file() else None


def main() -> int:
    code = run_pyinstaller()
    if code != 0:
        sys.stderr.write(f"PyInstaller failed with exit code {code}.\n")
        return code
    app_dir = ROOT / "dist" / APP_FOLDER
    gui_exe, cli_exe = app_dir / "Various Tools.exe", app_dir / "various-tools.exe"
    missing = [str(p) for p in (gui_exe, cli_exe) if not p.is_file()]
    if missing:
        sys.stderr.write("The build finished but these files are missing: " + ", ".join(missing))
        sys.stderr.write("\n")
        return 1
    zip_path = ROOT / "dist" / f"Various-Tools-{read_version()}-portable.zip"
    make_zip(app_dir, zip_path)
    sys.stdout.write(
        f"\nDesktop app: {gui_exe}\nCommand line: {cli_exe}\nPortable zip: {zip_path}\n"
    )
    if "--installer" in sys.argv[1:]:
        setup = make_installer(read_version())
        if setup is None:
            sys.stderr.write("The installer was not built.\n")
            return 1
        sys.stdout.write(f"Installer: {setup}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
