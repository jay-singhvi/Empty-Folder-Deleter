"""Fix File Extensions. The registry finds ``TOOL`` through the entry-point group."""

from various_tools.tools.wrong_extension.tool import (
    TOOL,
    WrongExtension,
    WrongExtensionOptions,
    set_many_selected,
    set_selected,
)

__all__ = ["TOOL", "WrongExtension", "WrongExtensionOptions", "set_many_selected", "set_selected"]
