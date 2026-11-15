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
import zlib
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
from pydantic import ValidationError

from various_tools.core import fs as fs_module
from various_tools.core.context import Cancelled, RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.plan import ActionKind, Plan
from various_tools.core.settings import Settings
from various_tools.tools.base import discover
from various_tools.tools.duplicates import TOOL, DuplicatesOptions, images
from various_tools.tools.duplicates import tool as dup_tool
from various_tools.tools.duplicates.explain import explain
from various_tools.tools.duplicates.images import ImageInfo, ImageRules, read_image
from various_tools.tools.duplicates.keeper import image_keep_key, video_keep_key
from various_tools.tools.duplicates.verify_ollama import OllamaVerifier
from various_tools.tools.duplicates.videos import VIDEO_EXT, VideoInfo, VideoRules, same_video

REPO = Path(__file__).resolve().parents[3]
LEGACY_IMAGES = REPO / "tests" / "fixtures" / "legacy_reference" / "python-find_duplicates.py"
LEGACY_VIDEOS = REPO / "tests" / "fixtures" / "legacy_reference" / "find_duplicate_videos.py"

MakeVideo = Callable[..., Path]


# ---- helpers ----------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def _ctx(tmp_path: Path, journal: Journal | None, deps: ExternalTools | None = None) -> RunContext:
    """The scan cache lives in the ``cache_folder`` setting. Tests keep it under ``tmp_path``."""
    return RunContext(
        settings=Settings.model_validate(
            {"onedrive_root": tmp_path, "cache_folder": tmp_path / "cache"}
        ),
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

    def spy(path: Path, size: int, mtime: float, params: images.ReadParams) -> ImageInfo:
        read.append(path)
        return real_read(path, size, mtime, params)

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
    monkeypatch.setattr(fs_module, "protected_reason", lambda _p, *_a, **_k: "Windows folder")
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

    def counting(path: Path, size: int, mtime: float, params: images.ReadParams) -> ImageInfo:
        calls.append(path)
        return real_read(path, size, mtime, params)

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
def ollama(tmp_path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """A local stand-in for Ollama. ``answer["chat"]`` decides what the model says."""
    answer: dict[str, Any] = {"chat": "YES", "models": "lfm3-vl:latest", "asked": 0, "fail": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._send({"models": [{"name": answer["models"]}]})

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            answer["asked"] += 1
            if answer["fail"]:
                self.send_error(500, "model crashed")
                return
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


def test_model_confirms_only_on_yes(tmp_path: Path, ollama: tuple[str, dict[str, Any]]) -> None:
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


def test_missing_model_is_reported_before_the_scan(ollama: tuple[str, dict[str, Any]]) -> None:
    url, answer = ollama
    answer["models"] = "something-else:latest"
    problem = OllamaVerifier(url, "lfm3-vl:latest").check_model()
    assert problem is not None and "not installed" in problem


def _borderline_opts(tree: Path, **extra: Any) -> DuplicatesOptions:
    """``phash_max=0`` makes the resized copy borderline (its pHash gap equals the limit), so
    the model is asked about it. Only the test sets this; the defaults are untouched."""
    return DuplicatesOptions(
        roots=[tree], media="images", use_cache=False, phash_max=0, verify_with_ollama=True, **extra
    )


def _visual(found: dup_tool.Findings) -> list[str]:
    return [m.duplicate.name for m in found.matches if m.kind == "visual"]


def test_unreachable_ollama_moves_no_borderline_match(tmp_path: Path) -> None:
    """The default test context has no Ollama, so no network call is made at all."""
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    found = dup_tool.collect(_borderline_opts(tree), _ctx(tmp_path, None))
    assert any("Ollama is not reachable" in note for note in found.notes)
    assert _visual(found) == []  # the borderline match was refused, not moved
    # Without the model check, the same tree does give that visual match.
    plain = _borderline_opts(tree).model_copy(update={"verify_with_ollama": False})
    assert _visual(dup_tool.collect(plain, _ctx(tmp_path, None))) == ["beach_small.png"]


def test_the_model_verdict_decides_a_borderline_match(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    asked: list[tuple[str, str]] = []

    def verdict(answer: bool) -> Callable[[Path, Path], bool]:
        def confirm(a: Path, b: Path) -> bool:
            asked.append((a.name, b.name))
            return answer

        return confirm

    no = dup_tool.collect(_borderline_opts(tree), _ctx(tmp_path, None), confirm=verdict(False))
    assert asked == [("beach_copy.png", "beach_small.png")]  # only the borderline pair is asked
    assert _visual(no) == []
    yes = dup_tool.collect(_borderline_opts(tree), _ctx(tmp_path, None), confirm=verdict(True))
    assert _visual(yes) == ["beach_small.png"]
    reason = next(m.reason for m in yes.matches if m.kind == "visual")
    assert "confirmed" in reason
    # Exact and burst matches never go to the model.
    assert {m.kind for m in yes.matches} == {"exact", "visual", "burst"}
    assert len(asked) == 2


def test_configured_ollama_url_is_used(tmp_path: Path, ollama: tuple[str, dict[str, Any]]) -> None:
    url, answer = ollama
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    settings = Settings.model_validate({"onedrive_root": tmp_path, "ollama_url": url})
    probed: list[str] = []

    def probe(candidate: str) -> str | None:
        probed.append(candidate)
        return "0.1" if candidate == url else None

    ctx = RunContext(settings=settings, deps=ExternalTools(env={}, ollama_probe=probe))
    found = dup_tool.collect(_borderline_opts(tree), ctx)
    assert url in probed
    assert not any("not reachable" in note for note in found.notes)
    assert answer["asked"] == 1  # the server at the configured URL really was called
    assert _visual(found) == ["beach_small.png"]


def test_a_failing_pair_is_noted_and_not_moved(
    tmp_path: Path, ollama: tuple[str, dict[str, Any]]
) -> None:
    url, answer = ollama
    answer["fail"] = True
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    settings = Settings.model_validate({"onedrive_root": tmp_path, "ollama_url": url})
    ctx = RunContext(
        settings=settings,
        deps=ExternalTools(env={}, ollama_probe=lambda u: "0.1" if u == url else None),
    )
    found = dup_tool.collect(_borderline_opts(tree), ctx)
    failed = [n for n in found.notes if n.startswith("Ollama check failed for")]
    assert len(failed) == 1
    assert "beach_copy.png vs beach_small.png" in failed[0] and "500" in failed[0]
    assert _visual(found) == []  # not confirmed, so not moved
    assert {m.kind for m in found.matches} == {"exact", "burst"}  # the scan still finished


# ---- helpers --------------------------------------------------------------------------------


def _ffmpeg(ffmpeg: Path, *args: str) -> None:
    subprocess.run(
        [str(ffmpeg), "-v", "error", "-y", *args], check=True, capture_output=True, timeout=120
    )


# ---- review fixes: scan-time junction check, cancellation, progress ----------------------------


def _junction(link: Path, target: Path) -> None:
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction in this environment")


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows feature")
def test_scan_refuses_a_duplicate_folder_that_is_a_junction(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    outside = tmp_path / "outside"
    outside.mkdir()
    _junction(tree / "duplicate", outside)
    opts = DuplicatesOptions(roots=[tree], media="images", use_cache=False)
    plan = TOOL.scan(opts, _ctx(tmp_path, None))
    assert any("junction or symlink" in note and "duplicate" in note for note in plan.notes)
    # Nothing is planned into the junction; the review folder (a normal path) still works.
    for action in plan.selected_actions():
        assert action.dst is not None
        assert action.dst.parent != tree / "duplicate"
    assert any(a.reason.startswith("burst") for a in plan.selected_actions())
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows feature")
def test_scan_refuses_an_explicit_folder_through_a_junction(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    _junction(link, outside)
    plan = TOOL.scan(
        DuplicatesOptions(
            roots=[tree], media="images", use_cache=False, duplicate_folder=link / "dups"
        ),
        _ctx(tmp_path, None),
    )
    assert any("junction or symlink" in note for note in plan.notes)
    assert not any(
        a.dst is not None and fs_module.is_inside(a.dst, link) for a in plan.selected_actions()
    )


def test_a_later_pass_checks_for_cancellation(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    ctx = _ctx(tmp_path, None)
    seen: list[str] = []

    def progress(done: int, total: int, message: str) -> None:
        seen.append(message)
        if message.startswith("Comparing beach"):  # a pass 2 message
            ctx.cancel()

    ctx.progress = progress
    with pytest.raises(Cancelled):
        dup_tool.collect(DuplicatesOptions(roots=[tree], media="images", use_cache=False), ctx)
    assert any(m.startswith("Comparing") for m in seen)
    assert not any(m.startswith("Checking burst") for m in seen)  # stopped before pass 3


def test_later_passes_report_progress(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    ctx = _ctx(tmp_path, None)
    seen: list[str] = []
    ctx.progress = lambda _d, _t, message: seen.append(message)
    dup_tool.collect(DuplicatesOptions(roots=[tree], media="images", use_cache=False), ctx)
    assert any(m.startswith("Comparing ") for m in seen)
    assert any(m.startswith("Checking burst") for m in seen)


# ---- thresholds: the tuned values, and each side of their boundary ---------------------------


def _bits(n: int) -> int:
    """A hash value that is ``n`` bits away from 0."""
    return (1 << n) - 1


def _flat(value: int) -> bytes:
    return zlib.compress(bytes([value]) * (128 * 128), 1)


def _crafted(
    name: str,
    *,
    phash: int = 0,
    dhash: int = 0,
    w: int = 100,
    h: int = 100,
    grey: int = 100,
) -> ImageInfo:
    return ImageInfo(
        path=Path(name),
        size=1,
        mtime=1.0,
        w=w,
        h=h,
        sha=name,
        phash=phash,
        dhash=dhash,
        sharp=1.0,
        taken=None,
        thumb_z=_flat(grey),
    )


def test_tuned_default_thresholds_are_unchanged() -> None:
    rules = ImageRules()
    assert (rules.phash_max, rules.dhash_max, rules.aspect_tol) == (2, 4, 0.02)
    assert (rules.burst_phash_max, rules.pixel_block_max) == (40, 3.0)
    video = VideoRules()
    assert (video.dur_tol_s, video.dur_tol_pct, video.aspect_tol) == (0.6, 0.01, 0.02)
    assert (video.frame_mean_max, video.frame_worst_max) == (10, 24)


@pytest.mark.parametrize(("gap", "expected"), [(2, True), (3, False)])
def test_phash_max_boundary(gap: int, expected: bool) -> None:
    a, b = _crafted("a.png"), _crafted("b.png", phash=_bits(gap))
    assert images.cheap_match(a, b, ImageRules()) is expected


@pytest.mark.parametrize(("gap", "expected"), [(4, True), (5, False)])
def test_dhash_max_boundary(gap: int, expected: bool) -> None:
    a, b = _crafted("a.png"), _crafted("b.png", dhash=_bits(gap))
    assert images.cheap_match(a, b, ImageRules()) is expected


@pytest.mark.parametrize(("width", "expected"), [(1019, True), (1021, False)])
def test_aspect_tolerance_boundary(width: int, expected: bool) -> None:
    a = _crafted("a.png", w=1000, h=1000)
    b = _crafted("b.png", w=width, h=1000)
    assert images.cheap_match(a, b, ImageRules()) is expected


@pytest.mark.parametrize(("grey", "expected"), [(103, True), (104, False)])
def test_pixel_block_boundary(grey: int, expected: bool) -> None:
    """A flat difference of 3 grey levels is a block difference of exactly 3.0 (kept)."""
    a, b = _crafted("a.png", grey=100), _crafted("b.png", grey=grey)
    matches = images.find_matches([a, b], ImageRules())
    assert (len(matches) == 1 and matches[0].kind == "visual") is expected


@pytest.mark.parametrize(("gap", "expected"), [(40, True), (41, False)])
def test_burst_phash_boundary(gap: int, expected: bool) -> None:
    a = _crafted("IMG_20240101_120000_1.png")
    b = _crafted("IMG_20240101_120000_2.png", phash=_bits(gap))
    matches = images.find_matches([a, b], ImageRules())
    assert [m.kind for m in matches] == (["burst"] if expected else [])


def _clip(name: str, dur: float, frames: tuple[int, ...] = (0,) * 5) -> VideoInfo:
    return VideoInfo(Path(name), 1, 1.0, dur, 1920, 1080, frames)


@pytest.mark.parametrize(
    ("dur_a", "dur_b", "expected"),
    [
        (50.0, 50.5, True),  # 0.5 s apart: inside the 0.6 s floor
        (50.0, 50.7, False),  # 0.7 s apart: outside both the floor and 1% of 50 s
        (100.0, 101.0, True),  # 1.0 s apart: outside 0.6 s but inside 1% of 101 s
        (100.0, 101.1, False),  # 1.1 s apart: outside 1% of 101.1 s (1.011 s)
    ],
)
def test_video_duration_tolerance_boundary(dur_a: float, dur_b: float, expected: bool) -> None:
    assert same_video(_clip("a.mp4", dur_a), _clip("b.mp4", dur_b), VideoRules()) is expected


@pytest.mark.parametrize(
    ("gaps", "expected"),
    [
        ((10, 10, 10, 10, 10), True),  # mean exactly 10
        ((11, 10, 10, 10, 10), False),  # mean 10.2
        ((24, 0, 0, 0, 0), True),  # worst frame exactly 24
        ((25, 0, 0, 0, 0), False),  # worst frame 25
    ],
)
def test_video_frame_boundaries(gaps: tuple[int, ...], expected: bool) -> None:
    a = _clip("a.mp4", 60.0)
    b = _clip("b.mp4", 60.0, tuple(_bits(g) for g in gaps))
    assert same_video(a, b, VideoRules()) is expected


# ---- configurable options (nothing is constant) ------------------------------------------------


def test_option_defaults_are_the_old_constants() -> None:
    opts = DuplicatesOptions()
    assert opts.thumb_size == 128 and opts.phash_size == 16 and opts.video_phash_size == 16
    assert opts.frame_width == 320 and opts.sharpness_px == 1600
    assert opts.frame_retries == 6 and opts.frame_step_back == 0.8
    assert opts.verify_borderline_block == 2.0
    assert opts.image_extensions == sorted(images.IMAGE_EXT)
    assert opts.visual_skip_extensions == [".gif"]
    assert opts.video_extensions == sorted(VIDEO_EXT)
    assert opts.ollama_image_px == 512 and opts.ollama_jpeg_quality == 85
    assert opts.ollama_prompt.endswith("Answer only YES or NO.")
    assert opts.burst_name == r"^(?:IMG_)?(\d{8}_\d{6})"
    assert "copy" in opts.copy_mark


def test_locked_thresholds_are_marked_locked_and_advanced() -> None:
    locked = {
        "phash_max",
        "dhash_max",
        "aspect_tol",
        "burst_phash_max",
        "pixel_block_max",
        "dur_tol_s",
        "dur_tol_pct",
        "frame_mean_max",
        "frame_worst_max",
        "sample_at",
    }
    for name in locked:
        info = DuplicatesOptions.model_fields[name]
        extra = info.json_schema_extra
        assert isinstance(extra, dict)
        assert extra["locked"] is True and extra["advanced"] is True
        assert (info.description or "").startswith("🔒"), name


def test_extension_lists_are_normalised_and_checked() -> None:
    opts = DuplicatesOptions(image_extensions=["JPG", " .Png ", "jpg"])
    assert opts.image_extensions == [".jpg", ".png"]
    for bad in ([""], ["a/b"], ["x.y"], [".j pg"]):
        with pytest.raises(ValidationError):
            DuplicatesOptions(image_extensions=bad)
    with pytest.raises(ValidationError):
        DuplicatesOptions(image_extensions=[".mp4"], video_extensions=[".mp4"])


def test_a_custom_extension_list_changes_what_is_scanned(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    original = _save(_scene(1), tree / "a.png")
    shutil.copyfile(original, tree / "b.png")
    shutil.copyfile(original, tree / "c.jpg")
    both = _ours(tree, tmp_path, None, media="images", use_cache=False, exact_only=True)
    assert len(both) == 2  # b.png and c.jpg are copies of a.png
    only_png = _ours(
        tree,
        tmp_path,
        None,
        media="images",
        use_cache=False,
        exact_only=True,
        image_extensions=[".png"],
    )
    assert only_png == {("exact", "a.png", "b.png")}


def test_visual_skip_extensions_decide_which_types_are_compared_by_bytes(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _save(_scene(1), tree / "a.png")
    _save(_scene(1).resize((200, 150)), tree / "b.png")
    default = _ours(tree, tmp_path, None, media="images", use_cache=False)
    assert ("visual", "a.png", "b.png") in default
    skipped = _ours(
        tree, tmp_path, None, media="images", use_cache=False, visual_skip_extensions=[".png"]
    )
    assert skipped == set()  # PNGs are now compared by bytes only


def test_burst_name_must_compile_and_have_one_group() -> None:
    for bad in ("(", r"\d+", r"(a)(b)"):
        with pytest.raises(ValidationError):
            DuplicatesOptions(burst_name=bad)
    DuplicatesOptions(burst_name=r"^(\d{6})_")


def test_a_custom_burst_pattern_groups_other_file_names() -> None:
    a = _crafted("shot_0101_1.png")
    b = _crafted("shot_0101_2.png", phash=_bits(30))
    assert images.find_matches([a, b], ImageRules()) == []  # the default pattern sees no burst
    rules = ImageRules(burst_name=r"^shot_(\d{4})")
    assert [m.kind for m in images.find_matches([a, b], rules)] == ["burst"]


def test_a_custom_copy_mark_changes_which_copy_stays() -> None:
    default_copy = image_keep_key(Path("a/x_dup.png"), w=4, h=3, size=10, mtime=1.0)
    custom_copy = image_keep_key(
        Path("a/x_dup.png"), w=4, h=3, size=10, mtime=1.0, copy_mark=r"_dup"
    )
    assert default_copy[2] == 0 and custom_copy[2] == 1


def test_borderline_block_option_controls_the_model_check() -> None:
    a, b = _crafted("a.png", grey=100), _crafted("b.png", grey=102)
    assert not images.is_borderline(a, b, 2.0, ImageRules())
    assert images.is_borderline(a, b, 2.0, ImageRules(verify_borderline_block=1.5))


def test_thumb_and_hash_sizes_are_part_of_the_cache_identity(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    cache_dir = tmp_path / "cache"
    base = DuplicatesOptions(roots=[tree], media="images")
    dup_tool.collect(base, _ctx(tmp_path, None))
    assert len(list(cache_dir.glob("*.sqlite"))) == 1
    dup_tool.collect(base.model_copy(update={"thumb_size": 64}), _ctx(tmp_path, None))
    dup_tool.collect(base.model_copy(update={"phash_size": 8}), _ctx(tmp_path, None))
    assert len(list(cache_dir.glob("*.sqlite"))) == 3  # each setting gets its own cache
    identities = {
        images.ReadParams().cache_identity(),
        images.ReadParams(thumb_size=64).cache_identity(),
        images.ReadParams(phash_size=8).cache_identity(),
        images.ReadParams(sharpness_px=800).cache_identity(),
        images.ReadParams(visual_skip_ext=frozenset()).cache_identity(),
    }
    assert len(identities) == 5


def test_changed_thumb_size_is_never_served_stale_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    dup_tool.collect(DuplicatesOptions(roots=[tree], media="images"), _ctx(tmp_path, None))
    seen: list[int] = []
    real_read = dup_tool.read_image

    def spy(path: Path, size: int, mtime: float, params: images.ReadParams) -> ImageInfo:
        info = real_read(path, size, mtime, params)
        if info.thumb_z is not None:
            seen.append(images.thumb_array(info.thumb_z).shape[0])
        return info

    monkeypatch.setattr(dup_tool, "read_image", spy)
    opts = DuplicatesOptions(roots=[tree], media="images", thumb_size=64)
    dup_tool.collect(opts, _ctx(tmp_path, None))
    assert seen and set(seen) == {64}  # recomputed at 64 px, not read back at 128 px


def test_a_small_hash_grid_and_thumbnail_still_find_copies(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    found = _ours(
        tree, tmp_path, None, media="images", use_cache=False, thumb_size=64, phash_size=8
    )
    assert {kind for kind, _, _ in found} == {"exact", "visual", "burst"}
    assert any(kind == "visual" and dup == "pics/beach_small.png" for kind, _, dup in found)


def test_cache_folder_setting_decides_where_the_cache_lives(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _make_image_tree(tree)
    target = tmp_path / "somewhere" / "else"
    settings = Settings.model_validate({"onedrive_root": tmp_path, "cache_folder": target})
    ctx = RunContext(
        settings=settings,
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=None,
    )
    dup_tool.collect(DuplicatesOptions(roots=[tree], media="images"), ctx)
    assert list(target.glob("duplicates-images-*.sqlite"))


def test_missing_program_notes_use_the_install_hint_from_deps(tmp_path: Path) -> None:
    from various_tools.core.deps import install_hint

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
    assert any(install_hint("ffprobe") in note for note in plan.notes)


def test_settings_timeouts_and_chunk_size_reach_the_readers(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "onedrive_root": tmp_path,
            "hash_chunk_kb": 8,
            "ffprobe_timeout_s": 11,
            "frame_grab_timeout_s": 22,
        }
    )
    ctx = RunContext(
        settings=settings, deps=ExternalTools(env={}, ollama_probe=lambda _u: None), journal=None
    )
    opts = DuplicatesOptions(frame_retries=3, frame_step_back=0.5, frame_width=160)
    assert opts.read_params(ctx).chunk_bytes == 8 * 1024
    video = opts.video_params(ctx)
    assert (video.probe_timeout_s, video.frame_timeout_s) == (11, 22)
    assert (video.frame_retries, video.frame_step_back, video.frame_width) == (3, 0.5, 160)


def test_ollama_options_reach_the_verifier(
    tmp_path: Path, ollama: tuple[str, dict[str, Any]]
) -> None:
    url, _answer = ollama
    seen: dict[str, Any] = {}

    class Spy(OllamaVerifier):
        def _jpeg_b64(self, path: Path) -> str:
            seen["px"], seen["quality"] = self.image_px, self.jpeg_quality
            return super()._jpeg_b64(path)

    verifier = Spy(url, "lfm3-vl:latest", prompt="Same?", image_px=64, jpeg_quality=50)
    a = _save(_scene(1), tmp_path / "a.png")
    assert verifier.confirms(a, a) is True
    assert seen == {"px": 64, "quality": 50}
    assert verifier.prompt == "Same?"
