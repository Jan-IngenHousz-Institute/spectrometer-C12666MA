# =============================================================================
#  main.py  -  Hamamatsu C12666MA spectrometer firmware
#  MicroPython for the Raspberry Pi Pico (RP2040) on the JII "C12666MA Board v2"
#  (C12666MA + MCP3301 13-bit ADC + MCP1541 4.096 V reference).
# =============================================================================
#
#  Install: copy this file to the Pico as  main.py  (Thonny: File > Save as >
#  Raspberry Pi Pico, or `mpremote cp firmware/main.py :main.py`). It starts on
#  power-up. Ctrl-C in Thonny still stops it and gives you the REPL.
#
#  --- WIRING (GPIO numbers) -----------------------------------------------------
#     GP2  sensor CLK      (via 74AHCT125 to 5 V)
#     GP3  sensor ST       (via 74AHCT125 to 5 V)
#     GP4  sensor Gain     (via 74AHCT125, 0 = high gain, 1 = low gain)
#     GP15 LED / excitation light
#     GP16 MCP3301 DOUT    (NOTE: 5 V logic straight into the Pico on board v2)
#     GP17 MCP3301 CS
#     GP18 MCP3301 CLK
#
#  --- HOW THE READOUT WORKS ------------------------------------------------------
#  All sensor timing is generated in hardware, so it does not depend on the
#  Python interpreter, garbage collection or USB interrupts:
#    PIO SM0  drives CLK and ST. One "command" word = one start-pulse period:
#             ST pulse, 1024 readout clocks (256 pixels x 4), extra clocks.
#    PIO SM1  runs one MCP3301 conversion per pixel. SM0 triggers it at the
#             datasheet "TRIG" point (the low phase just before the pixel's
#             video output ends).
#    DMA      copies every conversion into a ring buffer in RAM.
#  The CPU only queues commands, sums scans and sends them over USB.
#
#  From the datasheet (KACC1216E): the integration time IS the interval
#  between start pulses, every start pulse also starts the readout of the
#  previous integration, the start pulse must stay high >= 1030 clocks, CLK
#  duty must be 45-55 % and only one CLK falling edge may occur while ST is
#  low. The clock is crystal-derived, so the integration time is exact
#  (N clocks), identical for every pixel, and continuous streaming has no
#  dead time: at the minimum integration time (~5.2 ms at 200 kHz) it
#  delivers ~190 spectra/s.
#
#  --- SERIAL PROTOCOL ---------------------------------------------------------
#  Commands are text lines ending in "\n" or "\r". Replies are one text line
#  ending in "\n". The old (v1) command names still work.
#    hello                     -> C12666MA,v1.0      (device discovery)
#    idn                       -> C12666MA_PICO_PY_v2.0
#    status                    -> JSON with all settings and counters
#    spec / spec,raw           -> one spectrum as 256 comma-separated numbers
#                                 (spec subtracts the on-device dark frame)
#    frame                     -> one spectrum as a binary FRAME packet
#    set_integration,<us>      -> {"integration_us":<actual>}
#    set_gain,<0|1>            -> {"high_gain":<0|1>}  (1 = high gain)
#    set_avg,<n>               -> {"n_avg":<n>}        (1..16 scans summed)
#    set_led,<0|1>             -> {"led":<0|1>}
#    set_period,<us>           -> {"period_us":<n>}    (streaming; 0 = fastest)
#    set_clock,<hz>            -> {"clk_hz":<actual>}  (sensor clock)
#    get_integration / get_gain / get_avg / get_led / get_period / get_clock
#    dark / clear_dark         -> on-device dark frame used by `spec`
#    set_wl_coeff,<i>,<value>  / get_wl_coeffs   (pixel -> nm polynomial)
#    set_name,<text> / get_name
#    save                      -> write settings to flash (set_* no longer do)
#    stream[,<period_us>]      -> start streaming binary packets
#    stop                      -> stop streaming
#    reboot
#
#  While streaming, everything the device sends is a packet:
#    A5 5A | type u8 | length u16 | payload | crc32 u32
#  (little endian, crc32 over type+length+payload). type 1 = FRAME, type 2 =
#  TEXT (the reply to a command, or an event). Commands can still be sent;
#  set_led takes effect immediately, other settings restart the readout.
#  FRAME payload (544 bytes):
#    u32 seq, u64 t_start_us (device clock, start of integration),
#    u32 integration_us, u32 integration_clocks, u32 clk_hz,
#    u8 n_avg, u8 high_gain, u8 led, u8 flags, u16 n_pixels, u16 adc_errors,
#    256 x u16 pixel sums (divide by n_avg for counts)
#  flags: 1 = LED switched during the frame, 2 = first frame after a
#  (re)start, 4 = frame started late, 8 = ADC framing error.
# =============================================================================

