"""Simulated spectrometers with the same interface as `device.Spectrometer`.

Used by the tests and by `python -m c12666ma --simulate N` to try the GUI and
multi-device sessions without hardware. Each simulated unit keeps its
settings while "plugged in" (like the real device's RAM) and can be unplugged
to test reconnection.
"""

from __future__ import annotations

import heapq
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .calibration import WavelengthCalibration
from .device import DeviceError, DeviceInfo
from .protocol import FLAG_LATE, FLAG_LED_CHANGED, FLAG_RESTART, Frame

CLK_HZ = 200_000
MIN_PERIOD_CLKS = 1035
DARK_LEVEL = 320.0
NOISE = 10.0


@dataclass
class SimUnit:
    port: str
    uid: str
    name: str
    integration_us: int = 20_000
    n_avg: int = 1
    high_gain: int = 1
    led: int = 0
    period_us: int = 0
    wl_coeffs: list = field(default_factory=lambda: [0.0] * 6)
    plugged: bool = True
    unplugged_until: float = 0.0
    led_on_since: float = 0.0
    boot_offset_s: float = 0.0          # how long before the program this unit "booted"
    fixed_pattern: np.ndarray = field(default=None, repr=False)


_UNITS: dict[str, SimUnit] = {}
_BOOT_PERF = time.perf_counter()
_BOOT_EPOCH = time.time()


def create_units(n: int) -> list[str]:
    """Create n simulated units SIM1..SIMn (idempotent); returns their ports."""
    ports = []
    for i in range(1, n + 1):
        port = f"SIM{i}"
        if port not in _UNITS:
            rng = np.random.default_rng(i)
            _UNITS[port] = SimUnit(port, f"51A{i:013X}", f"SIM-{i}", boot_offset_s=37.1 * i,
                                   fixed_pattern=rng.normal(0, 2.0, 256))
        ports.append(port)
    return ports


def reset() -> None:
    """Remove all simulated units (tests)."""
    _UNITS.clear()


def available() -> list[DeviceInfo]:
    now = time.monotonic()
    for unit in _UNITS.values():
        if not unit.plugged and now >= unit.unplugged_until:
            unit.plugged = True
    return [DeviceInfo(u.port, u.uid) for u in _UNITS.values() if u.plugged]


def is_simulated(port: str) -> bool:
    return port.startswith("SIM")


