"""Sort by type (plan §4 Phase 7). The registry finds ``TOOL`` through the entry-point group."""

from various_tools.tools.sort_by_type.tool import (
    DEFAULT_RULES,
    TOOL,
    Rule,
    SortByType,
    SortByTypeOptions,
    parse_rule,
)

__all__ = ["DEFAULT_RULES", "TOOL", "Rule", "SortByType", "SortByTypeOptions", "parse_rule"]
