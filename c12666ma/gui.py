"""C12666MA spectrometer GUI, for one or more spectrometers.

Run from the repository folder:
    python -m c12666ma                 real spectrometers on USB
    python -m c12666ma --simulate 3    three simulated ones (no hardware)

The acquisition itself runs in `session.AcquisitionSession` (serial reader
threads, trigger scheduler, recording), so recording keeps every frame even
when the window redraws slower. The window redraws ~30 times per second.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from . import __version__, simulator
from .analysis import SATURATION_HIGH_GAIN, SATURATION_LOW_GAIN, Region, YieldStatus
from .device import DeviceError
from .processing import AnalysisSettings
from .session import AcquisitionSession, DeviceHandle

INCIDENT_COLOR = (70, 140, 255)
FLUO_COLOR = (230, 60, 60)
DEVICE_COLORS = [(235, 235, 235), (255, 170, 0), (80, 200, 255), (120, 220, 120),
                 (230, 100, 200), (255, 235, 90), (170, 140, 255), (255, 130, 130)]
WARN_STYLE = "background:#b3261e; color:white; padding:3px 8px; border-radius:3px;"
INFO_STYLE = "background:#8a6d00; color:white; padding:3px 8px; border-radius:3px;"
MAX_PLOT_POINTS = 300_000        # per device in the yield plot; older points thinned


def device_color(h: DeviceHandle) -> tuple:
    return DEVICE_COLORS[h.index % len(DEVICE_COLORS)]


class GrowingSeries:
    """Append-only x/y storage for the time plot. Above `max_points` every
    second point is dropped, so days of data stay in bounded memory."""

    def __init__(self, max_points: int = MAX_PLOT_POINTS) -> None:
        self.max_points = max_points
        self.clear()

    def clear(self) -> None:
        self._x = np.empty(4096)
        self._y = np.empty(4096)
        self.n = 0

    def append(self, x: float, y: float) -> None:
        if self.n >= self.max_points:
            keep = self.n // 2
            self._x[:keep] = self._x[:self.n:2][:keep].copy()
            self._y[:keep] = self._y[:self.n:2][:keep].copy()
            self.n = keep
        if self.n == len(self._x):
            self._x = np.concatenate([self._x, np.empty_like(self._x)])
            self._y = np.concatenate([self._y, np.empty_like(self._y)])
        self._x[self.n] = x
        self._y[self.n] = y
        self.n += 1

    @property
    def x(self) -> np.ndarray:
        return self._x[:self.n]

    @property
    def y(self) -> np.ndarray:
        return self._y[:self.n]


def _spin(minimum, maximum, value, decimals=1, step=1.0, suffix="") -> QtWidgets.QDoubleSpinBox:
    box = QtWidgets.QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    return box


def _duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d} d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, session: AcquisitionSession) -> None:
        super().__init__()
        self.session = session
        self.setWindowTitle(f"C12666MA Spectrometer {__version__}")
        self.settings = QtCore.QSettings("JII", "C12666MA")
        self.yield_series: dict[str, GrowingSeries] = {}
        self.spec_curves: dict[str, pg.PlotDataItem] = {}
        self.yield_curves: dict[str, pg.PlotDataItem] = {}
        self.warn_series = GrowingSeries()
        self.sat_series = GrowingSeries()
        self.t0: Optional[float] = None
        self.latest: dict[str, tuple] = {}             # uid -> (frame, result)
        self._rates: dict[str, float] = {}
        self._frames_at: dict[str, tuple[int, float]] = {}
        self._selected: Optional[str] = None
        self._table_uids: list[str] = []
        self._last_warning = ("", 0.0, "")
        self._last_event_t = 0.0
        self._detect_count = 0
        self._build_ui()
        self._load_settings()
        self._apply_regions()
        self._apply_period()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)
        self.slow_timer = QtCore.QTimer(self)
        self.slow_timer.timeout.connect(self._slow_tick)
        self.slow_timer.start(1000)
        QtCore.QTimer.singleShot(200, self.connect_all)

    # ================================================================== UI
    def _build_ui(self) -> None:
        panel = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(panel)

        # ---- devices
        g = QtWidgets.QGroupBox("Spectrometers")
        v = QtWidgets.QVBoxLayout(g)
        row = QtWidgets.QHBoxLayout()
        self.detect_btn = QtWidgets.QPushButton("Auto-detect && connect")
        self.remove_btn = QtWidgets.QPushButton("Disconnect")
        row.addWidget(self.detect_btn)
        row.addWidget(self.remove_btn)
        v.addLayout(row)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "Name", "Port", "Status", "Dark"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        hdr = self.table.horizontalHeader()
        for col, mode in enumerate((QtWidgets.QHeaderView.Fixed,
                                    QtWidgets.QHeaderView.ResizeToContents,
                                    QtWidgets.QHeaderView.ResizeToContents,
                                    QtWidgets.QHeaderView.Stretch,
                                    QtWidgets.QHeaderView.ResizeToContents)):
            hdr.setSectionResizeMode(col, mode)
        self.table.setColumnWidth(0, 22)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.table.setWordWrap(False)
        v.addWidget(self.table)
        form = QtWidgets.QFormLayout()
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setMaxLength(15)
        self.gain_box = QtWidgets.QComboBox()
        self.gain_box.addItems(["High", "Low"])
        self.integration_spin = _spin(5.2, 10_000, 20, 3, 1.0, " ms")
        self.avg_spin = QtWidgets.QSpinBox()
        self.avg_spin.setRange(1, 16)
        self.avg_spin.setKeyboardTracking(False)
        self.led_check = QtWidgets.QCheckBox("LED on")
        self.copy_btn = QtWidgets.QPushButton("Copy gain / integration / averages to all")
        self.device_info = QtWidgets.QLabel("")
        self.device_info.setWordWrap(True)
        form.addRow("Name", self.name_edit)
        form.addRow("Gain", self.gain_box)
        form.addRow("Integration", self.integration_spin)
        form.addRow("Averages", self.avg_spin)
        form.addRow("", self.led_check)
        v.addLayout(form)
        v.addWidget(self.copy_btn)
        v.addWidget(self.device_info)
        lay.addWidget(g)

        # ---- acquisition
        g = QtWidgets.QGroupBox("Acquisition (all spectrometers)")
        f = QtWidgets.QFormLayout(g)
        self.period_spin = _spin(5.2, 3_600_000, 1000, 1, 10, " ms")
        self.fastest_check = QtWidgets.QCheckBox("As fast as possible")
        self.fastest_check.setChecked(True)
        self.rate_label = QtWidgets.QLabel("")
        self.rate_label.setWordWrap(True)
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.setCheckable(True)
        f.addRow("Period", self.period_spin)
        f.addRow("", self.fastest_check)
        f.addRow(self.rate_label)
        f.addRow(self.start_btn)
        lay.addWidget(g)

        # ---- dark
        g = QtWidgets.QGroupBox("Dark spectrum (all spectrometers)")
        f = QtWidgets.QFormLayout(g)
        self.dark_n_spin = QtWidgets.QSpinBox()
        self.dark_n_spin.setRange(1, 1000)
        self.dark_n_spin.setValue(20)
        self.dark_led_check = QtWidgets.QCheckBox("Switch LEDs off while taking it")
        self.dark_led_check.setChecked(True)
        self.dark_btn = QtWidgets.QPushButton("Take dark")
        self.dark_clear_btn = QtWidgets.QPushButton("Clear")
        self.subtract_check = QtWidgets.QCheckBox("Plot dark-subtracted")
        self.subtract_check.setChecked(True)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.dark_btn)
        row.addWidget(self.dark_clear_btn)
        f.addRow("Frames", self.dark_n_spin)
        f.addRow(self.dark_led_check)
        f.addRow(row)
        f.addRow(self.subtract_check)
        lay.addWidget(g)

        # ---- regions
        g = QtWidgets.QGroupBox("Regions and rel. fluo. yield (shared)")
        f = QtWidgets.QFormLayout(g)
        self.inc_lo = _spin(300, 900, 440, 1, 1, " nm")
        self.inc_hi = _spin(300, 900, 480, 1, 1, " nm")
        self.flu_lo = _spin(300, 900, 680, 1, 1, " nm")
        self.flu_hi = _spin(300, 900, 760, 1, 1, " nm")
        self.k_spin = _spin(0.5, 100, 5, 1, 0.5, " σ")
        self.sat_high_spin = _spin(100, 4095, SATURATION_HIGH_GAIN, 0, 10, " counts")
        self.sat_low_spin = _spin(100, 4095, SATURATION_LOW_GAIN, 0, 10, " counts")
        f.addRow("incident_light from", self.inc_lo)
        f.addRow("to", self.inc_hi)
        f.addRow("fluorescence from", self.flu_lo)
        f.addRow("to", self.flu_hi)
        f.addRow("Signal threshold", self.k_spin)
        f.addRow("Saturation (high gain)", self.sat_high_spin)
        f.addRow("Saturation (low gain)", self.sat_low_spin)
        lay.addWidget(g)

        # ---- recording
        g = QtWidgets.QGroupBox("Recording")
        f = QtWidgets.QFormLayout(g)
        self.folder_edit = QtWidgets.QLineEdit(str(Path.home() / "C12666MA data"))
        browse = QtWidgets.QPushButton("…")
        browse.setFixedWidth(30)
        browse.clicked.connect(self._browse_folder)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.folder_edit)
        row.addWidget(browse)
        self.prefix_edit = QtWidgets.QLineEdit("C12666MA")
        self.note_edit = QtWidgets.QLineEdit()
        self.note_edit.setPlaceholderText("written into the file header")
        self.save_yield_check = QtWidgets.QCheckBox("Save rel. fluo. yield")
        self.save_yield_check.setChecked(True)
        self.duration_spin = _spin(0, 24 * 365, 0, 2, 1, " h")
        self.duration_spin.setSpecialValueText("until stopped")
        self.part_spin = _spin(0.1, 24 * 30, 24, 1, 1, " h")
        self.record_btn = QtWidgets.QPushButton("Start recording")
        self.record_btn.setCheckable(True)
        self.record_label = QtWidgets.QLabel("")
        self.record_label.setWordWrap(True)
        self.record_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        f.addRow("Folder", row)
        f.addRow("Name", self.prefix_edit)
        f.addRow("Note", self.note_edit)
        f.addRow("Duration", self.duration_spin)
        f.addRow("New file every", self.part_spin)
        f.addRow(self.save_yield_check)
        f.addRow(self.record_btn)
        f.addRow(self.record_label)
        lay.addWidget(g)
        lay.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(370)

        # ---- plots
        # No antialiasing: with PyQt5 it made Qt hold the Python lock for up to
        # ~200 ms per redraw, starving the serial reader threads at high rates.
        pg.setConfigOptions(antialias=False)
        self.spec_plot = pg.PlotWidget(title="Spectra")
        self.spec_plot.setLabel("bottom", "Wavelength", units="nm")
        self.spec_plot.setLabel("left", "Counts")
        self.spec_plot.showGrid(x=True, y=True, alpha=0.2)
        self.spec_plot.getAxis("left").enableAutoSIPrefix(False)
        self.spec_plot.addLegend(offset=(-10, 10))
        self.sat_line = pg.InfiniteLine(angle=0, pen=pg.mkPen((255, 170, 0), style=QtCore.Qt.DashLine))
        self.spec_plot.addItem(self.sat_line, ignoreBounds=True)
        self.inc_region = pg.LinearRegionItem(brush=pg.mkBrush(*INCIDENT_COLOR, 50),
                                              pen=pg.mkPen(INCIDENT_COLOR))
        self.flu_region = pg.LinearRegionItem(brush=pg.mkBrush(*FLUO_COLOR, 50),
                                              pen=pg.mkPen(FLUO_COLOR))
        for region, name, color in ((self.inc_region, "incident_light", INCIDENT_COLOR),
                                    (self.flu_region, "fluorescence", FLUO_COLOR)):
            self.spec_plot.addItem(region)
            pg.InfLineLabel(region.lines[0], name, position=0.95, color=color)

        self.yield_plot = pg.PlotWidget(title="rel. fluo. yield = fluorescence / incident_light")
        self.yield_plot.setLabel("bottom", "Time", units="s")
        self.yield_plot.setLabel("left", "rel. fluo. yield")
        self.yield_plot.showGrid(x=True, y=True, alpha=0.2)
        self.yield_plot.getAxis("left").enableAutoSIPrefix(False)
        self.yield_plot.setClipToView(True)
        self.yield_plot.setDownsampling(auto=True, mode="peak")
        self.warn_scatter = self.yield_plot.plot(pen=None, symbol="x", symbolSize=8,
                                                 symbolBrush=FLUO_COLOR, symbolPen=FLUO_COLOR)
        self.sat_scatter = self.yield_plot.plot(pen=None, symbol="o", symbolSize=5,
                                                symbolBrush=(255, 170, 0), symbolPen=None)
        tools = QtWidgets.QHBoxLayout()
        tools.addWidget(QtWidgets.QLabel("Show last"))
        self.window_spin = _spin(0, 30 * 86_400, 120, 0, 10, " s")
        self.window_spin.setSpecialValueText("all")
        tools.addWidget(self.window_spin)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self._clear_yield)
        tools.addWidget(clear)
        tools.addWidget(QtWidgets.QLabel(
            "<span style='color:#e63c3c'>✕</span> fluorescence without incident light "
            "&nbsp; <span style='color:#ffaa00'>●</span> saturated"))
        tools.addStretch(1)
        self.value_label = QtWidgets.QLabel("")
        tools.addWidget(self.value_label)
        bottom = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.yield_plot)
        bl.addLayout(tools)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setPlaceholderText("Events (connections, settings, recordings) appear here")

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.addWidget(self.spec_plot)
        split.addWidget(bottom)
        split.addWidget(self.log_view)
        split.setSizes([480, 340, 90])

        main = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        main.addWidget(scroll)
        main.addWidget(split)
        main.setSizes([380, 1000])
        self.setCentralWidget(main)

        self.warning_label = QtWidgets.QLabel("")
        self.stats_label = QtWidgets.QLabel("")
        self.statusBar().addWidget(self.warning_label)
        self.statusBar().addPermanentWidget(self.stats_label)

        # ---- signals
        self.detect_btn.clicked.connect(self.connect_all)
        self.remove_btn.clicked.connect(self.disconnect_selected)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.name_edit.editingFinished.connect(self._send_name)
        self.gain_box.currentIndexChanged.connect(
            lambda: self._apply_selected(high_gain=self.gain_box.currentIndex() == 0))
        self.integration_spin.valueChanged.connect(
            lambda v: self._apply_selected(integration_us=round(v * 1000)))
        self.avg_spin.valueChanged.connect(lambda v: self._apply_selected(n_avg=int(v)))
        self.led_check.toggled.connect(lambda on: self._apply_selected(led=on))
        self.copy_btn.clicked.connect(self._copy_to_all)
        self.period_spin.valueChanged.connect(self._apply_period)
        self.fastest_check.toggled.connect(self._apply_period)
        self.start_btn.toggled.connect(self.toggle_acquisition)
        self.dark_btn.clicked.connect(self.take_dark)
        self.dark_clear_btn.clicked.connect(self.clear_dark)
        for box in (self.inc_lo, self.inc_hi, self.flu_lo, self.flu_hi, self.k_spin,
                    self.sat_high_spin, self.sat_low_spin):
            box.valueChanged.connect(self._apply_regions)
        self.inc_region.sigRegionChangeFinished.connect(self._region_dragged)
        self.flu_region.sigRegionChangeFinished.connect(self._region_dragged)
        self.record_btn.toggled.connect(self.toggle_recording)
        self._show_selected()

    # ============================================================ settings
    _PERSIST = ["inc_lo", "inc_hi", "flu_lo", "flu_hi", "k_spin", "sat_high_spin",
                "sat_low_spin", "dark_n_spin", "window_spin", "period_spin",
                "duration_spin", "part_spin"]
    _PERSIST_TEXT = ["folder_edit", "prefix_edit"]
    _PERSIST_CHECK = ["save_yield_check", "fastest_check"]

    def _load_settings(self) -> None:
        # QSettings returns strings or numbers depending on platform and Qt
        # binding, and PyQt is strict about int vs float: convert explicitly
        # and ignore anything unreadable rather than refusing to start.
        for name in self._PERSIST:
            box = getattr(self, name)
            try:
                value = float(self.settings.value(name))
                box.setValue(int(round(value)) if isinstance(box, QtWidgets.QSpinBox) else value)
            except (TypeError, ValueError):
                pass
        for name in self._PERSIST_TEXT:
            value = self.settings.value(name)
            if value:
                getattr(self, name).setText(str(value))
        for name in self._PERSIST_CHECK:
            value = self.settings.value(name)
            if value is not None:
                getattr(self, name).setChecked(str(value) == "true")
        geometry = self.settings.value("geometry")
        if isinstance(geometry, QtCore.QByteArray):
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        for name in self._PERSIST:
            self.settings.setValue(name, getattr(self, name).value())
        for name in self._PERSIST_TEXT:
            self.settings.setValue(name, getattr(self, name).text())
        for name in self._PERSIST_CHECK:
            self.settings.setValue(name, "true" if getattr(self, name).isChecked() else "false")
        self.settings.setValue("geometry", self.saveGeometry())

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        rec = self.session.recording
        if rec is not None:
            answer = QtWidgets.QMessageBox.question(
                self, "C12666MA",
                f"A recording is running ({_duration(rec.elapsed_s)}, "
                f"{rec.n_spectra} spectra).\nStop it and quit?")
            if answer != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
        self._save_settings()
        self.timer.stop()
        self.slow_timer.stop()
        self.session.close()
        super().closeEvent(event)

    # ============================================================= devices
    def _handle(self, uid: Optional[str]) -> Optional[DeviceHandle]:
        return next((h for h in self.session.handles if h.uid == uid), None)

    @property
    def selected(self) -> Optional[DeviceHandle]:
        return self._handle(self._selected)

    def connect_all(self) -> None:
        if not self.session.detect():
            if not self.session.handles:
                self.device_info.setText("No spectrometer found. Plug one in; it is "
                                         "detected automatically.")
            return
        errors = self.session.connect_all()
        self._rebuild_table()
        self._apply_period()
        if errors:
            self._error("Some spectrometers could not be connected:\n" + "\n".join(errors))

    def disconnect_selected(self) -> None:
        h = self.selected
        if h is None:
            return
        if self.session.recording is not None:
            answer = QtWidgets.QMessageBox.question(
                self, "C12666MA", f"Disconnect {h.name}? It is part of the running recording.")
            if answer != QtWidgets.QMessageBox.Yes:
                return
        self.session.disconnect(h)
        self._forget_device(h.uid)
        self._selected = None
        self._rebuild_table()
        self._apply_period()
        if not self.session.running:
            self.start_btn.blockSignals(True)
            self.start_btn.setChecked(False)
            self.start_btn.setText("Start")
            self.start_btn.blockSignals(False)

    def _forget_device(self, uid: str) -> None:
        curve = self.spec_curves.pop(uid, None)
        if curve is not None:
            self.spec_plot.removeItem(curve)            # also removes the legend entry
        curve = self.yield_curves.pop(uid, None)
        if curve is not None:
            self.yield_plot.removeItem(curve)
        self.yield_series.pop(uid, None)
        self.latest.pop(uid, None)

    def _rename_in_legend(self, h: DeviceHandle) -> None:
        legend = self.spec_plot.plotItem.legend
        curve = self.spec_curves.get(h.uid)
        if legend is not None and curve is not None:
            legend.removeItem(curve)
            legend.addItem(curve, h.name)

    def _rebuild_table(self) -> None:
        handles = self.session.handles
        self._table_uids = [h.uid for h in handles]
        self.table.blockSignals(True)
        self.table.setRowCount(len(handles))
        for r, h in enumerate(handles):
            pix = QtGui.QPixmap(12, 12)
            pix.fill(QtGui.QColor(*device_color(h)))
            painter = QtGui.QPainter(pix)
            painter.setPen(QtGui.QColor(60, 60, 60))
            painter.drawRect(0, 0, 11, 11)
            painter.end()
            swatch = QtWidgets.QTableWidgetItem("")
            swatch.setIcon(QtGui.QIcon(pix))
            swatch.setToolTip("colour of this spectrometer in the plots")
            self.table.setItem(r, 0, swatch)
            for c in range(1, 5):
                if self.table.item(r, c) is None:
                    self.table.setItem(r, c, QtWidgets.QTableWidgetItem(""))
        rows = max(1, min(len(handles), 6))
        self.table.setFixedHeight(self.table.horizontalHeader().height() + 4
                                  + rows * self.table.verticalHeader().defaultSectionSize())
        if self._selected not in self._table_uids:
            self._selected = self._table_uids[0] if self._table_uids else None
        if self._selected is not None:
            self.table.selectRow(self._table_uids.index(self._selected))
        self.table.blockSignals(False)
        self._update_table()
        self._show_selected()

    def _update_table(self) -> None:
        for r, uid in enumerate(self._table_uids):
            h = self._handle(uid)
            if h is None:
                continue
            if not h.connected:
                status = f"lost {int(time.time() - (h.lost_since or time.time()))} s, reconnecting"
            elif self.session.running:
                status = f"{self._rates.get(uid, 0.0):.1f}/s"
                if h.missed or h.busy:
                    status += f", missed {h.missed}"
            else:
                status = "connected"
            if h.calibration.approximate:
                status += ", wavelengths approximate"
            p = h.processor
            progress = p.dark_progress
            if progress is not None:
                dark = f"{progress[0]}/{progress[1]}"
            elif p.dark is None:
                dark = "none"
            elif p.last_frame is not None and not p.dark.matches(p.last_frame):
                dark = "redo"
            else:
                dark = datetime.fromtimestamp(p.dark.timestamp).strftime("%H:%M")
            for c, text in ((1, h.name), (2, h.port), (3, status), (4, dark)):
                item = self.table.item(r, c)
                if item is not None and item.text() != text:
                    item.setText(text)
                    item.setToolTip(text)
            bad = (not h.connected) or dark == "redo"
            color = QtGui.QColor(230, 60, 60) if bad else self.palette().color(QtGui.QPalette.Text)
            for c in (3, 4):
                self.table.item(r, c).setForeground(QtGui.QBrush(color))

    def _selection_changed(self) -> None:
        rows = {i.row() for i in self.table.selectedItems()}
        if rows:
            r = min(rows)
            if r < len(self._table_uids):
                self._selected = self._table_uids[r]
        self._show_selected()

    def _show_selected(self) -> None:
        h = self.selected
        widgets = (self.name_edit, self.gain_box, self.integration_spin, self.avg_spin,
                   self.led_check)
        for w in widgets + (self.copy_btn, self.remove_btn):
            w.setEnabled(h is not None)
        self.copy_btn.setEnabled(h is not None and len(self.session.handles) > 1)
        if h is None:
            return
        for w in widgets:
            w.blockSignals(True)
        st = h.status
        self.name_edit.setText(h.name)
        self.gain_box.setCurrentIndex(0 if h.settings.high_gain else 1)
        self.integration_spin.setMinimum(st["min_integration_us"] / 1000)
        self.integration_spin.setValue(h.settings.integration_us / 1000)
        self.avg_spin.setMaximum(int(st["max_avg"]))
        self.avg_spin.setValue(int(h.settings.n_avg))
        self.led_check.setChecked(bool(h.settings.led))
        for w in widgets:
            w.blockSignals(False)
        cal = h.calibration
        warn = " ⚠ not calibrated for this unit" if cal.approximate else ""
        self.device_info.setText(f"{st['fw']}, uid {h.uid}\nwavelengths: {cal.source}{warn}")
        self.device_info.setStyleSheet("color:#e0a000" if cal.approximate else "")

    def _apply_selected(self, **kw) -> None:
        h = self.selected
        if h is None:
            return
        try:
            self.session.apply(h, **kw)
        except DeviceError as exc:
            self._error(f"{h.name}: {exc}")
        self._show_selected()
        self._update_rate_label()

    def _send_name(self) -> None:
        h = self.selected
        name = self.name_edit.text().strip()
        if h is not None and name and name != h.name:
            self._apply_selected(name=name)
            self._rebuild_table()
            self._rename_in_legend(h)

    def _copy_to_all(self) -> None:
        src = self.selected
        if src is None:
            return
        s = src.settings
        for h in self.session.connected:
            if h is not src:
                try:
                    self.session.apply(h, high_gain=s.high_gain,
                                       integration_us=s.integration_us, n_avg=s.n_avg)
                except DeviceError as exc:
                    self._error(f"{h.name}: {exc}")
        self._update_rate_label()

    # ========================================================= acquisition
    def _apply_period(self) -> None:
        fastest = self.fastest_check.isChecked()
        self.period_spin.setEnabled(not fastest)
        self.session.set_period_us(0 if fastest else round(self.period_spin.value() * 1000))
        self._update_rate_label()

    def _update_rate_label(self) -> None:
        s = self.session
        hs = s.connected
        if not hs:
            self.rate_label.setText("no spectrometer connected")
            return
        min_ms = s.min_period_us() / 1000
        eff_ms = s.effective_period_us() / 1000
        if len(hs) == 1:
            why = "averages × integration" if s.period_us == 0 else "one frame + margins"
        else:
            slowest = max(hs, key=lambda h: h.window_us)
            why = f"set by {slowest.name}, the slowest ({slowest.window_us / 1000:.1f} ms window)"
        mode = "continuous, no dead time" if (len(hs) == 1 and s.period_us == 0) \
            else "synchronised, windows centred"
        self.rate_label.setText(f"min {min_ms:.1f} ms ({why})\nactual {eff_ms:.1f} ms = "
                                f"{1000 / eff_ms:.2f} spectra/s per device, {mode}")

    def toggle_acquisition(self, start: bool) -> None:
        if start:
            try:
                self.session.start()
            except DeviceError as exc:
                self._error(str(exc))
                self.start_btn.blockSignals(True)
                self.start_btn.setChecked(False)
                self.start_btn.blockSignals(False)
                return
            self.start_btn.setText("Stop")
        else:
            if self.session.recording is not None:
                self.record_btn.setChecked(False)
            self.session.stop()
            self.start_btn.setText("Start")

    def _sync_start_button(self) -> None:
        running = self.session.running
        if self.start_btn.isChecked() != running:
            self.start_btn.blockSignals(True)
            self.start_btn.setChecked(running)
            self.start_btn.blockSignals(False)
        self.start_btn.setText("Stop" if running else "Start")

    def take_dark(self) -> None:
        try:
            self.session.take_dark(self.dark_n_spin.value(), self.dark_led_check.isChecked())
        except DeviceError as exc:
            self._error(str(exc))
        self._sync_start_button()

    def clear_dark(self) -> None:
        for h in self.session.handles:
            h.processor.cancel_dark()
            h.processor.dark = None

    # ============================================================= regions
    def _apply_regions(self) -> None:
        self.session.set_analysis(AnalysisSettings(
            Region("incident_light", self.inc_lo.value(), self.inc_hi.value()),
            Region("fluorescence", self.flu_lo.value(), self.flu_hi.value()),
            self.k_spin.value(), self.sat_high_spin.value(), self.sat_low_spin.value()))
        for region, lo, hi in ((self.inc_region, self.inc_lo, self.inc_hi),
                               (self.flu_region, self.flu_lo, self.flu_hi)):
            region.blockSignals(True)
            region.setRegion((lo.value(), hi.value()))
            region.blockSignals(False)

    def _region_dragged(self) -> None:
        for region, lo, hi in ((self.inc_region, self.inc_lo, self.inc_hi),
                               (self.flu_region, self.flu_lo, self.flu_hi)):
            a, b = region.getRegion()
            for box, v in ((lo, a), (hi, b)):
                box.blockSignals(True)
                box.setValue(v)
                box.blockSignals(False)
        self._apply_regions()

    # =========================================================== recording
    def _browse_folder(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Recording folder",
                                                            self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def toggle_recording(self, start: bool) -> None:
        if start:
            try:
                rec = self.session.start_recording(
                    Path(self.folder_edit.text()), self.prefix_edit.text(),
                    self.note_edit.text(), self.save_yield_check.isChecked(),
                    duration_s=self.duration_spin.value() * 3600,
                    part_hours=self.part_spin.value())
            except (DeviceError, OSError) as exc:
                self._error(f"Cannot start the recording: {exc}")
                self.record_btn.blockSignals(True)
                self.record_btn.setChecked(False)
                self.record_btn.blockSignals(False)
                return
            self.record_btn.setText("Stop recording")
            self.record_label.setText(str(rec.folder))
            self._sync_start_button()
        else:
            rec = self.session.stop_recording()
            self.record_btn.setText("Start recording")
            if rec is not None:
                self._show_finished_recording(rec)
        for w in (self.save_yield_check, self.duration_spin, self.part_spin):
            w.setEnabled(not start)

    def _show_finished_recording(self, rec) -> None:
        self.record_label.setText(f"{rec.stop_reason}: {rec.n_spectra} spectra in "
                                  f"{_duration(rec.elapsed_s)}\n{rec.folder}")

    def _update_record_label(self) -> None:
        rec = self.session.recording
        if rec is None:
            if self.record_btn.isChecked():            # stopped by itself (duration, disk)
                self.record_btn.blockSignals(True)
                self.record_btn.setChecked(False)
                self.record_btn.blockSignals(False)
                self.record_btn.setText("Start recording")
                for w in (self.save_yield_check, self.duration_spin, self.part_spin):
                    w.setEnabled(True)
                if self.session.last_recording is not None:
                    self._show_finished_recording(self.session.last_recording)
            return
        text = f"{rec.folder.name}\n{rec.n_spectra} spectra · {_duration(rec.elapsed_s)}"
        if rec.duration_s:
            text += f" · {_duration(max(0.0, rec.duration_s - rec.elapsed_s))} left"
        parts = max((r.part for r in rec.recorders.values()), default=1)
        if parts > 1:
            text += f" · file part {parts}"
        self.record_label.setText(text)

    # ============================================================= refresh
    def _clear_yield(self) -> None:
        for s in list(self.yield_series.values()) + [self.warn_series, self.sat_series]:
            s.clear()
        self.t0 = None

    def _curves_for(self, h: DeviceHandle):
        if h.uid not in self.spec_curves:
            pen = pg.mkPen(device_color(h), width=1.5)
            self.spec_curves[h.uid] = self.spec_plot.plot(pen=pen, name=h.name)
            self.yield_curves[h.uid] = self.yield_plot.plot(pen=pen, connect="finite")
            self.yield_series[h.uid] = GrowingSeries()
        return self.spec_curves[h.uid], self.yield_curves[h.uid], self.yield_series[h.uid]

    def _tick(self) -> None:
        results = self.session.results
        now = time.monotonic()
        changed = set()
        while results:
            h, frame, res = results.popleft()
            if self.t0 is None:
                self.t0 = frame.timestamp
            t = frame.timestamp - self.t0
            _, _, series = self._curves_for(h)
            series.append(t, res.value)
            if res.warning:
                self.warn_series.append(t, 0.0)
                self._last_warning = (f"{h.name}: fluorescence signal without incident light",
                                      now, WARN_STYLE)
            if res.saturated:
                self.sat_series.append(t, res.value if np.isfinite(res.value) else 0.0)
                self._last_warning = (f"{h.name}: saturated pixels in a region; lower the "
                                      "integration time or use low gain", now, WARN_STYLE)
            self.latest[h.uid] = (frame, res)
            changed.add(h.uid)
        for uid in changed:
            h = self._handle(uid)
            if h is None:
                continue
            frame, res = self.latest[uid]
            dark = h.processor.dark
            y = frame.counts
            if self.subtract_check.isChecked() and dark is not None and dark.matches(frame):
                y = y - dark.mean
            spec_curve, yield_curve, series = self._curves_for(h)
            spec_curve.setData(h.processor.wavelengths, y)
            yield_curve.setData(series.x, series.y)
        if changed:
            self.warn_scatter.setData(self.warn_series.x, self.warn_series.y)
            self.sat_scatter.setData(self.sat_series.x, self.sat_series.y)
            self._update_value_label(now)
            window = self.window_spin.value()
            last = max((s.x[-1] for s in self.yield_series.values() if s.n), default=0.0)
            if window > 0:
                self.yield_plot.setXRange(max(0.0, last - window), max(window, last), padding=0)
            else:
                self.yield_plot.enableAutoRange(axis="x")
        self._update_warning(now)

    def _update_value_label(self, now: float) -> None:
        h = self.selected
        if h is None or h.uid not in self.latest:
            self.value_label.setText("")
            return
        frame, res = self.latest[h.uid]
        sat = self.session.analysis.sat_high if frame.high_gain else self.session.analysis.sat_low
        dark = h.processor.dark
        subtract = self.subtract_check.isChecked() and dark is not None and dark.matches(frame)
        self.sat_line.setValue(sat - (float(np.mean(dark.mean)) if subtract else 0.0))
        if res.status == YieldStatus.NO_DARK:
            text = f"{h.name}: no matching dark spectrum"
            if h.processor.dark_progress is None and now - self._last_warning[1] > 1.0:
                self._last_warning = ("No matching dark spectrum: press 'Take dark' "
                                      "(needed for the yield)", now, INFO_STYLE)
        else:
            text = (f"{h.name}: incident_light {res.incident:.0f} ± {res.incident_noise:.0f} · "
                    f"fluorescence {res.fluorescence:.0f} ± {res.fluorescence_noise:.0f}")
            if np.isfinite(res.value):
                text += f" · yield {res.value:.4f}"
        self.value_label.setText(text)

    def _update_warning(self, now: float) -> None:
        text, t, style = self._last_warning
        if text and now - t < 1.5:
            self.warning_label.setText(text)
            self.warning_label.setStyleSheet(style)
        elif self.warning_label.text():
            self.warning_label.setText("")
            self.warning_label.setStyleSheet("")

    def _slow_tick(self) -> None:
        s = self.session
        now = time.monotonic()
        for h in s.handles:
            n, t = self._frames_at.get(h.uid, (h.frames, now - 1))
            if now > t:
                self._rates[h.uid] = (h.frames - n) / (now - t)
            self._frames_at[h.uid] = (h.frames, now)
        if [h.uid for h in s.handles] != self._table_uids:
            self._rebuild_table()
        else:
            self._update_table()
        self._sync_start_button()
        self._update_record_label()
        self._detect_count += 1
        if self._detect_count % 3 == 0:                  # USB scan every 3 s
            new = [] if s.recording is not None else s.detect()
            self.detect_btn.setText(f"Connect {len(new)} new spectrometer(s)" if new
                                    else "Auto-detect && connect")
            self.detect_btn.setStyleSheet("font-weight:bold" if new else "")
        missed = sum(h.missed for h in s.handles)
        text = f"{len(s.connected)}/{len(s.handles)} connected"
        if s.mode == "triggered":
            text += f" · tick {s.ticks_sent} · missed {missed}"
            if s.min_lead_us is not None:
                text += f" · triggers sent ≥ {s.min_lead_us / 1000:.0f} ms ahead"
            if s.late_triggers or s.ticks_skipped:
                text += f" · late {s.late_triggers} · skipped {s.ticks_skipped}"
        self.stats_label.setText(text)
        for t, line in list(s.events):
            if t > self._last_event_t:
                self.log_view.appendPlainText(line)
                self._last_event_t = t
        if self.selected is not None:
            self._update_rate_label()

    def _error(self, text: str) -> None:
        QtWidgets.QMessageBox.warning(self, "C12666MA", text)


def configure_qt() -> None:
    """Call before creating the QApplication. Qt5 does not scale for high-DPI
    displays by default; with Windows display scaling (e.g. 125 %) pyqtgraph
    then draws the axis tick labels at the wrong positions. Qt6 always scales."""
    if pg.Qt.QT_LIB in ("PyQt5", "PySide2"):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
        if hasattr(QtGui.QGuiApplication, "setHighDpiScaleFactorRoundingPolicy"):
            QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m c12666ma",
                                     description="C12666MA spectrometer GUI")
    parser.add_argument("--simulate", type=int, default=0, metavar="N",
                        help="add N simulated spectrometers (no hardware needed)")
    args = parser.parse_args(argv)
    if args.simulate:
        simulator.create_units(args.simulate)
    if QtWidgets.QApplication.instance() is None:
        configure_qt()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    win = MainWindow(AcquisitionSession(simulate=args.simulate > 0))
    win.resize(1450, 950)
    win.show()
    sys.exit(app.exec() if hasattr(app, "exec") else app.exec_())


if __name__ == "__main__":
    main()
