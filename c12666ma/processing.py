"""Per-spectrometer dark capture and frame processing.

Runs in the device's serial reader thread, so every frame is processed and
recorded even when the GUI redraws slower. AcquisitionSession combines
synchronized frames from the assigned devices for the relative fluorescence.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .analysis import (SATURATION_HIGH_GAIN, SATURATION_LOW_GAIN, DarkReference, Region,
                       YieldResult, compute_yield)
from .protocol import FLAG_LED_CHANGED, Frame


@dataclass(frozen=True)
class AnalysisSettings:
    """Shared by all devices. Replace the whole object to change it (thread safe)."""
    incident: Region = field(default_factory=lambda: Region("incident_light", 440.0, 480.0))
    fluorescence: Region = field(default_factory=lambda: Region("fluorescence", 680.0, 760.0))
    k: float = 5.0
    sat_high: float = float(SATURATION_HIGH_GAIN)
    sat_low: float = float(SATURATION_LOW_GAIN)


class DeviceProcessor:
    def __init__(self, wavelengths: np.ndarray, analysis: AnalysisSettings,
                 sink: Optional[Callable[[Frame, YieldResult], None]] = None) -> None:
        self.lock = threading.Lock()          # guards the recorder
        self.wavelengths = wavelengths
        self.analysis = analysis
        self.sink = sink
        self.dark: Optional[DarkReference] = None
        self.recorder = None                  # Recorder / RotatingRecorder, set by the session
        self.last_frame: Optional[Frame] = None
        self.dark_finished = threading.Event()
        self._dark_frames: Optional[list[Frame]] = None
        self._dark_target = 0
        self._dark_require_led_off = True

    # ------------------------------------------------------------------ dark
    def start_dark(self, n_frames: int, require_led_off: bool) -> None:
        self.dark_finished.clear()
        self._dark_target = n_frames
        self._dark_require_led_off = require_led_off
        self._dark_frames = []

    def cancel_dark(self) -> None:
        self._dark_frames = None

    @property
    def dark_progress(self) -> Optional[tuple[int, int]]:
        frames = self._dark_frames
        return None if frames is None else (len(frames), self._dark_target)

    def dark_matches(self) -> bool:
        return (self.dark is not None and self.last_frame is not None
                and self.dark.matches(self.last_frame))

    # ---------------------------------------------------------------- frames
    def on_frame(self, frame: Frame) -> YieldResult:
        self.last_frame = frame
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
        a = self.analysis
        saturation = a.sat_high if frame.high_gain else a.sat_low
        result = compute_yield(frame, self.dark, self.wavelengths, a.incident,
                               a.fluorescence, a.k, saturation)
        with self.lock:
            if self.recorder is not None:
                self.recorder.write_frame(frame, result if self.recorder.include_yield else None)
        if self.sink is not None:
            self.sink(frame, result)
        return result
