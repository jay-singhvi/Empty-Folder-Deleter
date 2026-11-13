# pyright: reportOptionalMemberAccess=false
"""Compare dialog for one duplicate group: cards, keep, explain match and the cloud-only rule."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pytestqt.qtbot import QtBot

from various_tools.core.media import imaging
from various_tools.core.media.thumbs import FileFacts, file_facts
from various_tools.core.plan import ActionKind, Plan, PlannedAction
from various_tools.gui.widgets import compare_view
from various_tools.gui.widgets.compare_view import (
    CLOUD_ONLY_TEXT,
    NO_PREVIEW_TEXT,
    CompareDialog,
    rules_for_plan,
)
from various_tools.tools.duplicates import images as dup_images
from various_tools.tools.duplicates import verify_ollama

ImageMaker = Callable[..., Path]


def group_plan(tmp_path: Path, keeper: Path, *dupes: Path) -> Plan:
    folder = tmp_path / "duplicate"
    return Plan(
        tool="duplicates",
        roots=[tmp_path],
        actions=[
            PlannedAction(
                ActionKind.MOVE, d, folder / d.name, f"exact copy; keeps {keeper.name}", str(keeper)
            )
            for d in dupes
        ],
    )


def open_dialog(qtbot: QtBot, plan: Plan, group_id: str) -> CompareDialog:
    dialog = CompareDialog(plan, group_id)
    qtbot.addWidget(dialog)
    assert dialog.wait_for_workers(15000)
    return dialog


@pytest.fixture
def pair(make_image: ImageMaker) -> tuple[Path, Path]:
    """Two files with identical pixels (so every match check passes) and one different file."""
    return make_image("g/a.png", seed=1), make_image("g/b.png", seed=1)


# ----- cards ----------------------------------------------------------------------------------


def test_cards_show_the_keeper_first_then_each_duplicate(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path], make_image: ImageMaker
) -> None:
    a, b = pair
    c = make_image("g/c.png", seed=1)
    plan = group_plan(tmp_path, a, b, c)
    dialog = open_dialog(qtbot, plan, str(a))
    assert [card.path for card in dialog.cards] == [a, b, c]
    keeper, first, second = dialog.cards
    assert keeper.is_keeper and not first.is_keeper and not second.is_keeper
    assert keeper.keep_button.isHidden() and keeper.explain_button.isHidden()
    assert not first.keep_button.isHidden() and not first.explain_button.isHidden()
    assert keeper.role_label.text() == "Kept"
    assert first.role_label.text() == "Duplicate"
    assert dialog.group_id == str(a)
    assert dialog.windowTitle() == "Compare: a.png"
    for card in dialog.cards:
        assert card.resolution_label.text() == "Resolution: 96 x 64"
        assert card.thumb_label.pixmap() is not None
        assert not card.thumb_label.pixmap().isNull()
        assert card.size_label.text().startswith("Size: ")
    assert "move" in first.action_label.text()
    assert first.name_label.text() == "b.png"


def test_unchecked_actions_are_marked_on_their_card(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, b = pair
    plan = group_plan(tmp_path, a, b)
    plan.actions[0].selected = False
    dialog = open_dialog(qtbot, plan, str(a))
    assert "unchecked" in dialog.cards[1].action_label.text()


def test_a_missing_file_gets_a_calm_card(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, _b = pair
    gone = tmp_path / "g" / "gone.png"
    dialog = open_dialog(qtbot, group_plan(tmp_path, a, gone), str(a))
    card = dialog.cards[1]
    assert card.thumb_label.text() == NO_PREVIEW_TEXT
    assert card.size_label.text() == "Size: file is missing"
    assert card.resolution_label.text() == "Resolution: unknown"


def test_an_unreadable_image_shows_no_preview(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, _b = pair
    broken = tmp_path / "g" / "broken.jpg"
    broken.write_bytes(b"this is not an image")
    dialog = open_dialog(qtbot, group_plan(tmp_path, a, broken), str(a))
    card = dialog.cards[1]
    assert card.thumb_label.text() == NO_PREVIEW_TEXT
    assert card.resolution_label.text() == "Resolution: unknown"


# ----- keep this one --------------------------------------------------------------------------


def test_keep_swaps_the_keeper_in_the_plan_and_rebuilds_the_cards(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, b = pair
    plan = group_plan(tmp_path, a, b)
    dialog = open_dialog(qtbot, plan, str(a))
    with qtbot.waitSignal(dialog.keeperChanged) as signal:
        new_id = dialog.keep(b)
    assert signal.args == [str(b)]
    assert new_id == str(b)
    assert dialog.group_id == str(b)
    assert dialog.wait_for_workers(15000)
    assert [card.path for card in dialog.cards] == [b, a]
    assert dialog.cards[0].is_keeper
    action = plan.actions[0]
    assert action.src == a
    assert action.dst == tmp_path / "duplicate" / "a.png"
    assert action.group_id == str(b)
    assert action.reason.endswith("keeps b.png")
    assert a.exists() and b.exists()  # the plan changed, the disk did not


def test_keep_button_click_does_the_same(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, b = pair
    plan = group_plan(tmp_path, a, b)
    dialog = open_dialog(qtbot, plan, str(a))
    with qtbot.waitSignal(dialog.keeperChanged):
        dialog.cards[1].keep_button.click()
    assert dialog.group_id == str(b)
    assert dialog.wait_for_workers(15000)


def test_keep_rejects_a_file_that_is_not_in_the_group(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, b = pair
    dialog = open_dialog(qtbot, group_plan(tmp_path, a, b), str(a))
    with pytest.raises(ValueError, match="not part of this group"):
        dialog.keep(tmp_path / "stranger.png")
    assert dialog.group_id == str(a)


# ----- explain match --------------------------------------------------------------------------


def test_explain_pair_shows_every_check_and_a_match_verdict(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, b = pair
    dialog = open_dialog(qtbot, group_plan(tmp_path, a, b), str(a))
    worker = dialog.explain_pair(a, b)
    qtbot.waitUntil(worker.isFinished, timeout=20000)
    assert dialog.wait_for_workers(15000)
    assert len(dialog.explain_dialogs) == 1
    explain = dialog.explain_dialogs[0]
    assert explain.table.rowCount() == 5
    results = [explain.table.item(r, 3).text() for r in range(explain.table.rowCount())]
    assert results == ["pass"] * 5
    assert explain.verdict_label.text().startswith("Verdict: visual duplicate")


def test_explain_pair_of_different_images_fails_a_check(
    qtbot: QtBot, tmp_path: Path, make_image: ImageMaker
) -> None:
    a = make_image("g/a.png", seed=1)
    other = make_image("g/other.png", seed=99)
    dialog = open_dialog(qtbot, group_plan(tmp_path, a, other), str(a))
    worker = dialog.explain_pair(a, other)
    qtbot.waitUntil(worker.isFinished, timeout=20000)
    assert dialog.wait_for_workers(15000)
    explain = dialog.explain_dialogs[0]
    results = [explain.table.item(r, 3).text() for r in range(explain.table.rowCount())]
    assert "FAIL" in results
    assert explain.verdict_label.text().startswith("Verdict: not a visual duplicate")


def test_explain_button_runs_explain_and_reenables(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, b = pair
    dialog = open_dialog(qtbot, group_plan(tmp_path, a, b), str(a))
    button = dialog.cards[1].explain_button
    button.click()
    assert not button.isEnabled()
    qtbot.waitUntil(lambda: len(dialog.explain_dialogs) == 1, timeout=20000)
    assert dialog.wait_for_workers(15000)
    qtbot.waitUntil(button.isEnabled, timeout=5000)


def test_explain_reports_an_unreadable_file_instead_of_raising(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, _b = pair
    bad = tmp_path / "g" / "bad.png"
    bad.write_bytes(b"not an image at all")
    dialog = open_dialog(qtbot, group_plan(tmp_path, a, bad), str(a))
    worker = dialog.explain_pair(a, bad)
    qtbot.waitUntil(worker.isFinished, timeout=20000)
    assert dialog.wait_for_workers(15000)
    explain = dialog.explain_dialogs[0]
    assert explain.table.isHidden()
    assert explain.verdict_label.text()
    assert not explain.verdict_label.text().startswith("Verdict")


def test_rules_default_when_the_plan_has_no_options(tmp_path: Path) -> None:
    from various_tools.tools.duplicates.images import ImageRules

    plan = Plan(tool="duplicates", roots=[tmp_path], options_json="{not json")
    assert rules_for_plan(plan) == ImageRules()


# ----- cloud-only files are never opened ------------------------------------------------------


class OpenSpy:
    """Records every attempt to open or read the cloud-only file. Other files pass through."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def _wrap(self, label: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(path: Path, *args: Any, **kwargs: Any) -> Any:
            if Path(path).name == self.name:
                self.calls.append(f"{label}:{path}")
                raise AssertionError(f"{label} was called for the cloud-only file {path}")
            return real(path, *args, **kwargs)

        return wrapper

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for module in (imaging, dup_images, verify_ollama):
            monkeypatch.setattr(module, "open_image", self._wrap("open_image", module.open_image))
        for attr in ("image_size", "thumbnail_jpeg"):
            monkeypatch.setattr(compare_view, attr, self._wrap(attr, getattr(compare_view, attr)))


