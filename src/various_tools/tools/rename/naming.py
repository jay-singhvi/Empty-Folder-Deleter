"""Pure naming helpers for the rename tool (plan §3.3). Nothing here touches the disk.

* ``date_from_filename()`` finds a date (and optionally a time) in a file name, such as
  ``Screenshot_20240101_123456.png``.
* ``template_problem()`` checks a name template before any file is looked at.
* ``render_name()`` builds a new name from the template.
* ``name_problem()`` rejects names that Windows cannot hold.
"""

from __future__ import annotations

import re
import string
from datetime import datetime
from typing import Literal

from various_tools.core.settings import DEFAULT_MIN_PLAUSIBLE_YEAR

CaseMode = Literal["keep", "lower", "upper"]

# The placeholders a template may use. ``time`` needs a time of day, which not every date has.
TEMPLATE_FIELDS = frozenset({"date", "time", "counter", "ext", "name"})

# Finds ``YYYYMMDD``, optionally followed by ``HHMMSS``, and ignores digits that belong to a
# longer number. It is the default of the "filename date pattern" option. A replacement needs
# three groups (year, month, day), or six when it also finds a time (hour, minute, second).
FILENAME_DATE_PATTERN = r"(?<!\d)(\d{4})(\d{2})(\d{2})(?:[_-]?(\d{2})(\d{2})(\d{2}))?(?!\d)"
_FILENAME_DATE = re.compile(FILENAME_DATE_PATTERN)
_INVALID_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|$)", re.IGNORECASE)
MAX_NAME_LENGTH = 255


def filename_pattern_problem(pattern: str) -> str | None:
    """Why a filename date pattern cannot be used, or None. It must compile and have the groups
    that :func:`date_from_filename` reads: 3 (year, month, day) or 6 (and hour, minute, second)."""
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"The filename date pattern is not a valid regular expression: {exc}"
    if compiled.groups not in (3, 6):
        return (
            "The filename date pattern needs 3 groups (year, month, day), "
            "or 6 with hour, minute and second."
        )
    return None


def date_from_filename(
    name: str, min_year: int = DEFAULT_MIN_PLAUSIBLE_YEAR, pattern: str | None = None
) -> tuple[datetime, bool] | None:
    """The first valid date in ``name``: ``YYYYMMDD``, optionally followed by ``HHMMSS``.

    ``pattern`` replaces the default regular expression (see :func:`filename_pattern_problem`).
    Returns ``(when, has_time)``, or None when the name holds no plausible date. With the default
    pattern, digits that are part of a longer number are not used.
    """
    regex = _FILENAME_DATE if pattern is None else re.compile(pattern)
    for match in regex.finditer(name):
        groups: list[str | None] = list(match.groups())
        has_time = len(groups) >= 6 and groups[3] is not None
        try:
            year, month, day = int(groups[0] or ""), int(groups[1] or ""), int(groups[2] or "")
            hour, minute, second = (int(g or "") for g in groups[3:6]) if has_time else (0, 0, 0)
            when = datetime(year, month, day, hour, minute, second)
        except ValueError, TypeError:
            continue
        if when.year >= min_year:
            return when, has_time
    return None


def template_fields(template: str) -> set[str]:
    """The placeholder names used in ``template``. Raises ``ValueError`` on malformed braces."""
    return {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}


def template_problem(template: str) -> str | None:
    """Why a template cannot be used, or None. ``{ext}`` is required so the file type is kept."""
    if not template:
        return "The name template is empty."
    try:
        fields = template_fields(template)
    except ValueError:
        return "The name template is not valid: check the braces."
    unknown = sorted(f for f in fields if f not in TEMPLATE_FIELDS)
    if unknown:
        shown = ", ".join("{" + f + "}" for f in unknown)
        return (
            f"The name template uses unknown field(s) {shown}. "
            "Use date, time, counter, ext or name."
        )
    if "ext" not in fields:
        return "The name template must include {ext}, so that the file keeps its type."
    return None


def render_name(
    template: str,
    *,
    when: datetime,
    has_time: bool,
    counter: int,
    ext: str,
    stem: str,
) -> str:
    """The new file name. ``date`` and ``time`` use strftime codes, e.g. ``{date:%Y%m%d}``."""
    return template.format(
        date=when,
        time=when if has_time else None,
        counter=counter,
        ext=ext,
        name=stem,
    )


def apply_case(suffix: str, case: CaseMode) -> str:
    """The extension in the chosen letter case. Only the extension is changed, never the name."""
    if case == "lower":
        return suffix.lower()
    if case == "upper":
        return suffix.upper()
    return suffix


def name_problem(name: str) -> str | None:
    """Why ``name`` is not a file name Windows accepts, or None."""
    if name in ("", ".", ".."):
        return "is not a usable file name"
    if any(ch in _INVALID_CHARS or ord(ch) < 32 for ch in name):
        return "contains a character that Windows does not allow"
    if name.endswith((" ", ".")):
        return "ends with a space or a dot"
    if _RESERVED_NAME.match(name):
        return "is a reserved Windows name"
    if len(name) > MAX_NAME_LENGTH:
        return "is too long"
    return None
