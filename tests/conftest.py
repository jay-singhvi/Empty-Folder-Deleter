"""Shared test setup. Qt runs offscreen so GUI tests need no display.

Every file a test touches is generated under pytest's ``tmp_path``. Nothing here reads a real
user folder. Tests that need ffmpeg or ExifTool are skipped when those programs are not installed.
"""

from __future__ import annotations

import os
import random
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from various_tools.core.deps import ExternalTools  # noqa: E402

ImageMaker = Callable[..., Path]


@pytest.fixture(autouse=True)
def _never_the_real_recycle_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No test may reach the real Recycle Bin, even one that forgets to fake it.

    Apply and undo send items to a throwaway folder instead, and undo finds no real bin to
    restore from. Tests that check trashing install their own fakes on top of this.
    """
    import shutil
    import uuid

    from various_tools.core import journal, plan, recycle

    def fake_send2trash(path: object) -> None:
        bin_folder = tmp_path_factory.mktemp("recycle-bin")
        source = Path(str(path))
        if not os.path.lexists(source):
            raise OSError(f"cannot send a missing item to the Recycle Bin: {source}")
        shutil.move(str(source), str(bin_folder / f"{uuid.uuid4().hex}-{source.name}"))

    monkeypatch.setattr(plan, "send2trash", fake_send2trash)
    monkeypatch.setattr(journal, "send2trash", fake_send2trash)
    monkeypatch.setattr(recycle, "current_user_bin", lambda _path: None)


@pytest.fixture
def make_image(tmp_path: Path) -> ImageMaker:
    """Write a seeded noise image (distinct per seed) and return its path.

    ``make_image(name, seed=1, size=(96, 64), fmt=None)`` writes ``tmp_path/name``. The image format
    is taken from the file name unless ``fmt`` is given.
    """

    def _make(
        name: str, seed: int = 1, size: tuple[int, int] = (96, 64), fmt: str | None = None
    ) -> Path:
        rng = random.Random(seed)
        image = Image.new("RGB", size)
        pixels = [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(size[0] * size[1])
        ]
        image.putdata(pixels)
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, format=fmt)
        return target

    return _make


@pytest.fixture
def fake_exe(tmp_path: Path) -> Callable[..., Path]:
    """A stand-in executable: a ``.bat`` file that prints fixed text whatever it is given.

    Windows runs ``.bat`` files directly, and ``PATHEXT`` lets them be found by name, so these
    behave like real programs for the version probe without any real tool being installed.
    """

    def _make(folder: Path, name: str, output: str = "fake 1.0") -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{name}.bat"
        target.write_text(f"@echo off\r\necho {output}\r\n", encoding="ascii")
        return target

    return _make


@pytest.fixture(scope="session")
def _external() -> ExternalTools:
    return ExternalTools()


@pytest.fixture(scope="session")
def ffmpeg_path(_external: ExternalTools) -> Path:
    path = _external.path("ffmpeg")
    if path is None:
        pytest.skip("ffmpeg is not installed; install it with `winget install Gyan.FFmpeg`")
    return path


@pytest.fixture(scope="session")
def ffprobe_path(_external: ExternalTools) -> Path:
    path = _external.path("ffprobe")
    if path is None:
        pytest.skip("ffprobe is not installed; install it with `winget install Gyan.FFmpeg`")
    return path


@pytest.fixture(scope="session")
def exiftool_path(_external: ExternalTools) -> Path:
    path = _external.path("exiftool")
    if path is None:
        pytest.skip(
            "ExifTool is not installed; install it with `winget install OliverBetz.ExifTool`"
        )
    return path


@pytest.fixture
def make_video(tmp_path: Path, ffmpeg_path: Path) -> Callable[..., Path]:
    """Generate a short test video with ffmpeg's test pattern, optionally tagged.

    ``creation_time`` (ISO 8601) is written as a container tag. ``rotate`` (degrees) sets the
    display matrix, which is how phones record orientation. A ``rotate`` metadata tag is not
    kept by the mp4 muxer, so the rotated copy is made by re-muxing with ``-display_rotation``.
    """

    def _run(args: list[str]) -> None:
        subprocess.run(
            [str(ffmpeg_path), "-v", "error", "-y", *args],
            check=True,
            capture_output=True,
            timeout=120,
        )

    def _make(
        name: str = "clip.mp4",
        seconds: float = 2.0,
        size: str = "160x120",
        creation_time: str | None = None,
        rotate: int | None = None,
    ) -> Path:
        target = tmp_path / name
        generated = target if rotate is None else target.with_name(f"plain-{name}")
        args = [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=10",
            "-t",
            str(seconds),
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
        ]
        if creation_time:
            args += ["-metadata", f"creation_time={creation_time}"]
        _run([*args, str(generated)])
        if rotate is not None:
            _run(
                [
                    "-display_rotation:v:0",
                    str(rotate),
                    "-i",
                    str(generated),
                    "-c",
                    "copy",
                    str(target),
                ]
            )
        return target

    return _make
