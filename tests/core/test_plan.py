"""Plans and the apply engine: guard rules, journaling, collisions, cancel, JSON round-trip."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from various_tools.core import plan as plan_module
from various_tools.core.context import Cancelled, RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal
from various_tools.core.plan import ActionKind, Plan, PlannedAction, apply_plan
from various_tools.core.settings import Settings


@pytest.fixture
def sent_to_trash(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Trash calls are recorded instead of sent to the real Recycle Bin."""
    calls: list[str] = []

    def fake_send2trash(path: str) -> None:
        calls.append(path)
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
    (root / "empty_a").mkdir()
    (root / "file.txt").write_text("hello", encoding="utf-8")
    return root


def test_move_puts_file_in_place_and_journals_it(
    tree: Path, journal: Journal, tmp_path: Path
) -> None:
    target = tree / "moved" / "file.txt"
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[PlannedAction(ActionKind.MOVE, src=tree / "file.txt", dst=target, reason="sort")],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.status == "done"
    assert result.count("done") == 1
    assert target.read_text(encoding="utf-8") == "hello"
    assert not (tree / "file.txt").exists()
    recorded = journal.actions(result.run_id)[0]
    assert recorded.status == "done" and recorded.dst == str(target)


def test_collisions_are_resolved_at_apply_time_and_recorded(
    tree: Path, journal: Journal, tmp_path: Path
) -> None:
    (tree / "dup").mkdir()
    (tree / "dup" / "file.txt").write_text("already here", encoding="utf-8")
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[
            PlannedAction(ActionKind.MOVE, src=tree / "file.txt", dst=tree / "dup" / "file.txt")
        ],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert (tree / "dup" / "file_dup1.txt").read_text(encoding="utf-8") == "hello"
    assert (tree / "dup" / "file.txt").read_text(encoding="utf-8") == "already here"
    assert journal.actions(result.run_id)[0].dst == str(tree / "dup" / "file_dup1.txt")


def test_root_folder_is_never_changed(tree: Path, journal: Journal, tmp_path: Path) -> None:
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[PlannedAction(ActionKind.DELETE, src=tree)],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal, delete_mode="permanent"))
    assert result.count("skipped") == 1
    assert "root folder" in result.outcomes[0].detail
    assert tree.is_dir()


def test_actions_outside_the_selected_folders_are_refused(
    tree: Path, journal: Journal, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[PlannedAction(ActionKind.MOVE, src=outside, dst=tree / "a.txt")],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.count("skipped") == 1
    assert "outside the selected folders" in result.outcomes[0].detail
    assert outside.exists()


def test_trash_goes_through_send2trash(
    tree: Path, journal: Journal, tmp_path: Path, sent_to_trash: list[str]
) -> None:
    target = tree / "file.txt"
    plan = Plan(tool="t", roots=[tree], actions=[PlannedAction(ActionKind.TRASH, src=target)])
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.count("done") == 1
    assert sent_to_trash == [str(target)]
    assert not target.exists()


def test_permanent_delete_is_refused_unless_settings_allow_it(
    tree: Path, journal: Journal, tmp_path: Path
) -> None:
    target = tree / "empty_a"
    plan = Plan(tool="t", roots=[tree], actions=[PlannedAction(ActionKind.DELETE, src=target)])
    result = apply_plan(plan, _ctx(tmp_path, journal))  # default is trash mode
    assert result.count("skipped") == 1
    assert "Recycle Bin" in result.outcomes[0].detail
    assert target.is_dir()


def test_permanent_delete_removes_only_empty_folders_and_files(
    tree: Path, journal: Journal, tmp_path: Path
) -> None:
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[
            PlannedAction(ActionKind.DELETE, src=tree / "empty_a"),
            PlannedAction(ActionKind.DELETE, src=tree / "file.txt"),
            PlannedAction(ActionKind.DELETE, src=tree / "keep"),
        ],
    )
    (tree / "keep" / "inside.txt").write_text("not empty", encoding="utf-8")
    result = apply_plan(plan, _ctx(tmp_path, journal, delete_mode="permanent"))
    assert not (tree / "empty_a").exists()
    assert not (tree / "file.txt").exists()
    assert (tree / "keep").is_dir()  # never a recursive delete
    assert result.count("done") == 2
    assert result.count("skipped") == 1  # refused up front: the folder is not empty
    assert "not empty" in result.outcomes[-1].detail
    assert result.status == "done"  # a refusal is a skip, not a failure


