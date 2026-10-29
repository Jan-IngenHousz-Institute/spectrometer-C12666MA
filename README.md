# C12666MA spectrometer

Hamamatsu C12666MA micro-spectrometer (340–780 nm, 256 pixels) read by a
Raspberry Pi Pico through an MCP3301 13-bit ADC, on the JII "C12666MA Board
v2". Includes a live GUI for chlorophyll fluorescence: two wavelength regions
(`incident_light`, `fluorescence`) and their ratio, the *rel. fluo. yield*,
plotted and recorded over time, from one spectrometer or several in sync,
for recordings of minutes to days. A terminal recorder uploads the spectra to
an [openJII](https://openjii.org) experiment
([README_recorder.md](README_recorder.md)).

```
firmware/main.py      MicroPython firmware (copy to the Pico as main.py)
c12666ma/             Python package: driver, analysis, recorder, GUI, openJII upload
calibration/          pixel -> wavelength calibration and its laser measurements
tools/hw_check.py     speed / reliability / noise check of a connected board
tests/                unit tests (no hardware needed)
notebooks/            K. Tolsma's measurement and PAM notebooks, and a Digilent WaveForms test
kicad/                PCB v2, two variants: Pico on the back / on the front
legacy/arduino/       earlier Arduino Uno sketches, incl. the MCP3301 bench test
legacy/python-v1/     the first MicroPython firmware (MAIN.py) and playground notebooks
```

## Quick start

```
pip install -r requirements.txt
python -m c12666ma                 # the GUI
python -m c12666ma --simulate 3    # try it with three simulated spectrometers
```

1. Spectrometers plugged in are connected at start-up; later ones with
   **Auto-detect & connect**. Select one in the table to set its name, gain,
   integration time, averages and LED.
2. **Start**. With one spectrometer, "As fast as possible" streams one
   spectrum per integration time (about 190 spectra/s at the 5.2 ms minimum).
   Otherwise all spectrometers take one spectrum together at every period (see
   below).
3. Cover the sensors or switch the light off and press **Take dark**. The
   yield needs a dark spectrum taken with the same gain, integration time and
   averages, and the table shows when one no longer matches ("redo").
4. Drag the blue (`incident_light`) and red (`fluorescence`) regions, or type
   their limits. They apply to all spectrometers.
5. The **3D spectrum history** tab shows the most recent spectra of the
   selected spectrometer as a surface over wavelength, time and counts. It
   keeps up to 5000 spectra per spectrometer in memory, can show the latest
   2–500, and updates while it is open.
6. **Start recording**, optionally with a duration. It runs until stopped or
   for days; a new file part starts every 24 h (adjustable).

`run_C12666MA_3D_GUI.py` starts the GUI from any folder:

```
python path\to\spectrometer-C12666MA\run_C12666MA_3D_GUI.py
```

### Several spectrometers

All spectrometers take a spectrum together at every tick. The PC sends each
trigger up to 0.5 s ahead, with the start time converted to that device's
own clock. The clocks are measured every 30 s (offset and crystal drift), so
the devices should start within about a millisecond even when the PC
stalls. Spectra of the same tick have the same `seq` in every file.

- **Minimum period:** set by the slowest spectrometer, its averages ×
  integration time plus about 12 ms (readout and margins). The GUI shows
  which one.
- **Different integration times:** the windows are centred on the same
  moment; a 10 ms spectrum is taken in the middle of a 1 s one.
- **Unplugged or reset devices** are reconnected automatically (by their
  unique ID) and the recording continues; gaps show up as missed ticks.
- **Long recordings:** the PC is kept awake while recording, and the
  recording stops cleanly (with a note) if less than 500 MB of disk is left.

### Rel. fluo. yield

`fluorescence / incident_light`, where each term is the sum of
dark-subtracted counts over its region. A region "has signal" when that sum
is more than *k* × its noise (k = 5 by default; the noise comes from the dark
frames).

| incident_light | fluorescence | result |
|---|---|---|
| signal | any | fluorescence / incident_light |
| no signal | signal | empty + warning *fluorescence without incident light* |
| no signal | no signal | 0 |

Frames with a pixel at saturation in either region are flagged (orange in
the plot, `;saturated` in the file). The saturation level is about 3100
counts at high gain and about 1950 at low gain; both can be adjusted in the
GUI.

### Recording format

Each recording is a folder named `<name>_<date>_<time>`:

```
SIM-1_000001_part001.csv    one series per spectrometer (<name>_<end of its ID>),
SIM-1_000001_part002.csv    a new part every 24 h
SIM-2_000002_part001.csv
session.json                devices, settings, calibrations, counts, why it stopped
events.log                  connections, reconnects, settings changes, ...
```

Open a part with `pandas.read_csv(path, comment="#")`. The header lines
(`# ...`) hold the settings, regions and calibration. The `kind` column
separates the row types:

- `wavelength_nm`: the wavelength of every pixel column `px0..px255`
- `dark` / `dark_std`: the dark reference (written whenever one is taken, and
  repeated at the top of every part)
- `spectrum`: one row per spectrum, with the timestamp (start of
  integration), `t_s` (seconds since the recording started, the same time base
  for all spectrometers), `seq` (the tick number), settings, optionally
  `incident_light`, `fluorescence`, `rel_fluo_yield`, `yield_status`, and the
  raw counts (not dark-subtracted).

### Unattended recording with upload to openJII

`python -m c12666ma.record` records with all connected spectrometers from
the terminal, for hours or days, and sends the spectra to an experiment on
[openJII](https://openjii.org) as an *upload table*. Each unit keeps its own
gain, integration time and averages.

```
python -m c12666ma.record                 asks everything: sign-in, experiment, settings
python -m c12666ma.record login           (re)enter the openJII API key
python -m c12666ma.record upload FOLDER   finish the uploads of an earlier recording
```

The spectra are written locally first (the recording folder described above)
and uploaded every 15 minutes, one row per spectrum with its timestamp and
settings. Without network they wait on disk and go out later.
**[README_recorder.md](README_recorder.md)** explains the whole procedure:
sign-in, laptop settings for runs of several days, the uploaded columns,
starting from Task Scheduler and troubleshooting.

### Scripting

```python
from c12666ma import Spectrometer

with Spectrometer.open() as spec:          # finds the Pico automatically
    spec.set_integration_us(20_000)
    frame = spec.read_frame()              # frame.counts: 256 values
    spec.start_stream(period_us=0)
    frame = spec.frames.get()              # frames keep arriving until stop_stream()
    spec.stop_stream()
```

## Firmware

Copy `firmware/main.py` to the Pico as `main.py`, with Thonny or with
`mpremote cp firmware/main.py :main.py`. The serial protocol is documented
at the top of the file. Commands are text lines (`set_integration,20000`,
`spec,raw`, ...), and the v1 commands still work. Settings stay in RAM until
you send `save`. Synchronised acquisition needs firmware v2.1 (`idn`); the
GUI says so if a device has an older one.

The sensor clock (200 kHz), the start pulses and the ADC conversions are
generated by the Pico's PIO state machines, and DMA moves the samples to RAM.
The readout therefore doesn't depend on Python timing. Per the datasheet,
the integration time is the interval between start pulses, so it is exactly
N clocks for every pixel. Readout overlaps the next integration, so
streaming has no dead time.

Measured on the board in October 2026 (`python tools/hw_check.py`, 60 s
run):

| | |
|---|---|
| frame rate at the 5.175 ms minimum integration | 193.2 spectra/s, period exact to 1 µs |
| dropped frames / checksum errors / ADC framing errors | 0 / 0 / 0 (11 593 frames) |
| device CPU load at that rate | 48 % |
| dark noise | ~11 counts rms per pixel (old firmware: same median, but outliers up to 350) |

The noise floor (~11 mV rms) is set by the analog front end, not the sensor
(datasheet readout noise 0.5 mV).

## Wavelength calibration

There is no Hamamatsu inspection sheet for this unit, so
`calibration/fit_wavelength.py` fits a weighted quadratic to:

- a green (532.0 nm) and a red (650 nm) laser pointer (`calibration/*_laser.csv`);
- the chlorophyll fluorescence peaks (685 / ~737 nm) in 13 leaf recordings,
  which constrain the curvature and the red end.

The fit:

    nm = 327.15 + 2.54396·p − 0.0024563·p²     (p = pixel 0..255)

Expect about ±1 nm near 532 nm and a few nm up to 745 nm. Below 532 nm the
curve is extrapolated, so a blue line would improve it: a violet (405 nm)
laser, or a fluorescent lamp (Hg lines at 404.7 / 435.8 / 546.1 nm).

`calibration/wavelength_calibration.json` is the reference. When the GUI
connects, it writes these coefficients to the device's flash if the device
holds different ones. Scripts can read them back with `get_wl_coeffs`.

Every sensor needs its own calibration. A calibration file applies to the
unit whose ID is in its `unit_uid` field (the ID is in `status` and in the
GUI). Save a new unit's fit as `calibration/wavelength_calibration_<name>.json`
with its `unit_uid`. A unit without its own file is shown with another unit's
calibration, marked "approximate", and that calibration is not written to it.

## Hardware notes (board v2)

- MCP3301 DOUT (5 V logic) is wired straight to GP16. The RP2040 is not 5 V
  tolerant. For the next revision, add a divider or a 3.3 V buffer; a
  ~2.2 kΩ series resistor is the quick fix.
- The video output reaches the ADC through 5 kΩ with no buffer amplifier (the
  datasheet recommends a CMOS op-amp buffer). This limits the ADC clock and
  probably adds noise.
- GP15 (the LED / excitation light) is not routed on the PCB and has to be
  wired separately.
- The schematic uses JII library symbols ("Library JII") that are embedded
  in the files but not included as a library.

## Development

```
python -m pytest                       # unit tests (simulated spectrometers, no hardware)
python tools/hw_check.py               # with a board connected
python -m c12666ma --simulate 3        # the GUI with simulated spectrometers
python calibration/fit_wavelength.py   # re-fit the calibration (the GUI copies it to the device)
```
