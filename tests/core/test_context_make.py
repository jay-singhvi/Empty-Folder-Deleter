"""make_deps / make_context: saved paths, one-off overrides, timeouts, journal lifetime."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from various_tools.core import deps as deps_module
from various_tools.core.context import make_context, make_deps
from various_tools.core.settings import Settings


def test_saved_paths_are_passed_as_overrides() -> None:
    deps = make_deps(Settings(external_tools={"exiftool": "C:\\saved\\exiftool.exe"}))
    assert deps._overrides == {"exiftool": "C:\\saved\\exiftool.exe"}  # pyright: ignore[reportPrivateUsage]


def test_one_off_wins_and_empty_one_off_is_ignored() -> None:
    settings = Settings(external_tools={"ffmpeg": "saved-ffmpeg", "exiftool": "saved-exif"})
    deps = make_deps(
        settings,
        {"ffmpeg": "one-off-ffmpeg", "exiftool": "", "ffprobe": None, "ollama": "http://x"},
    )
    overrides = deps._overrides  # pyright: ignore[reportPrivateUsage]
    assert overrides["ffmpeg"] == "one-off-ffmpeg"
    assert overrides["exiftool"] == "saved-exif"
    assert "ffprobe" not in overrides
    assert overrides["ollama"] == "http://x"
    # One-off values are never written back to the settings object.
    assert settings.external_tools == {"ffmpeg": "saved-ffmpeg", "exiftool": "saved-exif"}


def test_version_timeout_reaches_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    def fake_run_tool(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
        seen.append(timeout)
        return subprocess.CompletedProcess(args, 0, "ExifTool 12.0\n", "")

    monkeypatch.setattr(deps_module, "run_tool", fake_run_tool)
    exe = tmp_path / "exiftool.exe"
    exe.write_text("x", encoding="utf-8")
    deps = make_deps(Settings(version_check_timeout_s=7.5), {"exiftool": str(exe)})
    deps.resolve("exiftool")
    assert seen and set(seen) == {7.5}


def test_ollama_timeout_reaches_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    def fake_urlopen(url: str, timeout: float = 0) -> Any:
        seen.append(timeout)
        raise OSError("unreachable")

    monkeypatch.setattr(deps_module.urllib.request, "urlopen", fake_urlopen)
    deps = make_deps(Settings(ollama_probe_timeout_s=3.5), {"ollama": "http://localhost:1"})
    deps.resolve_ollama()
    assert seen
    assert set(seen) == {3.5}


def test_make_context_opens_a_journal_under_the_data_folder(tmp_path: Path) -> None:
    settings = Settings(data_folder=tmp_path / "data")
    ctx = make_context(settings)
    try:
        assert ctx.journal is not None
        assert settings.journal_path().is_file()
        assert settings.journal_path().parent == tmp_path / "data"
    finally:
        ctx.close()
    assert ctx.journal is None
    ctx.close()  # idempotent


def test_make_context_without_journal_creates_nothing(tmp_path: Path) -> None:
    settings = Settings(data_folder=tmp_path / "data")
    ctx = make_context(settings, with_journal=False)
    assert ctx.journal is None
    assert not (tmp_path / "data").exists()
    ctx.close()


def test_make_context_wires_progress_and_cancel(tmp_path: Path) -> None:
    event = threading.Event()
    seen: list[tuple[int, int, str]] = []
    ctx = make_context(
        Settings(data_folder=tmp_path),
        with_journal=False,
        progress=lambda d, t, m: seen.append((d, t, m)),
        cancel_event=event,
    )
    ctx.report(1, 2, "x")
    ctx.cancel()
    assert seen == [(1, 2, "x")] and event.is_set()
