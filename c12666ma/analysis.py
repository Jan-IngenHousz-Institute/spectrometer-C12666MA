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
