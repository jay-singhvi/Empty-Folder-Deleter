"""Built-in rename presets (plan §3.3). A preset is a starting point; any field can be overridden.

The ``snapchat`` preset reproduces ``legacy/rename_snapchat.py`` exactly: the same files match,
they are dated by modified time, numbered per minute, and the extension is lower-cased.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from various_tools.tools.rename.naming import CaseMode

MatchMode = Literal["glob", "regex"]
DateSource = Literal["capture", "modified", "created", "filename"]
CounterScope = Literal["per-minute", "per-day", "global"]


@dataclass(frozen=True)
class RenameSpec:
    """Everything a rename run needs, after the preset and any overrides are merged."""

    label: str
    match: str
    match_mode: MatchMode
    template: str
    date_source: DateSource
    counter_scope: CounterScope
    case: CaseMode


PRESETS: dict[str, RenameSpec] = {
    "snapchat": RenameSpec(
        label="Snapchat export",
        match=r"Snapchat-\d+\.(?:jpe?g|png|mp4|mov|mkv)",
        match_mode="regex",
        template="Snapchat-{date:%Y%m%d}-{time:%H_%M}-{counter:03}{ext}",
        date_source="modified",
        counter_scope="per-minute",
        case="lower",
    ),
    "whatsapp": RenameSpec(
        label="WhatsApp image",
        match=r"IMG-\d{8}-WA\d+\.(?:jpe?g|png|webp)",
        match_mode="regex",
        template="WhatsApp-{date:%Y%m%d}-{counter:03}{ext}",
        date_source="filename",
        counter_scope="per-day",
        case="lower",
    ),
    "screenshot": RenameSpec(
        label="Screenshot",
        match=r"Screenshot_\d{8}[_-]\d{6}\.(?:png|jpe?g|mp4)",
        match_mode="regex",
        template="Screenshot-{date:%Y%m%d}-{time:%H_%M}-{counter:03}{ext}",
        date_source="filename",
        counter_scope="per-minute",
        case="lower",
    ),
}
