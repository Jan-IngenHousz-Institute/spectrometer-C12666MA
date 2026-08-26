# =============================================================================
#  C12666MA_spectrometer_MCP3301_PICO.py
#  Hamamatsu C12666MA micro-spectrometer + MCP3301 SPI ADC, MicroPython port
#  for the Raspberry Pi Pico (RP2040). Same sensor timing and serial command
#  protocol as the earlier .ino versions.
# =============================================================================
#
#  --- WIRING -----------------------------------------------------------------
#  Same physical wiring as the Arduino .ino Pico port: sensor + MCP3301 stay
#  on 5V, six signals cross to the Pico through level shifters (sensor
#  CLK/ST/Gain, MCP3301 CLK/CS going Pico->5V, MCP3301 DOUT going 5V->Pico -
#  make sure that one channel is bidirectional). See that file's header
#  comment for the full pin-by-pin diagram; the GPIO numbers below match it:
#     SPEC_CLK=GP2, SPEC_ST=GP3, SPEC_GAIN=GP4, LED_PIN=GP15, ADC_CS=GP17
#     SPI0 hardware pins: SCK=GP18, MOSI=GP19 (unused, MCP3301 has no DIN),
#     MISO=GP16
#
#  --- WHY THIS IS A REAL PORT, NOT JUST A SYNTAX SWAP -------------------------
#  MicroPython's per-instruction overhead is much bigger than compiled C++.
#  The pixel readout here still bit-bangs SPEC_CLK/ST at ~1us edges in a pure
#  Python loop, same as the .ino version - which means the actual clock
#  period and pixel-to-pixel timing will likely come out slower and less
#  consistent than the Arduino version, even though the code asks for the
#  same delays. For this sensor that mostly costs you frame rate, not
#  correctness (the C12666MA's minimum clock frequency is only 1kHz), but if
#  you see noisy or shifted spectra, timing jitter here is the first thing
#  to suspect. If you outgrow this, the fix is to move CLK/ST generation and
#  pixel sampling into a PIO state machine (rp2.PIO) so the timing is
#  hardware-clocked instead of interpreter-clocked - not implemented here,
#  since you asked for the straightforward port first.
#
#  --- WHAT CHANGED FUNCTIONALLY vs. THE .ino VERSION --------------------------
#  1. Config storage: no EEPROM emulation - settings are saved as JSON to a
#     file (/config.json) on the Pico's onboard flash filesystem, which
#     MicroPython exposes directly. Persists across reboots the same way.
#  2. Reset: "reboot" now calls machine.reset().
#  3. Serial protocol: MicroPython's USB-CDC serial IS the REPL by default.
#     This script takes it over by polling sys.stdin directly once running,
#     using the exact same command protocol (lines ending in '\r', same
#     command names/replies) as the .ino versions - a serial terminal at
#     115200 8N1 talking to the Pico won't be able to tell the difference.
#     To auto-run this on power-up (instead of dropping to the REPL), save
#     this file as main.py on the Pico's filesystem (e.g. via Thonny).
#  4. No F() macro - MicroPython has no flash-vs-RAM string distinction to
#     worry about; string literals are just used directly.
#
#  --- HOW TO USE ---------------------------------------------------------------
#  1. Flash the official MicroPython UF2 onto the Pico (micropython.org),
#     or use Thonny's "Install MicroPython" helper.
#  2. Copy this file onto the Pico's filesystem as main.py (Thonny: File ->
#     Save As -> Raspberry Pi Pico), so it runs automatically on boot/reset.
#  3. Open a serial terminal at 115200 baud, line ending = "Carriage Return".
#  4. Same commands as before, e.g.:
#       hello
#       set_integration,2000
#       set_gain,1
#       dark
#       spec
#       spec,raw
# =============================================================================

import sys
import select
import time
import machine


try:
    import ujson as json
except ImportError:
    import json

from machine import Pin, SPI, reset

# =============================================================================
#  1. PINS & CONSTANTS
# =============================================================================

SPEC_CLK_PIN  = 2
SPEC_ST_PIN   = 3
SPEC_GAIN_PIN = 4
LED_PIN_NUM   = 15
ADC_CS_PIN    = 17

spec_clk  = Pin(SPEC_CLK_PIN,  Pin.OUT)
spec_st   = Pin(SPEC_ST_PIN,   Pin.OUT)
spec_gain = Pin(SPEC_GAIN_PIN, Pin.OUT)
led       = Pin(LED_PIN_NUM,   Pin.OUT)
adc_cs    = Pin(ADC_CS_PIN,    Pin.OUT)

