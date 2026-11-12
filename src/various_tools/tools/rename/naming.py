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

CaseMode = Literal["keep", "lower", "upper"]

# The placeholders a template may use. ``time`` needs a time of day, which not every date has.
TEMPLATE_FIELDS = frozenset({"date", "time", "counter", "ext", "name"})

_FILENAME_DATE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?:[_-]?(\d{2})(\d{2})(\d{2}))?(?!\d)")
_INVALID_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|$)", re.IGNORECASE)
MAX_NAME_LENGTH = 255


def date_from_filename(name: str, min_year: int = 1990) -> tuple[datetime, bool] | None:
    """The first valid ``YYYYMMDD`` (optionally followed by ``HHMMSS``) in ``name``.

    Returns ``(when, has_time)``, or None when the name holds no plausible date. Digits that are
    part of a longer number are not used.
    """
    for match in _FILENAME_DATE.finditer(name):
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        has_time = match.group(4) is not None
        hour = int(match.group(4)) if has_time else 0
        minute = int(match.group(5)) if has_time else 0
        second = int(match.group(6)) if has_time else 0
        try:
            when = datetime(year, month, day, hour, minute, second)
        except ValueError:
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
