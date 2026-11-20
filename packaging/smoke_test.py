"""Smoke-test the frozen build in ``dist/Various Tools/``. Exits non-zero if any check fails.

Everything happens in a temporary folder: a settings file, the data and cache folders, and the
generated trees the tools work on. Your real folders and settings are never touched. The
``empty_folders`` check sends a few empty temporary folders to the Recycle Bin.
"""

from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "dist" / "Various Tools"
GUI_EXE = APP_DIR / "Various Tools.exe"
CLI_EXE = APP_DIR / "various-tools.exe"

EXPECTED_TOOLS = [
    "checksum",
    "corrupt_media",
    "disk_usage",
    "duplicates",
    "empty_folders",
    "media_convert",
    "media_resize",
    "metadata_strip",
    "organize_by_date",
    "project_export",
    "rename",
    "set_date",
    "sort_by_type",
    "temp_cleaner",
]
GUI_UP_SECONDS = 6.0
GUI_WINDOW_WAIT_SECONDS = 30.0
CLI_TIMEOUT_SECONDS = 180

_results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    suffix = f"  ({detail})" if detail else ""
    sys.stdout.write(f"{'PASS' if ok else 'FAIL'}  {name}{suffix}\n")
    sys.stdout.flush()
    return ok


def check(name: str, body: Callable[[], str | None]) -> None:
    """Run ``body``; it returns an optional detail, or raises AssertionError / any error to fail."""
    try:
        record(name, True, body() or "")
    except AssertionError as exc:
        record(name, False, str(exc))
    except Exception as exc:  # a crash in a check is a failed check, not a crashed test run
        record(name, False, f"{type(exc).__name__}: {exc}")


# ----- helpers -------------------------------------------------------------------------------


