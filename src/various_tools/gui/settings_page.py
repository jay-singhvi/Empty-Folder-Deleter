"""Settings page: every app-wide setting in a generated form, the external programs, and Save.

The form is generated from the ``Settings`` model, so a new setting shows up here without any page
code. Changes are written only when the user clicks Save. A value that fails validation keeps the
page as it is and shows the problem next to the field. Locked (safety) values ask for confirmation
inside the form before they change.

Saving also applies the theme at once and, when the toggle changed, adds or removes the Explorer
right-click menu.

Tests replace :attr:`SettingsPage.confirm` and :attr:`SettingsPage.inform` so no message box opens,
and pass a ``FakeRegistry`` as ``shell_backend`` so the real registry is never touched.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from various_tools.core.settings import Settings
from various_tools.gui import shell, theme
from various_tools.gui.state import AppState
from various_tools.gui.widgets.external_tools import ExternalToolsPanel
from various_tools.gui.widgets.schema_form import SchemaForm

log = logging.getLogger(__name__)


def _default_confirm(title: str, text: str) -> bool:
    answer = QMessageBox.question(
        None,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def _default_inform(title: str, text: str) -> None:
    QMessageBox.warning(None, title, text)


class SettingsPage(QWidget):
    """The settings form, the external programs panel and the Save, Revert and Restore buttons."""

    def __init__(
        self,
        state: AppState,
        *,
        shell_backend: shell.RegistryBackend | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._shell_backend = shell_backend
        self.confirm: Callable[[str, str], bool] = _default_confirm
        self.inform: Callable[[str, str], None] = _default_inform

        self.form = SchemaForm(Settings, state.settings, exclude={"external_tools"})
        self.tools_panel = ExternalToolsPanel(state)

        self.save_button = QPushButton("Save")
        self.revert_button = QPushButton("Revert")
        self.defaults_button = QPushButton("Restore defaults")
        self.save_button.setToolTip("Write the settings to disk and apply them.")
        self.revert_button.setToolTip("Throw away unsaved changes and show the saved settings.")
        self.defaults_button.setToolTip(
            "Show every setting at its default value. Nothing is saved until you click Save."
        )
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.addWidget(self.form)
        container_layout.addWidget(self.tools_panel)
        container_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(container)

        buttons = QHBoxLayout()
        buttons.addWidget(self.status_label, 1)
        buttons.addWidget(self.defaults_button)
        buttons.addWidget(self.revert_button)
        buttons.addWidget(self.save_button)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addLayout(buttons)

        self.save_button.clicked.connect(lambda _checked=False: self.save())
        self.revert_button.clicked.connect(lambda _checked=False: self.revert())
        self.defaults_button.clicked.connect(lambda _checked=False: self.restore_defaults())
        self.tools_panel.settingsSaved.connect(self._panel_saved)

    def _panel_saved(self) -> None:
        """The programs panel saved the Ollama address: show it in the form too, so a later
        Save of the form does not put the old address back."""
        self.form.update_values({"ollama_url": self._state.settings.ollama_url})

    # ----- buttons ---------------------------------------------------------------------------

    def save(self) -> bool:
        """Validate the form and save it. Returns False, changing nothing, if it is not valid."""
        old = self._state.settings
        try:
            built = self.form.build()
        except ValidationError:
            self.status_label.setText("Not saved: fix the highlighted settings.")
            return False
        # The program paths are edited in the panel, which saves them itself, so keep the
        # current ones rather than the copy the form was loaded with.
        data = built.model_dump()
        data["external_tools"] = dict(old.external_tools)
        try:
            new = Settings.model_validate(data)
        except ValidationError as exc:
            self.status_label.setText(f"Not saved: {exc}")
            return False

        shell_failed = False
        if new.shell_integration != old.shell_integration:
            wanted = new.shell_integration
            try:
                shell.set_enabled(wanted, self._shell_backend)
            except OSError as exc:
                log.warning("Could not change the right-click menu: %s", exc)
                shell_failed = True
                # Keep the setting true to what Explorer shows.
                new = new.model_copy(update={"shell_integration": old.shell_integration})
                self.inform(
                    "Folder right-click menu",
                    f"The right-click menu could not be {'added' if wanted else 'removed'}: {exc}",
                )
        try:
            self._state.save_settings(new)
        except OSError as exc:
            self.status_label.setText(f"Not saved: {exc}")
            self.inform("Settings", f"The settings could not be saved: {exc}")
            return False
        if shell_failed:
            self.form.update_values({"shell_integration": new.shell_integration})

        app = QApplication.instance()
        if isinstance(app, QApplication):
            theme.apply_theme(app, new.theme)
        if self.tools_panel.isVisible():
            self.tools_panel.refresh()
        self.status_label.setText("Saved.")
        return True

    def revert(self) -> None:
        """Show the saved settings again, dropping unsaved changes."""
        self.form.set_values(self._state.settings)
        self.status_label.setText("")

    def restore_defaults(self) -> None:
        """After confirming, show every setting at its default. Click Save to keep them."""
        if not self.confirm(
            "Restore defaults",
            "Show every setting at its default value?\n\n"
            "Nothing is saved until you click Save, and Revert brings your settings back.",
        ):
            return
        self.form.reset()
        self.status_label.setText("Defaults shown. Click Save to keep them.")
