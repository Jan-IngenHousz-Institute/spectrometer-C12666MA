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

import io
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
    def __init__(self, path: Path, wavelengths: np.ndarray, metadata: dict,
                 include_yield: bool) -> None:
        self.path = Path(path)
        self.include_yield = include_yield
        self.n_spectra = 0
        self._t0_device: Optional[int] = None
        self._n_extra = len(BASE_COLUMNS) - 1 + (len(YIELD_COLUMNS) if include_yield else 0)
        self._file = open(self.path, "w", newline="", encoding="utf-8")
        f = self._file
        f.write("# C12666MA spectrometer recording\n")
        for key, value in metadata.items():
            f.write(f"# {key}: {value}\n")
        f.write("# pixel values: raw ADC counts (1 count = 1 mV) averaged over n_avg scans\n")
        f.write("# timestamp: start of integration; t_s: seconds since the first spectrum\n")
        columns = BASE_COLUMNS + (YIELD_COLUMNS if include_yield else [])
        columns += [f"px{i}" for i in range(len(wavelengths))]
        f.write(",".join(columns) + "\n")
        f.write("wavelength_nm" + "," * self._n_extra + ","
                + ",".join(f"{w:.2f}" for w in wavelengths) + "\n")
        f.flush()

    def _base(self, kind: str, frame_like: Frame) -> list:
        if self._t0_device is None:
            self._t0_device = frame_like.t_device_us
        return [kind, iso_time(frame_like.timestamp),
                f"{(frame_like.t_device_us - self._t0_device) / 1e6:.6f}",
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