import sys
import time
import select
import struct
import array
import micropython
import machine
import rp2
import uctypes
import binascii
from machine import Pin
from collections import deque

try:
    import ujson as json
except ImportError:
    import json

# =============================================================================
#  1. PINS & CONSTANTS
# =============================================================================

FW_ID = "C12666MA_PICO_PY_v2.0"
HELLO = "C12666MA,v1.0"        # unchanged so existing scripts still find it

PIN_CLK = 2
PIN_ST = 3
PIN_GAIN = 4
PIN_LED = 15
PIN_ADC_DOUT = 16
PIN_ADC_CS = 17
PIN_ADC_SCK = 18

GAIN_LOW_LEVEL = 1             # Gain pin at Vdd  -> low gain
GAIN_HIGH_LEVEL = 0            # Gain pin at GND  -> high gain

N_PIXELS = 256
TICKS_PER_CLK = 20             # PIO cycles per sensor clock (see programs)
CMD_FIXED_CLKS = 1027          # clocks in a command besides the extra ones
MIN_PERIOD_CLKS = 1035         # >= 1030 clocks between start pulses + margin
FLUSH_CLKS = 1100              # clocks with ST high to finish a cut-off scan
CLK_DEFAULT_HZ = 200_000       # MCP3301 SCK = 5 x this (1 MHz)
CLK_MIN_HZ = 20_000
CLK_MAX_HZ = 300_000           # SCK 1.5 MHz, MCP3301 max is 1.7 MHz at 5 V
MAX_INTEGRATION_US = 10_000_000
MAX_AVG = 16                   # 16 x 4095 still fits the u16 pixel sums

RING_SCANS = 8
RING_WORDS = RING_SCANS * N_PIXELS
RING_BYTES = RING_WORDS * 4
RING_SIZE_BITS = 13            # log2(RING_BYTES), DMA write-address ring
DMA_COUNT = 0xFFFFFFFF

PIO0_BASE = 0x50200000
PIO0_FDEBUG = PIO0_BASE + 0x008
PIO0_IRQ = PIO0_BASE + 0x030
PIO0_SM0_CLKDIV = PIO0_BASE + 0x0C8
PIO0_RXF1 = PIO0_BASE + 0x024
DREQ_PIO0_RX1 = 5
FDEBUG_TXSTALL_SM0 = 1 << 24
DMA_CHAN_ABORT = 0x50000444
TIMER_RAWH = 0x40054024
TIMER_RAWL = 0x40054028

PKT_MAGIC = b"\xa5\x5a"
PKT_FRAME = 1
PKT_TEXT = 2
FRAME_HDR_FMT = "<IQIIIBBBBHH"   # 32 bytes, see header comment
FRAME_PAYLOAD = 32 + 2 * N_PIXELS
FRAME_PKT_LEN = 5 + FRAME_PAYLOAD + 4

FLAG_LED_CHANGED = 1
FLAG_RESTART = 2
FLAG_LATE = 4
FLAG_ADC_ERROR = 8

CONFIG_PATH = "/config.json"

# =============================================================================
#  2. PIO PROGRAMS
# =============================================================================
#  SM0 timing: every sensor half-period is 10 PIO cycles (TICKS_PER_CLK / 2).
#  Command word: bits 7..0 = pixels - 1 (255), or 0 for a flush (no start
#  pulse); bits 31..8 = y, the number of extra clocks - 1.
#  Clocks per data command = 2 (start pulse) + 1024 (readout) + (y + 1).

@rp2.asm_pio(sideset_init=rp2.PIO.OUT_HIGH, set_init=rp2.PIO.OUT_HIGH,
             out_shiftdir=rp2.PIO.SHIFT_RIGHT, fifo_join=rp2.PIO.JOIN_TX)
def sensor_clk():
    wrap_target()
    pull(block)          .side(1)       # idle with CLK high until a command
    out(x, 8)            .side(1)
    out(y, 24)           .side(1)
    jmp(not_x, "extra")  .side(1)       # x == 0: flush, no start pulse
    nop()                .side(1)
    set(pins, 0)         .side(1) [4]   # ST low in the middle of a high phase
    nop()                .side(0) [9]   # the one falling edge while ST is low
    nop()                .side(1) [4]
    set(pins, 1)         .side(1) [4]   # ST high again
    nop()                .side(0) [9]
    label("pix")                        # 256 x (4 clocks) per scan
    nop()                .side(1) [9]   # video of this pixel becomes valid
    nop()                .side(0) [9]
    nop()                .side(1) [7]
    irq(0)               .side(1) [1]   # start the ADC 2 cycles early
    nop()                .side(0) [9]   # TRIG phase: ADC is sampling
    nop()                .side(1) [9]   # video returns to its offset level
    nop()                .side(0) [9]
    nop()                .side(1) [9]
    jmp(x_dec, "pix")    .side(0) [9]
    label("extra")                      # integration time beyond the readout
    nop()                .side(1) [9]
    jmp(y_dec, "extra")  .side(0) [9]
    wrap()


