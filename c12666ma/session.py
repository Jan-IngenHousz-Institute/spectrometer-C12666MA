"""Acquisition with one or more spectrometers.

With one spectrometer and "as fast as possible" the device streams
continuously (no dead time). Otherwise the PC triggers all devices together:
for each tick k a scheduler thread sends `trig_at,<k>,<t>` to every device
up to SEND_AHEAD_S in advance, with <t> the tick time in that device's own clock
(plus a delay that centres shorter integration windows on the tick). Frames
of the same tick therefore share sequence number k in every file, and start
together to about a millisecond however busy the PC is. The minimum period is
set by the slowest device (largest averages x integration time).

A maintenance thread re-measures each device's clock every 30 s (offset and
crystal drift),
reconnects devices that were unplugged or reset, keeps the PC awake while
recording, ends timed recordings and stops cleanly when the disk is full.
"""

from __future__ import annotations

import collections
import json
import re
import shutil
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__, simulator
from .analysis import YieldResult, compute_paired_yield
from .calibration import WavelengthCalibration
from .device import DeviceError, Spectrometer, find_devices
from .power import keep_awake
from .protocol import Frame
from .processing import AnalysisSettings, DeviceProcessor
from .recorder import PairedRecorder, RotatingRecorder

HOST_MARGIN_US = 5_000          # added to the slowest device's frame time
SEND_AHEAD_S = 0.5              # triggers are sent up to this long before their start
                                # time, in each device's own clock, so PC stalls (GUI
                                # redraws, Windows timers, virus scans) don't matter;
SEND_AHEAD_PERIODS = 12         # but at most this many periods ahead (device queue: 16)
CLOCK_SYNC_S = 30.0
RECONNECT_S = 3.0
FLUSH_S = 1.0
MIN_FREE_BYTES = 500_000_000


@dataclass
class DeviceSettings:
    high_gain: bool
    integration_us: int
    n_avg: int
    led: bool


class DeviceHandle:
    """One spectrometer in a session (survives reconnects)."""

    def __init__(self, session: "AcquisitionSession", spec, index: int) -> None:
        self.session = session
        self.index = index                      # stable colour index
        self.missed = 0                         # ticks without a frame
        self.busy = 0                           # triggers refused (frame still running)
        self.reconnects = 0
        self.frames = 0
        self.last_seq: Optional[int] = None
        self.lost_since: Optional[float] = None
        self._attach(spec)
        status = spec.status
        self.calibration, push = WavelengthCalibration.for_device(self.uid, status.get("wl_coeffs"))
        if push and not self.calibration.same_as(status.get("wl_coeffs")):
            spec.set_wl_coeffs(self.calibration.coefficients)
            spec.save()
            self.calibration.source += ", copied to the device"
        self.processor = DeviceProcessor(self.calibration.wavelengths, session.analysis,
                                         sink=self._sink)
        self.settings = DeviceSettings(bool(status["high_gain"]), int(status["integration_us"]),
                                       int(status["n_avg"]), bool(status["led"]))

    def _attach(self, spec) -> None:
        self.spec = spec
        self.uid = spec.uid
        self.port = spec.port
        self.name = spec.name
        self.status = spec.status
        self.connected = True
        spec.on_event = self._on_event

    # ------------------------------------------------------------------ info
    @property
    def window_us(self) -> int:
        """Integration window of one frame: averages x integration time."""
        return int(self.status["n_avg"]) * int(self.status["integration_us"])

    @property
    def min_trigger_period_us(self) -> int:
        return int(self.status.get("min_trigger_period_us",
                                   self.window_us + 7_200))

    def refresh(self) -> dict:
        self.status = self.spec.get_status()
        self.name = str(self.status.get("name", self.name))
        return self.status

    # ------------------------------------------------------------- callbacks
    def _sink(self, frame, result) -> None:
        if self.last_seq is not None and frame.seq > self.last_seq + 1:
            self.missed += frame.seq - self.last_seq - 1
        self.last_seq = frame.seq
        self.frames += 1
        self.session.results.append((self, frame, result))
        self.session._pair_frame(self, frame)

    def _on_event(self, event: dict) -> None:
        if event.get("event") == "busy":
            self.busy += 1
        else:
            self.session.log(f"device event {event.get('event')}", self)


