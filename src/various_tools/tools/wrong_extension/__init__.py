"""Fix File Extensions. The registry finds ``TOOL`` through the entry-point group."""

from various_tools.tools.wrong_extension.tool import TOOL, WrongExtension, WrongExtensionOptions

__all__ = ["TOOL", "WrongExtension", "WrongExtensionOptions"]