class Env:
    """The temporary world every check runs in."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.data = base / "data"
        self.cache = base / "cache"
        self.data.mkdir()
        self.cache.mkdir()
        self.settings = base / "settings.toml"
        self.settings.write_text(
            f'data_folder = "{self.data.as_posix()}"\ncache_folder = "{self.cache.as_posix()}"\n',
            encoding="utf-8",
        )

    def cli(
        self, *args: str, timeout: int = CLI_TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI_EXE), "--config", str(self.settings), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=self.base,
            check=False,
        )


def describe(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stdout + proc.stderr).strip()
    return f"exit {proc.returncode}: {' '.join(text.split())[:300]}"


def run_id_from(text: str) -> int:
    match = re.search(r"Run (\d+) (?:done|partial|failed|cancelled)", text)
    assert match, f"no run id in output: {' '.join(text.split())[:300]}"
    return int(match.group(1))


def visible_window_titles(pid: int) -> list[str]:
    """Titles of the visible top-level windows that belong to process ``pid``."""
    user32 = ctypes.windll.user32
    titles: list[str] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            titles.append(buffer.value)
        return True

    user32.EnumWindows(enum_proc(visit), 0)
    return titles


def gui_stays_up(env: Env, *extra: str) -> str:
    """Start the desktop app with the real Qt platform; it must show its window and keep running."""
    log = env.base / f"gui-{len(_results)}.log"
    with log.open("wb") as sink:
        proc = subprocess.Popen(
            [str(GUI_EXE), "--config", str(env.settings), *extra],
            stdout=sink,
            stderr=sink,
            cwd=env.base,
        )
    try:
        deadline = time.monotonic() + GUI_WINDOW_WAIT_SECONDS
        title = ""
        while time.monotonic() < deadline and proc.poll() is None:
            main = [t for t in visible_window_titles(proc.pid) if t.startswith("Various Tools")]
            if main:
                title = main[0]
                break
            time.sleep(0.25)
        assert proc.poll() is None, f"the app exited early with code {proc.returncode}"
        assert title, "no visible 'Various Tools' window appeared"
        time.sleep(GUI_UP_SECONDS)
        assert proc.poll() is None, f"the app exited with code {proc.returncode} after starting"
        return f"window '{title}'"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ----- the checks ----------------------------------------------------------------------------


def check_gui_default(env: Env) -> str:
    return gui_stays_up(env)


def check_gui_with_tool(env: Env) -> str:
    tree = env.base / "gui_tree"
    (tree / "empty").mkdir(parents=True)
    return gui_stays_up(env, "--tool", "empty_folders", "--folder", str(tree))


def check_cli_help(env: Env) -> str:
    proc = env.cli("--help")
    assert proc.returncode == 0, describe(proc)
    assert "empty_folders" in proc.stdout, "the help does not list the tools"
    return ""


def check_cli_version(env: Env) -> str:
    proc = env.cli("version")
    assert proc.returncode == 0, describe(proc)
    assert re.search(r"various-tools \d+\.\d+", proc.stdout), describe(proc)
    return proc.stdout.strip()


def check_cli_list(env: Env) -> str:
    proc = env.cli("list")
    assert proc.returncode == 0, describe(proc)
    # Rich pads cells; a tool id is one unbroken word in the first column.
    found = set(re.findall(r"^\W*([a-z_]+)\s", proc.stdout, flags=re.MULTILINE))
    missing = [t for t in EXPECTED_TOOLS if t not in found]
    assert not missing, f"missing tools: {', '.join(missing)}"
    return f"{len(EXPECTED_TOOLS)} tools"


def check_doctor(env: Env) -> str:
    proc = env.cli("doctor")
    assert proc.returncode == 0, describe(proc)
    assert "ffmpeg" in proc.stdout, describe(proc)
    return ""


def check_empty_folders(env: Env) -> str:
    tree = env.base / "empties"
    for name in ("a/b", "c", "d/e/f"):
        (tree / name).mkdir(parents=True)
    (tree / "keep").mkdir()
    (tree / "keep" / "file.txt").write_text("keep me", encoding="utf-8")

    def snapshot() -> list[str]:
        return sorted(str(p.relative_to(tree)) for p in tree.rglob("*"))

    before = snapshot()
    scan = env.cli("empty_folders", "scan", str(tree))
    assert scan.returncode == 0, describe(scan)
    assert snapshot() == before, "scan changed the disk"

    apply = env.cli("empty_folders", "apply", str(tree), "--yes")
    assert apply.returncode == 0, describe(apply)
    run_id = run_id_from(apply.stdout)
    after = snapshot()
    assert after == ["keep", str(Path("keep") / "file.txt")], f"unexpected tree: {after}"
    assert (tree / "keep" / "file.txt").read_text(encoding="utf-8") == "keep me"

    undo = env.cli("undo", str(run_id), "--yes")
    text = " ".join((undo.stdout + undo.stderr).split())
    # Undo restores the folders from the Recycle Bin (they were recycled a moment ago).
    assert undo.returncode == 0 and "undone" in text, describe(undo)
    assert snapshot() == before, f"undo did not restore the tree: {snapshot()}"
    return f"run {run_id}: 6 empty folders to the Recycle Bin, undo restored them"


def check_rename_undo(env: Env) -> str:
    tree = env.base / "renames"
    tree.mkdir()
    names = ["photo1.txt", "photo2.txt", "photo3.txt"]
    for index, name in enumerate(names):
        (tree / name).write_text(f"content {index}", encoding="utf-8")
    options = (
        "--match",
        "*.txt",
        "--template",
        "new_{counter:03}{ext}",
        "--counter-scope",
        "global",
    )

    scan = env.cli("rename", "scan", str(tree), *options)
    assert scan.returncode == 0, describe(scan)
    assert sorted(p.name for p in tree.iterdir()) == names, "scan changed the disk"

    apply = env.cli("rename", "apply", str(tree), *options, "--yes")
    assert apply.returncode == 0, describe(apply)
    run_id = run_id_from(apply.stdout)
    renamed = sorted(p.name for p in tree.iterdir())
    assert renamed == ["new_001.txt", "new_002.txt", "new_003.txt"], f"not renamed: {renamed}"

    undo = env.cli("undo", str(run_id), "--yes")
    assert undo.returncode == 0, describe(undo)
    restored = {p.name: p.read_text(encoding="utf-8") for p in tree.iterdir()}
    expected = {name: f"content {i}" for i, name in enumerate(names)}
    assert restored == expected, f"not restored: {restored}"
    return f"run {run_id}: renamed and restored"


def check_media_imports(env: Env) -> str:
    """Duplicate scan over generated PNG, JPEG and HEIC copies, exported to .xlsx."""
    # The dev environment makes the fixture images; the frozen app has to read them.
    import pillow_heif
    from PIL import Image, ImageDraw

    pillow_heif.register_heif_opener()  # type: ignore[reportUnknownMemberType]

    tree = env.base / "pics"
    tree.mkdir()
    image = Image.new("RGB", (128, 96))
    draw = ImageDraw.Draw(image)
    for x in range(128):
        draw.line([(x, 0), (x, 95)], fill=(x * 2, 255 - x * 2, 100))
    draw.ellipse((30, 20, 90, 70), fill=(250, 250, 10))
    image.save(tree / "a.png")
    image.save(tree / "b.png")
    image.save(tree / "c.jpg", quality=95)
    image.save(tree / "d.heic", quality=90)
    before = sorted(p.name for p in tree.iterdir())

    report = env.base / "duplicates.xlsx"
    proc = env.cli("duplicates", "scan", str(tree), "--export", str(report), "--json")
    assert proc.returncode == 0, describe(proc)
    assert sorted(p.name for p in tree.iterdir()) == before, "scan changed the disk"
    assert report.is_file() and report.stat().st_size > 0, "no .xlsx report was written"
    with zipfile.ZipFile(report) as book:
        assert "xl/workbook.xml" in book.namelist(), "the report is not a workbook"
    plan = json.loads(proc.stdout[proc.stdout.index("{") :])
    note = " ".join(plan["notes"])
    assert "1 exact, 2 visual" in note and "0 unreadable" in note, note
    moved = sorted(Path(a["src"]).name for a in plan["actions"])
    assert "d.heic" in moved, f"the HEIC was not read: {moved}"
    return note


# ----- main ----------------------------------------------------------------------------------


def main() -> int:
    if sys.platform != "win32":
        sys.stderr.write("The smoke test runs on Windows only.\n")
        return 2
    missing = [str(p) for p in (GUI_EXE, CLI_EXE) if not p.is_file()]
    if missing:
        sys.stderr.write("Build first (python packaging/build.py). Missing: " + ", ".join(missing))
        sys.stderr.write("\n")
        return 2

    with tempfile.TemporaryDirectory(prefix="various-tools-smoke-") as tmp:
        env = Env(Path(tmp))
        check("GUI starts and stays up (default Qt platform)", lambda: check_gui_default(env))
        check("GUI starts with --tool empty_folders --folder", lambda: check_gui_with_tool(env))
        check("CLI --help exits 0", lambda: check_cli_help(env))
        check("CLI version", lambda: check_cli_version(env))
        check("CLI list shows all 13 tools", lambda: check_cli_list(env))
        check("CLI doctor runs", lambda: check_doctor(env))
        check("empty_folders: scan, apply, undo restores", lambda: check_empty_folders(env))
        check("rename: scan, apply, undo restores names", lambda: check_rename_undo(env))
        check("duplicates: Pillow, HEIF, imagehash, openpyxl", lambda: check_media_imports(env))

    failed = [name for name, ok, _ in _results if not ok]
    sys.stdout.write(f"\n{len(_results) - len(failed)} of {len(_results)} checks passed.\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
