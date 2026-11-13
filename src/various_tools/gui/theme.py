"""Light and dark look for the app: the Fusion style with an explicit colour palette.

``apply_theme(app, mode)`` takes the ``theme`` setting ("system", "light" or "dark") and returns
the look that is actually in use ("light" or "dark"). In "system" mode the app follows the Windows
light or dark setting, also while it is running.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

MODES = ("system", "light", "dark")

_FOLLOW_PROPERTY = "variousToolsFollowSystem"
_CONNECTED_PROPERTY = "variousToolsThemeConnected"


def _palette(dark: bool) -> QPalette:
    """An explicit palette with good contrast, dim disabled text and visible selections."""
    if dark:
        window, base, alt_base = "#2b2d30", "#1f2023", "#26282b"
        text, bright, button = "#e6e6e6", "#ffffff", "#383b3f"
        highlight, highlighted_text, link = "#3d7fd9", "#ffffff", "#6cb0ff"
        disabled, tip_base, tip_text = "#7b7e83", "#3a3d41", "#f0f0f0"
        placeholder = "#8c8f94"
    else:
        window, base, alt_base = "#f2f2f2", "#ffffff", "#f7f7f7"
        text, bright, button = "#1d1d1f", "#000000", "#e4e4e4"
        highlight, highlighted_text, link = "#2a6fd0", "#ffffff", "#0b57b8"
        disabled, tip_base, tip_text = "#8a8a8d", "#fffbe6", "#1d1d1f"
        placeholder = "#76767a"

    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: window,
        QPalette.ColorRole.WindowText: text,
        QPalette.ColorRole.Base: base,
        QPalette.ColorRole.AlternateBase: alt_base,
        QPalette.ColorRole.ToolTipBase: tip_base,
        QPalette.ColorRole.ToolTipText: tip_text,
        QPalette.ColorRole.Text: text,
        QPalette.ColorRole.Button: button,
        QPalette.ColorRole.ButtonText: text,
        QPalette.ColorRole.BrightText: bright,
        QPalette.ColorRole.Link: link,
        QPalette.ColorRole.LinkVisited: link,
        QPalette.ColorRole.Highlight: highlight,
        QPalette.ColorRole.HighlightedText: highlighted_text,
        QPalette.ColorRole.PlaceholderText: placeholder,
    }
    for role, colour in roles.items():
        palette.setColor(role, QColor(colour))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(disabled))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Highlight,
        QColor("#4a4d52" if dark else "#b8b8bb"),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, QColor(disabled)
    )
    return palette


def _system_is_dark(app: QApplication) -> bool:
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _set_color_scheme(app: QApplication, scheme: Qt.ColorScheme) -> None:
    """Tell Qt (6.8+) which scheme is wanted, so title bars and native bits match."""
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):
        hints.setColorScheme(scheme)


def _on_system_scheme_changed(app: QApplication) -> None:
    if app.property(_FOLLOW_PROPERTY):
        app.setPalette(_palette(_system_is_dark(app)))


def apply_theme(app: QApplication, mode: str) -> str:
    """Apply "system", "light" or "dark" and return the effective "light" or "dark".

    An unknown ``mode`` is treated as "system".
    """
    if mode not in MODES:
        mode = "system"
    app.setStyle("Fusion")
    follow = mode == "system"
    app.setProperty(_FOLLOW_PROPERTY, follow)
    if mode == "dark":
        _set_color_scheme(app, Qt.ColorScheme.Dark)
        dark = True
    elif mode == "light":
        _set_color_scheme(app, Qt.ColorScheme.Light)
        dark = False
    else:
        _set_color_scheme(app, Qt.ColorScheme.Unknown)
        dark = _system_is_dark(app)
    app.setPalette(_palette(dark))
    if not app.property(_CONNECTED_PROPERTY):
        app.setProperty(_CONNECTED_PROPERTY, True)
        hints = app.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_args: _on_system_scheme_changed(app))
    return "dark" if dark else "light"
