"""Media resize: replacing the originals (``output="overwrite"``) and removing RAW originals.

Both send an original to the Recycle Bin, so both are journaled as their own ``trash`` rows and
undo can restore them. The Recycle Bin is a temp folder. There is no real CR3 sample, so the RAW
decode and the ExifTool metadata copy are faked. Every file is generated under ``tmp_path``.
"""

from __future__ import annotations

import io
import os
import struct
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from various_tools.core import onedrive
from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.settings import Settings
from various_tools.tools.media_resize import TOOL, MediaResizeOptions
from various_tools.tools.media_resize import images as images_module
from various_tools.tools.media_resize import tool as resize_tool
from various_tools.tools.media_resize.geometry import Target
from various_tools.tools.media_resize.images import ImageError, Rendered

_EPOCH_AS_FILETIME = 116444736000000000


class FakeBin:
    """Stands in for the Recycle Bin: trashing writes a Windows-style ``$I``/``$R`` pair."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.trashed: list[Path] = []

    def trash(self, path: Path | str) -> None:
        path = Path(path)
        self.folder.mkdir(parents=True, exist_ok=True)
        content = path.read_bytes()
        key = uuid.uuid4().hex[:6].upper()
        (self.folder / f"$R{key}{path.suffix}").write_bytes(content)
        text = str(path) + "\x00"
        when = int(datetime.now(UTC).timestamp() * 10_000_000) + _EPOCH_AS_FILETIME
        header = struct.pack("<qqqi", 2, len(content), when, len(text))
        (self.folder / f"$I{key}{path.suffix}").write_bytes(header + text.encode("utf-16-le"))
        path.unlink()
        self.trashed.append(path)

    def locate(self, _path: Path) -> Path | None:
        return self.folder


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBin:
    bin_ = FakeBin(tmp_path / "fake-bin")
    monkeypatch.setattr(plan_module, "send2trash", bin_.trash)
    return bin_


class _ExifToolDeps(ExternalTools):
    def exiftool(self) -> Path | None:
        return Path("fake-exiftool")


class _NoExifToolDeps(ExternalTools):
    def exiftool(self) -> Path | None:
        return None


class _VideoDeps(ExternalTools):
    def ffmpeg(self) -> Path | None:
        return Path("fake-ffmpeg")

    def ffprobe(self) -> Path | None:
        return Path("fake-ffprobe")


def _ctx(tmp_path: Path, journal: Journal | None, deps: ExternalTools | None = None) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}),
        deps=deps or ExternalTools(ollama_probe=lambda _u: None),
        journal=journal,
    )


def _image(path: Path, size: tuple[int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (size[0] % 256, size[1] % 256, 120)).save(path)
    return path


def _overwrite(folder: Path, **kwargs: Any) -> MediaResizeOptions:
    return MediaResizeOptions(roots=[folder], output="overwrite", allow_overwrite=True, **kwargs)


def _leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# ----- output: overwrite ----------------------------------------------------------------------


def test_overwrite_is_offered_but_needs_the_advanced_switch(tmp_path: Path) -> None:
    photo = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    plan = TOOL.scan(
        MediaResizeOptions(roots=[photo.parent], output="overwrite"), _ctx(tmp_path, None)
    )
    assert plan.actions == []
    assert any("Allow replacing the originals" in note for note in plan.notes)
    fields = MediaResizeOptions.model_fields
    switch = fields["allow_overwrite"].json_schema_extra
    assert isinstance(switch, dict) and switch.get("advanced") is True
    assert "Recycle Bin" in (fields["allow_overwrite"].description or "")
    assert "Recycle Bin" in (fields["output"].description or "")
    assert MediaResizeOptions(roots=[]).allow_overwrite is False


def test_overwrite_replaces_the_original_and_undo_restores_it(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin
) -> None:
    photo = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    os.utime(photo, (1_700_000_000, 1_700_000_000))
    original = photo.read_bytes()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_overwrite(photo.parent), ctx)
    (action,) = plan.actions
    assert action.selected and action.dst == photo
    assert "Recycle Bin" in action.reason
    assert photo.read_bytes() == original  # the scan changes nothing

    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1, result.outcomes
    with Image.open(photo) as out:
        assert out.size == (1920, 1080)
    assert int(photo.stat().st_mtime) == 1_700_000_000
    assert fake_bin.trashed == [photo]
    assert [(r.kind, r.status) for r in journal.actions(result.run_id)] == [
        ("convert", "done"),
        ("trash", "done"),
    ]
    assert _leftovers(photo.parent) == ["a.jpg"]  # no temp file left behind

    report = undo_run(
        journal, result.run_id, send_to_trash=fake_bin.trash, locate_bin=fake_bin.locate
    )
    assert report.reverted == 2, (report.skipped, report.failed)
    assert photo.read_bytes() == original


def _wrong_size_render(real: Any) -> Any:
    def render(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        if isinstance(result, Rendered):
            return Rendered(result.source_size, Target(7, 7), result.fmt, result.data, False)
        return result

    return render


def test_overwrite_keeps_the_original_when_the_check_fails(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    original = photo.read_bytes()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_overwrite(photo.parent), ctx)
    monkeypatch.setattr(resize_tool.images, "render", _wrong_size_render(images_module.render))
    result = TOOL.apply(plan, ctx)
    assert [o.status for o in result.outcomes] == ["failed"]
    assert "wrong size" in result.outcomes[0].detail
    assert photo.read_bytes() == original and fake_bin.trashed == []
    assert _leftovers(photo.parent) == ["a.jpg"]


def test_overwrite_keeps_the_original_when_the_recycle_bin_fails(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    original = photo.read_bytes()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_overwrite(photo.parent), ctx)

    def broken_bin(_path: object) -> None:
        raise OSError("the Recycle Bin is full")

    monkeypatch.setattr(plan_module, "send2trash", broken_bin)
    result = TOOL.apply(plan, ctx)
    assert [o.status for o in result.outcomes] == ["failed"]
    assert "Recycle Bin" in result.outcomes[0].detail
    assert photo.read_bytes() == original
    assert _leftovers(photo.parent) == ["a.jpg"]


def test_overwrite_rename_failure_keeps_the_copy_and_says_where_it_is(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_overwrite(photo.parent), ctx)

    def no_rename(_source: Path, _destination: Path) -> None:
        raise PermissionError("the file is open in another program")

    monkeypatch.setattr(images_module, "rename_no_replace", no_rename)
    result = TOOL.apply(plan, ctx)
    assert [o.status for o in result.outcomes] == ["failed"]
    detail = result.outcomes[0].detail
    assert "Recycle Bin" in detail
    (copy,) = [p for p in photo.parent.iterdir()]
    assert copy.name in detail  # the resized copy is kept, and the message names it
    assert fake_bin.trashed == [photo]


def test_overwrite_never_reads_a_file_that_became_cloud_only(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    original = photo.read_bytes()
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_overwrite(photo.parent), ctx)
    real_attributes = onedrive.get_attributes
    monkeypatch.setattr(
        onedrive,
        "get_attributes",
        lambda p: (
            onedrive.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
            if p.name == "a.jpg"
            else real_attributes(p)
        ),
    )
    monkeypatch.setattr(images_module, "open_image", lambda *_a, **_k: pytest.fail("read"))
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 0 and fake_bin.trashed == []
    monkeypatch.setattr(onedrive, "get_attributes", real_attributes)
    assert photo.read_bytes() == original


def test_overwrite_video_replaces_the_original_through_the_bin(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "clips"
    folder.mkdir()
    clip = folder / "clip.mp4"
    clip.write_bytes(b"x" * 1000)

    def display_size(_ffprobe: Path, path: Path, *, timeout: float) -> tuple[int, int]:
        return (3840, 2160) if path == clip else (1920, 1080)

    def transcode(_ffmpeg: Path, _src: Path, dst: Path, _args: list[str], **_kw: Any) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "xb") as handle:
            handle.write(b"small")

    monkeypatch.setattr(resize_tool.ffmpeg_media, "display_size", display_size)
    monkeypatch.setattr(resize_tool.videos.ffmpeg_media, "transcode", transcode)
    ctx = _ctx(tmp_path, journal, _VideoDeps(ollama_probe=lambda _u: None))
    plan = TOOL.scan(_overwrite(folder, media="videos"), ctx)
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1, result.outcomes
    assert clip.read_bytes() == b"small"
    assert fake_bin.trashed == [clip]
    assert _leftovers(folder) == ["clip.mp4"]


# ----- RAW originals ----------------------------------------------------------------------------


def _jpeg_bytes(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def fake_raw(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """RAW decoding and the ExifTool metadata copy are faked; they record their calls."""
    state: dict[str, Any] = {"copies": [], "fail_copy": None}

    def render(source: Path, out_suffix: str, rules: Any, raw_to_jpeg: bool) -> Rendered | str:
        onedrive.ensure_on_device(source)
        if not raw_to_jpeg:
            return "RAW file, and RAW conversion is off"
        return Rendered((6000, 4000), Target(1620, 1080), "JPEG", _jpeg_bytes((1620, 1080)), True)

    def copy_metadata(_exiftool: Path, source: Path, target: Path, _size: Any, _t: float) -> None:
        if state["fail_copy"]:
            raise ImageError(state["fail_copy"])
        state["copies"].append(source.name)

    monkeypatch.setattr(resize_tool.images, "render", render)
    monkeypatch.setattr(images_module, "_copy_raw_metadata", copy_metadata)
    return state


def _raw(tmp_path: Path) -> Path:
    raw = tmp_path / "raw" / "IMG_1.CR3"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"pretend this is a Canon RAW file")
    return raw


def _raw_opts(raw: Path, **kwargs: Any) -> MediaResizeOptions:
    values: dict[str, Any] = {
        "roots": [raw.parent],
        "mode": "shorter_side",
        "convert_raw_to_jpeg": True,
        "remove_raw_originals": True,
    }
    values.update(kwargs)
    return MediaResizeOptions(**values)


def test_raw_original_goes_to_the_bin_only_after_the_verified_jpeg_and_its_metadata(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin, fake_raw: dict[str, Any]
) -> None:
    raw = _raw(tmp_path)
    ctx = _ctx(tmp_path, journal, _ExifToolDeps(ollama_probe=lambda _u: None))
    plan = TOOL.scan(_raw_opts(raw), ctx)
    (action,) = plan.actions
    assert "RAW original goes to the Recycle Bin" in action.reason
    assert raw.exists()  # the scan changes nothing

    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1, result.outcomes
    jpeg = raw.parent / "resized" / "IMG_1.jpg"
    with Image.open(jpeg) as out:
        assert out.size == (1620, 1080)
    assert fake_raw["copies"] == ["IMG_1.CR3"]
    assert fake_bin.trashed == [raw] and not raw.exists()
    assert [r.kind for r in journal.actions(result.run_id)] == ["convert", "trash"]

    report = undo_run(
        journal, result.run_id, send_to_trash=fake_bin.trash, locate_bin=fake_bin.locate
    )
    assert report.reverted == 2, (report.skipped, report.failed)
    assert raw.exists() and not jpeg.exists()


def test_raw_is_kept_when_the_metadata_copy_fails(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin, fake_raw: dict[str, Any]
) -> None:
    raw = _raw(tmp_path)
    ctx = _ctx(tmp_path, journal, _ExifToolDeps(ollama_probe=lambda _u: None))
    plan = TOOL.scan(_raw_opts(raw), ctx)
    fake_raw["fail_copy"] = "ExifTool could not copy the metadata: boom"
    result = TOOL.apply(plan, ctx)
    assert [o.status for o in result.outcomes] == ["failed"]
    assert "metadata" in result.outcomes[0].detail and "RAW" in result.outcomes[0].detail
    assert raw.exists() and fake_bin.trashed == []
    assert not (raw.parent / "resized" / "IMG_1.jpg").exists()


def test_raw_is_kept_when_exiftool_is_missing_at_apply(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin, fake_raw: dict[str, Any]
) -> None:
    raw = _raw(tmp_path)
    with_exiftool = _ExifToolDeps(ollama_probe=lambda _u: None)
    plan = TOOL.scan(_raw_opts(raw), _ctx(tmp_path, journal, with_exiftool))
    result = TOOL.apply(
        plan, _ctx(tmp_path, journal, _NoExifToolDeps(ollama_probe=lambda _u: None))
    )
    assert [o.status for o in result.outcomes] == ["failed"]
    assert "ExifTool" in result.outcomes[0].detail
    assert raw.exists() and fake_bin.trashed == []


def test_raw_is_kept_when_metadata_is_not_copied(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin, fake_raw: dict[str, Any]
) -> None:
    raw = _raw(tmp_path)
    ctx = _ctx(tmp_path, journal, _ExifToolDeps(ollama_probe=lambda _u: None))
    plan = TOOL.scan(_raw_opts(raw, keep_metadata=False), ctx)
    (action,) = plan.actions
    assert "RAW original kept" in action.reason and "metadata" in action.reason
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1
    assert raw.exists() and fake_bin.trashed == [] and fake_raw["copies"] == []


def test_raw_originals_are_kept_by_default(
    tmp_path: Path, journal: Journal, fake_bin: FakeBin, fake_raw: dict[str, Any]
) -> None:
    raw = _raw(tmp_path)
    assert MediaResizeOptions(roots=[]).remove_raw_originals is False
    ctx = _ctx(tmp_path, journal, _ExifToolDeps(ollama_probe=lambda _u: None))
    plan = TOOL.scan(_raw_opts(raw, remove_raw_originals=False), ctx)
    assert TOOL.apply(plan, ctx).count("done") == 1
    assert raw.exists() and fake_bin.trashed == []
    hints = MediaResizeOptions.model_fields["remove_raw_originals"].json_schema_extra
    assert isinstance(hints, dict) and hints.get("advanced") is True


# ----- plan.cloud_only --------------------------------------------------------------------------


def test_skipped_cloud_only_files_are_listed_in_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = _image(tmp_path / "pics" / "a.jpg", (2560, 1440))
    monkeypatch.setattr(resize_tool, "_cloud_only", lambda info: True)
    plan = TOOL.scan(MediaResizeOptions(roots=[photo.parent]), _ctx(tmp_path, None))
    assert plan.cloud_only == [photo]
