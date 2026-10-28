"""Dark subtraction, wavelength regions and the relative fluorescence yield.

rel. fluo. yield = fluorescence / incident_light, where each term is the sum
of dark-subtracted counts over the pixels of its wavelength region.

A region "has signal" when its sum exceeds k times its noise. The noise
comes from the dark reference: sqrt(sum of per-pixel variances), including
the uncertainty of the dark mean itself.

    incident light   fluorescence    result
    signal           any             fluorescence / incident_light
    no signal        signal          NaN + FLUO_WITHOUT_INCIDENT warning
    no signal        no signal       0 (clamped)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

import numpy as np

from .protocol import Frame

# Raw counts at which the sensor output saturates (MCP3301 with 4.096 V
# reference: 1 count = 1 mV). Datasheet: offset ~0.35 V plus 2.8 V (high
# gain) / 1.7 V (low gain) typical saturation. High gain measured on our
# unit: flat tops at 3160-3240 counts.
SATURATION_HIGH_GAIN = 3100
SATURATION_LOW_GAIN = 1950


class YieldStatus(str, Enum):
    OK = "ok"
    NO_SIGNAL = "no_signal"                       # clamped to 0
    FLUO_WITHOUT_INCIDENT = "fluo_without_incident"  # warning, NaN
    NO_DARK = "no_dark"                           # no matching dark reference


class FLDStatus(str, Enum):
    OK = "ok"
    NO_DARK = "no_dark"
    WAVELENGTH_OUT_OF_RANGE = "wavelength_out_of_range"
    ZERO_TOP_CHANGE = "zero_top_change"


@dataclass
class Region:
    name: str
    lo_nm: float
    hi_nm: float

    def mask(self, wavelengths: np.ndarray) -> np.ndarray:
        lo, hi = sorted((self.lo_nm, self.hi_nm))
        return (wavelengths >= lo) & (wavelengths <= hi)


@dataclass
class DarkReference:
    mean: np.ndarray
    std: np.ndarray            # per-pixel temporal noise of a single frame
    n_frames: int
    settings_key: tuple
    timestamp: float

    @classmethod
    def from_frames(cls, frames: Sequence[Frame]) -> "DarkReference":
        if not frames:
            raise ValueError("no frames")
        keys = {f.settings_key for f in frames}
        if len(keys) != 1:
            raise ValueError("dark frames were taken with different settings")
        stack = np.array([f.counts for f in frames])
        mean = stack.mean(axis=0)
        if len(frames) >= 2:
            std = stack.std(axis=0, ddof=1)
        else:
            # one frame: estimate white noise from neighbouring-pixel differences
            std = np.full_like(mean, np.std(np.diff(mean)) / np.sqrt(2))
        return cls(mean, std, len(frames), keys.pop(), frames[0].timestamp)

    def matches(self, frame: Frame) -> bool:
        return frame.settings_key == self.settings_key

    def region_noise(self, mask: np.ndarray) -> float:
        var = self.std[mask] ** 2 * (1.0 + 1.0 / self.n_frames)
        return float(np.sqrt(var.sum()))


@dataclass
class YieldResult:
    value: float
    status: YieldStatus
    incident: float            # dark-subtracted sum over the incident_light region
    fluorescence: float
    incident_noise: float
    fluorescence_noise: float
    saturated: bool            # a pixel in either region reached saturation

    @property
    def warning(self) -> bool:
        return self.status == YieldStatus.FLUO_WITHOUT_INCIDENT


@dataclass
class FLDResult:
    value: float
    status: FLDStatus
    top_in: float
    top_out: float
    bottom_in: float
    bottom_out: float


def saturation_level(high_gain: bool) -> float:
    return SATURATION_HIGH_GAIN if high_gain else SATURATION_LOW_GAIN


def compute_yield(frame: Frame, dark: Optional[DarkReference], wavelengths: np.ndarray,
                  incident: Region, fluorescence: Region, k: float = 5.0,
                  saturation: Optional[float] = None) -> YieldResult:
    m_i = incident.mask(wavelengths)
    m_f = fluorescence.mask(wavelengths)
    if saturation is None:
        saturation = saturation_level(frame.high_gain)
    saturated = bool(np.any(frame.counts[m_i | m_f] >= saturation))
    if dark is None or not dark.matches(frame) or not m_i.any() or not m_f.any():
        return YieldResult(float("nan"), YieldStatus.NO_DARK, float("nan"), float("nan"),
                           float("nan"), float("nan"), saturated)
    signal = frame.counts - dark.mean
    s_i = float(signal[m_i].sum())
    s_f = float(signal[m_f].sum())
    n_i = dark.region_noise(m_i)
    n_f = dark.region_noise(m_f)
    has_i = s_i > k * n_i
    has_f = s_f > k * n_f
    if has_i:
        value, status = s_f / s_i, YieldStatus.OK
    elif has_f:
        value, status = float("nan"), YieldStatus.FLUO_WITHOUT_INCIDENT
    else:
        value, status = 0.0, YieldStatus.NO_SIGNAL
    return YieldResult(value, status, s_i, s_f, n_i, n_f, saturated)


def compute_paired_yield(incident_frame: Frame, incident_dark: Optional[DarkReference],
                         incident_wavelengths: np.ndarray, fluorescence_frame: Frame,
                         fluorescence_dark: Optional[DarkReference],
                         fluorescence_wavelengths: np.ndarray, incident: Region,
                         fluorescence: Region, k: float = 5.0,
                         sat_high: float = SATURATION_HIGH_GAIN,
                         sat_low: float = SATURATION_LOW_GAIN) -> YieldResult:
    """Calculate fluorescence / incoming light from synchronized, separate devices."""
    if incident_frame.seq != fluorescence_frame.seq:
        raise ValueError("paired frames must have the same sequence number")

    m_i = incident.mask(incident_wavelengths)
    m_f = fluorescence.mask(fluorescence_wavelengths)
    sat_i = sat_high if incident_frame.high_gain else sat_low
    sat_f = sat_high if fluorescence_frame.high_gain else sat_low
    saturated = (bool(np.any(incident_frame.counts[m_i] >= sat_i))
                 or bool(np.any(fluorescence_frame.counts[m_f] >= sat_f)))
    if (incident_dark is None or not incident_dark.matches(incident_frame)
            or fluorescence_dark is None or not fluorescence_dark.matches(fluorescence_frame)
            or not m_i.any() or not m_f.any()):
        return YieldResult(float("nan"), YieldStatus.NO_DARK, float("nan"), float("nan"),
                           float("nan"), float("nan"), saturated)

    s_i = float((incident_frame.counts - incident_dark.mean)[m_i].sum())
    s_f = float((fluorescence_frame.counts - fluorescence_dark.mean)[m_f].sum())
    n_i = incident_dark.region_noise(m_i)
    n_f = fluorescence_dark.region_noise(m_f)
    has_i = s_i > k * n_i
    has_f = s_f > k * n_f
    if has_i:
        value, status = s_f / s_i, YieldStatus.OK
    elif has_f:
        value, status = float("nan"), YieldStatus.FLUO_WITHOUT_INCIDENT
    else:
        value, status = 0.0, YieldStatus.NO_SIGNAL
    return YieldResult(value, status, s_i, s_f, n_i, n_f, saturated)


def compute_paired_fld(top_frame: Frame, top_dark: Optional[DarkReference],
                       top_wavelengths: np.ndarray, bottom_frame: Frame,
                       bottom_dark: Optional[DarkReference],
                       bottom_wavelengths: np.ndarray,
                       wavelength_in: float, wavelength_out: float) -> FLDResult:
    """Calculate FLD from synchronized, dark-subtracted top and bottom spectra."""
    if top_frame.seq != bottom_frame.seq:
        raise ValueError("paired frames must have the same sequence number")

    empty = float("nan")
    if (top_dark is None or not top_dark.matches(top_frame)
            or bottom_dark is None or not bottom_dark.matches(bottom_frame)):
        return FLDResult(empty, FLDStatus.NO_DARK, empty, empty, empty, empty)

    wavelength_in, wavelength_out = sorted((wavelength_in, wavelength_out))
    if (wavelength_in < top_wavelengths[0] or wavelength_out > top_wavelengths[-1]
            or wavelength_in < bottom_wavelengths[0]
            or wavelength_out > bottom_wavelengths[-1]):
        return FLDResult(empty, FLDStatus.WAVELENGTH_OUT_OF_RANGE,
                         empty, empty, empty, empty)

    top_signal = top_frame.counts - top_dark.mean
    bottom_signal = bottom_frame.counts - bottom_dark.mean
    top_in, top_out = np.interp([wavelength_in, wavelength_out],
                                top_wavelengths, top_signal)
    bottom_in, bottom_out = np.interp([wavelength_in, wavelength_out],
                                      bottom_wavelengths, bottom_signal)
    top_in, top_out = float(top_in), float(top_out)
    bottom_in, bottom_out = float(bottom_in), float(bottom_out)

    if top_out == top_in:
        status = FLDStatus.ZERO_TOP_CHANGE
        value = empty
    else:
        value = abs(wavelength_in - wavelength_out) * (
            top_out * bottom_in - bottom_out * top_in) / (top_out - top_in)
        status = FLDStatus.OK
    return FLDResult(value, status, top_in, top_out, bottom_in, bottom_out)
