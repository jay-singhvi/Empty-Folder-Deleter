"""SchemaForm editors added for the plan review: live dropdowns, the rules table and list text.

* a ``choices_from`` field is an editable dropdown filled by a worker (plan 3.4, Ollama models);
* a ``widget = "pairs"`` list is a two-column table (plan 7.2, sort-by-type rules);
* the generic comma-separated list box keeps an item that holds a comma in one piece.

Nothing here reaches the network: the choice loader is a fake that returns a worker.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from various_tools.gui.widgets.schema_form import DynamicChoiceEdit, PairsEdit, SchemaForm
from various_tools.gui.workers import Worker


class Model(BaseModel):
    model_name: str = Field(default="m:1", json_schema_extra={"choices_from": "models"})
    rules: list[str] = Field(
        default_factory=lambda: ["*.pdf=Docs", "*.jpg|*.png=Pics"],
        json_schema_extra={
            "widget": "pairs",
            "pair_separator": "=",
            "pair_headers": ["Pattern(s)", "Folder"],
        },
    )
    tags: list[str] = Field(default_factory=lambda: ["one", "two"])


def loader(answers: dict[str, list[str]], calls: list[str]) -> Callable[[str], Worker | None]:
    def load(source: str) -> Worker:
        calls.append(source)
        return Worker(lambda _w: list(answers.get(source, [])))

    return load


# ----- live dropdown -----------------------------------------------------------------------------


def test_choices_from_field_is_an_editable_dropdown_filled_when_shown(qtbot: QtBot) -> None:
    calls: list[str] = []
    answers = {"models": ["a:latest", "b:7b"]}
    form = SchemaForm(Model, load_choices=loader(answers, calls))
    qtbot.addWidget(form)
    edit = form.editor("model_name")
    assert isinstance(edit, DynamicChoiceEdit)
    assert edit.combo.isEditable()
    assert calls == []  # nothing runs before the form is shown
    form.show()
    qtbot.waitUntil(lambda: edit.combo.count() == 2, timeout=5000)
    assert calls == ["models"]
    assert [edit.combo.itemText(i) for i in range(2)] == ["a:latest", "b:7b"]
    assert edit.combo.currentText() == "m:1"  # the current value is kept
    assert form.raw_values()["model_name"] == "m:1"
    form.hide()
    form.show()
    assert calls == ["models"]  # filled once; Refresh asks again


def test_a_value_not_in_the_list_stays_valid_and_refresh_reloads(qtbot: QtBot) -> None:
    calls: list[str] = []
    answers = {"models": ["a:latest"]}
    form = SchemaForm(Model, load_choices=loader(answers, calls))
    qtbot.addWidget(form)
    form.show()
    edit = form.editor("model_name")
    assert isinstance(edit, DynamicChoiceEdit)
    qtbot.waitUntil(lambda: edit.combo.count() == 1, timeout=5000)
    edit.combo.setEditText("custom:3b")
    assert form.build().model_dump()["model_name"] == "custom:3b"
    answers["models"] = ["a:latest", "z:1"]
    assert edit.refresh_button.isVisible()
    edit.refresh_button.click()
    qtbot.waitUntil(lambda: edit.combo.count() == 2, timeout=5000)
    assert calls == ["models", "models"]
    assert edit.combo.currentText() == "custom:3b"


def test_an_empty_list_leaves_a_plain_editable_box(qtbot: QtBot) -> None:
    calls: list[str] = []
    form = SchemaForm(Model, load_choices=loader({}, calls))
    qtbot.addWidget(form)
    form.show()
    edit = form.editor("model_name")
    assert isinstance(edit, DynamicChoiceEdit)
    qtbot.waitUntil(lambda: calls == ["models"] and not edit.loading, timeout=5000)
    assert edit.combo.count() == 0
    edit.combo.setEditText("typed")
    assert form.raw_values()["model_name"] == "typed"


def test_without_a_loader_there_is_no_refresh_and_set_values_works(qtbot: QtBot) -> None:
    form = SchemaForm(Model)
    qtbot.addWidget(form)
    form.show()
    edit = form.editor("model_name")
    assert isinstance(edit, DynamicChoiceEdit)
    assert not edit.refresh_button.isVisible()
    form.set_values({"model_name": "x:y"})
    assert edit.combo.currentText() == "x:y"
    changes: list[int] = []
    form.changed.connect(lambda: changes.append(1))
    edit.combo.setEditText("x:z")
    assert changes and form.raw_values()["model_name"] == "x:z"


def test_the_duplicate_finder_model_field_uses_the_dropdown(qtbot: QtBot) -> None:
    from various_tools.tools.duplicates import DuplicatesOptions

    form = SchemaForm(DuplicatesOptions)
    qtbot.addWidget(form)
    assert isinstance(form.editor("ollama_model"), DynamicChoiceEdit)


# ----- rules table ------------------------------------------------------------------------------


def test_pairs_field_is_a_two_column_table(qtbot: QtBot) -> None:
    form = SchemaForm(Model)
    qtbot.addWidget(form)
    table = form.editor("rules")
    assert isinstance(table, PairsEdit)
    headers = [table.table.model().headerData(c, Qt.Orientation.Horizontal) for c in range(2)]
    assert headers == ["Pattern(s)", "Folder"]
    assert table.rows() == [("*.pdf", "Docs"), ("*.jpg|*.png", "Pics")]
    assert form.raw_values()["rules"] == ["*.pdf=Docs", "*.jpg|*.png=Pics"]


def test_pairs_table_add_remove_and_move(qtbot: QtBot) -> None:
    form = SchemaForm(Model)
    qtbot.addWidget(form)
    table = form.editor("rules")
    assert isinstance(table, PairsEdit)
    changes: list[int] = []
    form.changed.connect(lambda: changes.append(1))

    table.add_button.click()
    row = table.table.rowCount() - 1
    for column, text in ((0, "*.mp4"), (1, "Videos")):
        item = table.table.item(row, column)
        assert item is not None
        item.setText(text)
    assert form.raw_values()["rules"][-1] == "*.mp4=Videos"

    table.table.setCurrentCell(row, 0)
    table.up_button.click()
    assert form.raw_values()["rules"] == ["*.pdf=Docs", "*.mp4=Videos", "*.jpg|*.png=Pics"]
    assert table.table.currentRow() == 1
    table.down_button.click()
    assert form.raw_values()["rules"][-1] == "*.mp4=Videos"

    table.table.setCurrentCell(0, 0)
    table.remove_button.click()
    assert form.raw_values()["rules"] == ["*.jpg|*.png=Pics", "*.mp4=Videos"]
    assert changes  # every edit tells the page


def test_pairs_table_splits_at_the_first_separator_and_skips_blank_rows(qtbot: QtBot) -> None:
    form = SchemaForm(Model, {"rules": ["a=b=c", "no-separator"]})
    qtbot.addWidget(form)
    table = form.editor("rules")
    assert isinstance(table, PairsEdit)
    assert table.rows() == [("a", "b=c"), ("no-separator", "")]
    table.add_button.click()  # a blank row is ignored
    assert form.raw_values()["rules"] == ["a=b=c", "no-separator"]


def test_sort_by_type_rules_round_trip_through_the_table(qtbot: QtBot) -> None:
    from various_tools.tools.sort_by_type import TOOL as SORT

    form = SchemaForm(SORT.Options)
    qtbot.addWidget(form)
    assert isinstance(form.editor("rules"), PairsEdit)
    rules = ["*.pdf=Documents/PDF", "*.jpg|*.png=Pictures"]
    form.set_values({"rules": rules})
    assert form.build().model_dump()["rules"] == rules


# ----- list text with commas ----------------------------------------------------------------------


def test_a_list_item_with_a_comma_is_not_split(qtbot: QtBot) -> None:
    form = SchemaForm(Model, {"tags": ["a, b", "c"]})
    qtbot.addWidget(form)
    assert form.raw_values()["tags"] == ["a, b", "c"]
    assert form.build().model_dump()["tags"] == ["a, b", "c"]


def test_plain_list_items_still_show_comma_separated(qtbot: QtBot) -> None:
    form = SchemaForm(Model)
    qtbot.addWidget(form)
    line = form.editor("tags")
    assert line.text() == "one, two"  # type: ignore[attr-defined]
