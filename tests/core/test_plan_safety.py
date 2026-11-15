"""Apply-engine guards and safe moves (review fixes): links by tag, empty roots, junction
sources, write/convert guards, races, extra journal rows and the cloud_only plan field."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from various_tools.core import fs
from various_tools.core import plan as plan_module
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.plan import ActionKind, Plan, PlannedAction, apply_plan
from various_tools.core.settings import Settings


@pytest.fixture
def sent_to_trash(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_send2trash(path: str) -> None:
        calls.append(path)
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.remove(path)

    monkeypatch.setattr(plan_module, "send2trash", fake_send2trash)
    return calls


def _ctx(tmp_path: Path, journal: Journal | None, **settings: object) -> RunContext:
    base: dict[str, object] = {"onedrive_root": tmp_path}
    base.update(settings)
    return RunContext(
        settings=Settings.model_validate(base),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: None),
        journal=journal,
    )


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "keep").mkdir(parents=True)
    (root / "file.txt").write_text("hello", encoding="utf-8")
    return root


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are a Windows feature")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if made.returncode != 0 or not link.exists():
        pytest.skip("could not create a junction in this environment")


def _fake_lstat(monkeypatch: pytest.MonkeyPatch, path: Path, tag: int) -> None:
    """Make ``path`` look like a reparse point with ``tag`` to the engine's checks."""
    real = fs.stat_no_follow

    def fake(p: Path) -> os.stat_result | None:
        st = real(p)
        if st is None or not fs.same_path(p, path):
            return st
        return SimpleNamespace(  # type: ignore[return-value]
            st_mode=st.st_mode,
            st_size=st.st_size,
            st_mtime_ns=st.st_mtime_ns,
            st_file_attributes=fs.FILE_ATTRIBUTE_DIRECTORY | fs.FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=tag,
        )

    monkeypatch.setattr(fs, "stat_no_follow", fake)


# ---- item 1: a non-link reparse point is a normal folder ---------------------------------