def _integration_clocks(us: int) -> int:
    return max(MIN_PERIOD_CLKS, min(round(us * CLK_HZ / 1e6), 10_000_000 * CLK_HZ // 1_000_000))


class _Parser:
    crc_errors = 0


class SimulatedSpectrometer:
    def __init__(self, port: str) -> None:
        unit = _UNITS.get(port)
        if unit is None or not unit.plugged:
            raise DeviceError(f"{port}: no simulated spectrometer")
        self.unit = unit
        self.port = port
        self.uid = unit.uid
        self.serial_number = unit.uid
        self.name = unit.name
        self.reader_error: Optional[BaseException] = None
        self.frames_received = 0
        self.frames_dropped = 0
        self.parser = _Parser()
        self.events: list[str] = []
        self.on_event: Optional[Callable[[dict], None]] = None
        self._on_frame: Optional[Callable[[Frame], None]] = None
        self._streaming = False
        self._triggered = False
        self._busy_until_us = 0
        self._waiting: list[int] = []
        self._led_changed = False
        self._restart_flag = 0
        self._seq = 0
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._heap: list = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        # each unit's clock runs from its own "boot"; one shared PC anchor keeps
        # the simulated PC <-> device mapping exact
        self._boot = _BOOT_PERF - unit.boot_offset_s
        self._boot_epoch = _BOOT_EPOCH - unit.boot_offset_s
        self._rng = np.random.default_rng()
        self._wl = WavelengthCalibration.from_file().wavelengths
        self.status = self.get_status()

    # ------------------------------------------------------------- settings
    def _check(self) -> None:
        if self.reader_error is not None or not self.unit.plugged:
            raise DeviceError(f"{self.port}: device unplugged")

    def _now_us(self) -> int:
        return int((time.perf_counter() - self._boot) * 1e6)

    def device_to_host_time(self, t_device_us: int) -> float:
        return self._boot_epoch + t_device_us / 1e6

    def _window_clocks(self) -> int:
        return _integration_clocks(self.unit.integration_us) * self.unit.n_avg

    def get_status(self) -> dict:
        self._check()
        u = self.unit
        clocks = _integration_clocks(u.integration_us)
        window_us = round(clocks * u.n_avg * 1e6 / CLK_HZ)
        self.status = {
            "fw": "C12666MA_SIMULATED", "name": u.name, "uid": u.uid, "n_pixels": 256,
            "integration_us": round(clocks * 1e6 / CLK_HZ), "integration_clocks": clocks,
            "min_integration_us": round(MIN_PERIOD_CLKS * 1e6 / CLK_HZ),
            "max_integration_us": 10_000_000, "clk_hz": CLK_HZ, "n_avg": u.n_avg,
            "max_avg": 16, "high_gain": u.high_gain, "led": u.led, "period_us": u.period_us,
            "min_period_us": window_us,
            "effective_period_us": max(window_us, u.period_us),
            "min_trigger_period_us": window_us + round(MIN_PERIOD_CLKS * 1e6 / CLK_HZ) + 2000,
            "streaming": int(self._streaming),
            "mode": ("triggered" if self._triggered else "continuous") if self._streaming else "idle",
            "t_us": self._now_us(), "wl_coeffs": list(u.wl_coeffs),
            "counters": {}, "cpu_load_pct": 0,
        }
        return self.status

    def set_integration_us(self, us: int) -> int:
        self._check()
        self.unit.integration_us = round(_integration_clocks(int(us)) * 1e6 / CLK_HZ)
        self._restart()
        return self.unit.integration_us

    def set_gain(self, high: bool) -> bool:
        self._check()
        self.unit.high_gain = int(bool(high))
        self._restart()
        return bool(self.unit.high_gain)

    def set_avg(self, n: int) -> int:
        self._check()
        self.unit.n_avg = max(1, min(int(n), 16))
        self._restart()
        return self.unit.n_avg

    def set_led(self, on: bool) -> bool:
        self._check()
        if bool(on) != bool(self.unit.led):
            self.unit.led_on_since = time.perf_counter()
            self._led_changed = True
        self.unit.led = int(bool(on))
        return bool(self.unit.led)

    def set_period_us(self, us: int) -> dict:
        self._check()
        self.unit.period_us = max(0, int(us))
        return {"period_us": self.unit.period_us}

    def set_name(self, name: str) -> str:
        self._check()
        self.unit.name = self.name = name.strip()[:15]
        return self.unit.name

    def get_wl_coeffs(self) -> list:
        return list(self.unit.wl_coeffs)

    def set_wl_coeffs(self, coeffs) -> list:
        coeffs = list(coeffs) + [0.0] * (6 - len(coeffs))
        self.unit.wl_coeffs = [float(np.float32(c)) for c in coeffs[:6]]
        return list(self.unit.wl_coeffs)

    def save(self) -> None:
        self._check()

    def close(self) -> None:
        self.stop_stream()

    # ------------------------------------------------------------- faults
    def simulate_unplug(self, seconds: float) -> None:
        """Pull the USB cable for `seconds`."""
        self.unit.plugged = False
        self.unit.unplugged_until = time.monotonic() + seconds
        self.reader_error = OSError("simulated unplug")
        self._stop.set()

    # ------------------------------------------------------------- frames
    def _spectrum(self, t_start_us: int) -> np.ndarray:
        u = self.unit
        wl = self._wl
        t_s = _integration_clocks(u.integration_us) / CLK_HZ
        gain = 1.0 if u.high_gain else 0.35
        g = lambda c, w: np.exp(-0.5 * ((wl - c) / w) ** 2)
        rate = 2000.0 * (0.6 * g(462, 10) + 0.35 * g(570, 45))          # ambient white light, counts/s
        if u.led:
            since = max(0.0, time.perf_counter() - u.led_on_since)
            yld = 0.05 + 0.08 * np.exp(-since / 20.0)                     # Kautsky-like decline
            excitation = 60_000.0 * g(470, 9)
            fluo = g(685, 10) + 0.6 * g(735, 18)
            fluo *= yld * excitation.sum() / fluo.sum()
            rate = rate + excitation + fluo
        signal = rate * t_s * gain
        counts = DARK_LEVEL + u.fixed_pattern + signal
        counts += self._rng.normal(0, NOISE / np.sqrt(u.n_avg), 256)
        return np.minimum(counts, 3200.0 if u.high_gain else 2000.0).round(2)

    def _make_frame(self, seq: int, t_start_us: int, flags: int) -> Frame:
        u = self.unit
        clocks = _integration_clocks(u.integration_us)
        if self._led_changed:
            flags |= FLAG_LED_CHANGED
            self._led_changed = False
        frame = Frame(seq=seq, t_device_us=t_start_us,
                      integration_us=round(clocks * 1e6 / CLK_HZ), integration_clocks=clocks,
                      clk_hz=CLK_HZ, n_avg=u.n_avg, high_gain=bool(u.high_gain),
                      led=bool(u.led), flags=flags, adc_errors=0,
                      counts=self._spectrum(t_start_us))
        frame.timestamp = self.device_to_host_time(t_start_us)
        return frame

    def _emit(self, frame: Frame) -> None:
        if self._stop.is_set() or self.reader_error is not None:
            return
        self.frames_received += 1
        if self._on_frame is not None:
            self._on_frame(frame)

    # ------------------------------------------------------------- streaming
    @property
    def streaming(self) -> bool:
        return self._streaming

    def start_stream(self, period_us: int = 0, on_frame=None, triggered: bool = False) -> dict:
        self._check()
        self._on_frame = on_frame
        self._triggered = triggered
        self._streaming = True
        self._stop.clear()
        self._seq = 0
        self._restart_flag = FLAG_RESTART
        if triggered:
            self._worker = threading.Thread(target=self._deliver, daemon=True)
            self._worker.start()
        else:
            self._thread = threading.Thread(target=self._continuous, daemon=True)
            self._thread.start()
        return {"stream": "started"}

    def _restart(self) -> None:
        self._restart_flag = FLAG_RESTART

    def _continuous(self) -> None:
        t = self._now_us()
        while not self._stop.is_set():
            window_us = round(self._window_clocks() * 1e6 / CLK_HZ)
            t_end = t + window_us
            while self._now_us() < t_end and not self._stop.is_set():
                time.sleep(max(0.0, min(0.01, (t_end - self._now_us()) / 1e6)))
            frame = self._make_frame(self._seq, t, self._restart_flag)
            self._restart_flag = 0
            self._seq += 1
            self._emit(frame)
            t = t_end

    def sync_clock(self, samples: int = 5) -> float:
        self._check()
        return 0.0

    def device_time_at(self, perf_time: float) -> int:
        return int((perf_time - self._boot) * 1e6)

    def trigger(self, seq: int, delay_us: int = 0) -> None:
        self.trigger_at(seq, self._now_us() + max(0, int(delay_us)))

    def trigger_at(self, seq: int, device_time_us: int) -> None:
        """Like the firmware: up to 16 triggers wait for their start time; a frame
        that cannot start on time (the previous one is still running) starts right
        after it, flagged late; "busy" when the queue is full. Times are on the
        device clock; frames are delivered in order, like over the serial link."""
        self._check()
        now = self._now_us()
        due = int(device_time_us)
        with self._cv:
            self._waiting = [t for t in self._waiting if t > now]
            if len(self._waiting) >= 16:
                text = '{"event":"busy","trig":%d}' % seq
                self.events.append(text)
                if self.on_event is not None:
                    self.on_event({"event": "busy", "trig": seq})
                return
            t_start = max(due, self._busy_until_us, now)
            flags = FLAG_LATE if t_start > due + 1000 else 0
            self._waiting.append(t_start)
            self._busy_until_us = t_start + round((self._window_clocks() + MIN_PERIOD_CLKS)
                                                  * 1e6 / CLK_HZ)
            heapq.heappush(self._heap, (self._busy_until_us, seq, t_start, flags))
            self._cv.notify()

    def _deliver(self) -> None:
        """Triggered mode: emit each frame once its readout would be finished."""
        while not self._stop.is_set():
            with self._cv:
                if not self._heap:
                    self._cv.wait(0.1)
                    continue
                done_at, seq, t_start, flags = self._heap[0]
                wait_s = (done_at - self._now_us()) / 1e6
                if wait_s > 0:
                    self._cv.wait(min(wait_s, 0.05))
                    continue
                heapq.heappop(self._heap)
            frame = self._make_frame(seq, t_start, self._restart_flag | flags)
            self._restart_flag = 0
            self._emit(frame)

    def stop_stream(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        for thread in (self._thread, self._worker):
            if thread is not None:
                thread.join(timeout=2)
        self._thread = self._worker = None
        self._streaming = False
        self._busy_until_us = 0
        self._waiting = []
        self._heap = []