#  SM1: one MCP3301 conversion (16 SCK cycles, 4 PIO cycles each) per TRIG.
#  Side-set bit 0 = CS, bit 1 = SCK. Bits are read on SCK rising edges, so
#  bits 12..0 of each pushed word are the 13-bit result and bit 13 is the
#  ADC's null bit (always 0 when the framing is right).

@rp2.asm_pio(sideset_init=(rp2.PIO.OUT_HIGH, rp2.PIO.OUT_LOW),
             in_shiftdir=rp2.PIO.SHIFT_LEFT, autopush=True, push_thresh=16,
             fifo_join=rp2.PIO.JOIN_RX)
def adc_reader():
    wrap_target()
    wait(1, irq, 0)      .side(0b01)    # CS high, SCK low
    set(x, 15)           .side(0b00)    # CS low: the ADC starts sampling
    label("bit")
    in_(pins, 1)         .side(0b10) [1]
    jmp(x_dec, "bit")    .side(0b00) [1]
    wrap()

# =============================================================================
#  3. CONFIGURATION (kept in RAM; written to flash only by `save`)
# =============================================================================

DEFAULT_CONFIG = {
    "name": "C12666MA",
    "integration_us": 100_000,
    "n_avg": 1,
    "high_gain": 1,
    "clk_hz": CLK_DEFAULT_HZ,
    "period_us": 0,
    "wl_coeffs": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
}

g_cfg = dict(DEFAULT_CONFIG)


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump(g_cfg, f)


def load_config():
    global g_cfg
    g_cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            g_cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    g_cfg["n_avg"] = max(1, min(int(g_cfg["n_avg"]), MAX_AVG))
    g_cfg["clk_hz"] = max(CLK_MIN_HZ, min(int(g_cfg["clk_hz"]), CLK_MAX_HZ))
    g_cfg["high_gain"] = 1 if g_cfg["high_gain"] else 0
    while len(g_cfg["wl_coeffs"]) < 6:
        g_cfg["wl_coeffs"].append(0.0)

# =============================================================================
#  4. HARDWARE STATE
# =============================================================================

gain_pin = Pin(PIN_GAIN, Pin.OUT)
led_pin = Pin(PIN_LED, Pin.OUT, value=0)
sm0 = None
sm1 = None
dma = rp2.DMA()

# DMA ring buffer: the write address wraps on an aligned RING_BYTES boundary,
# so allocate twice the size and use the aligned half.
_ring_mem = bytearray(2 * RING_BYTES)
g_ring_addr = (uctypes.addressof(_ring_mem) + RING_BYTES - 1) & ~(RING_BYTES - 1)
g_dma_ctrl = dma.pack_ctrl(size=2, inc_read=False, inc_write=True,
                           ring_sel=True, ring_size=RING_SIZE_BITS,
                           treq_sel=DREQ_PIO0_RX1)

g_clk_hz = CLK_DEFAULT_HZ      # actual sensor clock after the PIO divider
g_led = 0
g_led_events = []              # [(t_us, state)] recent LED switches

g_sums = array.array("I", [0] * N_PIXELS)
g_dark = array.array("I", [0] * N_PIXELS)   # averaged counts, for `spec`
g_have_dark = False
g_pkt = bytearray(FRAME_PKT_LEN)
g_pkt[0:2] = PKT_MAGIC
g_pkt[2] = PKT_FRAME
struct.pack_into("<H", g_pkt, 3, FRAME_PAYLOAD)
g_pkt_mv = memoryview(g_pkt)


def now_us():
    """64-bit microseconds since boot (same crystal as the PIO clock)."""
    hi = machine.mem32[TIMER_RAWH]
    lo = machine.mem32[TIMER_RAWL]
    hi2 = machine.mem32[TIMER_RAWH]
    if hi2 != hi:
        lo = machine.mem32[TIMER_RAWL]
        hi = hi2
    return ((hi & 0xFFFFFFFF) << 32) | (lo & 0xFFFFFFFF)


def apply_gain():
    gain_pin.value(GAIN_HIGH_LEVEL if g_cfg["high_gain"] else GAIN_LOW_LEVEL)


