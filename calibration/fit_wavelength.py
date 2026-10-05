"""Fit the pixel -> wavelength calibration of our C12666MA.

There is no Hamamatsu inspection sheet for this unit, so the calibration is
built from two sources:

1. LED spectra (LR1B/): four LEDs (red, yellow, green, blue) measured with
   both a reference spectrometer (Aseq LR1, "calibration Arduino *.txt", nm
   vs counts) and the C12666MA ("Calibration file.csv", rows in pairs:
   background, then LED; 2026-08-07, K. Tolsma). For each LED the centroid
   of the C12666MA spectrum (pixels) is matched to the centroid of the
   reference spectrum after broadening it to the C12666MA resolution.
2. Chlorophyll fluorescence of leaves under blue excitation (in-vivo
   emission peaks near 685 nm and 735-740 nm). The pixel positions are the
   mean of sub-pixel peak fits in 13 recordings of the student's PAM
   measurements (data/spec 2026-09-01 ... 2026-09-14, not in git). These
   extend the calibration beyond the reddest LED (636 nm).

The result is a weighted quadratic in the 0-based pixel index, written to
wavelength_calibration.json. Run:  python calibration/fit_wavelength.py
"""

import csv
import json
from datetime import date
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LR1B = HERE / "LR1B"
OUT = HERE / "wavelength_calibration.json"

LEDS = ["Red", "Yellow", "Green", "Blue"]          # row-pair order in the CSV
REF_FILES = {"Red": "Red", "Yellow": "Yel", "Green": "Gre", "Blue": "Blu"}
RESOLUTION_FWHM_NM = 11.0      # fitted to the LED line shapes (datasheet: 12 typ.)
LED_SIGMA_NM = 1.0

# (pixel, wavelength nm, uncertainty nm, label)
FLUORESCENCE_ANCHORS = [
    (167.91, 685.0, 2.0, "chlorophyll fluorescence F685 (13 leaf recordings, pixel sd 1.0)"),
    (199.52, 737.0, 5.0, "chlorophyll fluorescence F740 band (13 leaf recordings, pixel sd 1.7)"),
]
DEGREE = 2


def load_c12666ma_leds():
    with open(LR1B / "Calibration file.csv", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f, delimiter=";"))
    raw = [np.array([int(x) for x in r[1:257]], dtype=float) for r in rows[1:9]]
    return {name: raw[2 * i + 1] - raw[2 * i] for i, name in enumerate(LEDS)}


def centroid(x, y, frac=0.5):
    sel = y > frac * y.max()
    return float(np.sum(x[sel] * y[sel]) / np.sum(y[sel]))


def reference_centroid(name):
    d = np.loadtxt(LR1B / f"calibration Arduino {REF_FILES[name]}.txt")
    wl, v = d[:, 0], d[:, 1]
    v = v - np.median(v[(wl < 330) | (wl > 800)])          # remove offset
    step = np.mean(np.diff(wl))
    sigma = RESOLUTION_FWHM_NM / 2.3548 / step
    k = np.arange(-int(4 * sigma), int(4 * sigma) + 1)
    kernel = np.exp(-0.5 * (k / sigma) ** 2)
    broadened = np.convolve(v, kernel / kernel.sum(), mode="same")
    return centroid(wl, broadened)


def main():
    pixels = np.arange(256)
    spectra = load_c12666ma_leds()
    points = []
    for name in LEDS:
        points.append((centroid(pixels, spectra[name]), reference_centroid(name),
                       LED_SIGMA_NM, f"{name} LED"))
    points += FLUORESCENCE_ANCHORS
    p = np.array([pt[0] for pt in points])
    lam = np.array([pt[1] for pt in points])
    sig = np.array([pt[2] for pt in points])

    coeffs_desc = np.polyfit(p, lam, DEGREE, w=1 / sig)
    resid = lam - np.polyval(coeffs_desc, p)
    coeffs = list(coeffs_desc[::-1]) + [0.0] * (6 - DEGREE - 1)   # ascending, 6 terms

    for (px, nm, s, label), r in zip(points, resid):
        print(f"{label:70s} pixel {px:7.2f}  {nm:6.1f} nm  residual {r:+5.2f} nm")
    wl = np.polyval(coeffs_desc, pixels)
    print(f"coefficients (ascending): {coeffs[:DEGREE + 1]}")
    print(f"pixel 0 -> {wl[0]:.1f} nm, pixel 255 -> {wl[-1]:.1f} nm, rms residual "
          f"{np.sqrt(np.mean(resid ** 2)):.2f} nm")

    OUT.write_text(json.dumps({
        "description": "C12666MA pixel to wavelength: nm = sum(c[i] * p**i), p = 0-based pixel index",
        "coefficients": [float(c) for c in coeffs],
        "pixel_index_base": 0,
        "valid_range_nm": [470, 745],
        "accuracy_nm": "about +-3 nm within the valid range; extrapolated (about +-10 nm) outside",
        "method": "weighted quadratic fit, see calibration/fit_wavelength.py",
        "points": [{"pixel": round(px, 2), "nm": round(nm, 1), "sigma_nm": s,
                    "residual_nm": round(float(r), 2), "source": label}
                   for (px, nm, s, label), r in zip(points, resid)],
        "date": date.today().isoformat(),
    }, indent=2) + "\n")
    print(f"written {OUT}")


if __name__ == "__main__":
    main()
