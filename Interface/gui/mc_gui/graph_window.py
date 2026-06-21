"""Pop-out real-time streaming graph window.

Backed by pyqtgraph, so mouse interaction is native: **left-drag pans**,
**scroll-wheel zooms** (both axes; hover an axis to zoom only that one),
**right-drag** box-style zoom, **right-click** for the context menu (export, axis
modes). The toolbar adds **Pause** (freeze the view while you inspect it),
**Auto-scroll** (X follows the newest sample), and channel selection.
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from .buffer import TelemetryBuffer

# distinct, high-contrast curve colours
_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0",
    "#f032e6", "#bcf60c", "#fabebe", "#008080", "#9a6324", "#800000",
    "#808000", "#000075", "#e6beff", "#808080",
]

REDRAW_HZ = 30


class GraphWindow(QWidget):
    _counter = 0

    def __init__(self, buffer: TelemetryBuffer, available: list[str],
                 initial: list[str] | None = None, parent=None):
        super().__init__(parent)
        GraphWindow._counter += 1
        self.setWindowTitle(f"Live Graph {GraphWindow._counter}")
        self.resize(900, 560)
        # top-level pop-out window even though it has a parent (so it stays grouped)
        self.setWindowFlag(Qt.WindowType.Window, True)

        self.buffer = buffer
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.paused = False
        self.autoscroll = True
        self._color_idx = 0

        self._build_ui(available)
        for name in (initial or []):
            self._set_channel(name, True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._redraw)
        self.timer.start(int(1000 / REDRAW_HZ))

    # --- UI -------------------------------------------------------------------
    def _build_ui(self, available: list[str]) -> None:
        root = QHBoxLayout(self)

        # left: channel picker
        left = QVBoxLayout()
        left.addWidget(QLabel("Channels"))
        self.channel_list = QListWidget()
        self.channel_list.setMaximumWidth(220)
        self.channel_list.itemChanged.connect(self._on_item_changed)
        left.addWidget(self.channel_list, 1)
        self.set_available_channels(available)
        root.addLayout(left)

        # right: plot + toolbar
        right = QVBoxLayout()
        bar = QHBoxLayout()
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._on_pause)
        bar.addWidget(self.btn_pause)

        self.chk_autoscroll = QCheckBox("Auto-scroll")
        self.chk_autoscroll.setChecked(True)
        self.chk_autoscroll.toggled.connect(self._on_autoscroll)
        bar.addWidget(self.chk_autoscroll)

        bar.addWidget(QLabel("Window (s):"))
        self.window_s = QDoubleSpinBox()
        self.window_s.setRange(0.1, 600.0)
        self.window_s.setValue(10.0)
        self.window_s.setSingleStep(1.0)
        bar.addWidget(self.window_s)

        btn_yauto = QPushButton("Y auto")
        btn_yauto.clicked.connect(lambda: self.plot.enableAutoRange(axis="y"))
        bar.addWidget(btn_yauto)

        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self.buffer.clear)
        bar.addWidget(btn_clear)
        bar.addStretch(1)
        right.addLayout(bar)

        pg.setConfigOptions(antialias=True)
        self.plot_widget = pg.PlotWidget(background="w")
        self.plot = self.plot_widget.getPlotItem()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.addLegend(offset=(10, 10))
        self.plot.setDownsampling(mode="peak", auto=True)
        self.plot.setClipToView(True)
        # turning the mouse manually disengages auto-scroll so you can inspect freely
        self.plot.getViewBox().sigRangeChangedManually.connect(self._on_manual_range)
        right.addWidget(self.plot_widget, 1)

        self.status = QLabel("")
        right.addWidget(self.status)
        root.addLayout(right, 1)

    def set_available_channels(self, names: list[str]) -> None:
        """Refresh the channel list, preserving existing checks/curves."""
        existing = {self.channel_list.item(i).text(): self.channel_list.item(i)
                    for i in range(self.channel_list.count())}
        wanted = sorted(set(names) | set(self.curves.keys()))
        self.channel_list.blockSignals(True)
        for name in wanted:
            if name in existing:
                continue
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in self.curves
                               else Qt.CheckState.Unchecked)
            self.channel_list.addItem(item)
        self.channel_list.blockSignals(False)

    # --- channel toggling -----------------------------------------------------
    def _on_item_changed(self, item: QListWidgetItem) -> None:
        self._set_channel(item.text(), item.checkState() == Qt.CheckState.Checked,
                          from_list=True)

    def _set_channel(self, name: str, on: bool, from_list: bool = False) -> None:
        if on and name not in self.curves:
            color = _PALETTE[self._color_idx % len(_PALETTE)]
            self._color_idx += 1
            self.curves[name] = self.plot.plot(pen=pg.mkPen(color, width=2), name=name)
        elif not on and name in self.curves:
            self.plot.removeItem(self.curves.pop(name))
        if not from_list:
            # reflect state into the list widget
            for i in range(self.channel_list.count()):
                it = self.channel_list.item(i)
                if it.text() == name:
                    it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                    break

    # --- toolbar handlers -----------------------------------------------------
    def _on_pause(self, checked: bool) -> None:
        self.paused = checked
        self.btn_pause.setText("Resume" if checked else "Pause")

    def _on_autoscroll(self, checked: bool) -> None:
        self.autoscroll = checked
        if checked:
            self.plot.enableAutoRange(axis="y")

    def _on_manual_range(self) -> None:
        if self.autoscroll:
            self.chk_autoscroll.setChecked(False)  # also sets self.autoscroll via signal

    # --- redraw ---------------------------------------------------------------
    def _redraw(self) -> None:
        if self.paused:
            self.status.setText("PAUSED - drag to pan, scroll to zoom")
            return
        for name, curve in self.curves.items():
            data = self.buffer.get(name)
            if data is not None and len(data[0]):
                curve.setData(data[0], data[1])
        if self.autoscroll and self.curves:
            t_end = self.buffer.latest_t
            self.plot.setXRange(t_end - self.window_s.value(), t_end, padding=0)
        self.status.setText(
            f"{len(self.curves)} channel(s) | t={self.buffer.latest_t:.3f}s | "
            f"{'auto-scroll' if self.autoscroll else 'manual view'}"
        )

    def closeEvent(self, event) -> None:
        self.timer.stop()
        super().closeEvent(event)
