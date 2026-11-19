"""The repository is open source: no tracked file may hold a path from someone's own PC.

A real Windows user folder (``C:\\Users\\<account>\\...``) is refused. Placeholders that the
docs and tests use on purpose are allowed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BINARY = {".png", ".ico", ".exe", ".jpg", ".jpeg", ".heic", ".mp4", ".mov", ".zip", ".lock"}
# A drive, "Users", then an account name that is not one of the placeholders.
ALLOWED = (
    r"(?:<you>|<name>|<user>|you|me|someone|me too|name~1|public|default|all users|runneradmin)"
)
USER_PATH = re.compile(
    r"\b[a-z]:(?:\\\\|\\|/)+users(?:\\\\|\\|/)+(?!" + ALLOWED + r"(?:\\|/|\"|'|\s|$))"
    r"[a-z0-9._ -]{2,}",
    re.IGNORECASE,
)


def _tracked_files() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    result = subprocess.run([git, "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False)
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    names = [n for n in result.stdout.decode("utf-8").split("\0") if n]
    return [ROOT / n for n in names if Path(n).suffix.lower() not in BINARY]


def test_no_tracked_file_holds_a_real_user_folder() -> None:
    found: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if USER_PATH.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not found, "machine-specific paths:\n" + "\n".join(found)
