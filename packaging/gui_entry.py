"""Entry script PyInstaller freezes into ``Various Tools.exe`` (the desktop app, no console)."""

import sys

from various_tools.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
