"""``discover()``: a broken or non-conforming plugin is logged and skipped, the rest still load."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from various_tools.tools import base

_MISSING = object()


class _Opts(base.ToolOptions):
    pass


class _NotOpts:
    pass


def _tool(tid: str, **overrides: Any) -> type:
    attrs: dict[str, Any] = {
        "id": tid,
        "name": tid.title(),
        "description": "d",
        "category": "Media",
        "Options": _Opts,
        "scan": lambda self, opts, ctx: None,
        "apply": lambda self, plan, ctx: None,
    }
    attrs.update(overrides)
    attrs = {k: v for k, v in attrs.items() if v is not _MISSING}
    return type(f"Tool_{tid}", (), attrs)


class _FakeEntry:
    def __init__(self, name: str, loader: Callable[[], object]) -> None:
        self.name = name
        self.value = f"fake.module:{name}"
        self._loader = loader

    def load(self) -> object:
        return self._loader()


def _patch(monkeypatch: pytest.MonkeyPatch, entries: list[_FakeEntry]) -> None:
    def fake(group: str) -> list[_FakeEntry]:
        assert group == base.ENTRY_POINT_GROUP
        return entries

    monkeypatch.setattr(base, "entry_points", fake)


def _entry(name: str, tool: object) -> _FakeEntry:
    return _FakeEntry(name, lambda: tool)


def test_broken_entry_point_is_logged_and_good_tools_still_load(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom() -> object:
        raise ImportError("no module named nothing")

    _patch(
        monkeypatch,
        [_entry("b", _tool("b")), _FakeEntry("broken", boom), _entry("a", _tool("a"))],
    )
    with caplog.at_level(logging.ERROR, logger=base.log.name):
        found = base.discover()
    assert list(found) == ["a", "b"]  # sorted by id
    assert "broken" in caplog.text and "failed to load" in caplog.text


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"category": "Nonsense"}, "category"),
        ({"description": _MISSING}, "missing description"),
        ({"scan": _MISSING}, "missing scan"),
        ({"Options": _NotOpts}, "ToolOptions"),
        ({"Options": "not a class"}, "ToolOptions"),
    ],
)
def test_non_conforming_tool_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, Any],
    message: str,
) -> None:
    _patch(monkeypatch, [_entry("bad", _tool("bad", **overrides)), _entry("ok", _tool("ok"))])
    with caplog.at_level(logging.ERROR, logger=base.log.name):
        found = base.discover()
    assert list(found) == ["ok"]
    assert message in caplog.text


def test_duplicate_id_keeps_the_first_and_skips_the_second(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first, second = _tool("same", name="First"), _tool("same", name="Second")
    _patch(monkeypatch, [_entry("one", first), _entry("two", second), _entry("x", _tool("x"))])
    with caplog.at_level(logging.ERROR, logger=base.log.name):
        found = base.discover()
    assert list(found) == ["same", "x"]
    assert found["same"] is first
    assert "already registered" in caplog.text


def test_every_category_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [_entry(f"t{i}", _tool(f"t{i}", category=c)) for i, c in enumerate(base.CATEGORIES)]
    _patch(monkeypatch, entries)
    assert len(base.discover()) == len(base.CATEGORIES)


def test_real_entry_points_all_load() -> None:
    found = base.discover()
    assert found
    assert all(t.category in base.CATEGORIES for t in found.values())
