"""Write the Windows version resources for the two executables.

The version lives only in ``pyproject.toml``. ``--print-version`` prints it; without arguments this
writes ``build/version_info_gui.txt`` and ``build/version_info_cli.txt`` in the PyInstaller
``VSVersionInfo`` format. The spec imports ``read_version`` and ``write_version_files``.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCT_NAME = "Various Tools"
COMPANY_NAME = "Jay Singhvi"
COPYRIGHT = "Copyright (c) Jay Singhvi. MIT License."

# (file name, FileDescription, OriginalFilename)
EXECUTABLES = {
    "gui": ("Various Tools", "Various Tools.exe"),
    "cli": ("Various Tools command line", "various-tools.exe"),
}

_TEMPLATE = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple},
    prodvers={tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', {company}),
         StringStruct('FileDescription', {description}),
         StringStruct('FileVersion', {version}),
         StringStruct('InternalName', {internal}),
         StringStruct('LegalCopyright', {copyright}),
         StringStruct('OriginalFilename', {original}),
         StringStruct('ProductName', {product}),
         StringStruct('ProductVersion', {version})])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def read_version(pyproject: Path | None = None) -> str:
    """The version string in ``pyproject.toml`` (for example ``1.2.3rc1``)."""
    path = pyproject or ROOT / "pyproject.toml"
    with path.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """Numeric ``(major, minor, patch, build)`` from a version like ``1.2.3rc1`` or ``1.2.3.dev0``.

    Only the leading numeric parts count: ``1.2.3rc1`` and ``1.2.3.dev0`` both give ``1.2.3.0``.
    """
    numbers: list[int] = []
    for part in version.split("+", 1)[0].split("."):
        match = re.match(r"\d+", part)
        if match is None:
            break
        numbers.append(int(match.group()))
        if match.end() != len(part):  # a suffix such as "rc1" ends the numeric part
            break
    numbers = (numbers + [0, 0, 0, 0])[:4]
    return numbers[0], numbers[1], numbers[2], numbers[3]


def version_info_text(version: str, description: str, original_filename: str) -> str:
    internal = original_filename.removesuffix(".exe")
    return _TEMPLATE.format(
        tuple=version_tuple(version),
        company=repr(COMPANY_NAME),
        description=repr(description),
        version=repr(version),
        internal=repr(internal),
        copyright=repr(COPYRIGHT),
        original=repr(original_filename),
        product=repr(PRODUCT_NAME),
    )


def write_version_files(out_dir: Path | None = None, version: str | None = None) -> dict[str, Path]:
    """Write ``version_info_gui.txt`` and ``version_info_cli.txt``; return their paths by key."""
    target = out_dir or ROOT / "build"
    target.mkdir(parents=True, exist_ok=True)
    resolved = version or read_version()
    paths: dict[str, Path] = {}
    for key, (description, original) in EXECUTABLES.items():
        path = target / f"version_info_{key}.txt"
        path.write_text(version_info_text(resolved, description, original), encoding="utf-8")
        paths[key] = path
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-version", action="store_true", help="Print the version and exit.")
    args = parser.parse_args(argv)
    if args.print_version:
        sys.stdout.write(read_version() + "\n")
        return 0
    for path in write_version_files().values():
        sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
