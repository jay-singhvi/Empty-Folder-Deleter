"""Every option and setting explains itself (owner requirement: nothing is constant).

Each field of every registered tool's options model, and of the app settings, must carry a
``title`` and a ``description``. Numbers need bounds and fixed choices need a ``Literal``, so the
GUI can render spinners and dropdowns. Locked (🔒) fields must be advanced and say so.
"""

from __future__ import annotations

import types
import typing
from typing import Any, Literal, get_args, get_origin

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from various_tools.core.settings import Settings
from various_tools.tools.base import CATEGORIES, ToolOptions, discover

TOOLS = discover()
MODELS: list[tuple[str, type[BaseModel]]] = [("settings", Settings)]
MODELS.extend((tool_id, tool.Options) for tool_id, tool in TOOLS.items())
FIELDS = [
    pytest.param(model, name, info, id=f"{label}.{name}")
    for label, model in MODELS
    for name, info in model.model_fields.items()
]


def _extra(info: FieldInfo) -> dict[str, Any]:
    extra = info.json_schema_extra
    return dict(extra) if isinstance(extra, dict) else {}


def _bare(annotation: Any) -> Any:
    """``X | None`` and ``Optional[X]`` give ``X``."""
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _has_bound(info: FieldInfo, kind: str) -> bool:
    return any(hasattr(m, kind) and getattr(m, kind) is not None for m in info.metadata)


def test_every_tool_is_registered_with_a_known_category() -> None:
    assert len(TOOLS) >= 6
    for tool in TOOLS.values():
        assert tool.category in CATEGORIES
        assert issubclass(tool.Options, ToolOptions)


@pytest.mark.parametrize(("model", "name", "info"), FIELDS)
def test_field_has_title_and_description(
    model: type[BaseModel], name: str, info: FieldInfo
) -> None:
    assert info.title and info.title.strip(), f"{model.__name__}.{name} has no title"
    assert info.description and len(info.description.strip()) >= 20, (
        f"{model.__name__}.{name} needs a description saying what it is for and what it does"
    )


@pytest.mark.parametrize(("model", "name", "info"), FIELDS)
def test_numbers_have_bounds(model: type[BaseModel], name: str, info: FieldInfo) -> None:
    bare = _bare(info.annotation)
    if bare in (int, float):
        lower = _has_bound(info, "ge") or _has_bound(info, "gt")
        upper = _has_bound(info, "le") or _has_bound(info, "lt")
        assert lower and upper, f"{model.__name__}.{name} needs ge/gt and le/lt bounds"


@pytest.mark.parametrize(("model", "name", "info"), FIELDS)
def test_locked_fields_are_advanced_and_say_so(
    model: type[BaseModel], name: str, info: FieldInfo
) -> None:
    extra = _extra(info)
    if extra.get("locked"):
        assert extra.get("advanced") is True, f"{model.__name__}.{name}: 🔒 fields are advanced"
        assert "🔒" in (info.description or ""), f"{model.__name__}.{name}: say it is locked"


@pytest.mark.parametrize(("model", "name", "info"), FIELDS)
def test_hints_have_a_group(model: type[BaseModel], name: str, info: FieldInfo) -> None:
    assert _extra(info).get("group"), f"{model.__name__}.{name} needs a group hint"


@pytest.mark.parametrize(("model", "name", "info"), FIELDS)
def test_defaults_validate(model: type[BaseModel], name: str, info: FieldInfo) -> None:
    """The default is a legal value, so a form showing defaults can always be submitted."""
    if info.is_required():
        return
    default = info.get_default(call_default_factory=True)
    if default is None:
        return
    bare = _bare(info.annotation)
    if get_origin(bare) is Literal:
        assert default in get_args(bare), f"{model.__name__}.{name} default not in its choices"
