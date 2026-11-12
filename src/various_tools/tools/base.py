"""The tool contract (plan §3.3). Each tool is a package that registers itself through the
``various_tools.tools`` entry-point group, so adding a tool needs no GUI or CLI code.

``scan()`` must never change the disk. ``apply()`` runs the plan through the generic engine.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from various_tools.core.context import RunContext
from various_tools.core.plan import Plan, RunResult

ENTRY_POINT_GROUP = "various_tools.tools"


class ToolOptions(BaseModel):
    """Base class for every tool's options. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class Tool(Protocol):
    id: str
    name: str
    description: str
    category: str
    Options: type[ToolOptions]

    def scan(self, opts: ToolOptions, ctx: RunContext) -> Plan: ...

    def apply(self, plan: Plan, ctx: RunContext) -> RunResult: ...


def discover() -> dict[str, Tool]:
    """Every tool registered in the entry-point group, keyed by tool id and sorted by id."""
    found: dict[str, Tool] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        tool: Tool = entry.load()
        found[tool.id] = tool
    return dict(sorted(found.items()))
