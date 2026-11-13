"""Entry script PyInstaller freezes into ``various-tools.exe`` (the command line)."""

import sys

from various_tools.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
