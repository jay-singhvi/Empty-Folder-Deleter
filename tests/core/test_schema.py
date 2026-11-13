"""Field introspection and text parsing (``core/schema.py``)."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field

from various_tools.core.schema import FieldSpec, describe, format_value, parse_value
from various_tools.core.settings import Settings
from various_tools.tools.base import ToolOptions, discover


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class Sample(BaseModel):
    flag: bool = Field(default=True, title="A flag", description="Turns it on.")
    count: int = Field(default=3, ge=1, le=10, title="Count")
    ratio: float = Field(default=0.5, gt=0, lt=1)
    name: str = "plain"
    folder: Path = Path("out")
    mode: Literal["a", "b", "c"] = "b"
    maybe_mode: Literal["x", "y"] | None = None
    maybe_num: int | None = Field(default=None, ge=0)
    only_true: Literal[True] = Field(default=True, json_schema_extra={"locked": True})
    color: Color = Color.RED
    folders: list[Path] = Field(
        default_factory=list, json_schema_extra={"widget": "folders", "group": "Files"}
    )
    tags: list[str] = Field(default_factory=lambda: ["one", "two"])
    sizes: list[float] = Field(default_factory=list)
    kinds: list[Literal["jpg", "png"]] = Field(default_factory=lambda: ["jpg"])
    extra: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)
    annotated: Annotated[int, Field(ge=2)] = 5
    needed: str = Field(
        title="Needed",
        json_schema_extra={
            "group": "Core",
            "advanced": True,
            "widget": "template",
            "depends_on": "mode=a",
        },
    )


def _spec(name: str) -> FieldSpec:
    return next(s for s in describe(Sample) if s.name == name)


def test_describe_keeps_declaration_order() -> None:
    assert [s.name for s in describe(Sample)] == list(Sample.model_fields)


def test_titles_descriptions_and_fallbacks() -> None:
    flag = _spec("flag")
    assert (flag.title, flag.description) == ("A flag", "Turns it on.")
    assert _spec("maybe_num").title == "Maybe num"  # prettified when no title is given
    assert _spec("name").description == ""


@pytest.mark.parametrize(
    ("name", "kind", "item_kind"),
    [
        ("flag", "bool", None),
        ("count", "int", None),
        ("ratio", "float", None),
        ("name", "str", None),
        ("folder", "path", None),
        ("mode", "choice", None),
        ("maybe_mode", "choice", None),
        ("maybe_num", "int", None),
        ("only_true", "bool", None),
        ("color", "choice", None),
        ("folders", "list", "path"),
        ("tags", "list", "str"),
        ("sizes", "list", "float"),
        ("kinds", "list", "choice"),
        ("extra", "dict", "str"),
        ("limits", "dict", "int"),
        ("annotated", "int", None),
    ],
)
def test_kinds(name: str, kind: str, item_kind: str | None) -> None:
    spec = _spec(name)
    assert spec.kind == kind
    assert spec.item_kind == item_kind


def test_choices_defaults_and_requirements() -> None:
    assert _spec("mode").choices == ("a", "b", "c")
    assert _spec("maybe_mode").choices == ("x", "y")
    assert _spec("color").choices == ("red", "blue")
    assert _spec("kinds").choices == ("jpg", "png")
    assert _spec("tags").default == ["one", "two"]  # the factory has been called
    assert _spec("folders").default == []
    assert _spec("mode").default == "b"
    assert _spec("maybe_num").default is None
    assert not _spec("mode").required
    assert _spec("needed").required
    assert _spec("needed").default is None


def test_nullable_and_bounds() -> None:
    assert _spec("maybe_num").nullable
    assert _spec("maybe_mode").nullable
    assert not _spec("count").nullable
    count = _spec("count")
    assert (count.ge, count.le, count.gt, count.lt) == (1, 10, None, None)
    assert count.bounds_text == "1 to 10"
    ratio = _spec("ratio")
    assert (ratio.gt, ratio.lt) == (0, 1)
    assert ratio.bounds_text == "more than 0 and less than 1"
    assert _spec("maybe_num").ge == 0
    assert _spec("maybe_num").bounds_text == "at least 0"
    assert _spec("annotated").ge == 2
    assert _spec("name").bounds_text == ""
    assert not _spec("name").has_bounds


def test_ui_hints() -> None:
    needed = _spec("needed")
    assert needed.group == "Core"
    assert needed.advanced
    assert needed.widget == "template"
    assert needed.depends_on == "mode=a"
    assert _spec("only_true").locked
    assert _spec("folders").widget == "folders"
    assert _spec("folders").group == "Files"
    plain = _spec("name")
    assert (plain.group, plain.advanced, plain.locked, plain.widget, plain.depends_on) == (
        "General",
        False,
        False,
        None,
        None,
    )


def test_every_tool_and_settings_model_can_be_described() -> None:
    for tool in discover().values():
        specs = describe(tool.Options)
        assert [s.name for s in specs] == list(tool.Options.model_fields)
    assert {s.name for s in describe(Settings)} == set(Settings.model_fields)
    delete_mode = next(s for s in describe(Settings) if s.name == "delete_mode")
    assert delete_mode.kind == "choice"
    assert delete_mode.locked
    external = next(s for s in describe(Settings) if s.name == "external_tools")
    assert external.kind == "dict"
    assert external.widget == "tool_paths"


def test_tool_options_base_class_is_describable() -> None:
    class Mine(ToolOptions):
        level: int = Field(default=1, ge=0, le=5, title="Level", description="How much.")

    [spec] = describe(Mine)
    assert (spec.title, spec.le) == ("Level", 5)


# ----- parse_value -----------------------------------------------------------------------


@pytest.mark.parametrize("text", ["true", "True", "YES", "y", "on", "1", " t "])
def test_parse_true_words(text: str) -> None:
    assert parse_value(_spec("flag"), text) is True


@pytest.mark.parametrize("text", ["false", "No", "off", "0", "n", "f"])
def test_parse_false_words(text: str) -> None:
    assert parse_value(_spec("flag"), text) is False


def test_parse_bool_rejects_other_words() -> None:
    with pytest.raises(ValueError, match="flag"):
        parse_value(_spec("flag"), "maybe")


def test_parse_numbers() -> None:
    assert parse_value(_spec("count"), " 7 ") == 7
    assert parse_value(_spec("ratio"), "0.25") == 0.25
    assert parse_value(_spec("maybe_num"), "4") == 4
    with pytest.raises(ValueError, match="whole number"):
        parse_value(_spec("count"), "7.5")
    with pytest.raises(ValueError, match="not a number"):
        parse_value(_spec("ratio"), "half")


def test_parse_choice_matches_exactly_then_ignoring_case() -> None:
    assert parse_value(_spec("mode"), "c") == "c"
    assert parse_value(_spec("mode"), "C") == "c"
    assert parse_value(_spec("color"), "BLUE") == "blue"
    with pytest.raises(ValueError, match="not one of a, b, c"):
        parse_value(_spec("mode"), "z")


def test_parse_nullable_words() -> None:
    for text in ("", "none", "None", "null", "  "):
        assert parse_value(_spec("maybe_num"), text) is None
        assert parse_value(_spec("maybe_mode"), text) is None
    assert parse_value(_spec("maybe_mode"), "x") == "x"
    # a field that is not nullable keeps "none" as text
    assert parse_value(_spec("name"), "none") == "none"


def test_parse_text_and_path() -> None:
    assert parse_value(_spec("name"), " keep  spaces ") == " keep  spaces "
    assert parse_value(_spec("folder"), '"C:\\My Photos"') == "C:\\My Photos"
    assert parse_value(_spec("folder"), "  D:\\x ") == "D:\\x"


def test_parse_lists() -> None:
    assert parse_value(_spec("tags"), "a, b ,c") == ["a", "b", "c"]
    assert parse_value(_spec("tags"), '["x, y", "z"]') == ["x, y", "z"]
    assert parse_value(_spec("tags"), "") == []
    assert parse_value(_spec("sizes"), "0.1, 0.5") == [0.1, 0.5]
    assert parse_value(_spec("sizes"), "[0.1, 2]") == [0.1, 2]
    assert parse_value(_spec("kinds"), "JPG,png") == ["jpg", "png"]
    with pytest.raises(ValueError, match="not one of"):
        parse_value(_spec("kinds"), "gif")
    with pytest.raises(ValueError, match="JSON"):
        parse_value(_spec("tags"), "[unterminated")
    with pytest.raises(ValueError, match="not a number"):
        parse_value(_spec("sizes"), "1, two")


def test_a_single_path_in_a_list_keeps_its_commas() -> None:
    assert parse_value(_spec("folders"), "C:\\Photos, 2020") == ["C:\\Photos, 2020"]
    assert parse_value(_spec("folders"), '["C:\\\\a", "D:\\\\b"]') == ["C:\\a", "D:\\b"]


def test_parse_dicts() -> None:
    assert parse_value(_spec("extra"), "a=1,b=2") == {"a": "1", "b": "2"}
    assert parse_value(_spec("extra"), "exiftool=C:\\Program Files\\x.exe") == {
        "exiftool": "C:\\Program Files\\x.exe"
    }
    assert parse_value(_spec("extra"), "k=a,b") == {"k": "a,b"}  # one pair, comma in the value
    assert parse_value(_spec("extra"), '{"a": "1"}') == {"a": "1"}
    assert parse_value(_spec("limits"), "a=1, b=2") == {"a": 1, "b": 2}
    assert parse_value(_spec("extra"), "") == {}
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_value(_spec("extra"), "novalue")
    with pytest.raises(ValueError, match="not a number|whole number"):
        parse_value(_spec("limits"), "a=x")
    with pytest.raises(ValueError, match="JSON"):
        parse_value(_spec("extra"), "{broken")


def test_parsed_values_are_accepted_by_pydantic() -> None:
    values: dict[str, Any] = {
        "flag": parse_value(_spec("flag"), "no"),
        "count": parse_value(_spec("count"), "9"),
        "folder": parse_value(_spec("folder"), "x/y"),
        "folders": parse_value(_spec("folders"), "x/y"),
        "kinds": parse_value(_spec("kinds"), "png"),
        "maybe_mode": parse_value(_spec("maybe_mode"), "y"),
        "color": parse_value(_spec("color"), "blue"),
        "needed": "x",
    }
    model = Sample.model_validate(values)
    assert model.folder == Path("x/y")
    assert model.folders == [Path("x/y")]
    assert model.color is Color.BLUE
    assert model.kinds == ["png"]
    assert model.flag is False


def test_format_value() -> None:
    assert format_value(None) == ""
    assert format_value(True) == "true"
    assert format_value(False) == "false"
    assert format_value(Color.RED) == "red"
    assert format_value(["a", 1, Path("p")]) == "a, 1, p"
    assert format_value({"k": "v", "n": 2}) == "k=v, n=2"
    assert format_value(3.5) == "3.5"
