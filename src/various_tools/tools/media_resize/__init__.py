"""Media resize (plan §3.5). The registry finds ``TOOL`` through the entry-point group."""

from various_tools.tools.media_resize.tool import TOOL, MediaResize, MediaResizeOptions

__all__ = ["TOOL", "MediaResize", "MediaResizeOptions"]
