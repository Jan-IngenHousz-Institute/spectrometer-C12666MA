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
openJII (prod): signed in as Ludovico Caracciolo
```

**2. Choose the experiment and the table.** Pick a number from the list of your
active experiments, or choose the last entry and paste the experiment's link
from the browser. The spectra go into an *upload table* of that experiment;
keep the default name unless you want separate tables.

```
Active experiments you are a member of:
   1. spectrometer-Kjell  (5db72a32-1c50-4d81-b12c-0352cc65b5e6)
   2. another one (paste its id or URL)
Record into experiment: 1
Upload table name in the experiment [c12666ma_spectra]:
Experiment: spectrometer-Kjell  (https://openjii.org/en-US/platform/experiments/5db72a32-...)
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
Recording to C:\Users\you\C12666MA data\spectrometer-Kjell_2026-10-28_22-16-18
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
Local files: C:\Users\you\C12666MA data\spectrometer-Kjell_2026-10-28_22-16-18  (2880 spectra)
```

If the network was down, or the laptop crashed or restarted during a
recording, send what is left later with the folder of that recording:

```
python -m c12666ma.record upload "C:\Users\you\C12666MA data\spectrometer-Kjell_2026-10-28_22-16-18"
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
python -m c12666ma.record --yes --experiment "spectrometer-Kjell" --table c12666ma_spectra ^
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
