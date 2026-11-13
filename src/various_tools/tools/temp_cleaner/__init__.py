"""Temp cleaner (plan §4 Phase 7). The registry finds ``TOOL`` through the entry-point group."""

from various_tools.tools.temp_cleaner.tool import (
    BROWSER_CACHE_PREFIX,
    TOOL,
    BrowserCache,
    TempCleaner,
    TempCleanerOptions,
    default_temp_roots,
    find_browser_caches,
    parse_browser_cache_note,
)

__all__ = [
    "BROWSER_CACHE_PREFIX",
    "TOOL",
    "BrowserCache",
    "TempCleaner",
    "TempCleanerOptions",
    "default_temp_roots",
    "find_browser_caches",
    "parse_browser_cache_note",
]
