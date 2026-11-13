"""The tool contract (plan §3.3). Each tool is a package that registers itself through the
``various_tools.tools`` entry-point group, so adding a tool needs no GUI or CLI code.

``scan()`` must never change the disk. ``apply()`` runs the plan through the generic engine.

Options conventions (every tool, owner requirement "nothing is constant"):

* every field has a ``title`` and a ``description`` (plain English, units, what the default
  means). ``tests/tools/test_option_hints.py`` fails when one is missing;
* fixed choice sets are ``Literal`` (rendered as a dropdown), numbers carry ``ge``/``le``;
* ``json_schema_extra`` holds ``group``, ``advanced``, ``widget``, ``depends_on`` and ``locked``
  (🔒 tuned or safety value: keep the default unless you know why);
* a field whose app-wide default lives in :class:`~various_tools.core.settings.Settings` is
  ``X | None = None``, and ``None`` means "use the setting". The description names the setting.
"""

from __future__ import annotations

import logging
from importlib.metadata import EntryPoint, entry_points
from typing import Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict

from various_tools.core.context import RunContext
from various_tools.core.plan import Plan, RunResult

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "various_tools.tools"

# Sidebar sections. A new tool picks one; adding a section is a one-line change here.
Category = Literal["Clean up", "Media", "Organize", "Developer"]
CATEGORIES: tuple[str, ...] = get_args(Category)


class ToolOptions(BaseModel):
    """Base class for every tool's options. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class Tool(Protocol):
    id: str
    name: str
    description: str
    category: Category
    Options: type[ToolOptions]

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan: ...

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult: ...


def _load(entry: EntryPoint) -> Tool | None:
    """Load one entry point, or log why it was skipped. A broken tool never hides the others."""
    try:
        tool: Tool = entry.load()
    except Exception:  # noqa: BLE001 - any import error in a plugin must not stop the app
        log.exception("Skipping tool %r: entry point %s failed to load", entry.name, entry.value)
        return None
    problems: list[str] = []
    for attr in ("id", "name", "description", "category", "Options", "scan", "apply"):
        if not hasattr(tool, attr):
            problems.append(f"missing {attr}")
    if not problems:
        if getattr(tool, "category", None) not in CATEGORIES:
            problems.append(f"category {tool.category!r} is not one of {', '.join(CATEGORIES)}")
        options = getattr(tool, "Options", None)
        if not (isinstance(options, type) and issubclass(options, ToolOptions)):
            problems.append("Options is not a ToolOptions subclass")
    if problems:
        log.error("Skipping tool %r: %s", entry.name, "; ".join(problems))
        return None
    return tool


def discover() -> dict[str, Tool]:
    """Every tool registered in the entry-point group, keyed by tool id and sorted by id.

    A tool that fails to import or does not meet the contract is logged and skipped.
    """
    found: dict[str, Tool] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        tool = _load(entry)
        if tool is None:
            continue
        if tool.id in found:
            log.error("Skipping tool %r: id %r is already registered", entry.name, tool.id)
            continue
        found[tool.id] = tool
    return dict(sorted(found.items()))
