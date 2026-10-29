"""Serial driver for the C12666MA spectrometer firmware.

The port is opened once and kept open. In text mode every command is a
line and gets one line back. In streaming mode a background thread parses
the binary packets: frames go to a queue (or a callback), replies to
commands go to `send()`.

    with Spectrometer.open() as spec:
        spec.set_integration_us(20_000)
        frame = spec.read_frame()
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import serial
from serial.tools import list_ports

from .protocol import PKT_FRAME, PKT_TEXT, Frame, PacketParser, parse_frame

PICO_VID = 0x2E8A
HELLO_REPLY = "C12666MA"


class DeviceError(RuntimeError):
    pass


@dataclass
class DeviceInfo:
    port: str
    serial_number: str          # USB serial = the Pico's flash unique ID


def find_devices() -> list[DeviceInfo]:
    """Raspberry Pi Picos on USB (not opened, so ports in use are listed too)."""
    return [DeviceInfo(p.device, (p.serial_number or "").upper())
            for p in list_ports.comports() if p.vid == PICO_VID]


def find_ports(probe: bool = True) -> list[str]:
    """Serial ports with a Raspberry Pi Pico; with probe=True only those that
    answer `hello` like the spectrometer firmware."""
    ports = [p.device for p in list_ports.comports() if p.vid == PICO_VID]
    if not probe:
        return ports
    found = []
    for port in ports:
        try:
            with serial.Serial(port, 115200, timeout=0.5) as ser:
                ser.write(b"stop\n")
                time.sleep(0.15)
                ser.reset_input_buffer()
                ser.write(b"hello\n")
                if HELLO_REPLY in ser.readline().decode(errors="replace"):
                    found.append(port)
        except (OSError, serial.SerialException):
            pass
    return found


class Spectrometer:
    """Connection to one spectrometer."""

    def __init__(self, port: str, timeout: float = 3.0) -> None:
        self.port = port
        self.timeout = timeout
        self._ser = serial.Serial(port, 115200, timeout=0.05, write_timeout=2)
        self._write_lock = threading.Lock()
        self._cmd_lock = threading.RLock()     # one request/reply at a time
        self._streaming = False
        self._reader: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()
        self._replies: "queue.Queue[str]" = queue.Queue()
        self._on_frame: Optional[Callable[[Frame], None]] = None
        self.on_event: Optional[Callable[[dict], None]] = None
        self.frames: "queue.Queue[Frame]" = queue.Queue(maxsize=10_000)
        self.parser = PacketParser()
        self.events: list[str] = []
        self.frames_received = 0
        self.frames_dropped = 0        # gaps in the device sequence numbers
        self.host_overflows = 0        # frames lost because nobody read the queue
        self._last_seq: Optional[int] = None
        self._clock = None             # (perf_counter, epoch, device us, rate) reference
        self._sync_points: list[tuple[float, int]] = []
        self.clock_rtt_ms = float("nan")
        self.reader_error: Optional[BaseException] = None
        self._reset_link()
        try:
            if HELLO_REPLY not in self.query("hello"):
                raise DeviceError(f"{port} is not a C12666MA spectrometer")
            self.status = self.get_status()
        except Exception:
            self._ser.close()
            raise
        usb = {p.device: (p.serial_number or "").upper() for p in list_ports.comports()}
        self.serial_number = usb.get(port, "")
        # firmware v2 reports the flash unique ID itself; the USB serial is the same
        self.uid = str(self.status.get("uid") or self.serial_number or port)
        self.name = str(self.status.get("name", "C12666MA"))

    # ------------------------------------------------------------------ setup
    @classmethod
    def open(cls, port: Optional[str] = None) -> "Spectrometer":
        if port is None:
            ports = find_ports()
            if not ports:
                raise DeviceError("no C12666MA spectrometer found")
            port = ports[0]
        return cls(port)

    def __enter__(self) -> "Spectrometer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._streaming:
                self.stop_stream()
        finally:
            self._ser.close()

    def _reset_link(self) -> None:
        """Bring the device to text mode whatever it was doing."""
        self._write(b"stop\n")
        time.sleep(0.15)
        self._ser.reset_input_buffer()

    def _write(self, data: bytes) -> None:
        with self._write_lock:
            self._ser.write(data)

    # -------------------------------------------------------------- commands
    def query(self, cmd: str, timeout: Optional[float] = None) -> str:
        """Send one command and return the reply line (works in both modes)."""
        if self._streaming:
            return self.send(cmd, timeout)
        timeout = self.timeout if timeout is None else timeout
        with self._cmd_lock:
            self._write(cmd.encode() + b"\n")
            deadline = time.monotonic() + timeout
            buf = b""
            while time.monotonic() < deadline:
                buf += self._ser.readline()
                if buf.endswith(b"\n"):
                    return buf.decode(errors="replace").strip()
        raise DeviceError(f"no reply to {cmd!r}")

    def send(self, cmd: str, timeout: Optional[float] = None) -> str:
        """While streaming: send a command, wait for its reply packet."""
        timeout = self.timeout if timeout is None else timeout
        with self._cmd_lock:
            while not self._replies.empty():
                self._replies.get_nowait()
            self._write(cmd.encode() + b"\n")
            try:
                return self._replies.get(timeout=timeout)
            except queue.Empty:
                raise DeviceError(f"no reply to {cmd!r}") from None

    def send_nowait(self, cmd: str) -> None:
        """Send a command that has no reply (e.g. `trig`)."""
        self._write(cmd.encode() + b"\n")

    def query_json(self, cmd: str) -> dict:
        reply = self.query(cmd)
        try:
            data = json.loads(reply)
        except ValueError:
            raise DeviceError(f"unexpected reply to {cmd!r}: {reply!r}") from None
        if "error" in data:
            raise DeviceError(f"{cmd!r}: {data['error']}")
        return data

    def get_status(self) -> dict:
        p0, e0 = time.perf_counter(), time.time()
        status = self.query_json("status")
        p1, e1 = time.perf_counter(), time.time()
        if not self._sync_points:               # rough clock model until sync_clock()
            self._clock = ((p0 + p1) / 2, (e0 + e1) / 2, status["t_us"], 1.0)
        self.status = status
        return status

    # ----------------------------------------------------------------- clock
    def sync_clock(self, samples: int = 5) -> float:
        """Measure the device clock against the PC: several `time` pings, keep the
        one with the shortest round trip (its midpoint is the most accurate).
        Successive syncs also give the crystal's rate relative to the PC.
        Returns that round trip in ms (the offset is known to about half of it)."""
        best = None
        for _ in range(samples):
            p0, e0 = time.perf_counter(), time.time()
            t_us = self.query_json("time")["t_us"]
            p1, e1 = time.perf_counter(), time.time()
            if best is None or p1 - p0 < best[0]:
                best = (p1 - p0, (p0 + p1) / 2, (e0 + e1) / 2, t_us)
        rtt, perf_mid, epoch_mid, t_us = best
        self._sync_points.append((perf_mid, t_us))
        del self._sync_points[:-20]
        rate = 1.0
        p_first, t_first = self._sync_points[0]
        if perf_mid - p_first > 20:             # enough span to measure the drift
            rate = (t_us - t_first) / 1e6 / (perf_mid - p_first)
            rate = min(max(rate, 1 - 2e-4), 1 + 2e-4)
        self._clock = (perf_mid, epoch_mid, t_us, rate)
        self.clock_rtt_ms = rtt * 1000
        return self.clock_rtt_ms

    def device_time_at(self, perf_time: float) -> int:
        """Device clock (us) at PC time `perf_time` (time.perf_counter())."""
        perf_ref, _, t_ref, rate = self._clock
        return int(t_ref + (perf_time - perf_ref) * 1e6 * rate)

    def device_to_host_time(self, t_device_us: int) -> float:
        """PC wall-clock time (epoch seconds) of device clock value `t_device_us`."""
        _, epoch_ref, t_ref, rate = self._clock
        return epoch_ref + (t_device_us - t_ref) / 1e6 / rate

    def set_integration_us(self, us: int) -> int:
        return self.query_json(f"set_integration,{int(us)}")["integration_us"]

    def set_gain(self, high: bool) -> bool:
        return bool(self.query_json(f"set_gain,{1 if high else 0}")["high_gain"])

    def set_avg(self, n: int) -> int:
        return self.query_json(f"set_avg,{int(n)}")["n_avg"]

    def set_led(self, on: bool) -> bool:
        return bool(self.query_json(f"set_led,{1 if on else 0}")["led"])

    def set_period_us(self, us: int) -> dict:
        return self.query_json(f"set_period,{int(us)}")

    def set_clock_hz(self, hz: int) -> int:
        return self.query_json(f"set_clock,{int(hz)}")["clk_hz"]

    def set_name(self, name: str) -> str:
        self.name = self.query_json(f"set_name,{name.replace(',', ' ')}")["device_name"]
        return self.name

    def get_wl_coeffs(self) -> list[float]:
        return self.query_json("get_wl_coeffs")["wl_coeffs"]

    def set_wl_coeffs(self, coeffs) -> list[float]:
        coeffs = list(coeffs) + [0.0] * (6 - len(coeffs))
        for i, c in enumerate(coeffs[:6]):
            reply = self.query_json(f"set_wl_coeff,{i},{c:.10g}")
        return reply["wl_coeffs"]

    def save(self) -> None:
        self.query_json("save")

    # ---------------------------------------------------------- single frames
    def read_frame(self, timeout: Optional[float] = None) -> Frame:
        """Acquire one frame with the current settings (text mode only)."""
        if self._streaming:
            raise DeviceError("read_frame() is not available while streaming")
        status = self.get_status()
        if timeout is None:
            timeout = 2.0 + status["integration_us"] * (status["n_avg"] + 1) / 1e6
        self._write(b"frame\n")
        parser = PacketParser()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for ptype, payload in parser.feed(self._ser.read(max(1, self._ser.in_waiting))):
                if ptype == PKT_FRAME:
                    frame = parse_frame(payload)
                    frame.timestamp = self.device_to_host_time(frame.t_device_us)
                    return frame
        raise DeviceError("no frame received")

    # -------------------------------------------------------------- streaming
    @property
    def streaming(self) -> bool:
        return self._streaming

    def start_stream(self, period_us: int = 0,
                     on_frame: Optional[Callable[[Frame], None]] = None,
                     triggered: bool = False) -> dict:
        """Start acquisition: continuous / periodic, or with triggered=True one
        frame per `trigger()`. Frames go to `on_frame` (called from the reader
        thread) or, without a callback, to the `frames` queue."""
        if self._streaming:
            raise DeviceError("already streaming")
        self.get_status()
        if "min_trigger_period_us" in self.status:   # firmware >= 2.1 has `time`
            self.sync_clock()
        self._on_frame = on_frame
        while not self.frames.empty():          # frames of an earlier stream
            self.frames.get_nowait()
        self._last_seq = None
        self.reader_error = None
        self.parser = PacketParser()
        self._stop_reader.clear()
        self._streaming = True
        self._reader = threading.Thread(target=self._read_loop, name="c12666ma-reader",
                                        daemon=True)
        self._reader.start()
        try:
            reply = json.loads(self.send("stream,trig" if triggered
                                         else f"stream,{int(period_us)}"))
        except Exception:
            self.stop_stream()
            raise
        return reply

    def trigger(self, seq: int, delay_us: int = 0) -> None:
        """Triggered mode: start frame number `seq` after `delay_us` (device clock)."""
        self.send_nowait(f"trig,{int(seq)},{max(0, int(delay_us))}")

    def trigger_at(self, seq: int, device_time_us: int) -> None:
        """Triggered mode: start frame number `seq` when the device clock reaches
        `device_time_us` (see device_time_at)."""
        self.send_nowait(f"trig_at,{int(seq)},{int(device_time_us)}")

    def stop_stream(self) -> None:
        if not self._streaming:
            return
        try:
            self.send("stop", timeout=2.0)
        except DeviceError:
            pass
        self._stop_reader.set()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
        self._streaming = False
        self._reset_link()

    def _read_loop(self) -> None:
        ser = self._ser
        try:
            while not self._stop_reader.is_set():
                data = ser.read(max(1, ser.in_waiting))
                if not data:
                    continue
                for ptype, payload in self.parser.feed(data):
                    if ptype == PKT_FRAME:
                        self._handle_frame(parse_frame(payload))
                    elif ptype == PKT_TEXT:
                        text = payload.decode(errors="replace")
                        if text.startswith('{"event"'):
                            self.events.append(text)
                            if self.on_event is not None:
                                try:
                                    self.on_event(json.loads(text))
                                except ValueError:
                                    pass
                        else:
                            self._replies.put(text)
        except Exception as exc:              # serial unplugged etc.
            self.reader_error = exc

    def _handle_frame(self, frame: Frame) -> None:
        frame.timestamp = self.device_to_host_time(frame.t_device_us)
        if self._last_seq is not None and frame.seq > self._last_seq + 1:
            self.frames_dropped += frame.seq - self._last_seq - 1
        self._last_seq = frame.seq
        self.frames_received += 1
        if self._on_frame is not None:
            self._on_frame(frame)
        else:
            try:
                self.frames.put_nowait(frame)
            except queue.Full:
                self.host_overflows += 1
