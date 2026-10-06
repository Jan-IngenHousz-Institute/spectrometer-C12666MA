"""C12666MA live spectrometer GUI.

Run from the repository folder:   python -m c12666ma

Frames are processed in the driver's reader thread (dark capture, rel. fluo.
yield, recording), so recording keeps every frame even when the plots
refresh slower. The window redraws ~30 times per second from a queue.
"""

from __future__ import annotations

import collections
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pyqtgraph as pg
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d import Axes3D
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from . import __version__
from .analysis import (SATURATION_HIGH_GAIN, SATURATION_LOW_GAIN, DarkReference, Region,
                       YieldResult, YieldStatus, compute_yield)
from .calibration import WavelengthCalibration
from .device import DeviceError, Spectrometer, find_ports
from .protocol import FLAG_LED_CHANGED, Frame
from .recorder import Recorder


INCIDENT_COLOR = (70, 140, 255)
FLUO_COLOR = (230, 60, 60)
WARN_STYLE = "background:#b3261e; color:white; padding:3px 8px; border-radius:3px;"
INFO_STYLE = "background:#8a6d00; color:white; padding:3px 8px; border-radius:3px;"


# =============================================================================
#  Processing (runs in the serial reader thread)
# =============================================================================

class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.wavelengths = WavelengthCalibration.uncalibrated().wavelengths
        self.incident = Region("incident_light", 440.0, 480.0)
        self.fluorescence = Region("fluorescence", 680.0, 760.0)
        self.k = 5.0
        self.sat_high = float(SATURATION_HIGH_GAIN)
        self.sat_low = float(SATURATION_LOW_GAIN)
        self.dark: Optional[DarkReference] = None
        self.recorder: Optional[Recorder] = None
        self.results: "collections.deque[tuple[Frame, YieldResult]]" = collections.deque(maxlen=20_000)
        self.dark_finished = threading.Event()
        self._dark_frames: Optional[list[Frame]] = None
        self._dark_target = 0
        self._dark_require_led_off = True

    def start_dark(self, n_frames: int, require_led_off: bool) -> None:
        self.dark_finished.clear()
        self._dark_target = n_frames
        self._dark_require_led_off = require_led_off
        self._dark_frames = []

    @property
    def dark_progress(self) -> Optional[tuple[int, int]]:
        frames = self._dark_frames
        return None if frames is None else (len(frames), self._dark_target)

    def cancel_dark(self) -> None:
        self._dark_frames = None

    def on_frame(self, frame: Frame) -> None:
        frames = self._dark_frames
        if frames is not None:
            usable = not (frame.flags & FLAG_LED_CHANGED)
            if self._dark_require_led_off and frame.led:
                usable = False
            if frames and frame.settings_key != frames[0].settings_key:
                frames.clear()                        # settings changed: start over
            if usable:
                frames.append(frame)
            if len(frames) >= self._dark_target:
                self.dark = DarkReference.from_frames(frames)
                self._dark_frames = None
                with self.lock:
                    if self.recorder is not None:
                        self.recorder.write_dark(self.dark)
                self.dark_finished.set()
        saturation = self.sat_high if frame.high_gain else self.sat_low
        result = compute_yield(frame, self.dark, self.wavelengths, self.incident,
                               self.fluorescence, self.k, saturation)
        with self.lock:
            if self.recorder is not None:
                self.recorder.write_frame(frame, result if self.recorder.include_yield else None)
        self.results.append((frame, result))


class GrowingSeries:
    """Append-only x/y storage for the time plot."""

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self._x = np.empty(4096)
        self._y = np.empty(4096)
        self.n = 0

    def append(self, x: float, y: float) -> None:
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


# =============================================================================
#  Main window
# =============================================================================