def set_led(state):
    global g_led
    state = 1 if state else 0
    if state != g_led:
        g_led = state
        led_pin.value(state)
        g_led_events.append((now_us(), state))
        if len(g_led_events) > 16:
            g_led_events.pop(0)


def led_during(t0, t1):
    """(state at t1, switched within (t0, t1]) from the recent LED switches."""
    state = g_led
    changed = False
    for t, s in reversed(g_led_events):
        if t > t1:
            state = 1 - s
        elif t > t0:
            changed = True
        else:
            break
    return state, changed


def integration_clocks(us):
    n = (us * g_clk_hz + 500_000) // 1_000_000
    n_max = (MAX_INTEGRATION_US * g_clk_hz) // 1_000_000
    return max(MIN_PERIOD_CLKS, min(n, n_max))


def clocks_to_us(n):
    return (n * 1_000_000 + g_clk_hz // 2) // g_clk_hz


def min_integration_us():
    return clocks_to_us(MIN_PERIOD_CLKS)


def data_cmd(n_clocks):
    return ((n_clocks - CMD_FIXED_CLKS) << 8) | (N_PIXELS - 1)


FLUSH_CMD = (FLUSH_CLKS - 1) << 8

# =============================================================================
#  5. FAST HELPERS (viper)
# =============================================================================

@micropython.viper
def _accumulate(src: int, sums: ptr32, first: int) -> int:
    """Add one scan (256 DMA words at address src) to sums; returns the number
    of words whose ADC null bit was wrong."""
    p = ptr32(src)
    bad = 0
    i = 0
    while i < 256:
        w = p[i]
        if w & 0x2000:
            bad += 1
        v = w & 0x1FFF
        if v & 0x1000:          # negative (IN+ below IN-): clamp to 0
            v = 0
        if first:
            sums[i] = v
        else:
            sums[i] = sums[i] + v
        i += 1
    return bad


@micropython.viper
def _put_u16(dst: ptr8, offset: int, sums: ptr32):
    i = 0
    j = offset
    while i < 256:
        v = sums[i]
        dst[j] = v & 0xFF
        dst[j + 1] = (v >> 8) & 0xFF
        i += 1
        j += 2

# =============================================================================
#  6. READOUT PIPELINE
# =============================================================================
#  Every command pushed to SM0 produces one scan (256 words) in the DMA ring,
#  except a flush. g_desc holds one descriptor per expected scan, in order:
#    (kind, frame_seq, last_in_frame, t_start_us)   kind 0 = discard, 1 = data
#  The first scan after a start pulse that follows idle time contains charge
#  from that idle time, so it is always discarded.

g_desc = deque((), 64)
g_words_done = 0
g_ticks = 0                    # PIO cycles queued since the run started
g_run_t0 = 0                   # time of the first push of the run
g_tick_hz = 1
g_mode = 0                     # 0 idle, 1 continuous, 2 periodic, 3 one-shot
g_cont_scan = 0                # continuous: index of the next data command
g_cont_clocks = 0
g_cont_restart = False
g_pending = []                 # periodic/one-shot: commands not yet pushed
g_next_due = 0
g_frame_seq = 0                # sequence number of the next frame to queue
g_last_seq = -1                # sequence number of the last finished frame
g_frame_t_start = 0            # integration start of the frame being queued
g_last_frame = (0, 0, 0, 0, 0, 0)  # (seq, t_start, flags, adc_err, clocks, led)
g_scan_in_frame = 0
g_adc_errors = 0
g_frame_ready = False          # one-shot result available in g_sums
g_late = False
g_counters = {"frames": 0, "restarts": 0, "overruns": 0, "stalls": 0,
              "adc_errors": 0}
g_streaming = False
g_stream_t0 = 0
g_busy_us = 0                  # time spent processing and sending scans
g_on_frame = None              # callback(seq, t_start, flags, adc_errors)


def _sm_init():
    """(Re)initialise both state machines: clears FIFOs, pins to idle."""
    global sm0, sm1, g_clk_hz, g_tick_hz
    freq = g_cfg["clk_hz"] * TICKS_PER_CLK
    sm1 = rp2.StateMachine(1, adc_reader, freq=freq,
                           sideset_base=Pin(PIN_ADC_CS),
                           in_base=Pin(PIN_ADC_DOUT))
    sm0 = rp2.StateMachine(0, sensor_clk, freq=freq,
                           sideset_base=Pin(PIN_CLK), set_base=Pin(PIN_ST))
    div = machine.mem32[PIO0_SM0_CLKDIV]
    div256 = ((div >> 16) & 0xFFFF) * 256 + ((div >> 8) & 0xFF)
    g_tick_hz = (machine.freq() * 256) // div256
    g_clk_hz = g_tick_hz // TICKS_PER_CLK


def _dma_abort():
    machine.mem32[DMA_CHAN_ABORT] = 1 << dma.channel
    while machine.mem32[DMA_CHAN_ABORT] & (1 << dma.channel):
        pass


def pipeline_stop():
    global g_mode, g_pending
    if sm0 is not None:
        sm0.active(0)
        sm1.active(0)
    _dma_abort()
    g_mode = 0
    g_pending = []


def pipeline_start(mode):
    """Abort whatever runs, re-arm DMA, flush the sensor and start `mode`."""
    global g_words_done, g_desc, g_ticks, g_run_t0, g_mode, g_cont_scan
    global g_cont_clocks, g_scan_in_frame, g_adc_errors, g_frame_ready
    global g_pending, g_next_due, g_cont_restart, g_late, g_frame_seq
    pipeline_stop()
    g_frame_seq = g_last_seq + 1       # frames queued but never read are not lost
    _sm_init()
    machine.mem32[PIO0_IRQ] = 0xFF                 # clear PIO IRQ flags
    dma.config(read=PIO0_RXF1, write=g_ring_addr, count=DMA_COUNT,
               ctrl=g_dma_ctrl, trigger=True)
    g_words_done = 0
    g_desc = deque((), 64)
    g_scan_in_frame = 0
    g_adc_errors = 0
    g_frame_ready = False
    g_pending = []
    g_late = False
    g_cont_scan = 0
    g_cont_clocks = integration_clocks(g_cfg["integration_us"])
    g_cont_restart = True
    g_mode = mode
    sm1.active(1)
    sm0.active(1)
    g_run_t0 = now_us()
    sm0.put(FLUSH_CMD)
    g_ticks = 4 + FLUSH_CLKS * TICKS_PER_CLK
    g_next_due = g_run_t0 + (g_ticks * 1_000_000) // g_tick_hz
    machine.mem32[PIO0_FDEBUG] = FDEBUG_TXSTALL_SM0
    g_counters["restarts"] += 1


def _st_time(ticks):
    """Device time of the start pulse of the command starting at `ticks`."""
    return g_run_t0 + ((ticks + 5) * 1_000_000) // g_tick_hz


def frame_period_us():
    """Shortest frame period possible with the current settings."""
    n = integration_clocks(g_cfg["integration_us"])
    return clocks_to_us(n * g_cfg["n_avg"])


def effective_period_us():
    p = g_cfg["period_us"]
    cont = frame_period_us()
    if p <= cont:
        return cont
    single = clocks_to_us(integration_clocks(g_cfg["integration_us"])
                          * g_cfg["n_avg"] + MIN_PERIOD_CLKS) + 2000
    return max(p, single)


def _queue_frame():
    """Periodic / one-shot: commands for one frame (1 discard + n_avg data).
    Call only while SM0 is idle; returns the frame's sequence number."""
    global g_frame_seq
    n = integration_clocks(g_cfg["integration_us"])
    navg = g_cfg["n_avg"]
    for i in range(navg + 1):
        clocks = n if i < navg else MIN_PERIOD_CLKS
        g_pending.append((data_cmd(clocks), clocks, 0 if i == 0 else 1, i == navg))
    g_frame_seq += 1
    return g_frame_seq - 1


def _push_pending(seq_for_frame):
    """Push queued periodic commands while SM0's FIFO has room."""
    global g_ticks, g_frame_t_start
    while g_pending and sm0.tx_fifo() < 8:
        word, clocks, kind, last = g_pending.pop(0)
        if kind == 0:
            # first command of a frame: SM0 is idle, so it starts right now
            _resync_ticks(now_us())
            g_frame_t_start = _st_time(g_ticks)
        sm0.put(word)
        g_desc.append((kind, seq_for_frame, last, g_frame_t_start))
        g_ticks += clocks * TICKS_PER_CLK


g_periodic_seq = 0


def _top_up_continuous():
    """Continuous mode: keep SM0's FIFO full of integration-time commands."""
    global g_cont_scan, g_ticks, g_frame_seq, g_frame_t_start
    navg = g_cfg["n_avg"]
    n = g_cont_clocks
    while sm0.tx_fifo() < 8 and len(g_desc) < 48:
        j = g_cont_scan
        if j == 0:
            g_desc.append((0, 0, False, 0))
        else:
            k = (j - 1) % navg
            if k == 0:
                g_frame_seq += 1
                g_frame_t_start = _st_time(g_ticks - n * TICKS_PER_CLK)
            g_desc.append((1, g_frame_seq - 1, k == navg - 1, g_frame_t_start))
        sm0.put(data_cmd(n))
        g_ticks += n * TICKS_PER_CLK
        g_cont_scan = j + 1



def _sm0_idle():
    return sm0.tx_fifo() == 0 and (machine.mem32[PIO0_FDEBUG] & FDEBUG_TXSTALL_SM0)


def _finish_frame(seq, t_start):
    global g_frame_ready, g_cont_restart, g_late, g_last_seq, g_last_frame
    flags = 0
    if g_cont_restart:
        flags |= FLAG_RESTART
        g_cont_restart = False
    if g_late:
        flags |= FLAG_LATE
        g_late = False
    if g_adc_errors:
        flags |= FLAG_ADC_ERROR
        g_counters["adc_errors"] += g_adc_errors
    n = integration_clocks(g_cfg["integration_us"])
    t_end = t_start + clocks_to_us(n * g_cfg["n_avg"])
    led_state, changed = led_during(t_start, t_end)
    if changed:
        flags |= FLAG_LED_CHANGED
    g_counters["frames"] += 1
    g_last_seq = seq
    g_last_frame = (seq, t_start, flags, g_adc_errors, n, led_state)
    g_frame_ready = True
    if g_on_frame is not None:
        g_on_frame(*g_last_frame)


def service_pipeline():
    """Queue commands, consume finished scans, detect timing problems.
    Call this as often as possible while a run is active."""
    global g_words_done, g_scan_in_frame, g_adc_errors, g_next_due, g_late
    global g_periodic_seq, g_busy_us
    if g_mode == 0:
        return
    # 1. queue work for the sensor
    if g_mode == 1:
        _top_up_continuous()
    elif g_pending:
        _push_pending(g_periodic_seq)
    elif g_mode == 2:
        t = now_us()
        if t >= g_next_due and _sm0_idle():
            if t > g_next_due + 1000 and not g_cont_restart:
                g_late = True
            period = effective_period_us()
            g_next_due += period
            if g_next_due < t:                     # far behind: resync
                g_next_due = t + period
            g_periodic_seq = _queue_frame()
            _push_pending(g_periodic_seq)
            machine.mem32[PIO0_FDEBUG] = FDEBUG_TXSTALL_SM0
    # 2. a stall in continuous mode would stretch an integration: restart
    if g_mode == 1 and machine.mem32[PIO0_FDEBUG] & FDEBUG_TXSTALL_SM0:
        g_counters["stalls"] += 1
        send_event("stall")
        pipeline_start(1)
        return
    # 3. consume finished scans
    done = DMA_COUNT - dma.count
    avail = done - g_words_done
    if avail >= RING_WORDS:
        g_counters["overruns"] += 1
        send_event("overrun")
        pipeline_start(g_mode)
        return
    if avail < N_PIXELS:
        return
    t_busy = time.ticks_us()
    while avail >= N_PIXELS and g_desc:
        kind, seq, last, t_start = g_desc.popleft()
        off = g_words_done % RING_WORDS
        if kind == 1:
            g_adc_errors += _accumulate(g_ring_addr + off * 4, g_sums,
                                        1 if g_scan_in_frame == 0 else 0)
            g_scan_in_frame += 1
            if last:
                _finish_frame(seq, t_start)
                g_scan_in_frame = 0
                g_adc_errors = 0
        g_words_done += N_PIXELS
        avail -= N_PIXELS
    g_busy_us += time.ticks_diff(time.ticks_us(), t_busy)


def _resync_ticks(t):
    """Periodic mode: SM0 is idle, so the next command starts right now."""
    global g_ticks
    g_ticks = ((t - g_run_t0) * g_tick_hz) // 1_000_000


def acquire_once(timeout_ms=None):
    """Blocking single frame (used by spec / frame / dark). Result in g_sums."""
    global g_periodic_seq
    pipeline_start(3)
    n = integration_clocks(g_cfg["integration_us"])
    if timeout_ms is None:
        timeout_ms = clocks_to_us(n * (g_cfg["n_avg"] + 1)) // 1000 + 1000
    # wait for the flush to finish, then queue one frame
    while not _sm0_idle():
        pass
    g_periodic_seq = _queue_frame()
    t_end = time.ticks_add(time.ticks_ms(), timeout_ms)
    while not g_frame_ready:
        service_pipeline()
        if time.ticks_diff(time.ticks_ms(), t_end) > 0:
            pipeline_stop()
            return False
    pipeline_stop()
    return True

# =============================================================================
#  7. OUTPUT
# =============================================================================

_out = sys.stdout.buffer


def write_line(text):
    _out.write(text.encode() + b"\n")


def send_text_packet(text):
    payload = text.encode()
    body = struct.pack("<BH", PKT_TEXT, len(payload)) + payload
    _out.write(PKT_MAGIC + body + struct.pack("<I", binascii.crc32(body)))


def reply(text):
    if g_streaming:
        send_text_packet(text)
    else:
        write_line(text)


def send_event(name):
    if g_streaming:
        send_text_packet('{"event":"%s"}' % name)


def send_frame_packet(seq, t_start, flags, adc_errors, n_clocks, led_state):
    struct.pack_into(FRAME_HDR_FMT, g_pkt, 5, seq & 0xFFFFFFFF, t_start,
                     clocks_to_us(n_clocks), n_clocks, g_clk_hz,
                     g_cfg["n_avg"], g_cfg["high_gain"], led_state, flags,
                     N_PIXELS, min(adc_errors, 0xFFFF))
    _put_u16(g_pkt, 5 + 32, g_sums)
    crc = binascii.crc32(g_pkt_mv[2:5 + FRAME_PAYLOAD])
    struct.pack_into("<I", g_pkt, 5 + FRAME_PAYLOAD, crc)
    _out.write(g_pkt)


def spectrum_text(raw):
    navg = g_cfg["n_avg"]
    half = navg // 2
    parts = []
    for i in range(N_PIXELS):
        v = (g_sums[i] + half) // navg
        if not raw and g_have_dark:
            v = max(0, v - g_dark[i])
        parts.append(str(v))
    return ",".join(parts)


def wl_coeffs_text():
    return '{"wl_coeffs":[' + ",".join("%.10g" % c for c in g_cfg["wl_coeffs"]) + "]}"


def status_text():
    return json.dumps({
        "fw": FW_ID, "name": g_cfg["name"], "n_pixels": N_PIXELS,
        "integration_us": clocks_to_us(integration_clocks(g_cfg["integration_us"])),
        "integration_clocks": integration_clocks(g_cfg["integration_us"]),
        "min_integration_us": min_integration_us(),
        "max_integration_us": MAX_INTEGRATION_US,
        "clk_hz": g_clk_hz, "n_avg": g_cfg["n_avg"], "max_avg": MAX_AVG,
        "high_gain": g_cfg["high_gain"], "led": g_led,
        "period_us": g_cfg["period_us"],
        "min_period_us": frame_period_us(),
        "effective_period_us": effective_period_us(),
        "streaming": 1 if g_streaming else 0,
        "mode": ("idle", "continuous", "periodic", "single")[g_mode],
        "t_us": now_us(),
        "wl_coeffs": g_cfg["wl_coeffs"],
        "counters": g_counters,
        "cpu_load_pct": round(100 * g_busy_us / max(1, now_us() - g_stream_t0), 1)
                        if g_streaming else 0,
    })

# =============================================================================
#  8. COMMANDS
# =============================================================================

def _int_arg(tokens, i, default=None):
    try:
        return int(tokens[i])
    except (IndexError, ValueError):
        return default


def start_stream(period_us=None):
    global g_streaming, g_last_seq, g_on_frame, g_stream_t0, g_busy_us
    if period_us is not None:
        g_cfg["period_us"] = max(0, period_us)
    g_streaming = True
    g_last_seq = -1                     # sequence numbers start at 0
    g_stream_t0 = now_us()
    g_busy_us = 0
    g_on_frame = send_frame_packet
    restart_stream()
    send_text_packet('{"stream":"started","period_us":%d,"effective_period_us":%d}'
                     % (g_cfg["period_us"], effective_period_us()))


def restart_stream():
    if g_cfg["period_us"] <= frame_period_us():
        pipeline_start(1)
    else:
        pipeline_start(2)


def stop_stream():
    global g_streaming, g_on_frame
    pipeline_stop()
    send_text_packet('{"stream":"stopped"}')
    g_streaming = False
    g_on_frame = None


def handle_command(cmd):
    global g_have_dark, g_on_frame
    tokens = cmd.strip().split(",")
    tok = tokens[0].strip().lower()
    if tok == "":
        return
    restart = False

    if tok == "hello":
        reply(HELLO)
    elif tok == "idn":
        reply(FW_ID)
    elif tok == "status":
        reply(status_text())

    elif tok == "set_integration":
        us = _int_arg(tokens, 1)
        if us is not None:
            g_cfg["integration_us"] = clocks_to_us(integration_clocks(us))
            restart = True
        reply('{"integration_us":%d}' % g_cfg["integration_us"])
    elif tok == "get_integration":
        reply(str(g_cfg["integration_us"]))

    elif tok == "set_gain":
        v = _int_arg(tokens, 1)
        if v is not None:
            g_cfg["high_gain"] = 1 if v else 0
            apply_gain()
            restart = True
        reply('{"high_gain":%d}' % g_cfg["high_gain"])
    elif tok == "get_gain":
        reply(str(g_cfg["high_gain"]))

    elif tok == "set_avg":
        v = _int_arg(tokens, 1)
        if v is not None:
            g_cfg["n_avg"] = max(1, min(v, MAX_AVG))
            restart = True
        reply('{"n_avg":%d}' % g_cfg["n_avg"])
    elif tok == "get_avg":
        reply(str(g_cfg["n_avg"]))

    elif tok == "set_led":
        v = _int_arg(tokens, 1)
        if v is not None:
            set_led(v)
        reply('{"led":%d}' % g_led)
    elif tok == "get_led":
        reply(str(g_led))

    elif tok == "set_period":
        v = _int_arg(tokens, 1)
        if v is not None:
            g_cfg["period_us"] = max(0, v)
            restart = True
        reply('{"period_us":%d,"effective_period_us":%d}'
              % (g_cfg["period_us"], effective_period_us()))
    elif tok == "get_period":
        reply(str(g_cfg["period_us"]))

    elif tok == "set_clock":
        v = _int_arg(tokens, 1)
        if v is not None:
            g_cfg["clk_hz"] = max(CLK_MIN_HZ, min(v, CLK_MAX_HZ))
            _sm_init()
            g_cfg["integration_us"] = clocks_to_us(
                integration_clocks(g_cfg["integration_us"]))
            restart = True
        reply('{"clk_hz":%d}' % g_clk_hz)
    elif tok == "get_clock":
        reply(str(g_clk_hz))

    elif tok == "stream":
        if not g_streaming:
            start_stream(_int_arg(tokens, 1))
        else:
            reply('{"stream":"already running"}')
    elif tok == "stop":
        if g_streaming:
            stop_stream()
        else:
            reply('{"stream":"stopped"}')

    elif g_streaming:
        # one-shot acquisitions would disturb the stream
        reply('{"error":"not allowed while streaming"}')

    elif tok == "spec" or tok == "frame" or tok == "dark":
        g_on_frame = None
        if not acquire_once():
            reply('{"error":"timeout"}')
        elif tok == "frame":
            send_frame_packet(*g_last_frame)
        elif tok == "dark":
            navg = g_cfg["n_avg"]
            for i in range(N_PIXELS):
                g_dark[i] = (g_sums[i] + navg // 2) // navg
            g_have_dark = True
            reply('{"dark":"ok"}')
        else:
            raw = len(tokens) > 1 and tokens[1].strip() == "raw"
            reply(spectrum_text(raw))
    elif tok == "clear_dark":
        g_have_dark = False
        reply('{"dark":"cleared"}')

    elif tok == "set_wl_coeff":
        i = _int_arg(tokens, 1)
        try:
            value = float(tokens[2])
            if i is not None and 0 <= i < 6:
                g_cfg["wl_coeffs"][i] = value
        except (IndexError, ValueError):
            pass
        reply(wl_coeffs_text())
    elif tok == "get_wl_coeffs":
        reply(wl_coeffs_text())

    elif tok == "set_name":
        if len(tokens) > 1:
            g_cfg["name"] = tokens[1].strip()[:15]
        reply('{"device_name":"%s"}' % g_cfg["name"])
    elif tok == "get_name":
        reply(g_cfg["name"])

    elif tok == "save":
        save_config()
        reply('{"save":"ok"}')
    elif tok == "reboot":
        reply('{"reboot":"ok"}')
        time.sleep_ms(50)
        machine.reset()
    else:
        reply('{"error":"unknown"}')

    if restart and g_streaming:
        restart_stream()

# =============================================================================
#  9. MAIN LOOP
# =============================================================================

def main():
    load_config()
    apply_gain()
    set_led(0)
    _sm_init()
    g_cfg["integration_us"] = clocks_to_us(integration_clocks(g_cfg["integration_us"]))
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    stdin = sys.stdin.buffer
    line = bytearray()
    while True:
        service_pipeline()
        while poll.poll(0 if g_mode else 1):
            c = stdin.read(1)
            if c == b"\r" or c == b"\n":
                if line:
                    try:
                        handle_command(line.decode())
                    except Exception as e:      # keep running on bad input
                        reply('{"error":"%s"}' % str(e).replace('"', "'"))
                    line = bytearray()
            elif len(line) < 64:
                line.extend(c)
            if g_mode:
                break                           # keep the pipeline serviced


if __name__ == "__main__":
    main()