def test_protected_paths_are_refused_by_default(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_profile = tmp_path / "profile"
    target = fake_profile / "Desktop" / "note.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(fake_profile))
    # The profile folder is not the root here, so the protected-path rule is what refuses it.
    plan = Plan(
        tool="t",
        roots=[tmp_path],
        actions=[PlannedAction(ActionKind.MOVE, src=fake_profile, dst=tmp_path / "z")],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.count("skipped") == 1
    assert "protected" in result.outcomes[0].detail
    assert fake_profile.is_dir()

    # A protected reason on a file inside the folders is refused unless the user allows it.
    monkeypatch.setattr(plan_module, "protected_reason", lambda _p, env=None: "Windows folder")
    plan2 = Plan(
        tool="t",
        roots=[fake_profile],
        actions=[PlannedAction(ActionKind.MOVE, src=target, dst=tmp_path / "moved.txt")],
    )
    refused = apply_plan(plan2, _ctx(tmp_path, journal))
    assert refused.count("skipped") == 1
    assert target.exists()

    allowed = apply_plan(plan2, _ctx(tmp_path, journal, allow_protected_paths=True))
    assert allowed.count("done") == 1


def test_a_failed_action_does_not_stop_the_rest(
    tree: Path, journal: Journal, tmp_path: Path
) -> None:
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[
            PlannedAction(ActionKind.MOVE, src=tree / "missing.txt", dst=tree / "x.txt"),
            PlannedAction(ActionKind.MOVE, src=tree / "file.txt", dst=tree / "y.txt"),
        ],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert [o.status for o in result.outcomes] == ["failed", "done"]
    assert "source is gone" in (result.outcomes[0].detail)
    assert (tree / "y.txt").exists()
    assert result.status == "partial"


def test_only_selected_actions_run(tree: Path, journal: Journal, tmp_path: Path) -> None:
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[
            PlannedAction(
                ActionKind.MOVE, src=tree / "file.txt", dst=tree / "a.txt", selected=False
            ),
            PlannedAction(
                ActionKind.MOVE, src=tree / "empty_a", dst=tree / "b_empty", selected=True
            ),
        ],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.count("done") == 1
    assert (tree / "file.txt").exists()
    assert (tree / "b_empty").is_dir()


def test_write_and_convert_need_a_handler(tree: Path, journal: Journal, tmp_path: Path) -> None:
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[PlannedAction(ActionKind.WRITE, src=tree / "file.txt", dst=tree / "out.txt")],
    )
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.count("failed") == 1
    assert "no handler" in result.outcomes[0].detail

    seen: list[PlannedAction] = []

    def handler(action: PlannedAction) -> Path | None:
        seen.append(action)
        (tree / "out.txt").write_text("generated", encoding="utf-8")
        return tree / "out.txt"

    ok = apply_plan(plan, _ctx(tmp_path, journal), handlers={ActionKind.WRITE: handler})
    assert ok.count("done") == 1
    assert seen and (tree / "out.txt").read_text(encoding="utf-8") == "generated"


def test_cancel_stops_before_the_next_action(tree: Path, journal: Journal, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, journal)
    ctx.cancel()
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[PlannedAction(ActionKind.MOVE, src=tree / "file.txt", dst=tree / "a.txt")],
    )
    result = apply_plan(plan, ctx)
    assert result.status == "cancelled"
    assert (tree / "file.txt").exists()
    assert journal.run(result.run_id).status == "cancelled"  # type: ignore[union-attr]


def test_progress_is_reported_per_action(tree: Path, journal: Journal, tmp_path: Path) -> None:
    updates: list[tuple[int, int]] = []
    ctx = _ctx(tmp_path, journal)
    ctx.progress = lambda done, total, _msg: updates.append((done, total))
    plan = Plan(
        tool="t",
        roots=[tree],
        actions=[
            PlannedAction(ActionKind.MOVE, src=tree / "file.txt", dst=tree / "a.txt"),
            PlannedAction(ActionKind.MOVE, src=tree / "empty_a", dst=tree / "b"),
        ],
    )
    apply_plan(plan, ctx)
    assert updates[0] == (0, 2)
    assert updates[-1] == (2, 2)


def test_apply_without_a_journal_is_refused(tree: Path, tmp_path: Path) -> None:
    plan = Plan(tool="t", roots=[tree], actions=[])
    with pytest.raises(ValueError, match="journal"):
        apply_plan(plan, _ctx(tmp_path, None))


def test_plan_json_round_trip_keeps_actions_and_options(tree: Path) -> None:
    plan = Plan(
        tool="duplicates",
        roots=[tree],
        actions=[
            PlannedAction(
                ActionKind.MOVE, src=tree / "a", dst=tree / "b", reason="exact", group_id="g1"
            ),
            PlannedAction(ActionKind.TRASH, src=tree / "c", selected=False),
        ],
        notes=["skipped 2 cloud-only files"],
        options_json='{"workers": 2}',
    )
    restored = Plan.from_json(plan.to_json())
    assert restored.tool == "duplicates"
    assert restored.roots == [tree]
    assert restored.notes == ["skipped 2 cloud-only files"]
    assert restored.options_json == '{"workers": 2}'
    assert [a.kind for a in restored.actions] == [ActionKind.MOVE, ActionKind.TRASH]
    assert restored.actions[0].group_id == "g1"
    assert restored.actions[1].selected is False
    assert restored.selected_actions() == [restored.actions[0]]


@pytest.mark.parametrize("text", ['{"format": 99, "tool": "x"}', "[]", '{"format": 1}'])
def test_plan_json_rejects_foreign_or_unsupported_files(text: str) -> None:
    with pytest.raises(ValueError):
        Plan.from_json(text)


def test_cancelled_exception_type_is_exported() -> None:
    assert issubclass(Cancelled, Exception)


def test_trash_refuses_a_folder_that_still_has_contents(
    tree: Path, journal: Journal, tmp_path: Path, sent_to_trash: list[str]
) -> None:
    # A trash of a non-empty folder would send its whole contents to the Recycle Bin.
    plan = Plan(
        tool="t", roots=[tree], actions=[PlannedAction(ActionKind.TRASH, src=tree / "keep")]
    )
    (tree / "keep" / "inside.txt").write_text("still here", encoding="utf-8")
    result = apply_plan(plan, _ctx(tmp_path, journal))
    assert result.count("skipped") == 1
    assert "not empty" in result.outcomes[0].detail
    assert sent_to_trash == []
    assert (tree / "keep" / "inside.txt").exists()