# SPI0 hardware pins on the Pico: SCK=GP18, MOSI=GP19, MISO=GP16. MOSI is
# wired up (SPI needs a pin assigned) but never actually used electrically -
# the MCP3301 has no DIN.
spi = SPI(0, baudrate=500_000, polarity=0, phase=0,
          sck=Pin(18), mosi=Pin(19), miso=Pin(16))

# Gain pin logic levels (same meaning as the .ino version - the shifter
# reproduces whatever level is set here on the 5V side).
GAIN_LOW  = 1  # low gain:  pin at/near Vdd
GAIN_HIGH = 0  # high gain: pin at/near GND

SPEC_CHANNELS = 256

# Half-period of each clock pulse, in microseconds. See the timing caveat
# above - actual pulse width will run slower than this due to Python
# interpreter overhead; increase if you need a more predictable clock.
CLK_DELAY_US = 1

MIN_INTEGRATION_US = 5000
MAX_AVG = 15

CONFIG_PATH = "/config.json"

# =============================================================================
#  2. CONFIGURATION (persisted as JSON on the Pico's flash filesystem)
# =============================================================================

DEFAULT_CONFIG = {
    "name": "C12666MA",
    "integration_us": MIN_INTEGRATION_US,
    "n_avg": 1,
    "wl_coeffs": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "high_gain": 0,
}

g_cfg = dict(DEFAULT_CONFIG)


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump(g_cfg, f)


def load_config():
    global g_cfg
    try:
        with open(CONFIG_PATH) as f:
            loaded = json.load(f)
        # Merge onto defaults so a config file missing a key (e.g. after
        # this script is updated) doesn't crash - same spirit as the .ino
        # version falling back to defaults on a bad magic byte.
        g_cfg = dict(DEFAULT_CONFIG)
        g_cfg.update(loaded)
    except (OSError, ValueError):
        g_cfg = dict(DEFAULT_CONFIG)
        save_config()

    # Make sure a corrupted value can never break averaging or integration.
    if g_cfg["n_avg"] < 1:
        g_cfg["n_avg"] = 1
    if g_cfg["n_avg"] > MAX_AVG:
        g_cfg["n_avg"] = MAX_AVG
    if g_cfg["integration_us"] < MIN_INTEGRATION_US:
        g_cfg["integration_us"] = MIN_INTEGRATION_US


# =============================================================================
#  3. FRAME BUFFERS
# =============================================================================

g_data = [0] * SPEC_CHANNELS   # latest frame; also used as the average sum
g_dark = [0] * SPEC_CHANNELS   # dark reference frame (post-averaging)
g_have_dark = False
g_led_on = False

# =============================================================================
#  4. SERIAL COMMAND BUFFER (this script owns the USB serial once running -
#     see header note above)
# =============================================================================

g_cmd = ""
_poll = select.poll()
_poll.register(sys.stdin, select.POLLIN)


def serial_write(s):
    sys.stdout.write(s)


# =============================================================================
#  5. ACQUISITION  (logic unchanged from the .ino versions - same protocol)
# =============================================================================

def pulse_clock():
    spec_clk.value(0)
    time.sleep_us(CLK_DELAY_US)
    spec_clk.value(1)
    time.sleep_us(CLK_DELAY_US)


def apply_gain():
    spec_gain.value(GAIN_HIGH if g_cfg["high_gain"] else GAIN_LOW)


def read_mcp3301():
    """Trigger one conversion on the MCP3301 and return its signed 13-bit
    result (-4096..+4095). Bit layout/sign-extension match the .ino version
    - re-verify with a bench test (known DC voltage from a pot) once wired
    up through the level shifters, same as noted there."""
    adc_cs.value(0)
    raw_bytes = spi.read(2, 0x00)
    adc_cs.value(1)

    hi_byte = raw_bytes[0]
    lo_byte = raw_bytes[1]
    raw = ((hi_byte & 0x1F) << 8) | lo_byte
    if raw & 0x1000:
        raw -= 0x2000   # sign-extend the 13-bit two's complement value
    return raw


def read_mcp3301_unsigned():
    raw = read_mcp3301()
    return 0 if raw < 0 else raw


