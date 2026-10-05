"""Hardware check: speed, reliability and noise of a connected spectrometer.

    python tools/hw_check.py [PORT] [--seconds 30]

Leaves the device settings as they were (nothing is saved to flash).
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from c12666ma import Spectrometer  # noqa: E402


def collect(spec, seconds):
    frames = []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        try:
            frames.append(spec.frames.get(timeout=0.2))
        except Exception:
            pass
    return frames


def report(name, frames, spec):
    t = np.array([f.t_device_us for f in frames], dtype=float)
    dt = np.diff(t) / 1000
    x = np.array([f.counts for f in frames])
    print(f"{name}: {len(frames)} frames, {len(frames) / ((t[-1] - t[0]) / 1e6 + dt.mean() / 1000):.1f}/s, "
          f"period {dt.mean():.3f} ms (min {dt.min():.3f}, max {dt.max():.3f}), "
          f"dropped {spec.frames_dropped}, checksum errors {spec.parser.crc_errors}, "
          f"ADC errors {sum(f.adc_errors for f in frames)}")
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?")
    ap.add_argument("--seconds", type=float, default=30)
    args = ap.parse_args()
    with Spectrometer.open(args.port) as spec:
        st = spec.get_status()
        saved = (st["integration_us"], st["n_avg"], st["high_gain"], st["period_us"], st["led"])
        print(f"{st['fw']} on {spec.port}, sensor clock {st['clk_hz'] / 1000:.0f} kHz, "
              f"min integration {st['min_integration_us'] / 1000:.3f} ms")
        t0 = time.perf_counter()
        for _ in range(20):
            spec.query("get_integration")
        print(f"command round trip: {(time.perf_counter() - t0) / 20 * 1000:.1f} ms")
        try:
            spec.set_avg(1)
            spec.set_integration_us(20_000)
            t0 = time.perf_counter()
            spec.read_frame()
            print(f"single frame at 20 ms integration: {(time.perf_counter() - t0) * 1000:.0f} ms")

            spec.set_integration_us(0)                       # clamps to the minimum
            spec.start_stream(0)
            frames = collect(spec, args.seconds)
            load = spec.get_status()["cpu_load_pct"]
            spec.stop_stream()
            x = report(f"fastest ({frames[0].integration_us} us)", frames, spec)
            print(f"  device CPU load {load} %, dark {x.mean():.1f} counts, "
                  f"temporal noise {np.median(x.std(axis=0)):.2f} counts/pixel (median), "
                  f"worst pixel {x.std(axis=0).max():.2f}")

            spec.set_integration_us(10_000)
            spec.start_stream(0)
            lat = []
            for i in range(20):
                t0 = time.perf_counter()
                spec.set_led(i % 2 == 0)
                lat.append(time.perf_counter() - t0)
                time.sleep(0.05)
            frames = collect(spec, 1.0)
            spec.stop_stream()
            print(f"commands while streaming at 100/s: {np.mean(lat) * 1000:.1f} ms average, "
                  f"{np.max(lat) * 1000:.1f} ms max")

            spec.set_avg(4)
            spec.start_stream(250_000)
            frames = collect(spec, 3.1)
            spec.stop_stream()
            report("every 250 ms, 4 x 10 ms averaged", frames, spec)
        finally:
            spec.set_integration_us(saved[0])
            spec.set_avg(saved[1])
            spec.set_gain(bool(saved[2]))
            spec.set_period_us(saved[3])
            spec.set_led(bool(saved[4]))


if __name__ == "__main__":
    main()
