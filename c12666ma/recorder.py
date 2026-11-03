"""CSV recording: one file per measurement.

Layout (open with pandas: ``pd.read_csv(path, comment="#")``):

    # metadata lines ...
    kind,timestamp,t_s,seq,integration_us,n_avg,high_gain,led,flags,[yield columns],px0..px255
    wavelength_nm,,,,,,,,,[...],346.0,348.3,...
    dark,...            dark reference (mean counts), every time one is taken
    dark_std,...        its per-pixel noise
    spectrum,...        one row per spectrum: raw counts averaged over n_avg

Spectra are stored raw (not dark-subtracted) so nothing is lost; subtract
the most recent `dark` row above them to get the signal.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from .analysis import DarkReference, YieldResult
from .protocol import Frame

BASE_COLUMNS = ["kind", "timestamp", "t_s", "seq", "integration_us", "n_avg",
                "high_gain", "led", "flags"]
YIELD_COLUMNS = ["incident_light", "fluorescence", "rel_fluo_yield", "yield_status"]


def iso_time(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s).astimezone().isoformat(timespec="milliseconds")


def _fmt_counts(values: np.ndarray) -> str:
    """Integers as integers, otherwise the shortest exact decimal (sums / n_avg
    are exact binary fractions, e.g. 300.25)."""
    if np.all(values == np.round(values)):
        return ",".join(map(str, values.astype(np.int64)))
    return ",".join(map(repr, values.tolist()))


def _fmt_float(value: float) -> str:
    return "" if np.isnan(value) else f"{value:.6g}"


class Recorder:
    """One CSV file. t_s counts from `t0_epoch` (host time, common to all
    devices of a session) if given, else from the first spectrum's device clock."""

    def __init__(self, path: Path, wavelengths: np.ndarray, metadata: dict,
                 include_yield: bool, t0_epoch: Optional[float] = None) -> None:
        self.path = Path(path)
        self.include_yield = include_yield
        self.n_spectra = 0
        self._t0_epoch = t0_epoch
        self._t0_device: Optional[int] = None
        self._n_extra = len(BASE_COLUMNS) - 1 + (len(YIELD_COLUMNS) if include_yield else 0)
        self._file = open(self.path, "w", newline="", encoding="utf-8")
        f = self._file
        f.write("# C12666MA spectrometer recording\n")
        for key, value in metadata.items():
            f.write(f"# {key}: {value}\n")
        f.write("# pixel values: raw ADC counts (1 count = 1 mV) averaged over n_avg scans\n")
        f.write("# timestamp: start of integration; t_s: seconds since "
                + ("the start of the session\n" if t0_epoch is not None else "the first spectrum\n"))
        columns = BASE_COLUMNS + (YIELD_COLUMNS if include_yield else [])
        columns += [f"px{i}" for i in range(len(wavelengths))]
        f.write(",".join(columns) + "\n")
        f.write("wavelength_nm" + "," * self._n_extra + ","
                + ",".join(f"{w:.2f}" for w in wavelengths) + "\n")
        f.flush()

    def _base(self, kind: str, frame_like: Frame) -> list:
        if self._t0_epoch is not None:
            t_s = frame_like.timestamp - self._t0_epoch
        else:
            if self._t0_device is None:
                self._t0_device = frame_like.t_device_us
            t_s = (frame_like.t_device_us - self._t0_device) / 1e6
        return [kind, iso_time(frame_like.timestamp), f"{t_s:.6f}",
                frame_like.seq, frame_like.integration_us, frame_like.n_avg,
                int(frame_like.high_gain), int(frame_like.led), frame_like.flags]

    def write_dark(self, dark: DarkReference) -> None:
        clocks, clk_hz, n_avg, high_gain = dark.settings_key
        info = [iso_time(dark.timestamp), "", "", round(clocks * 1e6 / clk_hz), n_avg,
                int(high_gain), "", ""]
        if self.include_yield:
            info += [""] * len(YIELD_COLUMNS)
        info = ",".join(map(str, info))
        self._file.write(f"dark,{info},{_fmt_counts(np.round(dark.mean, 3))}\n")
        self._file.write(f"dark_std,{info},{_fmt_counts(np.round(dark.std, 3))}\n")

    def write_frame(self, frame: Frame, result: Optional[YieldResult] = None) -> None:
        row = self._base("spectrum", frame)
        if self.include_yield:
            if result is None:
                row += ["", "", "", ""]
            else:
                row += [_fmt_float(result.incident), _fmt_float(result.fluorescence),
                        _fmt_float(result.value),
                        result.status.value + (";saturated" if result.saturated else "")]
        buf = io.StringIO()
        buf.write(",".join(map(str, row)))
        buf.write(",")
        buf.write(_fmt_counts(frame.counts))
        buf.write("\n")
        self._file.write(buf.getvalue())
        self.n_spectra += 1

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