def pulse_start_stop():
    spec_clk.value(0)
    time.sleep_us(CLK_DELAY_US)
    spec_clk.value(1)
    spec_st.value(0)
    time.sleep_us(CLK_DELAY_US)

    spec_clk.value(0)
    time.sleep_us(CLK_DELAY_US)
    spec_clk.value(1)
    spec_st.value(1)
    time.sleep_us(CLK_DELAY_US)


def acquire_one_pass(pass_index):
    # -- Step 1: one full frame's worth of leading clocks, ST idling HIGH. --
    for _ in range(SPEC_CHANNELS):
        pulse_clock()

    # -- Step 2: start pulse - marks the beginning of integration. --
    pulse_start_stop()

    # -- Step 3: integration window, timed against real elapsed
    #    microseconds via time.ticks_us()/ticks_diff(), not clock counting. --
    int_start = time.ticks_us()
    while time.ticks_diff(time.ticks_us(), int_start) < g_cfg["integration_us"]:
        pulse_clock()
        pulse_clock()

    # -- Step 4: stop pulse - marks the end of integration. --
    pulse_start_stop()

    # -- Step 5: read the 256 real pixels, 4 clocks per pixel, sampling on
    #    the first clock's falling edge. --
    for i in range(SPEC_CHANNELS):
        spec_clk.value(0)
        time.sleep_us(CLK_DELAY_US)
        spec_clk.value(1)
        time.sleep_us(CLK_DELAY_US)
        spec_clk.value(0)
        # Settling delay before sampling - see timing caveat up top.
        time.sleep_us(20)

        sample = read_mcp3301_unsigned()   # valid right after this falling edge

        if pass_index == 0:
            g_data[i] = sample
        else:
            accum = g_data[i] * pass_index + sample
            g_data[i] = accum // (pass_index + 1)

        spec_clk.value(1)
        time.sleep_us(CLK_DELAY_US)
        pulse_clock()
        pulse_clock()

    # -- Step 6: trailing clocks, leaves the sensor ready for the next
    #    acquisition. --
    for _ in range(SPEC_CHANNELS):
        pulse_clock()


def read_spectrometer():
    for i in range(SPEC_CHANNELS):
        g_data[i] = 0
    for p in range(g_cfg["n_avg"]):
        acquire_one_pass(p)
    # No final division required - the running average is already applied.


def capture_dark():
    global g_have_dark
    read_spectrometer()
    for i in range(SPEC_CHANNELS):
        g_dark[i] = g_data[i]
    g_have_dark = True


# =============================================================================
#  6. OUTPUT HELPERS
# =============================================================================

def print_spectrum(raw):
    parts = []
    for i in range(SPEC_CHANNELS):
        if raw or not g_have_dark:
            value = g_data[i]
        else:
            value = g_data[i] - g_dark[i]
            if value < 0:
                value = 0
        parts.append(str(value))
    serial_write(",".join(parts))
    serial_write("\r")


def print_wl_coeffs():
    coeffs_str = ",".join("%.8f" % c for c in g_cfg["wl_coeffs"])
    serial_write('{"wl_coeffs":[' + coeffs_str + "]}\r")


