"""Narrowing helpers for JSON-like data (ffprobe output, plan files, Ollama replies, settings).

``json.loads`` gives ``Any``. An ``isinstance(x, dict)`` check on that narrows to a dict of
unknown types, which strict type checking rejects. These helpers narrow to ``dict[str, Any]`` or
``list[Any]`` and return an empty value for anything else, so callers never see ``None``.
"""

from __future__ import annotations

from typing import Any, cast


def as_dict(value: object) -> dict[str, Any]:
    """``value`` as a dict with string-ish keys, or ``{}`` when it is not a dict."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    """``value`` as a list, or ``[]`` when it is not a list."""
    return cast("list[Any]", value) if isinstance(value, list) else []
