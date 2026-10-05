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
from typing import Callable, Optional

import serial
from serial.tools import list_ports

from .protocol import PKT_FRAME, PKT_TEXT, Frame, PacketParser, parse_frame

PICO_VID = 0x2E8A
HELLO_REPLY = "C12666MA"


class DeviceError(RuntimeError):
    pass


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
        self._streaming = False
        self._reader: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()
        self._replies: "queue.Queue[str]" = queue.Queue()
        self._on_frame: Optional[Callable[[Frame], None]] = None
        self.frames: "queue.Queue[Frame]" = queue.Queue(maxsize=10_000)
        self.parser = PacketParser()
        self.events: list[str] = []
        self.frames_received = 0
        self.frames_dropped = 0        # gaps in the device sequence numbers
        self.host_overflows = 0        # frames lost because nobody read the queue
        self._last_seq: Optional[int] = None
        self._clock_offset = 0.0       # host_time - device_time
        self.reader_error: Optional[BaseException] = None
        self._reset_link()
        if HELLO_REPLY not in self.query("hello"):
            self.close()
            raise DeviceError(f"{port} is not a C12666MA spectrometer")
        self.status = self.get_status()

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
        while not self._replies.empty():
            self._replies.get_nowait()
        self._write(cmd.encode() + b"\n")
        try:
            return self._replies.get(timeout=timeout)
        except queue.Empty:
            raise DeviceError(f"no reply to {cmd!r}") from None

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
        t0 = time.time()
        status = self.query_json("status")
        t1 = time.time()
        self._clock_offset = (t0 + t1) / 2 - status["t_us"] / 1e6
        self.status = status
        return status

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

    def get_wl_coeffs(self) -> list[float]:
        return self.query_json("get_wl_coeffs")["wl_coeffs"]

    def set_wl_coeffs(self, coeffs) -> list[float]:
        coeffs = list(coeffs) + [0.0] * (6 - len(coeffs))
        for i, c in enumerate(coeffs[:6]):
            reply = self.query_json(f"set_wl_coeff,{i},{c:.10g}")
        return reply["wl_coeffs"]

    def save(self) -> None:
        self.query_json("save")

    def device_to_host_time(self, t_device_us: int) -> float:
        return self._clock_offset + t_device_us / 1e6

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
                     on_frame: Optional[Callable[[Frame], None]] = None) -> dict:
        """Start continuous acquisition. Frames go to `on_frame` (called from
        the reader thread) or, without a callback, to the `frames` queue."""
        if self._streaming:
            raise DeviceError("already streaming")
        self.get_status()                       # refresh the clock offset
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
            reply = json.loads(self.send(f"stream,{int(period_us)}"))
        except Exception:
            self.stop_stream()
            raise
        return reply

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
