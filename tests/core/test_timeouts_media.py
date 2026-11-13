"""Custom timeouts, retries and step-back reach the subprocess helpers (no real ffmpeg)."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from various_tools.core.media import ffmpeg


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "v.mp4"
    path.write_bytes(b"not really a video")
    return path


def test_ffprobe_json_passes_its_timeout(monkeypatch: pytest.MonkeyPatch, video: Path) -> None:
    seen: list[float] = []

    def fake(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
        seen.append(timeout)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(ffmpeg, "run_tool", fake)
    ffmpeg.ffprobe_json(Path("ffprobe"), video, timeout=4.25)
    with pytest.raises(ffmpeg.MediaError):  # "{}" has no duration; the timeout is what we check
        ffmpeg.probe_duration(Path("ffprobe"), video, timeout=5.5)
    assert seen == [4.25, 5.5]


def test_ffprobe_timeout_becomes_media_error(monkeypatch: pytest.MonkeyPatch, video: Path) -> None:
    def fake(args: list[str], timeout: float = 60) -> Any:
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(ffmpeg, "run_tool", fake)
    with pytest.raises(ffmpeg.MediaError, match="timed out"):
        ffmpeg.ffprobe_json(Path("ffprobe"), video, timeout=1)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, "PNG")
    return buf.getvalue()


def test_grab_frame_steps_back_and_honours_retries(
    monkeypatch: pytest.MonkeyPatch, video: Path
) -> None:
    calls: list[tuple[str, float]] = []

    def fake(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
        calls.append((args[args.index("-ss") + 1], timeout))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(ffmpeg, "run_binary", fake)
    with pytest.raises(ffmpeg.MediaError, match="no video frame"):
        ffmpeg.grab_frame(Path("ffmpeg"), video, 10.0, timeout=9.0, retries=4, step_back=0.5)
    assert calls == [("10.000", 9.0), ("5.000", 9.0), ("2.500", 9.0), ("1.250", 9.0)]


def test_grab_frame_retries_below_one_still_tries_once(
    monkeypatch: pytest.MonkeyPatch, video: Path
) -> None:
    calls: list[str] = []

    def fake(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
        calls.append(args[args.index("-ss") + 1])
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(ffmpeg, "run_binary", fake)
    with pytest.raises(ffmpeg.MediaError):
        ffmpeg.grab_frame(Path("ffmpeg"), video, 3.0, retries=0)
    assert calls == ["3.000"]


def test_grab_frame_returns_the_first_non_empty_frame(
    monkeypatch: pytest.MonkeyPatch, video: Path
) -> None:
    outputs = [b"", _png()]
    calls: list[str] = []

    def fake(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
        calls.append(args[args.index("-ss") + 1])
        return subprocess.CompletedProcess(args, 0, outputs.pop(0), b"")

    monkeypatch.setattr(ffmpeg, "run_binary", fake)
    image = ffmpeg.grab_frame(Path("ffmpeg"), video, 8.0, retries=5, step_back=0.25)
    assert image.size == (4, 4) and image.mode == "RGB"
    assert calls == ["8.000", "2.000"]


def test_grab_frame_timeout_becomes_media_error(
    monkeypatch: pytest.MonkeyPatch, video: Path
) -> None:
    def fake(args: list[str], timeout: float = 60) -> Any:
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(ffmpeg, "run_binary", fake)
    with pytest.raises(ffmpeg.MediaError, match="timed out"):
        ffmpeg.grab_frame(Path("ffmpeg"), video, 1.0, timeout=0.1)


def test_transcode_passes_its_timeout_and_leaves_no_partial_file(
    monkeypatch: pytest.MonkeyPatch, video: Path, tmp_path: Path
) -> None:
    seen: list[float] = []

    def fake(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
        seen.append(timeout)
        return subprocess.CompletedProcess(args, 1, "", "boom")

    monkeypatch.setattr(ffmpeg, "run_tool", fake)
    out = tmp_path / "out" / "o.mp4"
    with pytest.raises(ffmpeg.MediaError, match="failed"):
        ffmpeg.transcode(Path("ffmpeg"), video, out, ["-c:v", "libx264"], timeout=77.0)
    assert seen == [77.0]
    assert list(out.parent.iterdir()) == []


def test_transcode_success_renames_into_place(
    monkeypatch: pytest.MonkeyPatch, video: Path, tmp_path: Path
) -> None:
    def fake(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
        Path(args[-1]).write_bytes(b"encoded")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(ffmpeg, "run_tool", fake)
    out = tmp_path / "o.mp4"
    ffmpeg.transcode(Path("ffmpeg"), video, out, [], timeout=1.0)
    assert out.read_bytes() == b"encoded"
