"""The Apply window: the summary, then how fast to work (all at once, or in batches with a wait).

Working in batches keeps a sync client such as OneDrive from being flooded. The pure helpers
(:func:`onedrive_suggestion`, :func:`estimate_wait`) need no Qt, so they are easy to test.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from various_tools.core import onedrive
from various_tools.core.context import Pace
from various_tools.core.plan import Plan
from various_tools.gui.plan_edit import human_size
from various_tools.gui.wording import (
    ALL_AT_ONCE,
    CUSTOM_PRESET,
    HOW_FAST,
    ONEDRIVE_BATCH_REASON,
    PACE_PRESETS,
    format_wait,
)

PACE_KEY = "__apply_pace__"
UNITS: tuple[tuple[str, int], ...] = (("seconds", 1), ("minutes", 60), ("hours", 3600))
SUGGEST_BYTES = 1024**3
SUGGEST_FILES = 500
UPLOAD_CHECK_TEXT = "Wait until OneDrive has uploaded each batch (at most"
UPLOAD_MINUTES_DEFAULT = 30


@dataclass(frozen=True)
class ApplyAsk:
    """What the Apply window needs: its title and summary, how many actions are ticked and an
    optional one-line reason to prefer batches."""

    title: str
    summary: str
    count: int
    suggestion: str | None = None
    touches_onedrive: bool = False


def plan_touches_onedrive(plan: Plan) -> bool:
    """True when a ticked action reads or writes inside a OneDrive folder (names only)."""
    return any(
        (a.src is not None and onedrive.is_inside_onedrive(a.src))
        or (a.dst is not None and onedrive.is_inside_onedrive(a.dst))
        for a in plan.actions
        if a.selected
    )


def pace_to_preselect(
    saved: Mapping[str, Any], *, touches_onedrive: bool, picked_this_session: bool
) -> dict[str, Any]:
    """The remembered pace as the Apply window should preselect it.

    A remembered "in batches" choice is preselected only when the plan touches OneDrive or the
    person chose it in this session; otherwise the window opens on "all at once" (the numbers
    are kept, so choosing batches again shows the old ones).
    """
    chosen = dict(saved)
    if chosen.get("mode") == "batches" and not (touches_onedrive or picked_this_session):
        chosen["mode"] = "all"
    return chosen


def onedrive_suggestion(plan: Plan, sizes: Sequence[int | None]) -> str | None:
    """The reason to work in batches, or None.

    It applies when a ticked action reads or writes inside a OneDrive folder and the ticked
    actions are more than 1 GB or 500 files. Compares names only; it never reads a file.
    """
    chosen = [(i, a) for i, a in enumerate(plan.actions) if a.selected]
    if not chosen or not any(
        (a.src is not None and onedrive.is_inside_onedrive(a.src))
        or (a.dst is not None and onedrive.is_inside_onedrive(a.dst))
        for _i, a in chosen
    ):
        return None
    size = sum(sizes[i] or 0 for i, _a in chosen if i < len(sizes))
    if size <= SUGGEST_BYTES and len(chosen) <= SUGGEST_FILES:
        return None
    return ONEDRIVE_BATCH_REASON.format(count=f"{len(chosen):,}", size=human_size(size))


def estimate_wait(count: int, pace: Pace | None) -> float:
    """Seconds spent waiting (not working) for ``count`` actions with this pace."""
    if pace is None or count <= pace.batch_size:
        return 0.0
    return (math.ceil(count / pace.batch_size) - 1) * pace.pause_s


class ApplyDialog(QDialog):
    def __init__(
        self,
        ask: ApplyAsk,
        last: Mapping[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(ask.title)
        self._count = ask.count
        self._touches_onedrive = ask.touches_onedrive
        saved = dict(last or {})

        summary = QLabel(ask.summary)
        summary.setWordWrap(True)
        self.all_radio = QRadioButton(ALL_AT_ONCE)
        self.batch_radio = QRadioButton("In batches of")
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 1_000_000)
        self.batch_spin.setSuffix(" files")
        self.wait_spin = QSpinBox()
        self.wait_spin.setRange(0, 100_000)
        self.unit_combo = QComboBox()
        for name, seconds in UNITS:
            self.unit_combo.addItem(name, seconds)
        self.preset_combo = QComboBox()
        for label, _batch, _secs in PACE_PRESETS:
            self.preset_combo.addItem(label)
        self.preset_combo.addItem(CUSTOM_PRESET)
        self.reason_label = QLabel(ask.suggestion or "")
        self.reason_label.setWordWrap(True)
        self.reason_label.setVisible(bool(ask.suggestion))
        self.estimate_label = QLabel()
        self.upload_check = QCheckBox(UPLOAD_CHECK_TEXT)
        self.upload_spin = QSpinBox()
        self.upload_spin.setRange(1, 100_000)
        upload_row = QHBoxLayout()
        upload_row.addWidget(self.upload_check)
        upload_row.addWidget(self.upload_spin)
        upload_row.addWidget(QLabel("minutes)"))
        upload_row.addStretch(1)
        self.upload_widgets = (self.upload_check, self.upload_spin)

        batch_row = QHBoxLayout()
        for widget in (
            self.batch_radio,
            self.batch_spin,
            QLabel(", then wait"),
            self.wait_spin,
            self.unit_combo,
        ):
            batch_row.addWidget(widget)
        batch_row.addStretch(1)
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Choice:"))
        preset_row.addWidget(self.preset_combo, 1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(QLabel(f"<b>{HOW_FAST}</b>"))
        layout.addWidget(self.all_radio)
        layout.addLayout(batch_row)
        layout.addLayout(preset_row)
        layout.addLayout(upload_row)
        layout.addWidget(self.reason_label)
        layout.addWidget(self.estimate_label)
        layout.addWidget(self.buttons)

        self._fill(saved, bool(ask.suggestion))
        self._fill_upload(saved)
        self.upload_check.setVisible(ask.touches_onedrive)
        self.upload_spin.setVisible(ask.touches_onedrive)
        upload_row.itemAt(2).widget().setVisible(ask.touches_onedrive)  # pyright: ignore[reportOptionalMemberAccess]
        self.all_radio.toggled.connect(self._changed)
        self.batch_spin.valueChanged.connect(self._edited)
        self.wait_spin.valueChanged.connect(self._edited)
        self.unit_combo.currentIndexChanged.connect(self._edited)
        self.preset_combo.currentIndexChanged.connect(self._preset_chosen)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._changed()

    def _fill(self, saved: Mapping[str, Any], suggested: bool) -> None:
        batch, wait, unit = PACE_PRESETS[0][1], PACE_PRESETS[0][2], "seconds"
        try:
            if int(saved.get("batch_size", 0)) >= 1:
                batch = int(saved["batch_size"])
                wait = max(0, int(saved.get("wait", 0)))
                unit = str(saved.get("unit", "minutes"))
        except TypeError, ValueError:
            pass
        index = max(self.unit_combo.findText(unit), 0)
        if unit == "seconds" and wait % 60 == 0 and wait:
            index, wait = 1, wait // 60  # show 300 s as 5 minutes
        self.batch_spin.setValue(batch)
        self.wait_spin.setValue(wait)
        self.unit_combo.setCurrentIndex(index)
        self._select_matching_preset()
        batches = saved.get("mode") == "batches"
        self.batch_radio.setChecked(suggested or batches)
        self.all_radio.setChecked(not (suggested or batches))

    def _fill_upload(self, saved: Mapping[str, Any]) -> None:
        """On by default when the plan touches OneDrive; the minutes come from the saved choice."""
        wanted = saved.get("wait_for_upload")
        self.upload_check.setChecked(wanted if isinstance(wanted, bool) else True)
        minutes = UPLOAD_MINUTES_DEFAULT
        with contextlib.suppress(TypeError, ValueError):
            minutes = max(1, int(saved.get("upload_minutes", minutes)))
        self.upload_spin.setValue(minutes)

    def _wait_seconds(self) -> float:
        return float(self.wait_spin.value() * int(self.unit_combo.currentData()))

    def _select_matching_preset(self) -> None:
        index = self.preset_combo.count() - 1  # Custom
        for i, (_label, batch, secs) in enumerate(PACE_PRESETS):
            if batch == self.batch_spin.value() and secs == self._wait_seconds():
                index = i
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(index)
        self.preset_combo.blockSignals(False)

    def _preset_chosen(self, index: int) -> None:
        if index >= len(PACE_PRESETS):
            return
        _label, batch, secs = PACE_PRESETS[index]
        for widget in (self.batch_spin, self.wait_spin, self.unit_combo):
            widget.blockSignals(True)
        self.batch_spin.setValue(batch)
        unit = 0 if secs % 60 else (2 if secs >= 3600 and secs % 3600 == 0 else 1)
        self.unit_combo.setCurrentIndex(unit)
        self.wait_spin.setValue(secs // int(self.unit_combo.currentData()))
        for widget in (self.batch_spin, self.wait_spin, self.unit_combo):
            widget.blockSignals(False)
        self.batch_radio.setChecked(True)
        self._changed()

    def _edited(self, *_: object) -> None:
        self._select_matching_preset()
        self._changed()

    def _changed(self, *_: object) -> None:
        batches = self.batch_radio.isChecked()
        for widget in (self.batch_spin, self.wait_spin, self.unit_combo, self.preset_combo):
            widget.setEnabled(batches)
        for widget in self.upload_widgets:
            widget.setEnabled(batches)
        wait = estimate_wait(self._count, self.pace())
        self.estimate_label.setText(
            f"Extra waiting time: about {format_wait(wait)}." if wait else "No extra waiting."
        )

    def pace(self) -> Pace | None:
        """The chosen pace, or None for all at once (or batches with nothing to wait)."""
        if not self.batch_radio.isChecked():
            return None
        upload = self._touches_onedrive and self.upload_check.isChecked()
        return Pace(
            self.batch_spin.value(),
            self._wait_seconds(),
            wait_for_upload=upload,
            upload_timeout_s=float(self.upload_spin.value() * 60),
        )

    def values(self) -> dict[str, Any]:
        """The choice, as stored for next time."""
        return {
            "mode": "batches" if self.batch_radio.isChecked() else "all",
            "batch_size": self.batch_spin.value(),
            "wait": self.wait_spin.value(),
            "unit": str(self.unit_combo.currentText()),
            "wait_for_upload": self.upload_check.isChecked(),
            "upload_minutes": self.upload_spin.value(),
        }