def _spin(minimum, maximum, value, decimals=1, step=1.0, suffix="") -> QtWidgets.QDoubleSpinBox:
    box = QtWidgets.QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    return box


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"C12666MA Spectrometer {__version__}")
        self.settings = QtCore.QSettings("JII", "C12666MA")
        self.engine = Engine()
        self.spec: Optional[Spectrometer] = None
        self.calibration = WavelengthCalibration.uncalibrated()
        self.yield_series = GrowingSeries()
        self.warn_series = GrowingSeries()
        self.sat_series = GrowingSeries()
        self.spectrum_history: collections.deque[tuple[float, Frame]] = collections.deque(
            maxlen=5000)
        self._spectrum_t0_device: Optional[int] = None
        self._last_3d_update = 0.0
        self.t0_device: Optional[int] = None
        self.last_frame: Optional[Frame] = None
        self.fps = 0.0
        self._fps_count = 0
        self._fps_t = time.monotonic()
        self._last_warning = ("", 0.0, "")
        self._last_flush = time.monotonic()
        self._led_before_dark: Optional[bool] = None
        self._build_ui()
        self._load_settings()
        self._apply_regions()
        self.refresh_ports()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        panel = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(panel)

        # connection
        g = QtWidgets.QGroupBox("Device")
        f = QtWidgets.QGridLayout(g)
        self.port_box = QtWidgets.QComboBox()
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.device_label = QtWidgets.QLabel("not connected")
        self.device_label.setWordWrap(True)
        f.addWidget(self.port_box, 0, 0)
        f.addWidget(self.refresh_btn, 0, 1)
        f.addWidget(self.connect_btn, 1, 0, 1, 2)
        f.addWidget(self.device_label, 2, 0, 1, 2)
        lay.addWidget(g)

        # acquisition
        g = QtWidgets.QGroupBox("Acquisition")
        f = QtWidgets.QFormLayout(g)
        self.gain_box = QtWidgets.QComboBox()
        self.gain_box.addItems(["High", "Low"])
        self.integration_spin = _spin(5.2, 10_000, 20, 3, 1.0, " ms")
        self.avg_spin = QtWidgets.QSpinBox()
        self.avg_spin.setRange(1, 16)
        self.avg_spin.setKeyboardTracking(False)
        self.fastest_check = QtWidgets.QCheckBox("As fast as possible")
        self.fastest_check.setChecked(True)
        self.period_spin = _spin(5.2, 3_600_000, 1000, 1, 10, " ms")
        self.rate_label = QtWidgets.QLabel("")
        self.led_check = QtWidgets.QCheckBox("LED on")
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.setCheckable(True)
        f.addRow("Gain", self.gain_box)
        f.addRow("Integration", self.integration_spin)
        f.addRow("Averages", self.avg_spin)
        f.addRow("Period", self.period_spin)
        f.addRow("", self.fastest_check)
        f.addRow("", self.rate_label)
        f.addRow("", self.led_check)
        f.addRow(self.start_btn)
        lay.addWidget(g)

        # dark
        g = QtWidgets.QGroupBox("Dark spectrum")
        f = QtWidgets.QFormLayout(g)
        self.dark_n_spin = QtWidgets.QSpinBox()
        self.dark_n_spin.setRange(1, 1000)
        self.dark_n_spin.setValue(20)
        self.dark_led_check = QtWidgets.QCheckBox("Switch LED off while taking it")
        self.dark_led_check.setChecked(True)
        self.dark_btn = QtWidgets.QPushButton("Take dark")
        self.dark_clear_btn = QtWidgets.QPushButton("Clear")
        self.dark_label = QtWidgets.QLabel("none")
        self.dark_label.setWordWrap(True)
        self.subtract_check = QtWidgets.QCheckBox("Plot dark-subtracted")
        self.subtract_check.setChecked(True)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.dark_btn)
        row.addWidget(self.dark_clear_btn)
        f.addRow("Frames", self.dark_n_spin)
        f.addRow(self.dark_led_check)
        f.addRow(row)
        f.addRow(self.dark_label)
        f.addRow(self.subtract_check)
        lay.addWidget(g)

        # regions
        g = QtWidgets.QGroupBox("Regions and rel. fluo. yield")
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

        # recording
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
        self.record_btn = QtWidgets.QPushButton("Start recording")
        self.record_btn.setCheckable(True)
        self.record_label = QtWidgets.QLabel("")
        self.record_label.setWordWrap(True)
        f.addRow("Folder", row)
        f.addRow("File prefix", self.prefix_edit)
        f.addRow("Note", self.note_edit)
        f.addRow(self.save_yield_check)
        f.addRow(self.record_btn)
        f.addRow(self.record_label)
        lay.addWidget(g)
        lay.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(330)

        # plots
        # No antialiasing: with PyQt5 it made Qt hold the Python lock for up to
        # ~200 ms per redraw, starving the serial reader thread at high rates.
        pg.setConfigOptions(antialias=False)
        self.spec_plot = pg.PlotWidget(title="Spectrum")
        self.spec_plot.setLabel("bottom", "Wavelength", units="nm")
        self.spec_plot.setLabel("left", "Counts")
        self.spec_plot.showGrid(x=True, y=True, alpha=0.2)
        self.spec_plot.getAxis("left").enableAutoSIPrefix(False)
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen((230, 230, 230), width=1.5))
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
        self.yield_curve = self.yield_plot.plot(pen=pg.mkPen((120, 220, 120), width=1.5),
                                                connect="finite")
        self.warn_scatter = self.yield_plot.plot(pen=None, symbol="x", symbolSize=8,
                                                 symbolBrush=FLUO_COLOR, symbolPen=FLUO_COLOR)
        self.sat_scatter = self.yield_plot.plot(pen=None, symbol="o", symbolSize=5,
                                                symbolBrush=(255, 170, 0), symbolPen=None)
        yield_tools = QtWidgets.QHBoxLayout()
        yield_tools.addWidget(QtWidgets.QLabel("Show last"))
        self.window_spin = _spin(0, 86_400, 120, 0, 10, " s")
        self.window_spin.setSpecialValueText("all")
        yield_tools.addWidget(self.window_spin)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self._clear_yield)
        yield_tools.addWidget(clear)
        legend = QtWidgets.QLabel("<span style='color:#e63c3c'>✕</span> fluorescence without "
                                  "incident light &nbsp; <span style='color:#ffaa00'>●</span> saturated")
        yield_tools.addWidget(legend)
        yield_tools.addStretch(1)
        self.value_label = QtWidgets.QLabel("")
        yield_tools.addWidget(self.value_label)
        bottom = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.yield_plot)
        bl.addLayout(yield_tools)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.addWidget(self.spec_plot)
        split.addWidget(bottom)
        split.setSizes([500, 350])

        self.plot_tabs = QtWidgets.QTabWidget()
        self.plot_tabs.addTab(split, "Live plots")
        plot_3d = QtWidgets.QWidget()
        plot_3d_layout = QtWidgets.QVBoxLayout(plot_3d)
        plot_3d_tools = QtWidgets.QHBoxLayout()
        plot_3d_tools.addWidget(QtWidgets.QLabel("Show last"))
        self.spectrum_count_spin = QtWidgets.QSpinBox()
        self.spectrum_count_spin.setRange(2, 500)
        self.spectrum_count_spin.setValue(100)
        plot_3d_tools.addWidget(self.spectrum_count_spin)
        plot_3d_tools.addWidget(QtWidgets.QLabel("spectra"))
        self.refresh_3d_btn = QtWidgets.QPushButton("Refresh 3D")
        self.refresh_3d_btn.clicked.connect(self._update_3d_plot)
        plot_3d_tools.addWidget(self.refresh_3d_btn)
        self.auto_3d_check = QtWidgets.QCheckBox("Update while viewing")
        self.auto_3d_check.setChecked(True)
        plot_3d_tools.addWidget(self.auto_3d_check)
        plot_3d_tools.addStretch(1)
        plot_3d_layout.addLayout(plot_3d_tools)
        self.figure_3d = Figure()
        self.canvas_3d = FigureCanvasQTAgg(self.figure_3d)
        self.axes_3d = self.figure_3d.add_subplot(111, projection="3d")
        self.canvas_3d.setMinimumSize(500, 400)
        plot_3d_layout.addWidget(self.canvas_3d)
        self.plot_tabs.addTab(plot_3d, "3D spectrum history")

        main = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        main.addWidget(scroll)
        main.addWidget(self.plot_tabs)
        main.setSizes([340, 1000])
        self.setCentralWidget(main)

        self.warning_label = QtWidgets.QLabel("")
        self.stats_label = QtWidgets.QLabel("")
        self.statusBar().addWidget(self.warning_label)
        self.statusBar().addPermanentWidget(self.stats_label)

        # signals
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.gain_box.currentIndexChanged.connect(self._send_gain)
        self.integration_spin.valueChanged.connect(self._send_integration)
        self.avg_spin.valueChanged.connect(self._send_avg)
        self.period_spin.valueChanged.connect(self._send_period)
        self.fastest_check.toggled.connect(self._send_period)
        self.led_check.toggled.connect(self._send_led)
        self.start_btn.toggled.connect(self.toggle_stream)
        self.dark_btn.clicked.connect(self.take_dark)
        self.dark_clear_btn.clicked.connect(self.clear_dark)
        for box in (self.inc_lo, self.inc_hi, self.flu_lo, self.flu_hi, self.k_spin,
                    self.sat_high_spin, self.sat_low_spin):
            box.valueChanged.connect(self._apply_regions)
        self.inc_region.sigRegionChangeFinished.connect(self._region_dragged)
        self.flu_region.sigRegionChangeFinished.connect(self._region_dragged)
        self.record_btn.toggled.connect(self.toggle_recording)
        self.plot_tabs.currentChanged.connect(self._plot_tab_changed)
        self.spectrum_count_spin.valueChanged.connect(self._refresh_3d_if_visible)
        self._set_connected(False)

    # ------------------------------------------------------------ settings
    _PERSIST = ["inc_lo", "inc_hi", "flu_lo", "flu_hi", "k_spin", "sat_high_spin",
                "sat_low_spin", "dark_n_spin", "window_spin"]

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
        for name in ("folder_edit", "prefix_edit"):
            value = self.settings.value(name)
            if value:
                getattr(self, name).setText(str(value))
        self.save_yield_check.setChecked(str(self.settings.value("save_yield", "true")) == "true")
        self.fastest_check.setChecked(str(self.settings.value("fastest", "true")) == "true")
        geometry = self.settings.value("geometry")
        if isinstance(geometry, QtCore.QByteArray):
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        for name in self._PERSIST:
            self.settings.setValue(name, getattr(self, name).value())
        for name in ("folder_edit", "prefix_edit"):
            self.settings.setValue(name, getattr(self, name).text())
        self.settings.setValue("save_yield", "true" if self.save_yield_check.isChecked() else "false")
        self.settings.setValue("fastest", "true" if self.fastest_check.isChecked() else "false")
        self.settings.setValue("geometry", self.saveGeometry())
        if self.spec is not None:
            self.settings.setValue("port", self.spec.port)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._save_settings()
        self.record_btn.setChecked(False)
        self.disconnect_device()
        super().closeEvent(event)

    # ------------------------------------------------------------- device
    def refresh_ports(self) -> None:
        current = self.port_box.currentText() or self.settings.value("port", "")
        self.port_box.clear()
        ports = find_ports(probe=False) if self.spec is None else [self.spec.port]
        self.port_box.addItems(ports)
        if current in ports:
            self.port_box.setCurrentText(current)

    def toggle_connection(self) -> None:
        if self.spec is None:
            self.connect_device()
        else:
            self.disconnect_device()

    def connect_device(self) -> None:
        port = self.port_box.currentText()
        if not port:
            self._error("No Raspberry Pi Pico found. Plug in the spectrometer and press Refresh.")
            return
        try:
            self.spec = Spectrometer(port)
        except (DeviceError, OSError) as exc:
            self.spec = None
            self._error(f"Could not connect to {port}:\n{exc}")
            return
        status = self.spec.status
        self.calibration = WavelengthCalibration.best(status.get("wl_coeffs"))
        self.engine.wavelengths = self.calibration.wavelengths
        wl = self.engine.wavelengths
        self.spec_plot.setXRange(wl[0], wl[-1], padding=0.01)
        self.device_label.setText(
            f"{status['fw']} on {port}\n{status['name']}, clock {status['clk_hz'] / 1000:.0f} kHz\n"
            f"wavelengths: {self.calibration.source}"
            + ("" if self.calibration.calibrated else " (nominal 340-780 nm!)"))
        self._show_device_settings(status)
        self._set_connected(True)

    def disconnect_device(self) -> None:
        if self.spec is None:
            return
        self.record_btn.setChecked(False)
        self.start_btn.setChecked(False)
        try:
            self.spec.close()
        except Exception:
            pass
        self.spec = None
        self.device_label.setText("not connected")
        self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.port_box.setEnabled(not connected)
        self.refresh_btn.setEnabled(not connected)
        for w in (self.gain_box, self.integration_spin, self.avg_spin, self.period_spin,
                  self.fastest_check, self.led_check, self.start_btn, self.dark_btn,
                  self.record_btn):
            w.setEnabled(connected)

    def _show_device_settings(self, status: dict) -> None:
        widgets = (self.gain_box, self.integration_spin, self.avg_spin, self.led_check,
                   self.period_spin, self.fastest_check)
        for w in widgets:
            w.blockSignals(True)
        self.gain_box.setCurrentIndex(0 if status["high_gain"] else 1)
        self.integration_spin.setMinimum(status["min_integration_us"] / 1000)
        self.integration_spin.setValue(status["integration_us"] / 1000)
        self.avg_spin.setMaximum(status["max_avg"])
        self.avg_spin.setValue(status["n_avg"])
        self.led_check.setChecked(bool(status["led"]))
        if status["period_us"] > 0:
            self.fastest_check.setChecked(False)
            self.period_spin.setValue(status["period_us"] / 1000)
        for w in widgets:
            w.blockSignals(False)
        self._update_rate_label(status)

    def _update_rate_label(self, status: Optional[dict] = None) -> None:
        if self.spec is None:
            return
        if status is None:
            status = self.spec.get_status()
        min_ms = status["min_period_us"] / 1000
        eff_ms = status["effective_period_us"] / 1000
        self.period_spin.blockSignals(True)
        self.period_spin.setMinimum(min_ms)
        self.period_spin.blockSignals(False)
        self.period_spin.setEnabled(not self.fastest_check.isChecked() and self.spec is not None)
        self.rate_label.setText(f"min {min_ms:.2f} ms (= averages × integration)\n"
                                f"actual {eff_ms:.2f} ms, {1000 / eff_ms:.1f} spectra/s")

    def _command(self, method: str, *args):
        if self.spec is None:
            return None
        try:
            return getattr(self.spec, method)(*args)
        except DeviceError as exc:
            self._error(str(exc))
            return None

    def _send_gain(self) -> None:
        self._command("set_gain", self.gain_box.currentIndex() == 0)
        self._update_rate_label()

    def _send_integration(self) -> None:
        actual = self._command("set_integration_us", round(self.integration_spin.value() * 1000))
        if actual is not None:
            self.integration_spin.blockSignals(True)
            self.integration_spin.setValue(actual / 1000)
            self.integration_spin.blockSignals(False)
        self._send_period()

    def _send_avg(self) -> None:
        self._command("set_avg", self.avg_spin.value())
        self._send_period()

    def _send_period(self) -> None:
        if self.spec is None:
            return
        period = 0 if self.fastest_check.isChecked() else round(self.period_spin.value() * 1000)
        self._command("set_period_us", period)
        self._update_rate_label()

    def _send_led(self, on: bool) -> None:
        self._command("set_led", on)

    # ------------------------------------------------------------ streaming
    def toggle_stream(self, start: bool) -> None:
        if self.spec is None:
            return
        if start:
            period = 0 if self.fastest_check.isChecked() else round(self.period_spin.value() * 1000)
            try:
                self.spec.start_stream(period, on_frame=self.engine.on_frame)
            except DeviceError as exc:
                self._error(str(exc))
                self.start_btn.setChecked(False)
                return
            self.t0_device = None
            self.spectrum_history.clear()
            self._spectrum_t0_device = None
            self._last_3d_update = 0.0
            self.start_btn.setText("Stop")
        else:
            self.record_btn.setChecked(False)
            self.engine.cancel_dark()
            self.spec.stop_stream()
            self.start_btn.setText("Start")

    def take_dark(self) -> None:
        if self.spec is None:
            return
        if not self.spec.streaming:
            self.start_btn.setChecked(True)
        led_off = self.dark_led_check.isChecked()
        if led_off and self.led_check.isChecked():
            self._led_before_dark = True
            self.led_check.setChecked(False)
        self.engine.start_dark(self.dark_n_spin.value(), led_off)
        self.dark_btn.setEnabled(False)

    def clear_dark(self) -> None:
        self.engine.cancel_dark()
        self.engine.dark = None
        self.dark_btn.setEnabled(self.spec is not None)

    # ------------------------------------------------------------ regions
    def _apply_regions(self) -> None:
        self.engine.incident = Region("incident_light", self.inc_lo.value(), self.inc_hi.value())
        self.engine.fluorescence = Region("fluorescence", self.flu_lo.value(), self.flu_hi.value())
        self.engine.k = self.k_spin.value()
        self.engine.sat_high = self.sat_high_spin.value()
        self.engine.sat_low = self.sat_low_spin.value()
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

    # ------------------------------------------------------------ recording
    def _browse_folder(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Recording folder",
                                                            self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def toggle_recording(self, start: bool) -> None:
        if start:
            if self.spec is None:
                self.record_btn.setChecked(False)
                return
            folder = Path(self.folder_edit.text()).expanduser()
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._error(f"Cannot create {folder}: {exc}")
                self.record_btn.setChecked(False)
                return
            if not self.spec.streaming:
                self.start_btn.setChecked(True)
                if not self.spec.streaming:
                    self.record_btn.setChecked(False)
                    return
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            path = folder / f"{self.prefix_edit.text() or 'C12666MA'}_{stamp}.csv"
            status = self.spec.status
            e = self.engine
            metadata = {
                "started": datetime.now().astimezone().isoformat(timespec="seconds"),
                "software": f"c12666ma {__version__}",
                "device": f"{status['fw']} ({status['name']}) on {self.spec.port}",
                "sensor_clock_hz": status["clk_hz"],
                "acquisition_period": "fastest" if self.fastest_check.isChecked()
                                      else f"{self.period_spin.value():.1f} ms",
                "wavelength_calibration": f"{self.calibration.source}, nm = sum(c[i] * pixel**i), "
                                          f"c = {self.calibration.coefficients}",
                "incident_light_nm": f"{e.incident.lo_nm:.1f}-{e.incident.hi_nm:.1f}",
                "fluorescence_nm": f"{e.fluorescence.lo_nm:.1f}-{e.fluorescence.hi_nm:.1f}",
                "rel_fluo_yield": "fluorescence / incident_light (dark-subtracted region sums); "
                                  f"region has signal if sum > {e.k:g} x noise; "
                                  "0 if neither region has signal",
                "saturation_counts": f"high gain {e.sat_high:g}, low gain {e.sat_low:g}",
                "note": self.note_edit.text().replace("\n", " "),
            }
            try:
                rec = Recorder(path, e.wavelengths, metadata, self.save_yield_check.isChecked())
            except OSError as exc:
                self._error(f"Cannot write {path}: {exc}")
                self.record_btn.setChecked(False)
                return
            if e.dark is not None:
                rec.write_dark(e.dark)
            with e.lock:
                e.recorder = rec
            self._record_t0 = time.monotonic()
            self.record_btn.setText("Stop recording")
            self.save_yield_check.setEnabled(False)
        else:
            with self.engine.lock:
                rec, self.engine.recorder = self.engine.recorder, None
            if rec is not None:
                rec.close()
                self.record_label.setText(f"saved {rec.n_spectra} spectra to\n{rec.path}")
            self.record_btn.setText("Start recording")
            self.save_yield_check.setEnabled(True)

    # ------------------------------------------------------------ refresh
    def _clear_yield(self) -> None:
        for s in (self.yield_series, self.warn_series, self.sat_series):
            s.clear()
        self.t0_device = None

    def _tick(self) -> None:
        spec = self.spec
        if spec is not None and spec.reader_error is not None:
            err = spec.reader_error
            self.disconnect_device()
            self._error(f"Connection lost: {err}")
            return
        results = self.engine.results
        latest = None
        now = time.monotonic()
        while results:
            frame, res = results.popleft()
            latest = (frame, res)
            if self.t0_device is None:
                self.t0_device = frame.t_device_us
            t = (frame.t_device_us - self.t0_device) / 1e6
            if self._spectrum_t0_device is None:
                self._spectrum_t0_device = frame.t_device_us
            spectrum_time = (frame.t_device_us - self._spectrum_t0_device) / 1e6
            self.spectrum_history.append((spectrum_time, frame))
            self.yield_series.append(t, res.value)
            if res.warning:
                self.warn_series.append(t, 0.0)
                self._last_warning = ("Fluorescence signal without incident light", now, WARN_STYLE)
            if res.saturated:
                self.sat_series.append(t, res.value if np.isfinite(res.value) else 0.0)
                self._last_warning = ("Saturated pixels in a region: lower the integration time "
                                      "or use low gain", now, WARN_STYLE)
            self._fps_count += 1
        if latest is not None:
            frame, res = latest
            self.last_frame = frame
            dark = self.engine.dark
            y = frame.counts
            subtract = self.subtract_check.isChecked() and dark is not None and dark.matches(frame)
            if subtract:
                y = y - dark.mean
            self.spec_curve.setData(self.engine.wavelengths, y)
            sat = self.engine.sat_high if frame.high_gain else self.engine.sat_low
            self.sat_line.setValue(sat - (float(np.mean(dark.mean)) if subtract else 0.0))
            if res.status == YieldStatus.NO_DARK and self.engine.dark_progress is None:
                if now - self._last_warning[1] > 1.0:
                    self._last_warning = ("No matching dark spectrum: press 'Take dark' "
                                          "(needed for the yield)", now, INFO_STYLE)
            if res.status == YieldStatus.NO_DARK:
                text = "no matching dark spectrum"
            else:
                text = (f"incident_light {res.incident:.0f} ± {res.incident_noise:.0f} · "
                        f"fluorescence {res.fluorescence:.0f} ± {res.fluorescence_noise:.0f}")
                if np.isfinite(res.value):
                    text += f" · yield {res.value:.4f}"
            self.value_label.setText(text)
            self._update_yield_plot()
        if (latest is not None and self.plot_tabs.currentIndex() == 1
                and self.auto_3d_check.isChecked()
                and now - self._last_3d_update >= 1.0):
            self._update_3d_plot()
        self._update_status(now)

    def _plot_tab_changed(self, index: int) -> None:
        if index == 1:
            self._update_3d_plot()

    def _refresh_3d_if_visible(self) -> None:
        if self.plot_tabs.currentIndex() == 1:
            self._update_3d_plot()

    def _update_3d_plot(self) -> None:
        count = self.spectrum_count_spin.value()
        history = list(self.spectrum_history)[-count:]
        self.figure_3d.clear()
        self.axes_3d = self.figure_3d.add_subplot(111, projection="3d")
        if len(history) < 2:
            self.axes_3d.text2D(
                0.5, 0.5, "Collect at least two spectra to plot a 3D surface",
                transform=self.axes_3d.transAxes, ha="center", va="center")
            self.axes_3d.set_axis_off()
        else:
            times = np.asarray([t for t, _ in history])
            wavelengths = self.engine.wavelengths
            dark = self.engine.dark
            subtract = self.subtract_check.isChecked()
            spectra = np.asarray([
                frame.counts - dark.mean
                if subtract and dark is not None and dark.matches(frame)
                else frame.counts
                for _, frame in history
            ])
            wavelength_grid, time_grid = np.meshgrid(wavelengths, times)
            surface = self.axes_3d.plot_surface(
                wavelength_grid, time_grid, spectra, cmap="viridis",
                linewidth=0, antialiased=False,
                rstride=max(1, len(history) // 100), cstride=2)
            self.axes_3d.set_xlabel("Wavelength (nm)")
            self.axes_3d.set_ylabel("Time (s)")
            self.axes_3d.set_zlabel(
                "Counts (dark-subtracted where matched)" if subtract else "Counts")
            self.axes_3d.set_title(f"Most recent {len(history)} spectra")
            self.figure_3d.colorbar(surface, ax=self.axes_3d, shrink=0.65,
                                    pad=0.12, label="Counts")
        self.figure_3d.tight_layout()
        self.canvas_3d.draw_idle()
        self._last_3d_update = time.monotonic()

    def _update_yield_plot(self) -> None:
        window = self.window_spin.value()
        x, y = self.yield_series.x, self.yield_series.y
        self.yield_curve.setData(x, y)
        self.warn_scatter.setData(self.warn_series.x, self.warn_series.y)
        self.sat_scatter.setData(self.sat_series.x, self.sat_series.y)
        if window > 0 and len(x):
            self.yield_plot.setXRange(max(0.0, x[-1] - window), max(window, x[-1]), padding=0)
        else:
            self.yield_plot.enableAutoRange(axis="x")

    def _update_status(self, now: float) -> None:
        if now - self._fps_t >= 1.0:
            self.fps = self._fps_count / (now - self._fps_t)
            self._fps_count = 0
            self._fps_t = now
        text, t, style = self._last_warning
        if text and now - t < 1.5:
            self.warning_label.setText(text)
            self.warning_label.setStyleSheet(style)
        else:
            self.warning_label.setText("")
            self.warning_label.setStyleSheet("")
        spec = self.spec
        if spec is not None and spec.streaming:
            self.stats_label.setText(
                f"{self.fps:.1f} spectra/s · received {spec.frames_received} · dropped "
                f"{spec.frames_dropped} · checksum errors {spec.parser.crc_errors}"
                + (f" · device events {len(spec.events)}" if spec.events else ""))
        elif spec is None:
            self.stats_label.setText("")
        # dark
        progress = self.engine.dark_progress
        dark = self.engine.dark
        if progress is not None:
            self.dark_label.setText(f"taking dark: {progress[0]} / {progress[1]} frames")
        elif self.engine.dark_finished.is_set():
            self.engine.dark_finished.clear()
            self.dark_btn.setEnabled(True)
            if self._led_before_dark:
                self.led_check.setChecked(True)
            self._led_before_dark = None
        if progress is None and dark is not None:
            ok = self.last_frame is None or dark.matches(self.last_frame)
            taken = datetime.fromtimestamp(dark.timestamp).strftime("%H:%M:%S")
            self.dark_label.setText(
                f"{dark.n_frames} frames at {taken}, mean {dark.mean.mean():.1f}, "
                f"noise {np.median(dark.std):.1f} counts/pixel"
                + ("" if ok else "\nsettings changed since: take a new dark"))
            self.dark_label.setStyleSheet("" if ok else "color:#e63c3c")
        elif progress is None:
            self.dark_label.setText("none")
            self.dark_label.setStyleSheet("")
        # recording
        rec = self.engine.recorder
        if rec is not None:
            elapsed = time.monotonic() - self._record_t0
            self.record_label.setText(f"{rec.path.name}\n{rec.n_spectra} spectra · "
                                      f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}")
            if now - self._last_flush > 1.0:
                with self.engine.lock:
                    rec.flush()
                self._last_flush = now

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


def main() -> None:
    if QtWidgets.QApplication.instance() is None:
        configure_qt()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.resize(1400, 900)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
