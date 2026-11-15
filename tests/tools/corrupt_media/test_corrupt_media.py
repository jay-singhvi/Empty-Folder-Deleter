"""Corrupt media finder: what counts as corrupt, the plan it builds, and the safety rules.

Every file is generated under ``tmp_path``: noise pictures, a truncated JPEG made by cutting bytes
off a good one, random bytes with a media extension, and zero-byte files. Videos come from ffmpeg's
test pattern and the tests are skipped when ffmpeg or ffprobe is missing. The Recycle Bin is never
used: ``send2trash`` is replaced by a fake that deletes inside the temp folder.
"""

from __future__ import annotations

import ctypes
import os
import random
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageFile
from pydantic import ValidationError

from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.plan import ActionKind, Plan, RunResult
from various_tools.core.settings import Settings
from various_tools.tools.corrupt_media import TOOL, CorruptMediaOptions
from various_tools.tools.corrupt_media import checks as checks_module

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows file attributes")
Maker = Callable[..., Path]


# ----- helpers --------------------------------------------------------------------------------


class _NoPrograms(ExternalTools):
    """Reports ffmpeg and ffprobe as not installed."""

    def ffprobe(self) -> Path | None:
        return None

    def ffmpeg(self) -> Path | None:
        return None


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


@pytest.fixture
def sent_to_trash(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Trash calls are recorded and then carried out inside the temp folder only."""
    calls: list[str] = []

    def fake_send2trash(path: str) -> None:
        calls.append(path)
        os.remove(path)

    monkeypatch.setattr(plan_module, "send2trash", fake_send2trash)
    return calls


def _ctx(
    tmp_path: Path,
    journal: Journal | None = None,
    deps: ExternalTools | None = None,
    **settings: object,
) -> RunContext:
    base: dict[str, object] = {"onedrive_root": tmp_path}
    base.update(settings)
    return RunContext(
        settings=Settings.model_validate(base),
        deps=deps or ExternalTools(ollama_probe=lambda _u: None),
        journal=journal,
    )


def _opts(**values: object) -> CorruptMediaOptions:
    return CorruptMediaOptions.model_validate(values)


def _scan(tmp_path: Path, root: Path, ctx: RunContext | None = None, **values: object) -> Plan:
    return TOOL.scan(_opts(roots=[root], **values), ctx or _ctx(tmp_path))


def _names(plan: Plan, root: Path) -> list[str]:
    return sorted(a.src.relative_to(root).as_posix() for a in plan.actions if a.src is not None)


def _snapshot(root: Path) -> dict[str, tuple[bool, int, int]]:
    return {
        p.relative_to(root).as_posix(): (p.is_dir(), p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


def _truncate(path: Path, keep: float = 0.5) -> Path:
    data = path.read_bytes()
    path.write_bytes(data[: int(len(data) * keep)])
    return path


def _random_file(path: Path, size: int = 4096, seed: int = 7) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(random.Random(seed).randbytes(size))
    return path


def _select_all(plan: Plan) -> None:
    for action in plan.actions:
        action.selected = True


def _set_attributes(path: Path, attributes: int) -> None:
    ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW(str(path), attributes)


@pytest.fixture
def media(make_image: Maker, tmp_path: Path) -> Path:
    """A folder with good pictures, and corrupt ones of each kind."""
    make_image("media/good.jpg", seed=1, size=(160, 120))
    make_image("media/good.png", seed=2)
    make_image("media/sub/good2.jpg", seed=3, size=(160, 120))
    _truncate(make_image("media/cut.jpg", seed=4, size=(160, 120)))
    _random_file(tmp_path / "media" / "noise.jpg")
    (tmp_path / "media" / "empty.png").write_bytes(b"")
    _truncate(make_image("media/sub/cut2.jpg", seed=5, size=(160, 120)), 0.3)
    (tmp_path / "media" / "notes.txt").write_text("not media", encoding="utf-8")
    return tmp_path / "media"


# ----- options --------------------------------------------------------------------------------


def test_defaults() -> None:
    opts = CorruptMediaOptions()
    assert opts.recursive is True and opts.media == "both"
    assert opts.check_depth == "standard" and opts.action == "quarantine"
    assert opts.quarantine_folder == "_quarantine" and opts.include_raw is False
    assert opts.include_hidden is False and opts.require_duration is True
    assert opts.decode_timeout_s is None and opts.workers is None
    assert ".jpg" in opts.image_extensions and ".mp4" in opts.video_extensions


@pytest.mark.parametrize(
    "bad",
    [
        {"check_depth": "paranoid"},
        {"action": "delete"},
        {"media": "audio"},
        {"quarantine_folder": "a/b"},
        {"quarantine_folder": "  "},
        {"quarantine_folder": ".."},
        {"quarantine_folder": "bad."},
        {"image_extensions": []},
        {"image_extensions": ["j pg"]},
        {"image_extensions": [".jpg", ".mp4"], "video_extensions": [".mp4"]},
        {"decode_timeout_s": 0},
        {"workers": 0},
        {"workers": 999},
        {"surprise": 1},
    ],
)
def test_bad_options_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CorruptMediaOptions.model_validate(bad)


def test_extensions_are_normalised() -> None:
    opts = CorruptMediaOptions(image_extensions=["JPG", ".Png"], video_extensions=["MP4"])
    assert opts.image_extensions == [".jpg", ".png"] and opts.video_extensions == [".mp4"]


def test_the_tool_contract() -> None:
    assert TOOL.id == "corrupt_media" and TOOL.category == "Media"
    assert TOOL.Options is CorruptMediaOptions


# ----- scan: pictures -------------------------------------------------------------------------


def test_scan_finds_every_kind_of_corrupt_picture_and_selects_none(
    media: Path, tmp_path: Path
) -> None:
    plan = _scan(tmp_path, media)
    assert _names(plan, media) == ["cut.jpg", "empty.png", "noise.jpg", "sub/cut2.jpg"]
    assert all(a.selected is False for a in plan.actions)
    assert all(a.kind is ActionKind.MOVE for a in plan.actions)
    reasons = {a.src.name: a.reason for a in plan.actions if a.src}
    assert reasons["empty.png"] == "corrupt picture: empty file (0 bytes)"
    assert reasons["noise.jpg"].startswith("corrupt picture: not a readable picture")
    assert "truncated" in reasons["cut.jpg"]
    assert plan.notes[0] == (
        "Checked 7 file(s) (standard): 4 corrupt, 0 skipped (cloud-only), 0 could not be "
        "checked. Tick the files to be quarantined; none is ticked yet."
    )
    assert plan.roots == [media]


def test_scan_changes_nothing(media: Path, tmp_path: Path) -> None:
    before = _snapshot(media)
    _scan(tmp_path, media)
    assert _snapshot(media) == before
    assert not (media / "_quarantine").exists()


def test_quarantine_destinations_keep_the_relative_path(media: Path, tmp_path: Path) -> None:
    plan = _scan(tmp_path, media)
    dsts = {a.src.relative_to(media).as_posix(): a.dst for a in plan.actions if a.src}
    assert dsts["cut.jpg"] == media / "_quarantine" / "cut.jpg"
    assert dsts["sub/cut2.jpg"] == media / "_quarantine" / "sub" / "cut2.jpg"
    other = _scan(tmp_path, media, quarantine_folder="Broken")
    assert {a.dst for a in other.actions if a.dst} >= {media / "Broken" / "cut.jpg"}


def test_depth_quick_does_not_decode_so_a_truncated_jpeg_passes(
    media: Path, tmp_path: Path
) -> None:
    quick = _scan(tmp_path, media, check_depth="quick")
    assert "cut.jpg" not in _names(quick, media)  # verify() alone cannot see the cut
    assert {"noise.jpg", "empty.png"} <= set(_names(quick, media))
    standard = _scan(tmp_path, media, check_depth="standard")
    assert "cut.jpg" in _names(standard, media)


def test_a_truncated_jpeg_is_corrupt_even_if_another_part_allowed_truncated_images(
    media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    plan = _scan(tmp_path, media)
    assert "cut.jpg" in _names(plan, media)
    assert ImageFile.LOAD_TRUNCATED_IMAGES is True  # put back as it was found


def test_a_damaged_png_is_corrupt(make_image: Maker, tmp_path: Path) -> None:
    path = make_image("media/p.png", seed=9, size=(120, 90))
    data = bytearray(path.read_bytes())
    for offset in range(len(data) // 2, len(data) // 2 + 64):
        data[offset] ^= 0xFF
    path.write_bytes(bytes(data))
    plan = _scan(tmp_path, path.parent)
    assert _names(plan, path.parent) == ["p.png"]


def test_good_pictures_of_other_formats_pass(make_image: Maker, tmp_path: Path) -> None:
    for name, fmt in (("a.bmp", None), ("b.gif", None), ("c.webp", None), ("d.tiff", None)):
        make_image(f"media/{name}", seed=11, fmt=fmt)
    plan = _scan(tmp_path, tmp_path / "media")
    assert plan.actions == [] and plan.notes[0].startswith("Checked 4 file(s)")


def test_heic_good_and_truncated(make_image: Maker, tmp_path: Path) -> None:
    good = tmp_path / "media" / "good.heic"
    good.parent.mkdir(parents=True)
    source = Image.new("RGB", (64, 48), (200, 30, 90))
    try:
        source.save(good, format="HEIF")
    except Exception as exc:  # noqa: BLE001 - no HEIF encoder in this environment
        pytest.skip(f"cannot write a HEIC file here: {exc}")
    cut = tmp_path / "media" / "cut.heic"
    cut.write_bytes(good.read_bytes())
    _truncate(cut, 0.5)
    plan = _scan(tmp_path, good.parent)
    assert _names(plan, good.parent) == ["cut.heic"]


def test_raw_files_are_only_checked_when_asked(tmp_path: Path) -> None:
    root = tmp_path / "media"
    _random_file(root / "shot.cr3")
    assert _scan(tmp_path, root).actions == []
    plan = _scan(tmp_path, root, include_raw=True)
    assert _names(plan, root) == ["shot.cr3"]


def test_media_choice_and_extension_lists(media: Path, tmp_path: Path) -> None:
    assert _scan(tmp_path, media, media="videos").actions == []
    only_png = _scan(tmp_path, media, image_extensions=[".png"])
    assert _names(only_png, media) == ["empty.png"]


def test_recursive_off_and_exclude_globs(media: Path, tmp_path: Path) -> None:
    flat = _scan(tmp_path, media, recursive=False)
    assert _names(flat, media) == ["cut.jpg", "empty.png", "noise.jpg"]
    skipped = _scan(tmp_path, media, exclude_globs=["sub", "noise.*"])
    assert _names(skipped, media) == ["cut.jpg", "empty.png"]


def test_hidden_files_follow_the_option(media: Path, tmp_path: Path) -> None:
    _random_file(media / ".hidden.jpg")
    _random_file(media / ".cache" / "x.jpg")
    assert not any(".hidden" in n or ".cache" in n for n in _names(_scan(tmp_path, media), media))
    shown = _names(_scan(tmp_path, media, include_hidden=True), media)
    assert ".hidden.jpg" in shown and ".cache/x.jpg" in shown


def test_the_quarantine_folder_is_never_scanned(media: Path, tmp_path: Path) -> None:
    _random_file(media / "_quarantine" / "already.jpg")
    _random_file(media / "sub" / "_quarantine" / "nested.jpg")
    plan = _scan(tmp_path, media)
    assert not any("_quarantine" in n for n in _names(plan, media))
    # The same name is protected when it contains pattern characters.
    odd = media / "[odd]"
    _random_file(odd / "x.jpg")
    skipped = _scan(tmp_path, media, quarantine_folder="[odd]")
    assert "[odd]/x.jpg" not in _names(skipped, media)


def test_trash_action_plans_trash_with_no_destination(media: Path, tmp_path: Path) -> None:
    plan = _scan(tmp_path, media, action="trash")
    assert len(plan.actions) == 4
    assert all(
        a.kind is ActionKind.TRASH and a.dst is None and not a.selected for a in plan.actions
    )
    assert "sent to the Recycle Bin" in plan.notes[0]


def test_no_folder_and_empty_folder(tmp_path: Path) -> None:
    assert TOOL.scan(_opts(), _ctx(tmp_path)).notes == ["Choose at least one folder."]
    empty = tmp_path / "empty"
    empty.mkdir()
    plan = _scan(tmp_path, empty)
    assert plan.actions == [] and plan.notes[0].startswith("Checked 0 file(s)")


def test_nested_and_repeated_roots_are_scanned_once(media: Path, tmp_path: Path) -> None:
    plan = TOOL.scan(_opts(roots=[media, media / "sub", media]), _ctx(tmp_path))
    assert plan.roots == [media]
    names = _names(plan, media)
    assert len(names) == len(set(names)) == 4


def test_workers_do_not_change_the_result(media: Path, tmp_path: Path) -> None:
    one = _names(_scan(tmp_path, media, workers=1), media)
    many = _names(_scan(tmp_path, media, workers=8), media)
    assert one == many


def test_cancelling_a_scan_raises(media: Path, tmp_path: Path) -> None:
    from various_tools.core.context import Cancelled

    ctx = _ctx(tmp_path)
    ctx.cancel()
    with pytest.raises(Cancelled):
        TOOL.scan(_opts(roots=[media]), ctx)


# ----- safety: roots, cloud-only, junctions ---------------------------------------------------


def test_protected_root_is_refused(
    media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERPROFILE", str(media))
    plan = _scan(tmp_path, media)
    assert plan.actions == [] and plan.roots == []
    assert any("user profile folder" in n for n in plan.notes)


def test_protected_folders_come_from_the_settings(media: Path, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, protected_folders=[str(media)])
    plan = _scan(tmp_path, media, ctx=ctx)
    assert plan.actions == [] and any("Protected folder refused" in n for n in plan.notes)


def test_a_missing_folder_is_refused(tmp_path: Path) -> None:
    plan = _scan(tmp_path, tmp_path / "nope")
    assert plan.actions == [] and any("Not a folder" in n for n in plan.notes)


@pytest.fixture
def watch_image_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """File names that Pillow was asked to open."""
    real_open = Image.open
    seen: list[str] = []

    def spy(fp: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(Path(str(fp)).name)
        return real_open(fp, *args, **kwargs)

    monkeypatch.setattr(Image, "open", spy)
    return seen


@windows_only
def test_cloud_only_files_are_skipped_and_never_opened(
    media: Path, tmp_path: Path, watch_image_opens: list[str]
) -> None:
    cloud = media / "noise.jpg"
    _set_attributes(cloud, 0x1000)  # OFFLINE: the content is not on this device
    try:
        plan = _scan(tmp_path, media)
    finally:
        _set_attributes(cloud, 0x80)
    assert "noise.jpg" not in _names(plan, media)
    assert "skipped: cloud-only: noise.jpg" in plan.notes
    assert "1 skipped (cloud-only)" in plan.notes[0]
    assert plan.cloud_only == [cloud]  # for the download action, once
    assert "noise.jpg" not in watch_image_opens
    assert "good.jpg" in watch_image_opens  # the other files were checked


def test_a_file_that_became_cloud_only_after_listing_is_skipped_not_opened(
    media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watch_image_opens: list[str]
) -> None:
    from various_tools.core import onedrive

    real = onedrive.get_attributes

    def attributes(path: Path) -> int | None:
        if path.name == "noise.jpg":
            return onedrive.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        return real(path)

    monkeypatch.setattr(onedrive, "get_attributes", attributes)
    plan = _scan(tmp_path, media)
    assert "noise.jpg" not in _names(plan, media) and "noise.jpg" not in watch_image_opens
    assert any(n.startswith("could not check: noise.jpg (cloud-only)") for n in plan.notes)
    assert "1 could not be checked" in plan.notes[0]


def test_a_onedrive_placeholder_that_shows_the_reparse_bit_is_not_a_link(
    media: Path, tmp_path: Path, cloud_placeholder: Callable[..., None]
) -> None:
    cloud_placeholder("noise.jpg", "sub")  # a placeholder file and a placeholder folder
    plan = _scan(tmp_path, media)
    assert _names(plan, media) == ["cut.jpg", "empty.png", "noise.jpg", "sub/cut2.jpg"]
    assert plan.cloud_only == []  # the content is on the device, so it was really checked


def test_junctions_are_not_followed(media: Path, tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are a Windows feature")
    outside = tmp_path / "outside"
    _random_file(outside / "elsewhere.jpg")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(media / "link"), str(outside)],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("this account cannot create junctions")
    try:
        plan = _scan(tmp_path, media)
        assert not any("elsewhere" in n for n in _names(plan, media))
    finally:
        os.rmdir(media / "link")  # removes the junction only, never its target
    assert (outside / "elsewhere.jpg").exists()


def test_quarantine_through_a_junction_is_refused_at_apply(
    media: Path, tmp_path: Path, journal: Journal
) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are a Windows feature")
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = _ctx(tmp_path, journal)
    plan = _scan(tmp_path, media, ctx=ctx)
    _select_all(plan)
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(media / "_quarantine"), str(outside)],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("this account cannot create junctions")
    try:
        result = TOOL.apply(plan, ctx)
        assert result.count("done") == 0 and result.count("skipped") == 4
        assert all("junction or symlink" in o.detail for o in result.outcomes)
        assert list(outside.rglob("*")) == []
        assert (media / "cut.jpg").exists()
    finally:
        os.rmdir(media / "_quarantine")


# ----- apply ----------------------------------------------------------------------------------


def test_nothing_happens_when_nothing_is_ticked(
    media: Path, tmp_path: Path, journal: Journal
) -> None:
    ctx = _ctx(tmp_path, journal)
    plan = _scan(tmp_path, media, ctx=ctx)
    before = _snapshot(media)
    result = TOOL.apply(plan, ctx)
    assert result.outcomes == [] and _snapshot(media) == before


def test_quarantine_moves_the_ticked_files_and_undo_restores_them(
    media: Path, tmp_path: Path, journal: Journal
) -> None:
    ctx = _ctx(tmp_path, journal)
    plan = _scan(tmp_path, media, ctx=ctx)
    chosen = [a for a in plan.actions if a.src and a.src.name in ("cut.jpg", "cut2.jpg")]
    for action in chosen:
        action.selected = True
    before = _snapshot(media)
    result: RunResult = TOOL.apply(plan, ctx)
    assert result.status == "done" and result.count("done") == 2

    assert not (media / "cut.jpg").exists()
    assert (media / "_quarantine" / "cut.jpg").exists()
    assert (media / "_quarantine" / "sub" / "cut2.jpg").exists()
    assert (media / "noise.jpg").exists()  # not ticked, not touched
    assert [a.kind for a in journal.actions(result.run_id)] == ["move", "move"]

    # A second scan does not look in the quarantine folder.
    again = _scan(tmp_path, media, ctx=ctx)
    assert _names(again, media) == ["empty.png", "noise.jpg"]

    report = undo_run(journal, result.run_id)
    assert report.reverted == 2 and not report.skipped and not report.failed
    # Files are back with the same size and modified time (folder times legitimately changed).
    restored = {k: v for k, v in _snapshot(media).items() if not v[0] and not k.startswith("_q")}
    assert restored == {k: v for k, v in before.items() if not v[0]}


def test_trash_goes_through_send2trash_only_for_ticked_files(
    media: Path, tmp_path: Path, journal: Journal, sent_to_trash: list[str]
) -> None:
    ctx = _ctx(tmp_path, journal)
    plan = _scan(tmp_path, media, ctx=ctx, action="trash")
    target = next(a for a in plan.actions if a.src and a.src.name == "noise.jpg")
    target.selected = True
    row_size = (media / "noise.jpg").stat().st_size  # the engine records it for undo
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1
    assert sent_to_trash == [str(media / "noise.jpg")]
    assert not (media / "noise.jpg").exists() and (media / "cut.jpg").exists()
    row = journal.actions(result.run_id)[0]
    assert (row.kind, row.status, row.item_size) == ("trash", "done", row_size)
    # Undo looks in the Recycle Bin; no bin of this user exists in tests, so the file is listed.
    report = undo_run(journal, result.run_id)
    assert report.reverted == 0 and report.failed == ()
    assert any("restore manually from the Recycle Bin" in s for s in report.skipped)


def test_apply_never_moves_a_file_outside_the_selected_folder(
    media: Path, tmp_path: Path, journal: Journal
) -> None:
    from various_tools.core.plan import PlannedAction

    stray = _random_file(tmp_path / "stray.jpg")
    ctx = _ctx(tmp_path, journal)
    plan = _scan(tmp_path, media, ctx=ctx)
    plan.actions = [
        PlannedAction(
            kind=ActionKind.MOVE, src=stray, dst=media / "_quarantine" / "stray.jpg", selected=True
        ),
        PlannedAction(kind=ActionKind.TRASH, src=media, selected=True),
    ]
    result = TOOL.apply(plan, ctx)
    assert result.count("skipped") == 2 and result.count("done") == 0
    assert stray.exists() and media.exists()


def test_name_collisions_in_the_quarantine_get_a_new_name(
    media: Path, tmp_path: Path, journal: Journal
) -> None:
    _random_file(media / "_quarantine" / "cut.jpg", seed=99)
    ctx = _ctx(tmp_path, journal)
    plan = _scan(tmp_path, media, ctx=ctx)
    action = next(a for a in plan.actions if a.src and a.src.name == "cut.jpg")
    action.selected = True
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1
    assert (media / "_quarantine" / "cut.jpg").exists()
    assert (media / "_quarantine" / "cut_dup1.jpg").exists()


# ----- could not be checked -------------------------------------------------------------------


def test_an_image_too_large_for_pillows_limit_is_skipped_not_called_corrupt(
    make_image: Maker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_image("media/big.png", seed=3)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    plan = _scan(tmp_path, path.parent)
    assert plan.actions == []
    assert any(
        "could not check: big.png (larger than the image safety limit" in n for n in plan.notes
    )


def test_a_permission_error_is_a_note_not_a_corrupt_file(
    make_image: Maker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_image("media/locked.png", seed=3)
    real_open = Image.open

    def deny(fp: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(str(fp)).name == "locked.png":
            raise PermissionError(13, "Permission denied")
        return real_open(fp, *args, **kwargs)

    monkeypatch.setattr(Image, "open", deny)
    plan = _scan(tmp_path, path.parent)
    assert plan.actions == []
    assert any(
        "could not check: locked.png (could not be read: Permission denied)" in n
        for n in plan.notes
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MemoryError(), "not enough memory"),
        (OSError(22, "Invalid argument"), "Invalid argument"),
        (OSError(None, "Insufficient system resources", None, 1450), "Insufficient system"),
        (OSError(None, "The specified network name is no longer available", None, 64), "network"),
        (OSError(None, "The process cannot access the file", None, 32), "cannot access"),
        (TimeoutError(110, "Connection timed out"), "timed out"),
    ],
    ids=["memory", "errno", "winerror-1450", "network", "sharing-violation", "timeout"],
)
def test_system_errors_mean_could_not_check_never_corrupt(
    make_image: Maker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected: str,
) -> None:
    path = make_image("media/fine.png", seed=3)
    real_open = Image.open

    def failing(fp: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(str(fp)).name == "fine.png":
            raise error
        return real_open(fp, *args, **kwargs)

    monkeypatch.setattr(Image, "open", failing)
    verdict = checks_module.check_image(path, "standard")
    assert verdict.status == "skipped" and expected in verdict.detail, verdict
    plan = _scan(tmp_path, path.parent)
    assert plan.actions == []
    assert any(n.startswith("could not check: fine.png") for n in plan.notes), plan.notes


@pytest.mark.parametrize(
    "error",
    [OSError("image file is truncated"), OSError("broken data stream"), SyntaxError("bad")],
    ids=["truncated", "broken-stream", "syntax"],
)
def test_pillow_decode_errors_without_an_errno_stay_corrupt(
    make_image: Maker, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    path = make_image("media/bad.png", seed=3)

    def failing(fp: Any, *args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(Image, "open", failing)
    assert checks_module.check_image(path, "standard").status == "corrupt"


# ----- videos ---------------------------------------------------------------------------------


@pytest.fixture
def clips(make_video: Maker, tmp_path: Path) -> Path:
    """A folder with one good video and several kinds of bad ones."""
    (tmp_path / "clips").mkdir()  # make_video does not create folders
    make_video("clips/good.mp4", seconds=2)
    _truncate(make_video("clips/cut.mp4", seconds=2), 0.4)  # the index at the end is lost
    _random_file(tmp_path / "clips" / "noise.mp4", size=8192)
    (tmp_path / "clips" / "empty.mkv").write_bytes(b"")
    return tmp_path / "clips"


def test_videos_good_truncated_random_and_empty(clips: Path, tmp_path: Path) -> None:
    plan = _scan(tmp_path, clips)
    assert _names(plan, clips) == ["cut.mp4", "empty.mkv", "noise.mp4"]
    reasons = {a.src.name: a.reason for a in plan.actions if a.src}
    assert reasons["empty.mkv"] == "corrupt video: empty file (0 bytes)"
    assert reasons["cut.mp4"].startswith("corrupt video: ffprobe cannot read the video")
    assert reasons["noise.mp4"].startswith("corrupt video: ffprobe cannot read the video")
    assert all(not a.selected for a in plan.actions)
    assert "Checked 4 file(s)" in plan.notes[0]


def test_a_video_with_no_video_stream_is_corrupt(
    clips: Path, tmp_path: Path, ffmpeg_path: Path
) -> None:
    audio_only = clips / "audio.mp4"
    subprocess.run(
        [
            str(ffmpeg_path),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=1",
            str(audio_only),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    plan = _scan(tmp_path, clips)
    reason = next(a.reason for a in plan.actions if a.src and a.src.name == "audio.mp4")
    assert reason == "corrupt video: the file has no video stream"


def test_without_ffprobe_videos_are_skipped_with_one_note(clips: Path, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, deps=_NoPrograms(ollama_probe=lambda _u: None))
    plan = _scan(tmp_path, clips, ctx=ctx)
    # Only the zero-byte file can be judged without ffprobe.
    assert _names(plan, clips) == ["empty.mkv"]
    assert any(
        "Videos are not checked: ffprobe is needed" in n and "winget install" in n
        for n in plan.notes
    )
    assert not any(n.startswith("could not check:") for n in plan.notes)


def test_deep_check_finds_damage_that_ffprobe_does_not(make_video: Maker, tmp_path: Path) -> None:
    (tmp_path / "deep").mkdir()
    path = make_video("deep/damaged.mp4", seconds=3, size="320x240")
    data = bytearray(path.read_bytes())
    start = len(data) // 4  # the middle of the picture data; the index at the end stays intact
    rng = random.Random(5)
    data[start : start + 4000] = rng.randbytes(4000)
    path.write_bytes(bytes(data))
    standard = _scan(tmp_path, path.parent, check_depth="standard")
    assert standard.actions == []  # the container is readable
    deep = _scan(tmp_path, path.parent, check_depth="deep")
    assert _names(deep, path.parent) == ["damaged.mp4"]
    assert deep.actions[0].reason.startswith("corrupt video: the video does not decode cleanly")


def test_a_good_video_passes_the_deep_check(make_video: Maker, tmp_path: Path) -> None:
    (tmp_path / "deep").mkdir()
    path = make_video("deep/ok.mp4", seconds=2)
    plan = _scan(tmp_path, path.parent, check_depth="deep")
    assert plan.actions == [] and plan.notes[0].startswith("Checked 1 file(s) (deep)")


def test_a_video_is_never_probed_when_cloud_only(
    clips: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        pytest.skip("needs Windows file attributes")
    probed: list[str] = []
    real = checks_module.run_tool

    def spy(args: list[str], timeout: float = 60) -> Any:
        probed.append(Path(args[-1]).name)
        return real(args, timeout=timeout)

    monkeypatch.setattr(checks_module, "run_tool", spy)
    target = clips / "good.mp4"
    _set_attributes(target, 0x1000)
    try:
        plan = _scan(tmp_path, clips, check_depth="deep")
    finally:
        _set_attributes(target, 0x80)
    assert "good.mp4" not in probed
    assert "skipped: cloud-only: good.mp4" in plan.notes
    assert plan.cloud_only == [target]


# ----- the checks on their own (ffprobe output faked) -----------------------------------------


def _fake_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str, code: int = 0
) -> Path:
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x" * 100)

    def run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, code, stdout, "boom: details" if code else "")

    monkeypatch.setattr(checks_module, "run_tool", run)
    return path


@pytest.mark.parametrize(
    ("stdout", "require", "status", "detail"),
    [
        ('{"streams":[{"codec_type":"video"}],"format":{"duration":"2.5"}}', True, "ok", ""),
        ('{"streams":[{"codec_type":"video","duration":"1.0"}],"format":{}}', True, "ok", ""),
        (
            '{"streams":[{"codec_type":"video"}],"format":{"duration":"0.000000"}}',
            True,
            "corrupt",
            "zero duration",
        ),
        (
            '{"streams":[{"codec_type":"video"}],"format":{"duration":"N/A"}}',
            True,
            "corrupt",
            "no duration",
        ),
        ('{"streams":[{"codec_type":"video"}],"format":{"duration":"N/A"}}', False, "ok", ""),
        (
            '{"streams":[{"codec_type":"audio"}],"format":{"duration":"2"}}',
            True,
            "corrupt",
            "no video stream",
        ),
        ("{}", True, "corrupt", "no video stream"),
        ("not json", True, "corrupt", "no usable details"),
    ],
)
def test_probe_verdicts(
    stdout: str,
    require: bool,
    status: str,
    detail: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _fake_probe(monkeypatch, tmp_path, stdout)
    verdict = checks_module.check_video_probe(
        Path("ffprobe"), path, timeout=5, require_duration=require
    )
    assert verdict.status == status and detail in verdict.detail


def test_probe_failure_and_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _fake_probe(monkeypatch, tmp_path, "", code=1)
    failed = checks_module.check_video_probe(
        Path("ffprobe"), path, timeout=5, require_duration=True
    )
    assert failed.status == "corrupt" and "boom: details" in failed.detail

    def slow(args: list[str], timeout: float = 60) -> Any:
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(checks_module, "run_tool", slow)
    timed_out = checks_module.check_video_probe(
        Path("ffprobe"), path, timeout=5, require_duration=True
    )
    assert timed_out.status == "skipped" and "longer than 5 s" in timed_out.detail

    def missing(args: list[str], timeout: float = 60) -> Any:
        raise FileNotFoundError("no such program")

    monkeypatch.setattr(checks_module, "run_tool", missing)
    gone = checks_module.check_video_probe(Path("ffprobe"), path, timeout=5, require_duration=True)
    assert gone.status == "skipped" and "could not run" in gone.detail


def test_decode_timeout_comes_from_the_setting_unless_the_option_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []

    def probe(*args: Any, **kwargs: Any) -> checks_module.Verdict:
        return checks_module.OK

    def decode(ffmpeg: Path, path: Path, *, timeout: float) -> checks_module.Verdict:
        seen.append(timeout)
        return checks_module.OK

    monkeypatch.setattr(checks_module, "check_video_probe", probe)
    monkeypatch.setattr(checks_module, "check_video_decode", decode)
    root = tmp_path / "v"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"x" * 10)
    deps = ExternalTools(ollama_probe=lambda _u: None)
    if deps.ffprobe() is None or deps.ffmpeg() is None:
        pytest.skip("ffmpeg is not installed")
    TOOL.scan(_opts(roots=[root], check_depth="deep"), _ctx(tmp_path, transcode_timeout_s=123))
    TOOL.scan(
        _opts(roots=[root], check_depth="deep", decode_timeout_s=45),
        _ctx(tmp_path, transcode_timeout_s=123),
    )
    assert seen == [123, 45]


def test_a_junction_as_the_selected_folder_is_refused(media: Path, tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are a Windows feature")
    link = tmp_path / "linkroot"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(media)], capture_output=True, check=False
    )
    if made.returncode != 0:
        pytest.skip("this account cannot create junctions")
    try:
        plan = _scan(tmp_path, link)
        assert plan.actions == [] and plan.roots == []
    finally:
        os.rmdir(link)  # removes the junction only, never its target
    assert (media / "_quarantine").exists() is False
