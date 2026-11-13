"""Write ``packaging/icon.ico`` from the drawing in ``various_tools.gui.icon``.

Run with ``uv run python packaging/make_icon.py``. The window icon is drawn at runtime from the
same code, so the two never drift apart.
"""

from pathlib import Path

from various_tools.gui.icon import write_ico

if __name__ == "__main__":
    target = Path(__file__).with_name("icon.ico")
    write_ico(str(target))
    print(f"wrote {target}")
