"""Choosing several folders: the dialog setup, the clean-up of its answer and the Add button."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QListView, QTreeView
from pytestqt.qtbot import QtBot

from various_tools.gui.widgets import path_picker
from various_tools.gui.widgets.path_picker import PathListEdit, folder_dialog, normalise_folders


def test_the_folder_dialog_allows_several_folders(qtbot: QtBot) -> None:
    dialog = folder_dialog(None, "Pick", "")
    qtbot.addWidget(dialog)
    assert dialog.fileMode() == QFileDialog.FileMode.Directory
    assert dialog.testOption(QFileDialog.Option.ShowDirsOnly)
    assert dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
    views = [*dialog.findChildren(QListView), *dialog.findChildren(QTreeView)]
    assert views
    for view in views:
        assert view.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection


def test_normalise_folders_drops_repeats_and_empty_entries() -> None:
    a = os.path.normpath("C:/data/a")
    result = normalise_folders(["C:/data/a", "", "  ", "C:/data/a/", "C:/DATA/A", "C:/data/b"])
    assert result == [a, os.path.normpath("C:/data/b")]


def test_add_takes_every_folder_the_dialog_returns(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edit = PathListEdit()
    qtbot.addWidget(edit)
    asked: list[str] = []

    def fake(_parent: object, title: str, start: str = "") -> list[str]:
        asked.append(title)
        return [str(tmp_path / "a"), str(tmp_path / "b"), str(tmp_path / "a")]

    monkeypatch.setattr(path_picker, "choose_folders", fake)
    with qtbot.waitSignal(edit.changed):
        edit.add_button.click()
    assert edit.paths() == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert asked and "folders" in asked[0].lower()


def test_add_with_a_cancelled_dialog_changes_nothing(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    edit = PathListEdit()
    qtbot.addWidget(edit)
    monkeypatch.setattr(path_picker, "choose_folders", lambda *_a, **_k: [])
    with qtbot.assertNotEmitted(edit.changed):
        edit.add_button.click()
    assert edit.paths() == []
