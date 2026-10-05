"""Binary packet format of the C12666MA firmware (see firmware/main.py).

Every packet is ``A5 5A | type u8 | length u16 | payload | crc32 u32``
(little endian, crc32 over type + length + payload).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

MAGIC = b"\xa5\x5a"
PKT_FRAME = 1
PKT_TEXT = 2
MAX_PAYLOAD = 4096

FRAME_HDR = struct.Struct("<IQIIIBBBBHH")   # 32 bytes

FLAG_LED_CHANGED = 1
FLAG_RESTART = 2
FLAG_LATE = 4
FLAG_ADC_ERROR = 8


@dataclass
class Frame:
    """One spectrum as sent by the device."""

    seq: int
    t_device_us: int          # device clock, start of integration
    integration_us: int
    integration_clocks: int
    clk_hz: int
    n_avg: int
    high_gain: bool
    led: bool
    flags: int
    adc_errors: int
    counts: np.ndarray        # float64, counts averaged over n_avg scans
    timestamp: float = float("nan")   # host epoch seconds, filled by Spectrometer
    extra: dict = field(default_factory=dict)

    @property
    def integration_s(self) -> float:
        return self.integration_clocks / self.clk_hz

    @property
    def settings_key(self) -> tuple:
        """Settings that change the dark level; a dark frame must match them."""
        return (self.integration_clocks, self.clk_hz, self.n_avg, self.high_gain)


def parse_frame(payload: bytes) -> Frame:
    (seq, t_us, integ_us, integ_clk, clk_hz, n_avg, gain, led, flags,
     n_pix, adc_err) = FRAME_HDR.unpack_from(payload, 0)
    sums = np.frombuffer(payload, dtype="<u2", count=n_pix, offset=FRAME_HDR.size)
    return Frame(seq=seq, t_device_us=t_us, integration_us=integ_us,
                 integration_clocks=integ_clk, clk_hz=clk_hz, n_avg=n_avg,
                 high_gain=bool(gain), led=bool(led), flags=flags,
                 adc_errors=adc_err, counts=sums.astype(np.float64) / max(n_avg, 1))


def build_packet(ptype: int, payload: bytes) -> bytes:
    """Encode a packet the same way the firmware does (used by the tests)."""
    body = struct.pack("<BH", ptype, len(payload)) + payload
    return MAGIC + body + struct.pack("<I", zlib.crc32(body))


def build_frame_payload(seq: int, sums, *, t_us: int = 0, integration_clocks: int = 2000,
                        clk_hz: int = 200_000, n_avg: int = 1, high_gain: int = 1,
                        led: int = 0, flags: int = 0, adc_errors: int = 0) -> bytes:
    sums = np.asarray(sums, dtype="<u2")
    integ_us = round(integration_clocks * 1e6 / clk_hz)
    return FRAME_HDR.pack(seq, t_us, integ_us, integration_clocks, clk_hz, n_avg,
                          high_gain, led, flags, len(sums), adc_errors) + sums.tobytes()


class PacketParser:
    """Incremental parser: feed it raw serial bytes, get (type, payload) back.

    Bytes outside valid packets are skipped, and a packet with a bad checksum
    is dropped, after which the parser resynchronises on the next magic word.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.crc_errors = 0
        self.bytes_skipped = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        buf = self._buf
        buf += data
        out = []
        while True:
            i = buf.find(MAGIC)
            if i < 0:
                keep = 1 if buf.endswith(MAGIC[:1]) else 0
                self.bytes_skipped += len(buf) - keep
                del buf[:len(buf) - keep]
                break
            if i:
                self.bytes_skipped += i
                del buf[:i]
            if len(buf) < 5:
                break
            ptype = buf[2]
            length = buf[3] | (buf[4] << 8)
            if ptype not in (PKT_FRAME, PKT_TEXT) or length > MAX_PAYLOAD:
                self.bytes_skipped += 1
                del buf[:1]
                continue
            total = 5 + length + 4
            if len(buf) < total:
                break
            crc = int.from_bytes(buf[5 + length:total], "little")
            if zlib.crc32(buf[2:5 + length]) != crc:
                self.crc_errors += 1
                self.bytes_skipped += 1
                del buf[:1]
                continue
            out.append((ptype, bytes(buf[5:5 + length])))
            del buf[:total]
        return out