# =============================================================================
#  7. COMMAND HANDLING
# =============================================================================
#
#  Supported commands (each line ends with Carriage Return, '\r') - identical
#  to the .ino versions:
#    hello                     -> "C12666MA,v1.0"        (auto-discovery)
#    idn                       -> "C12666MA_PICO_PY_v1.0"
#    spec                      -> dark-subtracted spectrum (256 numbers)
#    spec,raw                  -> raw spectrum (256 numbers)
#    set_integration,<us>      -> {"integration_us":<n>}  (floored at 1280)
#    get_integration           -> <n>
#    set_gain,<0|1>            -> {"high_gain":<0|1>}     (0=low, 1=high)
#    get_gain                  -> <0|1>
#    set_led,<0|1>             -> {"led":<0|1>}          (0=off, 1=on)
#    get_led                   -> <0|1>
#    dark                      -> {"dark":"ok"}          (capture dark frame)
#    clear_dark                -> {"dark":"cleared"}
#    set_avg,<n>               -> {"n_avg":<n>}          (1..15)
#    get_avg                   -> <n>
#    set_wl_coeff,<i>,<value>  -> {"wl_coeffs":[...]}    (i = 0..5)
#    get_wl_coeffs             -> {"wl_coeffs":[...]}
#    set_name,<text>           -> {"device_name":"..."}
#    get_name                  -> <n>
#    reboot                    -> soft reset (machine.reset())
#
def handle_command(cmd):
    global g_led_on, g_have_dark

    tokens = cmd.split(",")
    tok = tokens[0] if tokens else ""
    if tok == "":
        return

    if tok == "hello":
        serial_write("C12666MA,v1.0\r\n")

    elif tok == "idn":
        serial_write("C12666MA_PICO_PY_v1.0\r\n")

    elif tok == "spec":
        arg = tokens[1] if len(tokens) > 1 else None
        raw = (arg == "raw")
        read_spectrometer()
        print_spectrum(raw)
        serial_write("\r\n")

    elif tok == "set_integration":
        if len(tokens) > 1:
            try:
                requested = int(tokens[1])
            except ValueError:
                requested = 0
            g_cfg["integration_us"] = max(requested, MIN_INTEGRATION_US)
            save_config()
        serial_write('{"integration_us":%d}\r\n' % g_cfg["integration_us"])

    elif tok == "get_integration":
        serial_write(str(g_cfg["integration_us"]))
        serial_write("\r\n")

    elif tok == "set_gain":
        if len(tokens) > 1:
            try:
                g_cfg["high_gain"] = 1 if int(tokens[1]) != 0 else 0
            except ValueError:
                pass
            apply_gain()
            save_config()
        serial_write('{"high_gain":%d}\r\n' % g_cfg["high_gain"])

    elif tok == "get_gain":
        serial_write(str(g_cfg["high_gain"]))
        serial_write("\r\n")

    elif tok == "set_led":
        if len(tokens) > 1:
            try:
                g_led_on = int(tokens[1]) != 0
            except ValueError:
                pass
            led.value(1 if g_led_on else 0)
        serial_write('{"led":%d}\r\n' % (1 if g_led_on else 0))

    elif tok == "get_led":
        serial_write(str(1 if g_led_on else 0))
        serial_write("\r\n")

    elif tok == "dark":
        capture_dark()
        serial_write('{"dark":"ok"}\r\n')

    elif tok == "clear_dark":
        g_have_dark = False
        serial_write('{"dark":"cleared"}\r\n')

    elif tok == "set_avg":
        if len(tokens) > 1:
            try:
                n = int(tokens[1])
            except ValueError:
                n = 1
            n = max(1, min(n, MAX_AVG))
            g_cfg["n_avg"] = n
            save_config()
        serial_write('{"n_avg":%d}\r\n' % g_cfg["n_avg"])

    elif tok == "get_avg":
        serial_write(str(g_cfg["n_avg"]))
        serial_write("\r\n")

    elif tok == "set_wl_coeff":
        if len(tokens) > 2:
            try:
                idx = int(tokens[1])
                value = float(tokens[2])
                if 0 <= idx < 6:
                    g_cfg["wl_coeffs"][idx] = value
                    save_config()
            except ValueError:
                pass
        print_wl_coeffs()

    elif tok == "get_wl_coeffs":
        print_wl_coeffs()

    elif tok == "set_name":
        if len(tokens) > 1:
            g_cfg["name"] = tokens[1][:15]
            save_config()
        serial_write('{"device_name":"%s"}\r\n' % g_cfg["name"])

    elif tok == "get_name":
        serial_write(g_cfg["name"])
        serial_write("\r\n")

    elif tok == "reboot":
        serial_write('{"reboot":"ok"}\r')
        time.sleep_ms(50)   # let the reply flush out over USB-CDC
        reset()

    else:
        serial_write('{"error":"unknown"}\r\n')


# =============================================================================
#  8. setup / main loop
# =============================================================================

def setup():
    spec_clk.value(0)
    spec_st.value(1)    # ST idles HIGH on the C12666MA
    led.value(0)         # start with the LED off
    adc_cs.value(1)      # idle deselected

    load_config()
    apply_gain()


def main():
    global g_cmd
    setup()

    while True:
        # Non-blocking check for available serial input.
        events = _poll.poll(0)
        if events:
            c = sys.stdin.read(1)
            if c == "\r" or c == "\n":
                if len(g_cmd) > 0:
                    handle_command(g_cmd)
                    g_cmd = ""
            elif len(g_cmd) < 39:
                g_cmd += c
            # (characters beyond the buffer size are dropped on purpose)


main()