@pytest.fixture
def cloud_only(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], OpenSpy]:
    """``cloud_only(name)``: files with that name report ``cloud_only=True``; opening is spied."""

    def _install(name: str) -> OpenSpy:
        spy = OpenSpy(name)
        spy.install(monkeypatch)

        def fake_facts(path: Path) -> FileFacts:
            facts = file_facts(path)
            return dataclasses.replace(facts, cloud_only=True) if path.name == name else facts

        monkeypatch.setattr(compare_view, "file_facts", fake_facts)
        return spy

    return _install


def test_cloud_only_card_is_not_opened(
    qtbot: QtBot,
    tmp_path: Path,
    pair: tuple[Path, Path],
    make_image: ImageMaker,
    cloud_only: Callable[[str], OpenSpy],
) -> None:
    a, _b = pair
    cloud = make_image("g/cloud.png", seed=1)
    spy = cloud_only("cloud.png")
    # Only the cloud file is the subject, so spy failures cannot come from the other cards.
    plan = group_plan(tmp_path, cloud, cloud)
    dialog = CompareDialog(plan, str(cloud))
    qtbot.addWidget(dialog)
    assert dialog.wait_for_workers(15000)
    for card in dialog.cards:
        assert card.thumb_label.text() == CLOUD_ONLY_TEXT
        assert card.resolution_label.text() == "Resolution: not read (cloud-only)"
        assert card.size_label.text().startswith("Size: ")  # known without reading the content
    assert spy.calls == []
    assert a.exists()


