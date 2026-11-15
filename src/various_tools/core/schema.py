"""Field introspection for options and settings models, shared by the CLI and the GUI.

``describe(Model)`` turns a pydantic model into a list of :class:`FieldSpec`, one per field in
declaration order. Each spec carries what a front end needs to draw the field: a label (``title``),
help text (``description``), a ``kind`` (checkbox, number, text, path, dropdown, list, key/value
table), bounds, the default and the UI hints from ``json_schema_extra`` (``group``, ``advanced``,
``locked``, ``widget``, ``depends_on``, ``choices_from``, ``pair_separator``, ``pair_headers``).

``parse_value(spec, text)`` turns text typed by a person (a CLI flag, a config value) into a
value that pydantic accepts for that field, and :func:`format_value` does the reverse for display.

Conventions this module understands (see ``tools/base.py``): ``X | None = None`` means "use the
app setting"; ``Literal[...]`` is a fixed set of choices; ``ge``/``gt``/``le``/``lt`` are bounds.
There is no UI code here and no Qt import, so ``core/`` stays free of both.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin

from annotated_types import Ge, Gt, Le, Lt
from pydantic import BaseModel
from pydantic.fields import FieldInfo

FieldKind = Literal["bool", "int", "float", "str", "path", "choice", "list", "dict"]

DEFAULT_GROUP = "General"
_TRUE_WORDS = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_WORDS = frozenset({"0", "false", "f", "no", "n", "off"})
_NONE_WORDS = frozenset({"", "none", "null"})
_LIST_ORIGINS = (list, set, frozenset, tuple)


@dataclass(frozen=True)
class FieldSpec:
    """Everything a front end needs to know about one model field.

    ``default`` is the value (a ``default_factory`` has already been called); copy it before
    changing it. ``choices`` holds the allowed values as text for ``choice`` fields, and for a
    ``list`` of choices it holds the allowed items. ``item_kind`` is the kind of a list item or
    of a dict value.
    """

    name: str
    title: str
    description: str
    annotation: Any
    kind: FieldKind
    item_kind: FieldKind | None
    choices: tuple[str, ...]
    default: Any
    required: bool
    nullable: bool
    ge: float | None
    gt: float | None
    le: float | None
    lt: float | None
    group: str
    advanced: bool
    locked: bool
    widget: str | None
    depends_on: str | None
    # Name of a source in ``core/choices.py`` that lists suggestions at run time (for example
    # ``ollama_models``). The field stays free text; a front end may show an editable dropdown
    # and fill it in a worker thread with ``dynamic_choices(choices_from, ctx)``.
    choices_from: str | None = None
    # ``widget = "pairs"``: each list item is two cells joined by ``pair_separator`` (split at
    # its first occurrence), shown under ``pair_headers`` (exactly two titles, or empty).
    pair_separator: str | None = None
    pair_headers: tuple[str, ...] = ()

    @property
    def has_bounds(self) -> bool:
        return any(b is not None for b in (self.ge, self.gt, self.le, self.lt))

    @property
    def bounds_text(self) -> str:
        """Bounds in words, for example ``1 to 64`` or ``at least 0``. Empty when unbounded."""
        low = _fmt_number(self.ge) if self.ge is not None else None
        high = _fmt_number(self.le) if self.le is not None else None
        if low is not None and high is not None:
            return f"{low} to {high}"
        if low is not None:
            return f"at least {low}"
        if high is not None:
            return f"at most {high}"
        parts: list[str] = []
        if self.gt is not None:
            parts.append(f"more than {_fmt_number(self.gt)}")
        if self.lt is not None:
            parts.append(f"less than {_fmt_number(self.lt)}")
        return " and ".join(parts)


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


# ----- introspection ---------------------------------------------------------------------


def _strip_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """``X | None`` becomes ``(X, True)``. Several non-None members stay a union."""
    annotation = _strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        members: list[Any] = [m for m in get_args(annotation) if m is not type(None)]
        nullable = len(members) != len(get_args(annotation))
        if len(members) == 1:
            return _strip_annotated(members[0]), nullable
        return annotation, nullable
    return annotation, False


def _literal_values(annotation: Any) -> list[Any]:
    values: list[Any] = []
    for arg in get_args(annotation):
        if get_origin(arg) is Literal:
            values.extend(_literal_values(arg))
        else:
            values.append(arg)
    return values


def _scalar_kind(annotation: Any) -> tuple[FieldKind, tuple[str, ...]]:
    """The kind of a non-container type, and its choices for a ``choice``."""
    annotation = _strip_annotated(annotation)
    if get_origin(annotation) is Literal:
        values = [v for v in _literal_values(annotation) if v is not None]
        if values and all(isinstance(v, bool) for v in values):
            return "bool", ()
        return "choice", tuple(str(v) for v in values)
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return "choice", tuple(str(m.value) for m in annotation)
        if issubclass(annotation, bool):
            return "bool", ()
        if issubclass(annotation, int):
            return "int", ()
        if issubclass(annotation, float):
            return "float", ()
        if issubclass(annotation, Path):
            return "path", ()
    return "str", ()


def _classify(annotation: Any) -> tuple[FieldKind, FieldKind | None, tuple[str, ...]]:
    """``(kind, item_kind, choices)`` for an annotation with ``None`` and ``Annotated`` removed."""
    origin = get_origin(annotation)
    if origin in _LIST_ORIGINS or annotation in _LIST_ORIGINS:
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        if not args:
            return "list", "str", ()
        item_kind, choices = _scalar_kind(_unwrap_optional(args[0])[0])
        return "list", item_kind, choices
    if origin is dict or annotation is dict:
        args = get_args(annotation)
        if len(args) == 2:
            item_kind, choices = _scalar_kind(_unwrap_optional(args[1])[0])
            return "dict", item_kind, choices
        return "dict", "str", ()
    kind, choices = _scalar_kind(annotation)
    return kind, None, choices


def _bound(info: FieldInfo, meta_type: type[Any], attr: str) -> float | None:
    for meta in info.metadata:
        if isinstance(meta, meta_type):
            value: Any = getattr(meta, attr, None)
            if isinstance(value, int | float):
                return float(value)
    return None


def _default_of(info: FieldInfo) -> Any:
    if info.is_required():
        return None
    factory: Callable[..., Any] | None = info.default_factory
    if factory is None:
        return info.default
    try:
        return factory()
    except TypeError:
        return factory({})


def _extra(info: FieldInfo) -> Mapping[str, Any]:
    extra: Any = info.json_schema_extra
    if isinstance(extra, Mapping):
        return cast("Mapping[str, Any]", extra)
    return {}


def _prettify(name: str) -> str:
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def describe(model: type[BaseModel]) -> list[FieldSpec]:
    """One :class:`FieldSpec` per field of ``model``, in declaration order."""
    specs: list[FieldSpec] = []
    for name, info in model.model_fields.items():
        annotation: Any = info.annotation
        inner, nullable = _unwrap_optional(annotation)
        kind, item_kind, choices = _classify(inner)
        extra = _extra(info)
        widget = extra.get("widget")
        depends_on = extra.get("depends_on")
        choices_from = extra.get("choices_from")
        specs.append(
            FieldSpec(
                name=name,
                title=info.title or _prettify(name),
                description=info.description or "",
                annotation=annotation,
                kind=kind,
                item_kind=item_kind,
                choices=choices,
                default=_default_of(info),
                required=info.is_required(),
                nullable=nullable,
                ge=_bound(info, Ge, "ge"),
                gt=_bound(info, Gt, "gt"),
                le=_bound(info, Le, "le"),
                lt=_bound(info, Lt, "lt"),
                group=str(extra.get("group") or DEFAULT_GROUP),
                advanced=bool(extra.get("advanced", False)),
                locked=bool(extra.get("locked", False)),
                widget=str(widget) if widget else None,
                depends_on=str(depends_on) if depends_on else None,
                choices_from=str(choices_from) if choices_from else None,
                pair_separator=_pair_separator(extra),
                pair_headers=_pair_headers(extra),
            )
        )
    return specs


def _pair_separator(extra: Mapping[str, Any]) -> str | None:
    value = extra.get("pair_separator")
    return value if isinstance(value, str) and value else None


def _pair_headers(extra: Mapping[str, Any]) -> tuple[str, ...]:
    value: Any = extra.get("pair_headers")
    if not isinstance(value, list | tuple):
        return ()
    items = [str(item) for item in cast("list[Any] | tuple[Any, ...]", value)]
    return tuple(items) if len(items) == 2 else ()


# ----- parsing ---------------------------------------------------------------------------


def _item_annotation(spec: FieldSpec) -> Any:
    """The annotation of a list item or dict value, for mapping choice text to its value."""
    inner, _ = _unwrap_optional(spec.annotation)
    args = [a for a in get_args(inner) if a is not Ellipsis]
    if spec.kind == "list" and args:
        return _unwrap_optional(args[0])[0]
    if spec.kind == "dict" and len(args) == 2:
        return _unwrap_optional(args[1])[0]
    return str


def _choice_value(annotation: Any, text: str, choices: tuple[str, ...]) -> Any:
    """Map text to the matching choice (exact first, then ignoring case)."""
    annotation = _strip_annotated(annotation)
    wanted = text.strip()
    options: list[tuple[str, Any]]
    if get_origin(annotation) is Literal:
        options = [(str(v), v) for v in _literal_values(annotation)]
    elif isinstance(annotation, type) and issubclass(annotation, Enum):
        options = [(str(m.value), m.value) for m in annotation]
    else:
        options = [(c, c) for c in choices]
    for shown, value in options:
        if shown == wanted:
            return value
    for shown, value in options:
        if shown.casefold() == wanted.casefold():
            return value
    raise ValueError(f"{text!r} is not one of {', '.join(c for c, _ in options)}")


def _parse_scalar(kind: FieldKind, annotation: Any, choices: tuple[str, ...], text: str) -> Any:
    wanted = text.strip()
    if kind == "bool":
        word = wanted.casefold()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise ValueError(f"{text!r} is not yes or no (use true/false, yes/no, on/off, 1/0)")
    if kind == "int":
        try:
            return int(wanted)
        except ValueError:
            raise ValueError(f"{text!r} is not a whole number") from None
    if kind == "float":
        try:
            return float(wanted)
        except ValueError:
            raise ValueError(f"{text!r} is not a number") from None
    if kind == "choice":
        return _choice_value(annotation, text, choices)
    if kind == "path":
        return _unquote(wanted)
    return text


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1]
    return text


def _split_commas(text: str) -> list[str]:
    return [piece.strip() for piece in text.split(",") if piece.strip()]


def _parse_list(spec: FieldSpec, text: str) -> list[Any]:
    wanted = text.strip()
    item_kind: FieldKind = spec.item_kind or "str"
    item_annotation = _item_annotation(spec)
    raw_items: list[Any]
    if wanted.startswith("["):
        try:
            loaded: Any = json.loads(wanted)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not a valid JSON list: {exc.msg}") from None
        if not isinstance(loaded, list):
            raise ValueError("not a JSON list")
        raw_items = cast("list[Any]", loaded)
        return [
            _parse_scalar(item_kind, item_annotation, spec.choices, item)
            if isinstance(item, str)
            else item
            for item in raw_items
        ]
    if not wanted:
        return []
    # A path may contain a comma, so a single path is kept whole. Give several as JSON.
    pieces = [_unquote(wanted)] if item_kind == "path" else _split_commas(wanted)
    return [_parse_scalar(item_kind, item_annotation, spec.choices, piece) for piece in pieces]


def _parse_dict(spec: FieldSpec, text: str) -> dict[str, Any]:
    wanted = text.strip()
    item_kind: FieldKind = spec.item_kind or "str"
    item_annotation = _item_annotation(spec)
    if wanted.startswith("{"):
        try:
            loaded: Any = json.loads(wanted)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not a valid JSON object: {exc.msg}") from None
        if not isinstance(loaded, dict):
            raise ValueError("not a JSON object")
        return {str(k): v for k, v in cast("dict[Any, Any]", loaded).items()}
    if not wanted:
        return {}
    pieces = _split_commas(wanted)
    if not all("=" in piece for piece in pieces):
        pieces = [wanted]  # a single pair whose value contains commas
    result: dict[str, Any] = {}
    for piece in pieces:
        key, sep, value = piece.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"{piece!r} is not KEY=VALUE")
        result[key.strip()] = _parse_scalar(item_kind, item_annotation, spec.choices, value.strip())
    return result


def parse_value(spec: FieldSpec, text: str) -> Any:
    """Turn typed text into a value pydantic accepts for ``spec``. Raises ``ValueError``.

    * a nullable field takes ``""``, ``none`` or ``null`` as ``None`` (use the app setting);
    * bool: ``true/false``, ``yes/no``, ``on/off``, ``1/0``;
    * int and float: plain numbers; choice: one of ``spec.choices`` (case does not matter);
    * path: the text, without surrounding double quotes (pydantic makes the ``Path``);
    * list: a JSON list, or comma-separated items (a single path is kept whole);
    * dict: a JSON object, or ``key=value`` pairs separated by commas.

    Bounds are not checked here; pydantic checks them when the model is built.
    """
    if spec.nullable and text.strip().casefold() in _NONE_WORDS:
        return None
    try:
        if spec.kind == "list":
            return _parse_list(spec, text)
        if spec.kind == "dict":
            return _parse_dict(spec, text)
        return _parse_scalar(spec.kind, _unwrap_optional(spec.annotation)[0], spec.choices, text)
    except ValueError as exc:
        raise ValueError(f"{spec.name}: {exc}") from None


# ----- display ---------------------------------------------------------------------------


def format_value(value: Any) -> str:
    """A value as short text, for help, tables and ``config get``. ``None`` is an empty string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Mapping):
        pairs = cast("Mapping[Any, Any]", value)
        return ", ".join(f"{k}={format_value(v)}" for k, v in pairs.items())
    if isinstance(value, list | tuple | set | frozenset):
        items: list[Any] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        return ", ".join(format_value(item) for item in items)
    return str(value)
