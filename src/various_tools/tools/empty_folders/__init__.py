"""Empty Folders tool (plan §3.1). The registry finds ``TOOL`` through the entry-point group."""

from various_tools.tools.empty_folders.tool import (
    TOOL,
    EmptyFolders,
    EmptyFoldersOptions,
    set_folder_selected,
    set_folders_selected,
)

__all__ = [
    "TOOL",
    "EmptyFolders",
    "EmptyFoldersOptions",
    "set_folder_selected",
    "set_folders_selected",
]
