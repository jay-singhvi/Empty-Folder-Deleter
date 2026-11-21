"""The filter bar shared by the Timeline and the Map.

Both pages show one of these on the same :class:`~various_tools.gui.library_model.LibraryModel`,
so a change in one is seen in the other. It holds: a search box (file name or folder),
Photos / Videos / Both, a From and a To month and the folder. Nothing here reads a file.
"""

from __future__ import annotations

import calendar
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from various_tools.core.media.library import MediaFilter
from various_tools.gui.library_model import LibraryModel

ANY_MONTH = "Any month"
ALL_FOLDERS = "All folders"
KINDS: tuple[tuple[str, str], ...] = (
    ("both", "Photos and videos"),
    ("photo", "Photos only"),
    ("video", "Videos only"),
)
SEARCH_DELAY_MS = 200


def month_text(year: int, month: int) -> str:
    return f"{calendar.month_name[month]} {year}"


def month_key(year: int, month: int) -> str:
    """The value kept in a month box: "2024-01"."""
    return f"{year:04d}-{month:02d}"


def parse_month_key(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    year, _dash, month = value.partition("-")
    return (int(year), int(month)) if year.isdigit() and month.isdigit() else None


class FilterBar(QWidget):
    def __init__(self, model: LibraryModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by file name or folder")
        self.search.setClearButtonEnabled(True)
        self.kind_box = QComboBox()
        for value, label in KINDS:
            self.kind_box.addItem(label, value)
        self.from_box = QComboBox()
        self.to_box = QComboBox()
        self.folder_box = QComboBox()
        self.folder_box.setMinimumContentsLength(14)
        self.folder_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Show everything again")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.search, 2)
        layout.addWidget(self.kind_box)
        layout.addWidget(QLabel("From"))
        layout.addWidget(self.from_box)
        layout.addWidget(QLabel("to"))
        layout.addWidget(self.to_box)
        layout.addWidget(self.folder_box, 1)
        layout.addWidget(self.clear_button)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SEARCH_DELAY_MS)
        self._timer.timeout.connect(self.apply_now)
        self.search.textChanged.connect(lambda _t: self._timer.start())
        for box in (self.kind_box, self.from_box, self.to_box, self.folder_box):
            box.currentIndexChanged.connect(lambda _i: self.apply_now())
        self.clear_button.clicked.connect(lambda _c=False: self.model.set_filter(MediaFilter()))

        model.itemsChanged.connect(self._fill_choices)
        model.filterChanged.connect(self._show_filter)
        self._fill_choices()

    # ----- from the widgets to the model -----------------------------------------------------

    def current_filter(self) -> MediaFilter:
        start = self.from_box.currentData()
        end = self.to_box.currentData()
        folder = self.folder_box.currentData()
        return MediaFilter(
            text=self.search.text().strip(),
            kind=str(self.kind_box.currentData() or "both"),
            start=parse_month_key(start),
            end=parse_month_key(end),
            folder=Path(str(folder)) if folder else None,
        )

    def apply_now(self) -> None:
        """Send what the widgets show to the model (the search box waits a moment on its own)."""
        self._timer.stop()
        self.model.set_filter(self.current_filter())

    # ----- from the model to the widgets -----------------------------------------------------

    def _fill_choices(self) -> None:
        """The months and folders that exist now. Keeps the choice when it is still there."""
        flt = self.model.filter
        months = self.model.months()
        for box, chosen in ((self.from_box, flt.start), (self.to_box, flt.end)):
            box.blockSignals(True)
            box.clear()
            box.addItem(ANY_MONTH, None)
            shown = list(months)
            if chosen is not None and chosen not in shown:
                shown.append(chosen)
                shown.sort(reverse=True)
            for year, month in shown:
                box.addItem(month_text(year, month), month_key(year, month))
            box.setCurrentIndex(
                max(0, box.findData(month_key(*chosen))) if chosen is not None else 0
            )
            box.blockSignals(False)
        self.folder_box.blockSignals(True)
        self.folder_box.clear()
        self.folder_box.addItem(ALL_FOLDERS, None)
        for root in self.model.roots():
            self.folder_box.addItem(str(root), str(root))
        if flt.folder is not None and self.folder_box.findData(str(flt.folder)) < 0:
            self.folder_box.addItem(str(flt.folder), str(flt.folder))
        index = self.folder_box.findData(str(flt.folder)) if flt.folder is not None else 0
        self.folder_box.setCurrentIndex(max(0, index))
        self.folder_box.blockSignals(False)

    def _show_filter(self) -> None:
        flt = self.model.filter
        if self.search.text().strip() != flt.text:
            self.search.blockSignals(True)
            self.search.setText(flt.text)
            self.search.blockSignals(False)
        self.kind_box.blockSignals(True)
        self.kind_box.setCurrentIndex(max(0, self.kind_box.findData(flt.kind)))
        self.kind_box.blockSignals(False)
        self._fill_choices()
