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


# Recording to openJII from the terminal

`python -m c12666ma.record` records with every C12666MA spectrometer plugged
into the laptop, one spectrum per unit at a fixed interval, for hours or days.
Each unit can have its own gain, integration time and averages. Every spectrum
is saved on the laptop first and then uploaded to an experiment on
[openJII](https://openjii.org), with its timestamp and all the settings it was
taken with.

No GUI is needed: the script asks its questions in the terminal, and remembers
the answers for the next run.

- [Before you start](#before-you-start)
- [First recording, step by step](#first-recording-step-by-step)
- [Leaving it running for days](#leaving-it-running-for-days)
- [Stopping, and finishing the uploads](#stopping-and-finishing-the-uploads)
- [What is saved](#what-is-saved)
- [Starting without questions](#starting-without-questions)
- [Troubleshooting](#troubleshooting)

## Before you start

1. **Python** 3.12 or 3.13, with the packages of this repository:

   ```
   pip install -r requirements.txt
   ```

   Run every command below from the repository folder (the one with this file).

2. **Firmware v2.1 on every Pico.** Copy `firmware/main.py` to the Pico as
   `main.py` (see the main [README](README.md#firmware)). The script refuses
   older firmware.

3. **A name and a calibration per unit.** Units are recognised by their unique
   ID and shown by the name stored on the device (set it once in the GUI,
   `python -m c12666ma`). A unit without its own wavelength calibration still
   records, with a warning: its wavelengths are those of another unit (see
   [Wavelength calibration](README.md#wavelength-calibration)).

4. **An openJII account and an experiment** you are a member of. To upload into
   an existing table you need *contribute* access. To create a new table, which
   the first upload does, you need *manage* access (experiment admin).

5. **Close the GUI, Thonny or anything else** that has the spectrometers' COM
   ports open. A port can only be used by one program at a time.

## First recording, step by step

Plug in the spectrometers and run:

```
python -m c12666ma.record
```

The script goes through four steps. Press Enter to accept the value in
`[brackets]`.

**1. Sign in to openJII.** The first time, the script opens
*Account > API keys* on openJII in the browser. Create a personal key there,
copy it and paste it into the terminal; it is not shown while you paste. The
key is checked and stored in your Windows profile, so later runs skip this
step.

```
Sign in to openJII (prod): create a personal API key at
  https://openjii.org/en-US/platform/account/api-keys
Open that page in the browser? (y/n) [y]:
Paste the API key (hidden):
openJII (prod): signed in as John Doe
```

**2. Choose the experiment and the table.** Pick a number from the list of your
active experiments, or choose the last entry and paste the experiment's link
from the browser. The spectra go into an *upload table* of that experiment;
keep the default name unless you want separate tables.

```
Active experiments you are a member of:
   1. test-experiment  (5db72a32-1c50-4d11-b12c-0152cc65b5e6)
   2. another one (paste its id or URL)
Record into experiment: 1
Upload table name in the experiment [c12666ma_spectra]:
Experiment: test-experiment  (https://openjii.org/en-US/platform/experiments/5db72a32-...)
Table: c12666ma_spectra (will be created)
```

**3. Set each spectrometer.** Every unit found on USB is listed, then asked for
its gain (`high` or `low`), integration time in milliseconds, number of
averages (1 to 16) and whether its LED is on. The units can have different
settings.

```
2 spectrometer(s):
  Fluor      COM42  id E664B012CF234E35  gain high  integration 100 ms  averages 1  LED off
  Top        COM7   id E663589863441E2E  gain high  integration 50 ms  averages 2  LED off

Fluor on COM42 (id E664B012CF234E35)
  gain (high/low) [high]:
  integration time in ms [100.0]:
  averages (1-16) [1]:
  LED on during the recording? (y/n) [n]:
```

**4. Timing and files.**

| Question | What it means |
|---|---|
| Period between spectra in seconds | All units take one spectrum together at every period. The minimum shown depends on the slowest unit: its averages × integration time, plus about 12 ms. |
| Upload to openJII every | How often the new spectra are sent. 15 minutes is a good default; the minimum is 1 minute. |
| Local folder for the recording | Where the files are written. Each recording gets its own subfolder. Default: `C12666MA data` in your user folder. |
| Duration | For example `36h` or `3d`. `0` records until you press Ctrl-C. |
| Note for the file header | Free text saved with every spectrum, e.g. the plant or the chamber. |
| Take a dark spectrum first? | Optional. Cover the sensors when asked; 20 dark frames per unit are recorded and uploaded as `dark` rows. |

The recording then starts. Every minute a status line shows what has been
measured and uploaded:

```
Recording to C:\Users\you\C12666MA data\test-experiment_2026-10-28_22-16-18
Period 60 s, upload every 15m, until Ctrl-C

22:17:18 · tick 7 · Fluor 6 · Top 6 · 12 spectra on disk · uploads 0 confirmed, 1 waiting
2026-10-28T22:17:20+01:00  upload: sent 1 batch(es), 12 rows, upload 7d8098da
2026-10-28T22:18:24+01:00  upload 7d8098da: 12 rows queryable on openJII
```

The spectra appear in the experiment's data on openJII about a minute after
each upload. A new table is listed in the experiment a few minutes after its
first upload. Until then the next uploads wait and retry every minute; the
terminal says *the table is not listed on the platform yet*, which is normal.

## Leaving it running for days

The script keeps the laptop awake while it records, reconnects units that were
unplugged or reset, and starts a new local file every 24 hours. Windows can
still interrupt it, so before a long run:

- **Connect the charger.**
- **Closing the lid:** set *Control Panel > Power Options > Choose what closing
  the lid does* to *Do nothing* when plugged in, or leave the lid open.
- **Windows Update:** pause updates for the length of the run (*Settings >
  Windows Update > Pause updates*). An update restart stops the recording.
- **USB power saving:** in *Power Options > Change plan settings > Change
  advanced power settings*, set *USB settings > USB selective suspend setting*
  to *Disabled*.
- **Don't select text in the terminal window.** In the classic Windows
  console, a selection pauses the program until you press Esc.
- **Disk space:** the recording stops cleanly, with a note in the log, when
  less than 500 MB is left on the disk.

Network outages don't matter. The spectra keep being written to disk, the
status line shows *OFFLINE, keeping local*, and everything that was missed is
uploaded once the connection is back.

Two units at one spectrum per minute use about this much disk space:

| | Per day |
|---|---|
| Local CSV files | about 4 MB |
| Upload batches, kept in `uploads\` | about 10 MB |

## Stopping, and finishing the uploads

Press **Ctrl-C once**. The script sends the spectra not uploaded yet, waits up
to a few minutes for openJII to confirm them, and then tells you whether
anything is left:

```
Stopping (Ctrl-C)...
Uploading the last batch and waiting for the platform's confirmation...
Uploads: {'completed': 96}
Local files: C:\Users\you\C12666MA data\test-experiment_2026-10-28_22-16-18  (2880 spectra)
```

If the network was down, or the laptop crashed or restarted during a
recording, send what is left later with the folder of that recording:

```
python -m c12666ma.record upload "C:\Users\you\C12666MA data\test-experiment_2026-10-28_22-16-18"
```

This also recovers the spectra written just before a crash. Running it on a
recording that is already fully uploaded does nothing, so it is always safe.

A restarted laptop does not resume the recording by itself. Start a new
recording; it goes into a new folder, and you finish the old folder's uploads
with the command above.

## What is saved

### On the laptop

Each recording folder holds:

```
Fluor_234E35_part001.csv   spectra of one unit (<name>_<end of its ID>), a new part every 24 h
Top_441E2E_part001.csv
session.json               units, settings, calibrations, number of spectra, why it stopped
events.log                 connections, reconnects, uploads, errors
uploads\                   the batches sent to openJII and their status (ledger.json)
```

The CSV format is described in the main
[README](README.md#recording-format). Open a part with
`pandas.read_csv(path, comment="#")`.

### On openJII

Each spectrum is one row in the experiment's upload table, by default
`c12666ma_spectra`. Every row describes itself completely:

| Column | Content |
|---|---|
| `kind` | `spectrum`, or `dark` for a dark reference |
| `timestamp_utc`, `timestamp_local`, `unix_time` | start of the integration |
| `t_s` | seconds since the recording started, the same time base for all units |
| `tick` | measurement number; spectra with the same tick were taken together |
| `session_id` | the recording folder name |
| `note` | the note entered at the start |
| `device_uid`, `device_name`, `port`, `firmware` | which unit took it |
| `integration_ms`, `integration_us`, `n_avg` | integration time and number of averages |
| `gain`, `high_gain` | `high` or `low` |
| `led` | whether the unit's LED was on |
| `flags`, `flag_names`, `adc_errors` | quality flags of the frame, e.g. `late` or `led_changed` |
| `clk_hz`, `n_pixels` | sensor clock and number of pixels (256) |
| `wavelength_coeffs`, `wavelength_nm` | the unit's calibration, and the wavelength of every pixel |
| `counts` | the 256 raw values, not dark-subtracted |

Dark rows hold `counts_mean` and `counts_std` instead of `counts`, plus the
number of frames averaged in `n_frames`.

## Starting without questions

Every question has a flag. With `--yes` the script asks nothing and uses the
flags, then the answers remembered from the last run. This is how to start it
from a batch file or from Windows Task Scheduler.

```
python -m c12666ma.record --yes --experiment "test-experiment" --table c12666ma_spectra ^
    --device Fluor=high,100,1 --device Top=low,50,4,on --period 60 --upload-every 15m ^
    --duration 5d --folder "D:\spectra" --note "chamber 2"
```

| Flag | Meaning |
|---|---|
| `--experiment` | experiment ID, link, or exact name |
| `--table` | upload table name (default `c12666ma_spectra`) |
| `--device NAME=gain,ms,averages[,on]` | settings of one unit, by name or ID; repeat for each unit |
| `--all gain,ms,averages[,on]` | the same settings for every unit |
| `--period` | seconds between spectra |
| `--upload-every` | e.g. `15m`, `1h` |
| `--folder`, `--prefix` | where to record, and the start of the folder name (default: the experiment name) |
| `--duration` | e.g. `36h`, `3d`; without it, until Ctrl-C |
| `--note` | note saved with every spectrum |
| `--dark [N]` | take N dark frames per unit first (default 20). With `--yes` it measures straight away, without asking you to cover the sensors. |
| `--part-hours` | hours per local file part (default 24) |
| `--no-upload` | record on the laptop only |
| `--env dev` | use the openJII development platform instead of openjii.org |

`--yes` needs a stored API key. Run `python -m c12666ma.record login` once
first.

**Task Scheduler:** create a task with the action *Start a program*. Set
*Program* to the full path of `python.exe`, *Arguments* to everything after
`python` in the command above, and *Start in* to the repository folder.

## Troubleshooting

| Message | What to do |
|---|---|
| `no spectrometer found on USB` | Check the cables, and close the GUI or Thonny if they are open. |
| `firmware too old` | Copy `firmware/main.py` to that Pico. |
| `warning: Top has no calibration of its own` | The unit records with another unit's wavelengths. Calibrate it when possible. |
| `your account cannot contribute data` | Ask the experiment's admin to add you as a member. |
| `table ... does not exist ... (needs manage access)` | Ask an admin to make the first upload, or use a table name that already exists. |
| `no valid openJII API key` | No key is stored, or openJII no longer accepts it. Run `python -m c12666ma.record login`. |
| `the table is not listed on the platform yet` | Normal for a new table during its first minutes. The upload retries by itself. |
| `OFFLINE, keeping local` in the status line | No connection to openJII. Nothing to do: the uploads catch up later. |
| `LOST` next to a unit in the status line | The unit was unplugged or reset. It is reconnected automatically; the gap shows as missed ticks. |
| `upload ... FAILED on the platform` | openJII refused that batch and it is not resent. The spectra are still in the local CSV files and in `uploads\`. Report the upload ID shown. |

**The stored key and answers** are in `%APPDATA%\c12666ma\openjii.json`. To use
another key or account, run `python -m c12666ma.record login`. If the laptop
is shared or lost, delete the key on openJII under *Account > API keys*.

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