class SessionRecording:
    """One folder with per-device spectra and an optional paired-measurements CSV."""

    def __init__(self, session: "AcquisitionSession", root: Path, prefix: str, note: str,
                 include_yield: bool, duration_s: float, part_hours: float) -> None:
        self.session = session
        self.t0 = time.time()
        stamp = datetime.fromtimestamp(self.t0).strftime("%Y-%m-%d_%H-%M-%S")
        self.folder = Path(root).expanduser() / f"{_safe(prefix) or 'C12666MA'}_{stamp}"
        self.folder.mkdir(parents=True, exist_ok=False)
        self.note = note
        self.include_yield = include_yield
        self.duration_s = duration_s
        self.part_hours = part_hours
        self.stopped_at: Optional[float] = None
        self.stop_reason = ""
        self.recorders: dict[str, RotatingRecorder] = {}
        self.measurement_roles = {
            "incoming_light_uid": session.incoming_uid,
            "fluorescence_uid": session.fluorescence_uid,
        }
        self._measurement_lock = threading.Lock()
        self.paired_recorder: Optional[PairedRecorder] = None
        if include_yield:
            incident = session._handle(session.incoming_uid)
            fluorescence = session._handle(session.fluorescence_uid)
            self.paired_recorder = PairedRecorder(
                self.folder / "measurements.csv",
                f"{incident.name} ({incident.uid})",
                f"{fluorescence.name} ({fluorescence.uid})",
                (session.analysis.incident.lo_nm, session.analysis.incident.hi_nm),
                (session.analysis.fluorescence.lo_nm, session.analysis.fluorescence.hi_nm),
                session.analysis.k, self.t0)
        self._events = open(self.folder / "events.log", "a", encoding="utf-8", buffering=1)
        self.write_manifest()

    @property
    def elapsed_s(self) -> float:
        return (self.stopped_at or time.time()) - self.t0

    @property
    def n_spectra(self) -> int:
        return sum(r.n_spectra for r in self.recorders.values())

    def add_device(self, h: DeviceHandle) -> None:
        if h.uid in self.recorders:
            return
        s = self.session
        a = s.analysis
        meta = {
            "started": datetime.fromtimestamp(self.t0).astimezone().isoformat(timespec="seconds"),
            "software": f"c12666ma {__version__}",
            "device": f"{h.status['fw']} '{h.name}' uid {h.uid} on {h.port}",
            "sensor_clock_hz": h.status["clk_hz"],
            "session": f"{len(s.connected)} device(s), mode {s.mode}, period "
                       f"{s.effective_period_us() / 1000:.1f} ms, windows centred",
            "wavelength_calibration": f"{h.calibration.source}, nm = sum(c[i] * pixel**i), "
                                      f"c = {h.calibration.coefficients}",
            "incident_light_nm": f"{a.incident.lo_nm:.1f}-{a.incident.hi_nm:.1f}",
            "fluorescence_nm": f"{a.fluorescence.lo_nm:.1f}-{a.fluorescence.hi_nm:.1f}",
            "paired_measurement": (
                "relative fluorescence from synchronized frames of the assigned devices"
                if self.include_yield else "disabled for this recording"),
            "saturation_counts": f"high gain {a.sat_high:g}, low gain {a.sat_low:g}",
            "seq": "trigger number: spectra with the same seq were taken together"
                   if s.mode == "triggered" else "frame number",
            "note": self.note.replace("\n", " "),
        }
        rec = RotatingRecorder(self.folder, f"{_safe(h.name)}_{h.uid[-6:]}",
                               h.processor.wavelengths, meta, False,
                               self.part_hours, t0_epoch=self.t0)
        if h.processor.dark is not None:
            rec.write_dark(h.processor.dark)
        with h.processor.lock:
            h.processor.recorder = rec
        self.recorders[h.uid] = rec

    def write_measurement(self, incident_handle: "DeviceHandle", incident_frame,
                          fluorescence_handle: "DeviceHandle", fluorescence_frame,
                          result: YieldResult) -> None:
        with self._measurement_lock:
            if self.paired_recorder is not None and self.stopped_at is None:
                self.paired_recorder.write(
                    incident_frame.seq, incident_frame.timestamp, fluorescence_frame.timestamp,
                    incident_handle.name, fluorescence_handle.name, result)

    def log(self, line: str) -> None:
        if not self._events.closed:
            self._events.write(line + "\n")

    def flush(self) -> None:
        for h in self.session.handles:
            rec = self.recorders.get(h.uid)
            if rec is not None:
                with h.processor.lock:
                    rec.flush()
        if self.paired_recorder is not None:
            self.paired_recorder.flush()

    def write_manifest(self) -> None:
        s = self.session
        a = s.analysis
        manifest = {
            "started": datetime.fromtimestamp(self.t0).astimezone().isoformat(timespec="seconds"),
            "stopped": (datetime.fromtimestamp(self.stopped_at).astimezone().isoformat(
                timespec="seconds") if self.stopped_at else None),
            "stop_reason": self.stop_reason or None,
            "software": f"c12666ma {__version__}",
            "mode": s.mode,
            "period_us_requested": s.period_us,
            "period_us_effective": s.effective_period_us(),
            "alignment": "integration windows centred on each tick",
            "duration_s": self.duration_s or None,
            "part_hours": self.part_hours,
            "note": self.note,
            "analysis": {"incident_light_nm": [a.incident.lo_nm, a.incident.hi_nm],
                         "fluorescence_nm": [a.fluorescence.lo_nm, a.fluorescence.hi_nm],
                         "k": a.k, "saturation_high_gain": a.sat_high,
                         "saturation_low_gain": a.sat_low},
            "measurement_roles": self.measurement_roles,
            "measurements_file": ("measurements.csv" if self.paired_recorder is not None else None),
            "ticks_sent": s.ticks_sent,
            "devices": [{
                "uid": h.uid, "name": h.name, "port": h.port, "firmware": h.status.get("fw"),
                "settings": asdict(h.settings),
                "delay_us": s.delays_us().get(h.uid, 0),
                "calibration": {"source": h.calibration.source,
                                "coefficients": h.calibration.coefficients},
                "files": [p.name for p in self.recorders[h.uid].paths] if h.uid in self.recorders else [],
                "spectra": self.recorders[h.uid].n_spectra if h.uid in self.recorders else 0,
                "missed_ticks": h.missed, "busy": h.busy, "reconnects": h.reconnects,
            } for h in s.handles],
        }
        (self.folder / "session.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def close(self, reason: str = "") -> None:
        self.stopped_at = time.time()
        self.stop_reason = reason
        for h in self.session.handles:
            rec = self.recorders.get(h.uid)
            if rec is not None:
                with h.processor.lock:
                    if h.processor.recorder is rec:
                        h.processor.recorder = None
                    rec.close()
        with self._measurement_lock:
            if self.paired_recorder is not None:
                self.paired_recorder.close()
        self.write_manifest()
        self._events.close()


class AcquisitionSession:
    def __init__(self, simulate: bool = False) -> None:
        self.lock = threading.RLock()
        self.handles: list[DeviceHandle] = []
        self.analysis = AnalysisSettings()
        self.results: "collections.deque" = collections.deque(maxlen=100_000)
        self.paired_results: "collections.deque" = collections.deque(maxlen=100_000)
        self.incoming_uid: Optional[str] = None
        self.fluorescence_uid: Optional[str] = None
        self._pair_lock = threading.Lock()
        self._incoming_frames: dict[int, tuple[DeviceHandle, Frame]] = {}
        self._fluorescence_frames: dict[int, tuple[DeviceHandle, Frame]] = {}
        self.events: "collections.deque[tuple[float, str]]" = collections.deque(maxlen=5_000)
        self.period_us = 0                       # requested; 0 = as fast as possible
        self.running = False
        self.mode = "idle"                       # idle / continuous / triggered
        self.recording: Optional[SessionRecording] = None
        self.last_recording: Optional[SessionRecording] = None
        self.simulate = simulate
        self.ticks_sent = 0
        self.ticks_skipped = 0
        self.late_triggers = 0          # triggers sent after their start time
        self.min_lead_us: Optional[int] = None   # least time a trigger was sent ahead
        self.send_ahead_s = SEND_AHEAD_S
        if sys.getswitchinterval() > 0.001:
            sys.setswitchinterval(0.001)    # threads waiting for the GIL (scheduler,
                                            # serial readers) get it back sooner
        self._led_restore: set[str] = set()
        self._next_index = 0
        self._timing: tuple[float, dict] = (1.0, {})
        self._sched_stop = threading.Event()
        self._sched: Optional[threading.Thread] = None
        self._closing = threading.Event()
        self._maint = threading.Thread(target=self._maintenance, name="c12666ma-maintenance",
                                       daemon=True)
        self._maint.start()

    # ------------------------------------------------------------------ log
    def log(self, text: str, h: Optional[DeviceHandle] = None) -> None:
        line = f"{datetime.now().astimezone().isoformat(timespec='seconds')}  " \
               f"{('[' + h.name + '] ') if h else ''}{text}"
        self.events.append((time.time(), line))
        rec = self.recording
        if rec is not None:
            rec.log(line)

    def _handle(self, uid: Optional[str]) -> Optional[DeviceHandle]:
        return next((h for h in self.handles if h.uid == uid), None)

    def set_measurement_roles(self, incoming_uid: Optional[str],
                              fluorescence_uid: Optional[str]) -> None:
        if incoming_uid == fluorescence_uid and incoming_uid is not None:
            raise ValueError("incoming-light and fluorescence devices must be different")
        known = {h.uid for h in self.handles}
        if incoming_uid is not None and incoming_uid not in known:
            raise ValueError(f"unknown incoming-light device: {incoming_uid}")
        if fluorescence_uid is not None and fluorescence_uid not in known:
            raise ValueError(f"unknown fluorescence device: {fluorescence_uid}")
        with self._pair_lock:
            self.incoming_uid = incoming_uid
            self.fluorescence_uid = fluorescence_uid
            self._incoming_frames.clear()
            self._fluorescence_frames.clear()

    def _pair_frame(self, h: DeviceHandle, frame: Frame) -> None:
        incident_pair = None
        with self._pair_lock:
            incoming_uid, fluorescence_uid = self.incoming_uid, self.fluorescence_uid
            if incoming_uid is None or fluorescence_uid is None:
                return
            if h.uid == incoming_uid:
                own, other = self._incoming_frames, self._fluorescence_frames
            elif h.uid == fluorescence_uid:
                own, other = self._fluorescence_frames, self._incoming_frames
            else:
                return
            own[frame.seq] = (h, frame)
            counterpart = other.pop(frame.seq, None)
            if counterpart is not None:
                own.pop(frame.seq, None)
                incident_pair = ((h, frame), counterpart) if h.uid == incoming_uid \
                    else (counterpart, (h, frame))
            for pending in (own, other):
                while len(pending) > 128:
                    del pending[min(pending)]

        if incident_pair is None:
            return
        (incident_handle, incident_frame), (fluorescence_handle, fluorescence_frame) = incident_pair
        result = compute_paired_yield(
            incident_frame, incident_handle.processor.dark,
            incident_handle.processor.wavelengths,
            fluorescence_frame, fluorescence_handle.processor.dark,
            fluorescence_handle.processor.wavelengths,
            self.analysis.incident, self.analysis.fluorescence, self.analysis.k,
            self.analysis.sat_high, self.analysis.sat_low)
        paired = (incident_handle, incident_frame, fluorescence_handle,
                  fluorescence_frame, result)
        self.paired_results.append(paired)
        recording = self.recording
        if recording is not None:
            recording.write_measurement(*paired)

    # -------------------------------------------------------------- devices
    @property
    def connected(self) -> list[DeviceHandle]:
        return [h for h in self.handles if h.connected]

    def detect(self) -> list:
        """Devices on USB (and simulated ones) not yet in the session."""
        infos = find_devices() + (simulator.available() if self.simulate else [])
        known = {h.uid for h in self.handles} | {h.port for h in self.connected}
        return [i for i in infos if i.serial_number not in known and i.port not in known]

    def _open(self, port: str):
        return simulator.SimulatedSpectrometer(port) if simulator.is_simulated(port) \
            else Spectrometer(port)

    def connect(self, port: str) -> DeviceHandle:
        spec = self._open(port)
        with self.lock:
            if any(h.uid == spec.uid for h in self.handles):
                spec.close()
                raise DeviceError(f"{port}: this spectrometer ({spec.uid}) is already connected")
            h = DeviceHandle(self, spec, self._next_index)
            self._next_index += 1
            self.handles.append(h)
            if len(self.handles) >= 2 and self.incoming_uid is None \
                    and self.fluorescence_uid is None:
                self.set_measurement_roles(self.handles[0].uid, self.handles[1].uid)
            self.log(f"connected on {port} (uid {h.uid}, wavelengths: {h.calibration.source})", h)
            if self.running:
                self._restart_acquisition()
            if self.recording is not None:
                self.recording.add_device(h)
        return h

    def connect_all(self) -> list[str]:
        """Connect every detected spectrometer; returns error messages."""
        errors = []
        for info in self.detect():
            try:
                self.connect(info.port)
            except (DeviceError, OSError) as exc:
                errors.append(f"{info.port}: {exc}")
        return errors

    def disconnect(self, h: DeviceHandle) -> None:
        with self.lock:
            if h.connected:
                try:
                    h.spec.close()
                except Exception:
                    pass
            h.connected = False
            if self.recording is not None and h.uid in self.recording.recorders:
                with h.processor.lock:
                    h.processor.recorder = None
                self.recording.recorders[h.uid].close()
            self.handles.remove(h)
            if h.uid in (self.incoming_uid, self.fluorescence_uid):
                incoming = None if h.uid == self.incoming_uid else self.incoming_uid
                fluorescence = None if h.uid == self.fluorescence_uid else self.fluorescence_uid
                self.set_measurement_roles(incoming, fluorescence)
            self.log("disconnected", h)
            if self.running:
                if self.connected:
                    self._restart_acquisition()
                else:
                    self.stop()

    def apply(self, h: DeviceHandle, *, high_gain=None, integration_us=None, n_avg=None,
              led=None, name=None) -> None:
        """Change one device's settings (also while running)."""
        spec = h.spec
        if high_gain is not None:
            h.settings.high_gain = spec.set_gain(high_gain)
        if integration_us is not None:
            h.settings.integration_us = spec.set_integration_us(integration_us)
        if n_avg is not None:
            h.settings.n_avg = spec.set_avg(n_avg)
        if led is not None:
            h.settings.led = spec.set_led(led)
        if name is not None:
            spec.set_name(name)
            spec.save()
        h.refresh()
        changed = {k: v for k, v in dict(high_gain=high_gain, integration_us=integration_us,
                                         n_avg=n_avg, led=led, name=name).items() if v is not None}
        self.log(f"settings {changed}", h)
        with self.lock:
            if self.running:
                if (self.mode == "continuous") != self._continuous_eligible():
                    self._restart_acquisition()
                else:
                    self._update_timing()

    def set_analysis(self, analysis: AnalysisSettings) -> None:
        self.analysis = analysis
        for h in self.handles:
            h.processor.analysis = analysis

    # --------------------------------------------------------------- timing
    def _continuous_eligible(self) -> bool:
        return len(self.connected) == 1 and self.period_us == 0

    def min_period_us(self) -> int:
        hs = self.connected
        if not hs:
            return 0
        if self._continuous_eligible():
            return hs[0].window_us
        return max(h.min_trigger_period_us for h in hs) + HOST_MARGIN_US

    def effective_period_us(self) -> int:
        return max(self.period_us, self.min_period_us())

    def delays_us(self) -> dict[str, int]:
        """Trigger delay per device so that all integration windows are centred."""
        hs = self.connected
        if not hs:
            return {}
        w_max = max(h.window_us for h in hs)
        return {h.uid: (w_max - h.window_us) // 2 for h in hs}

    def set_period_us(self, period_us: int) -> None:
        with self.lock:
            self.period_us = max(0, int(period_us))
            if self.running:
                if (self.mode == "continuous") != self._continuous_eligible():
                    self._restart_acquisition()
                else:
                    self._update_timing()

    def _update_timing(self) -> None:
        self._timing = (self.effective_period_us() / 1e6, self.delays_us())

    # ---------------------------------------------------------- acquisition
    def start(self) -> None:
        with self.lock:
            if self.running:
                return
            hs = self.connected
            if not hs:
                raise DeviceError("no spectrometer connected")
            with self._pair_lock:
                self._incoming_frames.clear()
                self._fluorescence_frames.clear()
            if self._continuous_eligible():
                hs[0].spec.start_stream(0, on_frame=hs[0].processor.on_frame)
                self.mode = "continuous"
            else:
                old = [h.name for h in hs if "min_trigger_period_us" not in h.status]
                if old:
                    raise DeviceError(f"{', '.join(old)}: firmware too old for synchronised "
                                      "acquisition; copy firmware/main.py to the Pico")
                for h in hs:
                    h.last_seq = None
                    h.spec.start_stream(on_frame=h.processor.on_frame, triggered=True)
                self.mode = "triggered"
                self._update_timing()
                self._sched_stop.clear()
                self._sched = threading.Thread(target=self._scheduler, name="c12666ma-ticks",
                                               daemon=True)
                self._sched.start()
            self.running = True
            self.log(f"acquisition started: {self.mode}, period "
                     f"{self.effective_period_us() / 1000:.1f} ms")

    def stop(self) -> None:
        with self.lock:
            if not self.running:
                return
            self._sched_stop.set()
            if self._sched is not None:
                self._sched.join(timeout=2)
                self._sched = None
            for h in self.connected:
                h.processor.cancel_dark()
                try:
                    h.spec.stop_stream()
                except Exception:
                    pass
            self.running = False
            self.mode = "idle"
            self.log("acquisition stopped")

    def _restart_acquisition(self) -> None:
        recording = self.recording
        self.stop()
        self.start()
        if recording is not None:
            recording.log(f"acquisition restarted ({self.mode})")

    def _scheduler(self) -> None:
        """Send the trigger for tick k send_ahead_s before it, with the start time
        converted to each device's clock (plus that device's centring delay). The
        devices queue it and start on their own crystal, so how precisely this
        thread wakes up does not matter as long as it is within send_ahead_s."""
        k = self.ticks_sent
        self.send_ahead_s = min(SEND_AHEAD_S, SEND_AHEAD_PERIODS * self._timing[0])
        t_tick = time.perf_counter() + self.send_ahead_s + 0.05
        self.min_lead_us = None
        while not self._sched_stop.is_set():
            period_s, delays = self._timing
            self.send_ahead_s = min(SEND_AHEAD_S, SEND_AHEAD_PERIODS * period_s)
            send_at = t_tick - self.send_ahead_s
            while not self._sched_stop.is_set():
                remaining = send_at - time.perf_counter()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.05))
            if self._sched_stop.is_set():
                break
            lead_us = int((t_tick - time.perf_counter()) * 1e6)
            self.min_lead_us = lead_us if self.min_lead_us is None else min(self.min_lead_us, lead_us)
            if lead_us < 0:
                self.late_triggers += 1            # sent after its start time
            for h in self.connected:
                try:
                    h.spec.trigger_at(k, h.spec.device_time_at(t_tick + delays.get(h.uid, 0) / 1e6))
                except Exception as exc:          # unplugged: maintenance reconnects
                    h.spec.reader_error = h.spec.reader_error or exc
            k += 1
            self.ticks_sent = k
            t_tick += period_s
            behind = time.perf_counter() - t_tick
            if behind > period_s:                 # past the next tick itself: PC suspended?
                skip = int(behind / period_s) + 1
                k += skip
                t_tick += skip * period_s
                self.ticks_skipped += skip
                self.log(f"PC too slow or suspended: skipped {skip} ticks")

    # ------------------------------------------------------------------ dark
    def take_dark(self, n_frames: int, led_off: bool) -> None:
        for h in self.connected:
            if led_off and h.settings.led:
                self.apply(h, led=False)
                self._led_restore.add(h.uid)
            h.processor.start_dark(n_frames, led_off)
        if not self.running:
            self.start()

    # -------------------------------------------------------------- record
    def start_recording(self, root: Path, prefix: str = "C12666MA", note: str = "",
                        include_yield: bool = True, duration_s: float = 0,
                        part_hours: float = 24.0) -> SessionRecording:
        with self.lock:
            if self.recording is not None:
                raise DeviceError("already recording")
            if include_yield and (
                    self._handle(self.incoming_uid) not in self.connected
                    or self._handle(self.fluorescence_uid) not in self.connected
                    or self.incoming_uid == self.fluorescence_uid):
                raise DeviceError("assign two connected spectrometers to incoming light and "
                                  "fluorescence before saving relative fluorescence")
            if not self.running:
                self.start()
            rec = SessionRecording(self, root, prefix, note, include_yield, duration_s, part_hours)
            for h in self.handles:
                rec.add_device(h)
            self.recording = rec
            self.log(f"recording started in {rec.folder}"
                     + (f" for {duration_s / 3600:.2f} h" if duration_s else ""))
            return rec

    def stop_recording(self, reason: str = "stopped by user") -> Optional[SessionRecording]:
        with self.lock:
            rec = self.recording
            if rec is None:
                return None
            self.log(f"recording stopped: {reason}")
            self.recording = None
            rec.close(reason)
            self.last_recording = rec
            return rec

    # ---------------------------------------------------------- maintenance
    def close(self) -> None:
        self.stop_recording("program closed")
        self.stop()
        self._closing.set()
        self._maint.join(timeout=3)
        for h in list(self.handles):
            if h.connected:
                try:
                    h.spec.close()
                except Exception:
                    pass

    def _maintenance(self) -> None:
        awake = False
        last_sync = last_reconnect = last_flush = time.monotonic()
        while not self._closing.wait(0.5):
            now = time.monotonic()
            recording = self.recording
            if (recording is not None) != awake:          # keep the PC awake while recording
                awake = recording is not None
                keep_awake(awake)
            for h in list(self.handles):
                if h.connected and h.spec.reader_error is not None:
                    self._mark_lost(h)
            if now - last_reconnect >= RECONNECT_S:
                last_reconnect = now
                for h in [h for h in self.handles if not h.connected]:
                    self._try_reconnect(h)
            if now - last_sync >= CLOCK_SYNC_S:
                last_sync = now
                for h in self.connected:
                    try:
                        if self.running and "min_trigger_period_us" in h.status:
                            h.spec.sync_clock()           # offset and drift of its crystal
                        else:
                            h.refresh()
                    except Exception:
                        pass
            for h in list(self.connected):
                if h.uid in self._led_restore and h.processor.dark_finished.is_set():
                    self._led_restore.discard(h.uid)
                    try:
                        self.apply(h, led=True)
                    except Exception:
                        pass
            if recording is not None:
                if now - last_flush >= FLUSH_S:
                    last_flush = now
                    recording.flush()
                if recording.duration_s and recording.elapsed_s >= recording.duration_s:
                    self.stop_recording("duration reached")
                else:
                    try:
                        free = shutil.disk_usage(recording.folder).free
                    except OSError:
                        free = MIN_FREE_BYTES
                    if free < MIN_FREE_BYTES:
                        self.stop_recording(f"disk almost full ({free / 1e6:.0f} MB left)")
        keep_awake(False)

    def _mark_lost(self, h: DeviceHandle) -> None:
        err = h.spec.reader_error
        h.connected = False
        h.lost_since = time.time()
        try:
            h.spec.close()
        except Exception:
            pass
        self.log(f"connection lost ({err}); will reconnect", h)
        with self.lock:
            if self.running and self.mode == "triggered":
                self._update_timing()

    def _try_reconnect(self, h: DeviceHandle) -> None:
        infos = find_devices() + (simulator.available() if self.simulate else [])
        in_use = {c.port for c in self.connected}
        # the USB serial number is the unit's uid; if no port reports it, probe the free ones
        candidates = [i for i in infos if i.serial_number == h.uid.upper()] or \
                     [i for i in infos if i.port not in in_use]
        for info in candidates:
            try:
                spec = self._open(info.port)
            except (DeviceError, OSError):
                continue
            if spec.uid != h.uid:
                spec.close()
                continue
            try:
                s = h.settings
                spec.set_gain(s.high_gain)
                spec.set_integration_us(s.integration_us)
                spec.set_avg(s.n_avg)
                spec.set_led(s.led)
                spec.get_status()
                with self.lock:
                    # stream first, then mark connected: the scheduler must not
                    # trigger a device that is still in text mode
                    if self.running:
                        if self.mode == "triggered":
                            spec.start_stream(on_frame=h.processor.on_frame, triggered=True)
                        else:
                            spec.start_stream(0, on_frame=h.processor.on_frame)
                    h._attach(spec)
                    h.reconnects += 1
                    gone = time.time() - (h.lost_since or time.time())
                    h.lost_since = None
                    if self.running and self.mode == "triggered":
                        self._update_timing()
                self.log(f"reconnected on {info.port} after {gone:.0f} s", h)
            except Exception as exc:
                self.log(f"reconnect failed: {exc}", h)
                try:
                    spec.close()
                except Exception:
                    pass
            return


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())[:40]
