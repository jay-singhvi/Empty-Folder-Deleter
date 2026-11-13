# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one folder, two programs (plan section 4, Phase 8).

    dist/Various Tools/Various Tools.exe   the desktop app (no console window)
    dist/Various Tools/various-tools.exe   the command line (console)

Both share one set of libraries (the ``_internal`` folder). No UPX. Run it through
``packaging/build.py``, which also writes the portable zip.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

PACKAGING = Path(SPECPATH)  # noqa: F821  (SPECPATH is provided by PyInstaller)
ROOT = PACKAGING.parent
sys.path.insert(0, str(PACKAGING))

import make_version_info  # noqa: E402

version_files = make_version_info.write_version_files(ROOT / "build")
ICON = str(PACKAGING / "icon.ico")

# ---- what to bundle ------------------------------------------------------------------------

datas = []
binaries = []
hiddenimports = []

# Tools are found through the package's entry points, which live in its metadata.
datas += copy_metadata("various-tools")
# Every tool, the GUI and the CLI are imported by name at run time (entry points, pkgutil
# fallback), so PyInstaller's static analysis cannot see all of them.
hiddenimports += collect_submodules("various_tools")

# Libraries that ship native DLLs or data files.
for package in ("pillow_heif", "rawpy"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
# (numpy and scipy have their own PyInstaller hooks; collecting every submodule would also pull in
# their test suites.)
for package in ("imagehash", "openpyxl", "pathspec", "platformdirs"):
    datas += collect_data_files(package)
    hiddenimports += collect_submodules(package)
# imagehash imports these inside functions (phash, whash).
hiddenimports += ["scipy.fftpack", "pywt"]
binaries += collect_dynamic_libs("numpy")

# pydantic and pydantic-settings load parts of themselves lazily.
for package in ("pydantic", "pydantic_settings", "pydantic_core"):
    hiddenimports += collect_submodules(package)
datas += copy_metadata("pydantic")

# send2trash picks its backend by platform; the Windows one is what the app needs. The other
# backends cannot be imported on Windows, so they are left out.
hiddenimports += collect_submodules(
    "send2trash", filter=lambda name: not name.startswith(("send2trash.mac", "send2trash.plat_"))
)

# Qt: the hooks collect the platform plugin, styles and DLLs for the modules imported.
hiddenimports += ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"]

# Obviously unused (be conservative: nothing here is imported by the app or by Qt Widgets).
excludes = [
    "tkinter",
    "_tkinter",
    "pytest",
    "pytest_qt",
    "_pytest",
    "IPython",
    "matplotlib",
    "pandas",
    "pyright",
    "ruff",
    "pre_commit",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtQuick",
    "PySide6.QtQml",
]


def analysis(script: str) -> Analysis:  # noqa: F821
    return Analysis(  # noqa: F821
        [str(PACKAGING / script)],
        pathex=[str(ROOT / "src")],
        binaries=list(binaries),
        datas=list(datas),
        hiddenimports=list(hiddenimports),
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=list(excludes),
        noarchive=False,
    )


a_gui = analysis("gui_entry.py")
a_cli = analysis("cli_entry.py")

pyz_gui = PYZ(a_gui.pure)  # noqa: F821
pyz_cli = PYZ(a_cli.pure)  # noqa: F821

exe_gui = EXE(  # noqa: F821
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="Various Tools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
    version=str(version_files["gui"]),
)
exe_cli = EXE(  # noqa: F821
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="various-tools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON,
    version=str(version_files["cli"]),
)

coll = COLLECT(  # noqa: F821
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    name="Various Tools",
)
