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

**Extras.** A tool may also offer one-off commands that are not a scan and an apply, such as
exporting its catalog. It lists them in an optional ``extras`` attribute (a tuple of
:class:`ToolExtra`); callers read it with ``getattr(tool, "extras", ())``, so tools without
extras need nothing. The CLI turns each extra into a subcommand and the GUI into a button.

**Working folder.** The GUI has one working folder that every tool looks in by default. A tool's
*workspace field* is the option that receives it: the optional ``workspace_field`` attribute (the
option's name; ``None`` opts out, as the Temp Cleaner does because its folders are extra temp
folders), else the tool's first folder option when it is named ``roots``, ``sources`` or ``root``.
Callers read it with ``getattr(tool, "workspace_field", ...)``; tools that need nothing set nothing.
"""

from __future__ import annotations

import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
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


@dataclass(frozen=True)
class ToolExtra:
    """A one-off command a tool offers besides scan and apply (see the module docstring).

    ``run`` does the work for the path the person chose and returns a one-line result. It
    raises :class:`ValueError` with a message a person can read when it cannot be done.
    """

    id: str  # kebab-case, becomes the CLI subcommand name, e.g. "catalog-export"
    label: str  # button text
    description: str
    path_kind: Literal["save_file", "open_file"]
    file_filter: str  # Qt-style, e.g. "Excel workbook (*.xlsx);;CSV file (*.csv)"
    run: Callable[[Path, RunContext], str]  # returns a one-line result; raises ValueError


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


def _builtin_entry_points() -> list[EntryPoint]:
    """Entry points for the tool packages shipped inside ``various_tools.tools``.

    Used only when no entry point is registered, which happens in a frozen (PyInstaller) build
    that did not copy the package metadata. Each sub-package is expected to export ``TOOL``.
    """
    from various_tools import tools as package

    return [
        EntryPoint(
            name=info.name, value=f"{package.__name__}.{info.name}:TOOL", group=ENTRY_POINT_GROUP
        )
        for info in pkgutil.iter_modules(package.__path__)
        if info.ispkg
    ]


def discover() -> dict[str, Tool]:
    """Every tool registered in the entry-point group, keyed by tool id and sorted by id.

    A tool that fails to import or does not meet the contract is logged and skipped. When the
    group is empty (a frozen build without package metadata), the built-in tool packages are
    loaded instead.
    """
    found: dict[str, Tool] = {}
    entries = list(entry_points(group=ENTRY_POINT_GROUP)) or _builtin_entry_points()
    for entry in entries:
        tool = _load(entry)
        if tool is None:
            continue
        if tool.id in found:
            log.error("Skipping tool %r: id %r is already registered", entry.name, tool.id)
            continue
        found[tool.id] = tool
    return dict(sorted(found.items()))