def test_a_non_empty_cloud_folder_is_never_trashed_whole(
    tree: Path,
    journal: Journal,
    tmp_path: Path,
    sent_to_trash: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tree / "keep"
    (folder / "inside.txt").write_text("x", encoding="utf-8")
    _fake_lstat(monkeypatch, folder, 0x9000701A)  # OneDrive placeholder folder
    plan = Plan(tool="t", roots=[tree], actions=[PlannedAction(ActionKind.TRASH, src=folder)])
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.outcomes[0].status == "skipped"
    assert "not empty" in result.outcomes[0].detail
    assert sent_to_trash == []
    assert (folder / "inside.txt").exists()


def test_a_non_empty_folder_with_an_unknown_tag_counts_as_a_link(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tree / "keep"
    (folder / "inside.txt").write_text("x", encoding="utf-8")
    _fake_lstat(monkeypatch, folder, fs.IO_REPARSE_TAG_MOUNT_POINT)
    refusal = plan_module._non_empty_folder_refusal(folder)  # pyright: ignore[reportPrivateUsage]
    assert refusal is None  # removing a junction removes the link, not what it points to


# ---- item 3: plan guards -----------------------------------------------------------------


@pytest.mark.parametrize("kind", [ActionKind.MOVE, ActionKind.RENAME, ActionKind.TRASH])
def test_a_plan_without_roots_changes_nothing(
    kind: ActionKind,
    tree: Path,
    journal: Journal,
    tmp_path: Path,
    sent_to_trash: list[str],
) -> None:
    src = tree / "file.txt"
    action = PlannedAction(kind, src=src, dst=tree / "renamed.txt")
    result = apply_plan(Plan(tool="t", roots=[], actions=[action]), _ctx(tmp_path, journal))
    assert result.outcomes[0].status == "skipped"
    assert "no selected folders" in result.outcomes[0].detail
    assert src.exists() and sent_to_trash == []


def test_a_plan_without_roots_from_json_is_refused(
    tree: Path, journal: Journal, tmp_path: Path
) -> None:
    text = Plan(
        tool="t", roots=[tree], actions=[PlannedAction(ActionKind.DELETE, src=tree / "file.txt")]
    ).to_json()
    data = json.loads(text)
    data["roots"] = []  # a hand-edited plan file
    edited = Plan.from_json(json.dumps(data))
    result = apply_plan(edited, _ctx(tmp_path, journal, delete_mode="permanent"))
    assert result.outcomes[0].status == "skipped"
    assert (tree / "file.txt").exists()


def test_a_root_refused_at_apply_time_blocks_its_actions(
    tree: Path, journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        plan_module, "root_refusal", lambda root, *_a, **_k: f"Junction, not followed: {root}"
    )
    action = PlannedAction(ActionKind.MOVE, src=tree / "file.txt", dst=tree / "keep" / "f.txt")
    result = apply_plan(Plan(tool="t", roots=[tree], actions=[action]), _ctx(tmp_path, journal))
    assert result.outcomes[0].status == "skipped"
    assert "Junction" in result.outcomes[0].detail
    assert (tree / "file.txt").exists()


def test_a_source_below_a_junction_inside_the_root_is_refused(
    tree: Path, journal: Journal, tmp_path: Path, sent_to_trash: list[str]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("keep me", encoding="utf-8")
    _junction(tree / "link", outside)
    action = PlannedAction(ActionKind.TRASH, src=tree / "link" / "victim.txt")
    result = apply_plan(Plan(tool="t", roots=[tree], actions=[action]), _ctx(tmp_path, journal))
    assert result.outcomes[0].status == "skipped"
    assert "junction" in result.outcomes[0].detail
    assert (outside / "victim.txt").exists() and sent_to_trash == []


def test_write_and_convert_destinations_are_checked_by_the_engine(
    tree: Path, journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _junction(tree / "link", outside)
    ran: list[PlannedAction] = []

    def handler(action: PlannedAction) -> Path | None:
        ran.append(action)
        return action.dst

    actions = [
        PlannedAction(ActionKind.CONVERT, src=tree / "file.txt", dst=tree / "link" / "out.jpg"),
        PlannedAction(ActionKind.WRITE, src=None, dst=tree / "link" / "manifest.txt"),
    ]
    result = apply_plan(
        Plan(tool="t", roots=[tree], actions=actions),
        _ctx(tmp_path, journal),
        handlers={ActionKind.CONVERT: handler, ActionKind.WRITE: handler},
    )
    assert [o.status for o in result.outcomes] == ["skipped", "skipped"]
    assert ran == []


def test_write_to_a_protected_path_is_refused(
    tree: Path, journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        plan_module,
        "protected_reason",
        lambda p, *_a, **_k: "Windows folder" if Path(p).name == "out.txt" else None,
    )
    ran: list[PlannedAction] = []
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[PlannedAction(ActionKind.WRITE, src=tree / "file.txt", dst=tree / "out.txt")],
    )
    result = apply_plan(
        plan, _ctx(tmp_path, journal), handlers={ActionKind.WRITE: lambda a: ran.append(a)}
    )
    assert result.outcomes[0].status == "skipped"
    assert "protected" in result.outcomes[0].detail
    assert ran == []


# ---- item 2: moves never overwrite --------------------------------------------------------


def test_a_destination_created_between_check_and_move_is_not_overwritten(
    tree: Path, journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dst = tree / "keep" / "file.txt"
    real_record = journal.record_action

    def record_then_race(*args: object, **kwargs: object) -> int:
        row = real_record(*args, **kwargs)  # type: ignore[arg-type]
        if kwargs.get("status", "pending") == "pending":
            dst.write_text("arrived meanwhile", encoding="utf-8")
        return row

    monkeypatch.setattr(journal, "record_action", record_then_race)
    action = PlannedAction(ActionKind.MOVE, src=tree / "file.txt", dst=dst)
    result = apply_plan(Plan(tool="t", roots=[tree], actions=[action]), _ctx(tmp_path, journal))
    assert result.outcomes[0].status == "failed"
    assert dst.read_text(encoding="utf-8") == "arrived meanwhile"
    assert (tree / "file.txt").read_text(encoding="utf-8") == "hello"


def test_cross_volume_move_of_a_cloud_only_file_is_refused(
    tree: Path, journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "same_volume", lambda _a, _b: False)
    monkeypatch.setattr(
        "various_tools.core.onedrive.get_attributes",
        lambda p: 0x1000 if Path(p).name == "file.txt" else 0,
    )
    action = PlannedAction(ActionKind.MOVE, src=tree / "file.txt", dst=tree / "keep" / "f.txt")
    result = apply_plan(Plan(tool="t", roots=[tree], actions=[action]), _ctx(tmp_path, journal))
    assert result.outcomes[0].status == "failed"
    assert "cloud-only" in result.outcomes[0].detail
    assert (tree / "file.txt").exists()
    assert list((tree / "keep").iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows names are case-insensitive")
def test_a_case_only_rename_is_applied_and_undone(
    tree: Path, journal: Journal, tmp_path: Path
) -> None:
    from various_tools.core.journal import undo_run

    src = tree / "photo.JPG"
    src.write_bytes(b"x")
    action = PlannedAction(ActionKind.RENAME, src=src, dst=tree / "photo.jpg")
    result = apply_plan(Plan(tool="t", roots=[tree], actions=[action]), _ctx(tmp_path, journal))
    assert result.outcomes[0].status == "done"
    assert "photo.jpg" in os.listdir(tree)
    assert journal.actions(result.run_id)[0].dst == str(tree / "photo.jpg")
    report = undo_run(journal, result.run_id)
    assert report.reverted == 1
    assert "photo.JPG" in os.listdir(tree)


# ---- item 6a: extra journal rows from a handler --------------------------------------------


def test_a_handler_can_journal_the_recycling_of_the_original(
    tree: Path, journal: Journal, tmp_path: Path, sent_to_trash: list[str]
) -> None:
    src = tree / "file.txt"
    out = tree / "file.out"

    def convert(action: PlannedAction) -> Path | None:
        out.write_text("converted", encoding="utf-8")
        plan_module.trash_extra(src, reason="original of a verified conversion")
        return out

    plan = Plan(
        tool="t", roots=[tree], actions=[PlannedAction(ActionKind.CONVERT, src=src, dst=out)]
    )
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: convert})
    assert result.count("done") == 1
    rows = journal.actions(result.run_id)
    assert [(r.kind, r.status) for r in rows] == [("convert", "done"), ("trash", "done")]
    assert rows[1].src == str(src) and rows[1].seq == rows[0].seq
    assert rows[1].item_size == 5  # recorded before it went, to find it in the Recycle Bin
    assert rows[0].item_size == len("converted") and rows[0].item_mtime_ns is not None
    assert sent_to_trash == [str(src)]


def test_a_failed_extra_trash_is_journaled_as_failed(
    tree: Path, journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(_path: str) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(plan_module, "send2trash", locked)
    src = tree / "file.txt"

    def convert(action: PlannedAction) -> Path | None:
        plan_module.trash_extra(src)
        return None

    plan = Plan(tool="t", roots=[tree], actions=[PlannedAction(ActionKind.CONVERT, src=src)])
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: convert})
    rows = journal.actions(result.run_id)
    assert [(r.kind, r.status) for r in rows] == [("convert", "failed"), ("trash", "failed")]
    assert "in use" in (rows[1].error or "")


def test_an_extra_trash_still_obeys_the_guards(
    tree: Path, journal: Journal, tmp_path: Path, sent_to_trash: list[str]
) -> None:
    def convert(action: PlannedAction) -> Path | None:
        plan_module.trash_extra(tree)  # the root itself: never
        return None

    plan = Plan(
        tool="t", roots=[tree], actions=[PlannedAction(ActionKind.CONVERT, src=tree / "file.txt")]
    )
    result = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.CONVERT: convert})
    rows = journal.actions(result.run_id)
    assert [(r.kind, r.status) for r in rows] == [("convert", "failed"), ("trash", "skipped")]
    assert sent_to_trash == [] and tree.is_dir()


def test_trash_extra_outside_apply_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        plan_module.trash_extra(tmp_path / "x")


# ---- item 11: cloud_only ------------------------------------------------------------------


def test_cloud_only_files_round_trip_through_plan_json(tree: Path) -> None:
    plan = Plan(tool="t", roots=[tree], cloud_only=[tree / "a.jpg", tree / "b.mov"])
    text = plan.to_json()
    assert '"cloud_only"' in text
    assert Plan.from_json(text).cloud_only == [tree / "a.jpg", tree / "b.mov"]
    legacy = text.replace('"cloud_only"', '"ignored_key"')
    assert Plan.from_json(legacy).cloud_only == []
    assert Plan(tool="t", roots=[]).cloud_only == []
