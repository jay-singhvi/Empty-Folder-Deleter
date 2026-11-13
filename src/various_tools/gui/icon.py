"""The app icon, drawn in code so no image file has to ship with the package.

:func:`draw_icon` paints it with Pillow at any size. ``packaging/make_icon.py`` writes the same
drawing to ``packaging/icon.ico`` for PyInstaller and the installer; :func:`app_icon` turns it
into a ``QIcon`` for the window.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
_BLUE = (37, 99, 235, 255)
_WHITE = (255, 255, 255, 255)
_AMBER = (245, 158, 11, 255)


def draw_icon(size: int = 256) -> Image.Image:
    """A rounded blue square with a white folder and an amber check mark."""
    scale = 4  # draw large, then shrink, for smooth edges
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=big // 5, fill=_BLUE)
    # Folder: a tab and a body.
    left, right = big * 0.18, big * 0.82
    top, bottom = big * 0.30, big * 0.76
    draw.rounded_rectangle(
        (left, top - big * 0.07, left + big * 0.26, top + big * 0.05), radius=big // 40, fill=_WHITE
    )
    draw.rounded_rectangle((left, top, right, bottom), radius=big // 25, fill=_WHITE)
    # Check mark.
    width = max(1, int(big * 0.07))
    points = [(big * 0.34, big * 0.53), (big * 0.46, big * 0.65), (big * 0.68, big * 0.42)]
    draw.line(points, fill=_AMBER, width=width, joint="curve")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def icon_png(size: int) -> bytes:
    buffer = io.BytesIO()
    draw_icon(size).save(buffer, format="PNG")
    return buffer.getvalue()


def write_ico(path: str) -> None:
    """Write a multi-size Windows .ico file."""
    draw_icon(256).save(path, format="ICO", sizes=[(s, s) for s in ICON_SIZES])


def app_icon():  # -> QIcon (Qt is imported here so packaging/make_icon.py needs no Qt)
    from PySide6.QtGui import QIcon, QPixmap

    icon = QIcon()
    for size in ICON_SIZES:
        pixmap = QPixmap()
        pixmap.loadFromData(icon_png(size), "PNG")
        icon.addPixmap(pixmap)
    return icon