def test_explain_refuses_a_cloud_only_file_without_opening_it(
    qtbot: QtBot,
    tmp_path: Path,
    pair: tuple[Path, Path],
    make_image: ImageMaker,
    cloud_only: Callable[[str], OpenSpy],
) -> None:
    a, _b = pair
    cloud = make_image("g/cloud.png", seed=1)
    # Build the dialog first with normal facts, then turn the cloud flag on for explain.
    plan = group_plan(tmp_path, a, cloud)
    dialog = open_dialog(qtbot, plan, str(a))
    spy = cloud_only("cloud.png")
    for pair_order in ((a, cloud), (cloud, a)):
        before = len(dialog.explain_dialogs)
        worker = dialog.explain_pair(*pair_order)
        qtbot.waitUntil(worker.isFinished, timeout=20000)
        assert dialog.wait_for_workers(15000)
        assert len(dialog.explain_dialogs) == before + 1
        explain = dialog.explain_dialogs[-1]
        assert explain.verdict_label.text() == "skipped: cloud-only (cloud.png)"
        assert explain.table.isHidden()
    assert spy.calls == []


def test_explain_button_on_a_cloud_only_card_is_refused(
    qtbot: QtBot,
    tmp_path: Path,
    pair: tuple[Path, Path],
    make_image: ImageMaker,
    cloud_only: Callable[[str], OpenSpy],
) -> None:
    a, _b = pair
    cloud = make_image("g/cloud.png", seed=1)
    spy = cloud_only("cloud.png")
    plan = group_plan(tmp_path, a, cloud)
    dialog = CompareDialog(plan, str(a))
    qtbot.addWidget(dialog)
    assert dialog.wait_for_workers(15000)
    cloud_card = next(c for c in dialog.cards if c.path == cloud)
    cloud_card.explain_button.click()
    qtbot.waitUntil(lambda: len(dialog.explain_dialogs) == 1, timeout=20000)
    assert dialog.explain_dialogs[0].verdict_label.text() == "skipped: cloud-only (cloud.png)"
    assert dialog.wait_for_workers(15000)
    assert spy.calls == []
    assert cloud_card.thumb_label.text() == CLOUD_ONLY_TEXT


def test_closing_the_dialog_drops_late_results(
    qtbot: QtBot, tmp_path: Path, pair: tuple[Path, Path]
) -> None:
    a, b = pair
    dialog = CompareDialog(group_plan(tmp_path, a, b), str(a))
    qtbot.addWidget(dialog)
    worker = dialog.explain_pair(a, b)
    dialog.close()
    qtbot.waitUntil(worker.isFinished, timeout=20000)
    assert dialog.explain_dialogs == []