class RotatingRecorder:
    """A recording split into parts of `part_hours` each: <stem>_part001.csv, ...
    Every part is a complete CSV (header, wavelength row, latest dark)."""

    def __init__(self, folder: Path, stem: str, wavelengths: np.ndarray, metadata: dict,
                 include_yield: bool, part_hours: float = 24.0,
                 t0_epoch: Optional[float] = None, clock=time.time) -> None:
        self.folder = Path(folder)
        self.stem = stem
        self.wavelengths = wavelengths
        self.metadata = metadata
        self.include_yield = include_yield
        self.part_seconds = part_hours * 3600.0
        self.t0_epoch = t0_epoch
        self.clock = clock
        self.part = 0
        self.paths: list[Path] = []
        self.n_spectra = 0
        self._dark: Optional[DarkReference] = None
        self._rec: Optional[Recorder] = None
        self._open_part()

    @property
    def path(self) -> Path:
        return self._rec.path

    def _open_part(self) -> None:
        if self._rec is not None:
            self._rec.close()
        self.part += 1
        path = self.folder / f"{self.stem}_part{self.part:03d}.csv"
        meta = dict(self.metadata, part=self.part)
        self._rec = Recorder(path, self.wavelengths, meta, self.include_yield, self.t0_epoch)
        self.paths.append(path)
        self._part_start = self.clock()
        if self._dark is not None:
            self._rec.write_dark(self._dark)

    def write_dark(self, dark: DarkReference) -> None:
        self._dark = dark
        self._rec.write_dark(dark)

    def write_frame(self, frame: Frame, result: Optional[YieldResult] = None) -> None:
        if self.clock() - self._part_start >= self.part_seconds:
            self._open_part()
        self._rec.write_frame(frame, result)
        self.n_spectra += 1

    def flush(self) -> None:
        self._rec.flush()

    def close(self) -> None:
        self._rec.close()


class PairedRecorder:
    """CSV output for one synchronized incoming/fluorescence device pair."""

    def __init__(self, path: Path, incident_device: str, fluorescence_device: str,
                 incident_region: tuple[float, float],
                 fluorescence_region: tuple[float, float], k: float,
                 t0_epoch: float) -> None:
        self.path = Path(path)
        self.t0_epoch = t0_epoch
        self._file = open(self.path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._lock = threading.Lock()
        self.n_measurements = 0
        for key, value in (
                ("incoming_light_device", incident_device),
                ("fluorescence_device", fluorescence_device),
                ("incoming_light_nm", f"{incident_region[0]:.1f}-{incident_region[1]:.1f}"),
                ("fluorescence_nm", f"{fluorescence_region[0]:.1f}-{fluorescence_region[1]:.1f}"),
                ("signal_threshold", f"{k:g} x noise")):
            self._file.write(f"# {key}: {value}\n")
        self._writer.writerow([
            "seq", "t_s", "incoming_light_timestamp", "fluorescence_timestamp",
            "incoming_light_device", "fluorescence_device", "incident_light",
            "fluorescence", "rel_fluo_yield", "yield_status",
        ])

    def write(self, seq: int, incident_timestamp: float, fluorescence_timestamp: float,
              incident_device: str, fluorescence_device: str, result: YieldResult) -> None:
        with self._lock:
            self._writer.writerow([
                seq, f"{fluorescence_timestamp - self.t0_epoch:.6f}",
                iso_time(incident_timestamp), iso_time(fluorescence_timestamp),
                incident_device, fluorescence_device, _fmt_float(result.incident),
                _fmt_float(result.fluorescence), _fmt_float(result.value),
                result.status.value + (";saturated" if result.saturated else ""),
            ])
            self.n_measurements += 1

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()
