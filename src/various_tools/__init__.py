"""Various Tools: a shared core, plugin tools, and a CLI and GUI generated from them."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The version lives only in pyproject.toml; read it back from installed metadata.
    __version__ = version("various-tools")
except PackageNotFoundError:  # pragma: no cover - only when run without installing the project
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
