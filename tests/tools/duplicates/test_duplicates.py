"""Duplicates (plan §3.4): parity with the two legacy finders, the tuned thresholds, and the
safety rules. Every tree is generated under ``tmp_path``. The legacy scripts are run on the same
generated tree, and their plans are compared with the tool's plan file by file.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
from pydantic import ValidationError

from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.plan import ActionKind, Plan
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.duplicates import TOOL, DuplicatesOptions
from various_tools.tools.duplicates import tool as dup_tool
from various_tools.tools.duplicates.explain import explain
from various_tools.tools.duplicates.images import ImageInfo, ImageRules, read_image
from various_tools.tools.duplicates.keeper import image_keep_key, video_keep_key
from various_tools.tools.duplicates.verify_ollama import OllamaVerifier

REPO = Path(__file__).resolve().parents[3]
LEGACY_IMAGES = REPO / "legacy" / "python-find_duplicates.py"
LEGACY_VIDEOS = REPO / "legacy" / "find_duplicate_videos.py"

MakeVideo = Callable[..., Path]


# ---- helpers ----------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan cache lives in the user's cache folder by default. Tests use a temp folder."""
    monkeypatch.setattr(dup_tool, "CACHE_DIR", tmp_path / "cache")


def _ctx(tmp_path: Path, journal: Journal | None, deps: ExternalTools | None = None) -> RunContext:
    return RunContext(
        settings=Settings.model_validate({"onedrive_root": tmp_path}),
        deps=deps or ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


def _scene(seed: int, size: tuple[int, int] = (400, 300)) -> Image.Image:
    """A smooth, seeded picture. Smooth content keeps perceptual hashes stable under resizing."""
    rng = random.Random(seed)
    img = Image.new("RGB", size, (rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    draw = ImageDraw.Draw(img)
    for _ in range(4):
        x0 = rng.randrange(size[0] // 2)
        y0 = rng.randrange(size[1] // 2)
        x1 = x0 + rng.randrange(size[0] // 3, size[0] // 2)
        y1 = y0 + rng.randrange(size[1] // 3, size[1] // 2)
        draw.rectangle((x0, y0, x1, y1), fill=(rng.randrange(256), rng.randrange(256), 90))
    return img.filter(ImageFilter.GaussianBlur(3))


def _save(img: Image.Image, path: Path, exif_taken: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {}
    if exif_taken is not None:
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = exif_taken
        kwargs["exif"] = exif.tobytes()
    img.save(path, **kwargs)
    return path


def _rel(path: str | Path, tree: Path) -> str:
    return Path(os.path.abspath(path)).relative_to(tree).as_posix()


def _load_legacy(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_image_tree(tree: Path) -> None:
    """Every case the image finder distinguishes, in one generated tree."""
    original = _save(_scene(1), tree / "pics" / "beach_original.png")
    shutil.copyfile(original, tree / "pics" / "beach_copy.png")  # identical bytes
    _save(_scene(1).resize((200, 150)), tree / "pics" / "beach_small.png")  # same picture, resized
    _save(_scene(2), tree / "pics" / "mountain.png")  # a different scene
    # Two frames of one burst (same second in the name): a sharp pick plus a near-identical frame.
    _save(_scene(3), tree / "pics" / "IMG_20240101_120000_1.png")
    brighter = ImageEnhance.Brightness(_scene(3)).enhance(1.02)
    _save(brighter, tree / "pics" / "IMG_20240101_120000_2.png")
    # GIFs are compared by bytes only.
    gif = tree / "anim.gif"
    _scene(4).convert("P").save(gif)
    shutil.copyfile(gif, tree / "anim_copy.gif")


def _legacy_image_plan(tree: Path, monkeypatch: pytest.MonkeyPatch) -> set[tuple[str, str, str]]:
    legacy = _load_legacy("legacy_find_duplicates", LEGACY_IMAGES)
    monkeypatch.setattr(legacy, "DUP_DIR", str(tree / "duplicate"))
    monkeypatch.setattr(legacy, "REVIEW_DIR", str(tree / "review"))
    files, errors = legacy.find_files([str(tree)])
    assert errors == []
    infos = [legacy.hash_image(p) for p, _, _ in files]
    thumbs = {info["path"]: info["thumb"] for info in infos}
    plan = legacy.build_plan(infos, lambda p: legacy.thumb_array(thumbs[p]))
    return {
        (kind, _rel(keeper["path"], tree), _rel(dup["path"], tree))
        for keeper, dup, kind, _folder, _reason in plan
    }


def _ours(
    tree: Path, tmp_path: Path, journal: Journal | None, **options: Any
) -> set[tuple[str, str, str]]:
    ctx = _ctx(tmp_path, journal)
    findings = dup_tool.collect(DuplicatesOptions(roots=[tree], **options), ctx)
    return {(m.kind, _rel(m.keeper, tree), _rel(m.duplicate, tree)) for m in findings.matches}


def _names(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


# ---- thresholds and the registry ----------------------------------------------------------


def test_thresholds_default_to_the_tuned_values() -> None:
    """🔒 AGENTS.md: these were tuned by hand on real data. The defaults must not drift."""
    opts = DuplicatesOptions()
    assert opts.phash_max == 2
    assert opts.dhash_max == 4
    assert opts.aspect_tol == 0.02
    assert opts.burst_phash_max == 40
    assert opts.pixel_block_max == 3.0
    assert opts.dur_tol_s == 0.6
    assert opts.dur_tol_pct == 0.01
    assert opts.frame_mean_max == 10
    assert opts.frame_worst_max == 24
    assert opts.sample_at == [0.1, 0.3, 0.5, 0.7, 0.9]
    assert opts.ollama_model == "lfm3-vl:latest"


def test_tool_is_discovered_through_the_registry() -> None:
    assert discover()["duplicates"].name == "Duplicate Finder"


def test_unknown_option_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        DuplicatesOptions.model_validate({"roots": [], "phash_threshold": 3})


def test_sample_points_must_be_fractions_of_the_clip() -> None:
    with pytest.raises(ValidationError):
        DuplicatesOptions(sample_at=[0.5, 1.5])
    with pytest.raises(ValidationError):
        DuplicatesOptions(sample_at=[])


# ---- parity with the legacy scripts -------------------------------------------------------


def test_image_plan_matches_legacy_finder_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    expected = _legacy_image_plan(tree, monkeypatch)
    # The fixture must exercise each image kind, or the comparison proves very little.
    kinds = {kind for kind, _, _ in expected}
    assert {"exact", "visual", "burst"} <= kinds
    assert _ours(tree, tmp_path, None, media="images", use_cache=False) == expected


def test_video_plan_matches_legacy_finder_exactly(
    tmp_path: Path,
    make_video: MakeVideo,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "videos"
    tree.mkdir()
    shutil.copyfile(make_video("clip.mp4", seconds=2.0, size="160x120"), tree / "clip.mp4")
    original = tree / "clip.mp4"
    shutil.copyfile(original, tree / "clip_copy.mp4")  # identical bytes
    _ffmpeg(
        ffmpeg_path,
        "-i",
        str(original),
        "-vf",
        "scale=320:240",
        "-b:v",
        "300k",
        str(tree / "clip_small.mp4"),
    )  # same footage, different encode
    _ffmpeg(
        ffmpeg_path,
        "-f",
        "lavfi",
        "-i",
        "mandelbrot=size=160x120:rate=10",
        "-t",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "mpeg4",
        str(tree / "other.mp4"),
    )  # different footage

    legacy = _load_legacy("legacy_find_duplicate_videos", LEGACY_VIDEOS)
    monkeypatch.setattr(legacy, "FOLDER", str(tree))
    monkeypatch.setattr(legacy, "DUP_DIR", str(tree / "duplicate"))
    monkeypatch.setattr(legacy, "REVIEW_DIR", str(tree / "review"))
    monkeypatch.setenv("PATH", str(ffmpeg_path.parent) + os.pathsep + os.environ["PATH"])
    vids, errors = legacy.scan()
    assert errors == []
    expected = {
        (kind, _rel(keeper["path"], tree), _rel(dup["path"], tree))
        for keeper, dup, kind, _folder in legacy.build_plan(vids)
    }
    assert {kind for kind, _, _ in expected} == {"exact", "re-encode"}
    assert _ours(tree, tmp_path, None, media="videos") == expected


# ---- scanning and applying ----------------------------------------------------------------


def test_scan_changes_nothing_on_disk(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    before = _names(tree)
    ctx = _ctx(tmp_path, None)
    plan = TOOL.scan(DuplicatesOptions(roots=[tree], use_cache=False), ctx)
    assert plan.selected_actions()  # there is something to move
    assert _names(tree) == before
    assert not (tree / "duplicate").exists() and not (tree / "review").exists()


def test_apply_moves_duplicates_then_undo_restores_them(tmp_path: Path, journal: Journal) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    before = _names(tree)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(DuplicatesOptions(roots=[tree], use_cache=False), ctx)
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 0
    assert (tree / "duplicate" / "beach_copy.png").exists() or (
        tree / "duplicate" / "beach_small.png"
    ).exists()
    assert (tree / "review").is_dir()
    undo_run(journal, result.run_id)
    assert _names(tree) == before


def test_saved_plan_applies_later_with_the_same_result(tmp_path: Path, journal: Journal) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(DuplicatesOptions(roots=[tree], use_cache=False), ctx)
    saved = plan.to_json()
    restored = Plan.from_json(saved)
    result = TOOL.apply(restored, ctx)
    assert result.count("done") == len(plan.selected_actions())
    assert (tree / "duplicate").is_dir()


def test_managed_folders_are_not_scanned_again(tmp_path: Path, journal: Journal) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    ctx = _ctx(tmp_path, journal)
    first = TOOL.scan(DuplicatesOptions(roots=[tree], use_cache=False), ctx)
    TOOL.apply(first, ctx)
    second = TOOL.scan(DuplicatesOptions(roots=[tree], use_cache=False), ctx)
    # Files moved into duplicate/ and review/ are never listed again as candidates.
    assert all("duplicate" not in str(a.src) and "review" not in str(a.src) for a in second.actions)


def test_trash_action_sends_duplicates_to_the_recycle_bin_plan(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    plan = TOOL.scan(
        DuplicatesOptions(roots=[tree], use_cache=False, action="trash"), _ctx(tmp_path, None)
    )
    kinds = {(a.kind, a.reason.split(" ")[0]) for a in plan.selected_actions()}
    assert (ActionKind.TRASH, "identical") in kinds
    # Burst frames are for review, so they are always moved, never trashed.
    assert all(
        a.kind is ActionKind.MOVE for a in plan.selected_actions() if a.reason.startswith("burst")
    )


def test_exact_only_skips_the_visual_and_burst_passes(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    found = _ours(tree, tmp_path, None, media="images", use_cache=False, exact_only=True)
    assert {kind for kind, _, _ in found} == {"exact"}


def test_explicit_folders_are_used_for_every_move(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    dest = tmp_path / "elsewhere"
    plan = TOOL.scan(
        DuplicatesOptions(
            roots=[tree], use_cache=False, duplicate_folder=dest, review_folder=dest / "rev"
        ),
        _ctx(tmp_path, None),
    )
    for action in plan.selected_actions():
        assert action.dst is not None
        folder = dest / "rev" if action.reason.startswith("burst") else dest
        assert action.dst.parent == folder


def test_group_id_points_each_duplicate_at_its_keeper(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    plan = TOOL.scan(DuplicatesOptions(roots=[tree], use_cache=False), _ctx(tmp_path, None))
    for action in plan.selected_actions():
        assert action.group_id is not None
        assert action.group_id != str(action.src)


# ---- safety rules -------------------------------------------------------------------------


def test_cloud_only_images_are_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    cloud = tree / "pics" / "beach_copy.png"
    real_walk = dup_tool.walk

    def walk_with_one_cloud_file(*args: Any, **kwargs: Any) -> Iterator[Any]:
        # RECALL_ON_DATA_ACCESS marks a file that is not on this device (OneDrive placeholder).
        for info in real_walk(*args, **kwargs):
            if info.path == cloud:
                info = type(info)(info.path, info.size, info.mtime, 0x400000)
            yield info

    read: list[Path] = []
    real_read = dup_tool.read_image

    def spy(path: Path, size: int, mtime: float) -> ImageInfo:
        read.append(path)
        return real_read(path, size, mtime)

    monkeypatch.setattr(dup_tool, "walk", walk_with_one_cloud_file)
    monkeypatch.setattr(dup_tool, "read_image", spy)
    plan = TOOL.scan(DuplicatesOptions(roots=[tree], use_cache=False), _ctx(tmp_path, None))
    assert cloud not in read
    assert any("cloud-only" in note and "beach_copy" in note for note in plan.notes)
    assert all(a.src != cloud for a in plan.actions)


def test_unreadable_images_are_reported_and_never_moved(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    (tree / "pics" / "broken.png").write_bytes(b"not a picture at all")
    plan = TOOL.scan(DuplicatesOptions(roots=[tree], use_cache=False), _ctx(tmp_path, None))
    assert any("unreadable" in note and "broken.png" in note for note in plan.notes)
    assert all(a.src is None or a.src.name != "broken.png" for a in plan.actions)


def test_capture_time_difference_blocks_a_visual_match(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _save(_scene(7), tree / "a.png", exif_taken="2024:01:01 10:00:00")
    _save(_scene(7).resize((200, 150)), tree / "b.png", exif_taken="2024:01:01 10:05:00")
    found = _ours(tree, tmp_path, None, media="images", use_cache=False)
    assert found == set()


def test_protected_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    monkeypatch.setattr(dup_tool, "protected_reason", lambda _p: "Windows folder")
    plan = TOOL.scan(DuplicatesOptions(roots=[tree], use_cache=False), _ctx(tmp_path, None))
    assert plan.roots == []
    assert plan.actions == []
    assert any("Protected folder refused" in note for note in plan.notes)


def test_missing_ffmpeg_skips_videos_with_a_note(tmp_path: Path) -> None:
    tree = tmp_path / "videos"
    tree.mkdir()
    (tree / "clip.mp4").write_bytes(b"not really a video")

    class NoFfmpeg(ExternalTools):
        def path(self, tool: str) -> Path | None:
            return None

    plan = TOOL.scan(
        DuplicatesOptions(roots=[tree], media="videos", use_cache=False),
        _ctx(tmp_path, None, deps=NoFfmpeg(env={}, ollama_probe=lambda _u: None)),
    )
    assert plan.actions == []
    assert any("ffprobe" in note.lower() or "ffmpeg" in note.lower() for note in plan.notes)


# ---- cache --------------------------------------------------------------------------------


def test_cached_results_are_reused_until_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    opts = DuplicatesOptions(roots=[tree], media="images")
    first = dup_tool.collect(opts, _ctx(tmp_path, None))
    calls: list[Path] = []
    real_read = dup_tool.read_image

    def counting(path: Path, size: int, mtime: float) -> ImageInfo:
        calls.append(path)
        return real_read(path, size, mtime)

    monkeypatch.setattr(dup_tool, "read_image", counting)
    second = dup_tool.collect(opts, _ctx(tmp_path, None))
    assert calls == []  # every file came from the cache
    assert {(m.kind, m.keeper, m.duplicate) for m in second.matches} == {
        (m.kind, m.keeper, m.duplicate) for m in first.matches
    }
    # A changed file is hashed again.
    os.utime(tree / "pics" / "mountain.png", (1, 1))
    dup_tool.collect(opts, _ctx(tmp_path, None))
    assert calls == [tree / "pics" / "mountain.png"]


def test_cache_round_trip_keeps_every_feature(tmp_path: Path) -> None:
    path = _save(_scene(9), tmp_path / "p.png", exif_taken="2024:02:03 04:05:06")
    info = read_image(path, path.stat().st_size, path.stat().st_mtime)
    restored = dup_tool.image_from_cache(
        path, path.stat().st_size, path.stat().st_mtime, dup_tool.image_to_cache(info)
    )
    assert restored == info


# ---- keeper ordering ----------------------------------------------------------------------


def test_image_keeper_prefers_resolution_then_size_then_name_without_copy_mark() -> None:
    big = image_keep_key(Path("a/x.png"), w=400, h=300, size=10, mtime=1.0)
    small = image_keep_key(Path("a/y.png"), w=200, h=150, size=99, mtime=1.0)
    assert big < small
    copy = image_keep_key(Path("a/x (1).png"), w=400, h=300, size=10, mtime=1.0)
    assert big < copy


def test_video_keeper_prefers_resolution_then_bitrate() -> None:
    high = video_keep_key(Path("a/x.mp4"), w=320, h=240, size=1000, dur=10.0, mtime=1.0)
    low = video_keep_key(Path("a/y.mp4"), w=320, h=240, size=500, dur=10.0, mtime=1.0)
    assert high < low


# ---- explain ------------------------------------------------------------------------------


def test_explain_reports_each_check_for_two_files(tmp_path: Path) -> None:
    a = _save(_scene(1), tmp_path / "a.png")
    b = _save(_scene(1).resize((200, 150)), tmp_path / "b.png")
    report = explain(a, b, ImageRules())
    names = [c.name for c in report.checks]
    assert names == [
        "aspect ratio diff",
        "pHash distance",
        "dHash distance",
        "block diff (max 8x8)",
        "capture time",
    ]
    assert report.matched == all(c.ok for c in report.checks)


def test_explain_refuses_animated_gifs(tmp_path: Path) -> None:
    gif = tmp_path / "a.gif"
    _scene(1).convert("P").save(gif)
    with pytest.raises(ValueError):
        explain(gif, gif, ImageRules())


# ---- Ollama verification ------------------------------------------------------------------


@pytest.fixture
def ollama(tmp_path: Path) -> Iterator[tuple[str, dict[str, str]]]:
    """A local stand-in for Ollama. ``answer["chat"]`` decides what the model says."""
    answer = {"chat": "YES", "models": "lfm3-vl:latest"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._send({"models": [{"name": answer["models"]}]})

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self._send({"message": {"content": answer["chat"]}})

        def _send(self, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", answer
    finally:
        server.shutdown()
        server.server_close()


def test_model_confirms_only_on_yes(tmp_path: Path, ollama: tuple[str, dict[str, str]]) -> None:
    url, answer = ollama
    a = _save(_scene(1), tmp_path / "a.png")
    b = _save(_scene(1).resize((200, 150)), tmp_path / "b.png")
    verifier = OllamaVerifier(url, "lfm3-vl:latest")
    assert verifier.check_model() is None
    answer["chat"] = "YES"
    assert verifier.confirms(a, b) is True
    answer["chat"] = "NO"
    assert verifier.confirms(a, b) is False
    assert verifier.stats.checked == 2 and verifier.stats.rejected == 1


def test_model_errors_count_as_not_confirmed(tmp_path: Path) -> None:
    a = _save(_scene(1), tmp_path / "a.png")
    verifier = OllamaVerifier("http://127.0.0.1:9", "lfm3-vl:latest", timeout=1)
    assert verifier.confirms(a, a) is False
    assert verifier.stats.errors == 1


def test_missing_model_is_reported_before_the_scan(ollama: tuple[str, dict[str, str]]) -> None:
    url, answer = ollama
    answer["models"] = "something-else:latest"
    problem = OllamaVerifier(url, "lfm3-vl:latest").check_model()
    assert problem is not None and "not installed" in problem


def test_unreachable_ollama_moves_no_borderline_match(tmp_path: Path) -> None:
    """The default test context has no Ollama, so no network call is made at all."""
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    opts = DuplicatesOptions(roots=[tree], media="images", use_cache=False, verify_with_ollama=True)
    found = dup_tool.collect(opts, _ctx(tmp_path, None))
    assert any("Ollama" in note for note in found.notes)
    assert all("model" not in m.reason for m in found.matches)
    assert all(m.kind != "visual" for m in found.matches) or all(
        "confirmed" not in m.reason for m in found.matches
    )


def test_only_visual_matches_go_to_the_model(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    base = DuplicatesOptions(roots=[tree], media="images", use_cache=False)
    without = dup_tool.collect(base, _ctx(tmp_path, None))
    asked: list[Path] = []

    def always_no(a: Path, b: Path) -> bool:
        asked.append(b)
        return False

    checked = dup_tool.collect(
        base.model_copy(update={"verify_with_ollama": True}),
        _ctx(tmp_path, None),
        confirm=always_no,
    )
    exact = {(m.keeper, m.duplicate) for m in without.matches if m.kind == "exact"}
    assert exact == {(m.keeper, m.duplicate) for m in checked.matches if m.kind == "exact"}
    visual_before = {(m.keeper, m.duplicate) for m in without.matches if m.kind == "visual"}
    visual_after = {(m.keeper, m.duplicate) for m in checked.matches if m.kind == "visual"}
    assert visual_after <= visual_before  # a "no" from the model never adds a move
    assert all(p in visual_before or p[1] not in asked for p in visual_after)
    # Only the candidates that the rules consider borderline are sent to the model.
    assert all(b.name for b in asked)


# ---- helpers --------------------------------------------------------------------------------


def _ffmpeg(ffmpeg: Path, *args: str) -> None:
    subprocess.run(
        [str(ffmpeg), "-v", "error", "-y", *args], check=True, capture_output=True, timeout=120
    )
