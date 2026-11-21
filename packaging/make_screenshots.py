"""Make the README screenshots: ``uv run python packaging/make_screenshots.py``.

Builds the real widgets offscreen on generated demo folders and saves PNGs (about 1200 px wide,
light theme) into ``docs/images/``:

* ``main-window-empty-folders.png``: the main window on Empty Folder Deleter with a scanned plan
  in the folder tree view;
* ``duplicates-compare.png``: the duplicates compare view on generated pictures;
* ``disk-usage-size-map.png``: Disk Usage with its size map;
* ``history.png``: History after two applied runs;
* ``settings-external-programs.png``: Settings, scrolled to the external programs;
* ``home.png``: the Home page with its tool cards;
* ``tool-page-sort-by-type.png``: a tool page (Sort by type) with a scanned plan;
* ``timeline.png``: the Timeline of a generated photo library;
* ``map.png``: the Map of the same library (the built-in offline map, no network).

No personal data can appear:

* everything lives in a demo folder at the drive root (``C:\\VariousTools-Demo``), created here
  and deleted at the end, so no shown path holds the Windows user name. The script refuses to
  run if that folder exists without its marker file, so it never deletes anything of yours;
* a temporary settings file and journal inside that folder; your settings are never read;
* the external programs panel shows fixed, made-up locations and versions (a fake lookup), and
  the Ollama model list is a fixed fake list, so nothing on this PC is probed;
* applying a plan "trashes" with a stand-in that deletes only inside the demo folder: nothing
  reaches the Recycle Bin.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# The offscreen platform finds no fonts on Windows by itself; point it at the system fonts.
_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
if _FONTS.is_dir():
    os.environ.setdefault("QT_QPA_FONTDIR", str(_FONTS))

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
DEMO = Path(os.environ.get("SYSTEMDRIVE", "C:") + os.sep) / "VariousTools-Demo"
MARKER = ".various-tools-demo"
WIDTH, HEIGHT = 1200, 820

FAKE_PROGRAMS: dict[str, tuple[str, str]] = {
    "ffmpeg": (r"C:\Program Files\ffmpeg\bin\ffmpeg.exe", "7.1"),
    "ffprobe": (r"C:\Program Files\ffmpeg\bin\ffprobe.exe", "7.1"),
    "exiftool": (r"C:\Program Files\ExifTool\exiftool.exe", "13.10"),
}


# ----- the demo folder ----------------------------------------------------------------------------


def make_demo_folder() -> None:
    if DEMO.exists():
        if not (DEMO / MARKER).is_file():
            sys.exit(f"{DEMO} exists and was not made by this script; refusing to touch it.")
        shutil.rmtree(DEMO)
    try:
        DEMO.mkdir()
    except OSError as exc:
        sys.exit(f"Cannot create {DEMO} ({exc}). Run from an account that can write there.")
    (DEMO / MARKER).write_text("made by packaging/make_screenshots.py; safe to delete\n")


def remove_demo_folder() -> None:
    if DEMO.exists() and (DEMO / MARKER).is_file():
        shutil.rmtree(DEMO, ignore_errors=True)


def write(path: Path, size: int = 0, text: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text, encoding="utf-8")
        return
    with path.open("wb") as handle:
        handle.truncate(size)  # quick to make, and the size is what the tools see


def make_trees() -> dict[str, Path]:
    files = DEMO / "Shared Files"
    for folder in (
        "Old Projects/2019/drafts",
        "Old Projects/2019/exports/empty",
        "Old Projects/2020",
        "Camera Uploads/Empty Album",
        "Downloads/unzipped/temp",
        "Music/Playlists",
    ):
        (files / folder).mkdir(parents=True, exist_ok=True)
    write(files / "Old Projects" / "2019" / "drafts" / "desktop.ini", text="[.ShellClassInfo]\n")
    write(files / "Camera Uploads" / "Empty Album" / "Thumbs.db", 2048)
    write(files / "Music" / "Playlists" / "road trip.m3u", text="song.mp3\n")
    write(files / "Old Projects" / "2020" / "report.docx", 24_000)

    usage = DEMO / "Disk Space"
    mb = 1024 * 1024
    for rel, size in (
        ("Videos/Holiday 2023.mp4", 38 * mb),
        ("Videos/Birthday.mov", 22 * mb),
        ("Videos/Clips/clip 01.mp4", 9 * mb),
        ("Videos/Clips/clip 02.mp4", 7 * mb),
        ("Photos/2024/IMG_0001.jpg", 6 * mb),
        ("Photos/2024/IMG_0002.jpg", 5 * mb),
        ("Photos/2023/IMG_0420.jpg", 8 * mb),
        ("Photos/2023/Panorama.tif", 12 * mb),
        ("Music/Album/track 01.flac", 9 * mb),
        ("Music/Album/track 02.flac", 8 * mb),
        ("Music/Podcast.mp3", 4 * mb),
        ("Downloads/installer.msi", 15 * mb),
        ("Downloads/archive.zip", 11 * mb),
        ("Documents/Manual.pdf", 3 * mb),
        ("Documents/Budget.xlsx", 1 * mb),
        ("Projects/site/assets/video.webm", 6 * mb),
        ("Projects/site/build.zip", 4 * mb),
        ("notes.txt", 200_000),
    ):
        write(usage / rel, size)

    sorting = DEMO / "To Sort"
    for name, size in (
        ("Invoice March.pdf", 80_000),
        ("Holiday.jpg", 900_000),
        ("Song.mp3", 3_000_000),
        ("Setup.exe", 2_000_000),
        ("Slides.pptx", 400_000),
    ):
        write(sorting / name, size)

    photos = DEMO / "Photos"
    photos.mkdir()
    make_photos(photos)
    library = DEMO / "Library"
    make_library(library)
    return {
        "empty": files,
        "usage": usage,
        "sort": sorting,
        "photos": photos,
        "library": library,
    }


# Made-up trips for the Timeline and Map: (folder, "YYYY:MM:DD", latitude, longitude, count).
TRIPS: tuple[tuple[str, str, float, float, int], ...] = (
    ("Lisbon", "2023:04:14", 38.72, -9.14, 6),
    ("Kyoto", "2023:11:02", 35.01, 135.77, 7),
    ("Cape Town", "2024:02:18", -33.92, 18.42, 5),
    ("New York", "2024:06:09", 40.71, -74.0, 6),
    ("Reykjavik", "2024:09:21", 64.15, -21.94, 5),
    ("Home", "2025:01:05", 47.61, -122.33, 4),
)


def make_library(folder: Path) -> None:
    """Generated pictures with a taken date and a place, so the Timeline and Map have content."""
    from PIL import Image

    exif_ifd, gps_ifd = 0x8769, 0x8825

    def dms(value: float) -> tuple[float, float, float]:
        value = abs(value)
        degrees = int(value)
        minutes = int((value - degrees) * 60)
        return float(degrees), float(minutes), round((value - degrees - minutes / 60) * 3600, 2)

    for trip, (name, date, lat, lon, count) in enumerate(TRIPS):
        year, month, day = (int(part) for part in date.split(":"))
        for i in range(count):
            data = Image.Exif()
            data.get_ifd(exif_ifd)[0x9003] = f"{year}:{month:02d}:{day + i % 3:02d} 1{i % 10}:20:00"
            gps = data.get_ifd(gps_ifd)
            gps[1], gps[2] = "N" if lat >= 0 else "S", dms(lat + i * 0.003)
            gps[3], gps[4] = "E" if lon >= 0 else "W", dms(lon + i * 0.003)
            picture = make_scene(trip * 7 + i, (480, 360))
            target = folder / name / f"{name} {i + 1:02d}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            picture.save(target, "JPEG", quality=88, exif=data.tobytes())


def make_scene(seed: int, size: tuple[int, int] = (1600, 1200)) -> Any:
    """A generated landscape picture; the seed changes its colours."""
    from PIL import Image, ImageDraw, ImageFilter

    image = Image.new("RGB", size, (120 + seed * 7 % 60, 170, 220))
    draw = ImageDraw.Draw(image)
    w, h = size
    draw.rectangle((0, int(h * 0.62), w, h), fill=(70, 140 - seed % 30, 70))
    draw.ellipse((int(w * 0.68), int(h * 0.1), int(w * 0.86), int(h * 0.34)), fill=(250, 220, 90))
    for i in range(6):
        x = int(w * (0.08 + 0.15 * i))
        draw.polygon(
            [
                (x, int(h * 0.62)),
                (x + int(w * 0.07), int(h * 0.3)),
                (x + int(w * 0.14), int(h * 0.62)),
            ],
            fill=(90 + 10 * i, 110, 130 + seed % 100),
        )
    return image.filter(ImageFilter.GaussianBlur(1.2))


def make_photos(folder: Path) -> None:
    """A generated picture, an exact copy and a smaller re-save, plus an unrelated picture."""
    from PIL import Image

    original = make_scene(1)
    original.save(folder / "Lake view.jpg", quality=92)
    shutil.copyfile(folder / "Lake view.jpg", folder / "Lake view (copy).jpg")
    original.resize((800, 600)).save(folder / "Lake view small.jpg", quality=85)
    make_scene(9).transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(folder / "Hills.jpg", quality=90)


# ----- fakes --------------------------------------------------------------------------------------


class FakeDeps:
    """Fixed program locations, so the screenshot shows no real paths or versions."""

    def resolve(self, tool: str) -> Any:
        from various_tools.core.deps import ToolLocation, ToolSource

        path, version = FAKE_PROGRAMS[tool]
        return ToolLocation(tool, Path(path), None, version, ToolSource.KNOWN_LOCATION, True)

    def resolve_ollama(self, preferred_url: str | None = None) -> Any:
        from various_tools.core.deps import ToolLocation, ToolSource

        url = "http://localhost:11434"
        return ToolLocation("ollama", None, url, "0.12.6", ToolSource.DEFAULT, True)

    def redetect(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:  # anything else a page might ask for
        raise AttributeError(name)


def install_fakes() -> None:
    from various_tools.core import choices
    from various_tools.core import plan as plan_module

    def demo_only_trash(path: object) -> None:
        target = Path(str(path))
        if not target.is_relative_to(DEMO):
            raise RuntimeError(f"refusing to remove {target}: outside the demo folder")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    plan_module.send2trash = demo_only_trash  # type: ignore[attr-defined]
    choices.register_source("ollama_models", lambda _ctx: ["lfm3-vl:latest", "llava:7b"])


# ----- driving the widgets ------------------------------------------------------------------------


def settle(app: Any, seconds: float = 0.3) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait(app: Any, condition: Callable[[], bool], timeout: float = 60.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            raise TimeoutError("the window did not finish in time")
        app.processEvents()
        time.sleep(0.02)


def quiet(page: Any) -> None:
    page.confirm = lambda _title, _text: True
    page.inform = lambda _title, _text: None


def run_page(app: Any, page: Any, *, apply: bool = False) -> None:
    quiet(page)
    assert page.scan() is not None, "scan did not start"
    wait(app, lambda: page.worker is None)
    if apply:
        assert page.apply() is not None, "apply did not start"
        wait(app, lambda: page.worker is None)


def save(widget: Any, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / name
    if not widget.grab().save(str(target), "PNG"):
        raise OSError(f"could not save {target}")
    print(f"saved {target.relative_to(ROOT)}")
    return target


def main() -> int:
    make_demo_folder()
    try:
        return shoot()
    finally:
        remove_demo_folder()


def fill_library(state: Any) -> None:
    """Read the generated library into the catalog (in the demo folder) before the pages open."""
    from various_tools.core.context import make_context
    from various_tools.core.media.library import MediaLibrary, library_path

    ctx = make_context(state.settings, with_journal=False)
    try:
        with MediaLibrary(library_path(state.settings)) as lib:
            lib.sync(state.settings.library_folders, ctx)
    finally:
        ctx.close()


def shoot() -> int:
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from various_tools.gui import app as gui_app
    from various_tools.gui import workers
    from various_tools.gui.main_window import HISTORY_ID, HOME_ID, MAP_ID, SETTINGS_ID, TIMELINE_ID
    from various_tools.gui.shell import FakeRegistry
    from various_tools.gui.state import AppState
    from various_tools.gui.theme import apply_theme
    from various_tools.gui.widgets import compare_view

    trees = make_trees()
    install_fakes()
    config = DEMO / "config" / "settings.toml"
    config.parent.mkdir()
    config.write_text(
        "\n".join(
            [
                f'data_folder = "{(DEMO / "data").as_posix()}"',
                f'cache_folder = "{(DEMO / "cache").as_posix()}"',
                f'onedrive_root = "{(DEMO / "OneDrive").as_posix()}"',
                f'library_folders = ["{trees["library"].as_posix()}"]',
                'theme = "light"',
                'ollama_url = "http://localhost:11434"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(sys.argv[:1])
    apply_theme(app, "light")
    app.setFont(QFont("Segoe UI", 9))  # what the app shows on Windows; offscreen has no default
    state = AppState.load(config)
    state.deps = FakeDeps()  # type: ignore[assignment]
    fill_library(state)
    window = gui_app.make_window(state, shell_backend=FakeRegistry())
    quiet(window)
    window.resize(WIDTH, HEIGHT)
    window.show()
    settle(app)

    # 1. Empty folders, scanned, in the folder tree.
    page = window.open_tool("empty_folders", str(trees["empty"]))
    assert page is not None
    run_page(app, page)
    page.table.view.expandAll()
    settle(app)
    save(window, "main-window-empty-folders.png")
    run_page(app, page, apply=True)  # for History (the demo-only stand-in "trashes")

    # 2. A second run for History: sort by type moves the demo files into folders.
    page = window.open_tool("sort_by_type", str(trees["sort"]))
    assert page is not None
    run_page(app, page, apply=True)

    # 3. Disk usage with the size map.
    page = window.open_tool("disk_usage", str(trees["usage"]))
    assert page is not None
    page.form.update_values({"min_size_mb": 0, "tree_min_share_pct": 0.5})
    run_page(app, page)
    if page.size_map is not None:
        page.results_tabs.setCurrentWidget(page.size_map)
    settle(app, 0.5)
    save(window, "disk-usage-size-map.png")

    # 4. The duplicates compare view.
    page = window.open_tool("duplicates", str(trees["photos"]))
    assert page is not None
    page.form.update_values({"media": "images"})
    run_page(app, page)
    assert page.plan is not None and page.plan.actions, "the duplicate scan found nothing"
    group = next(a.group_id for a in page.plan.actions if a.group_id)
    dialog = page.open_compare(group)
    assert dialog is not None
    dialog.resize(WIDTH, 700)
    settle(app, 0.5)
    wait(app, lambda: not any(w.isRunning() for w in compare_view._LIVE))  # pyright: ignore[reportPrivateUsage]
    settle(app, 0.5)
    save(dialog, "duplicates-compare.png")
    dialog.close()

    # 5. History.
    window.show_page(HISTORY_ID)
    wait(app, lambda: not window.history_page.loading)
    window.history_page.select_run(1)  # the empty-folder run, with its actions below
    settle(app, 0.3)
    wait(app, lambda: not window.history_page.loading)
    settle(app, 0.5)
    save(window, "history.png")

    # 6. Home, a tool page, the Timeline and the Map.
    window.show_page(HOME_ID)
    settle(app, 0.5)
    save(window, "home.png")

    page = window.open_tool("sort_by_type", str(trees["sort"]))
    assert page is not None
    run_page(app, page)
    settle(app, 0.5)
    save(window, "tool-page-sort-by-type.png")

    timeline = window.show_page(TIMELINE_ID)
    wait(app, lambda: not workers.active_workers(), timeout=60)
    settle(app, 1.5)  # thumbnails load in the background
    save(window, "timeline.png")

    window.show_page(MAP_ID)
    settle(app, 1.5)
    save(window, "map.png")
    del timeline

    # 7. Settings, at the external programs.
    window.show_page(SETTINGS_ID)
    settle(app, 0.3)
    panel = window.settings_page.tools_panel
    wait(app, lambda: not panel._refreshing)  # pyright: ignore[reportPrivateUsage]
    from PySide6.QtWidgets import QScrollArea

    scroll = window.settings_page.findChild(QScrollArea)
    if scroll is not None:
        scroll.ensureWidgetVisible(panel, 0, 0)
        bar = scroll.verticalScrollBar()
        bar.setValue(min(bar.maximum(), panel.y()))
    settle(app, 0.5)
    save(window, "settings-external-programs.png")

    wait(app, lambda: not workers.active_workers(), timeout=30)
    window.close()
    settle(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
