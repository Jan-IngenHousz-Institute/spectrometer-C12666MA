"""Fit the pixel -> wavelength calibration of our C12666MA.

There is no Hamamatsu inspection sheet for this unit, so the calibration is
built from:

1. Laser pointers (green_laser.csv, red_laser.csv; 2026-10-06, firmware v2).
   Spectra exported from the GUI plot: one row per pixel, y = counts. The
   green pointer is a frequency-doubled Nd laser at 532.0 nm. The red one is
   a diode laser labelled 650 nm; diodes vary by a few nm, hence +-5 nm.
2. Chlorophyll fluorescence of leaves under blue excitation (in-vivo
   emission peaks near 685 nm and 735-740 nm). The pixel positions are the
   mean of sub-pixel peak fits in 13 recordings of K. Tolsma's PAM
   measurements (data/spec 2026-09-01 ... 2026-09-14, not in git). With
   only two laser lines a quadratic would be unconstrained; these extend the
   calibration to the red end.

The result is a weighted quadratic in the 0-based pixel index, written to
wavelength_calibration.json. The GUI copies it to the device when it
connects. Run:  python calibration/fit_wavelength.py
"""

import json
from datetime import date
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE / "wavelength_calibration.json"

# (file, wavelength nm, uncertainty nm, label)
LASERS = [
    ("green_laser.csv", 532.0, 0.3, "green laser pointer (532.0 nm DPSS)"),
    ("red_laser.csv", 650.0, 5.0, "red laser pointer (650 nm diode)"),
]

# (pixel, wavelength nm, uncertainty nm, label)
FLUORESCENCE_ANCHORS = [
    (167.91, 685.0, 2.5, "chlorophyll fluorescence F685 (13 leaf recordings, pixel sd 1.0)"),
    (199.52, 737.0, 5.0, "chlorophyll fluorescence F740 band (13 leaf recordings, pixel sd 1.7)"),
]
DEGREE = 2


def laser_pixel(filename):
    """Centroid (pixels) of the part of the peak above half its height."""
    counts = np.loadtxt(HERE / filename, delimiter=",", skiprows=1)[:, 1]
    signal = counts - np.median(counts)
    sel = signal > 0.5 * signal.max()
    pixels = np.arange(len(counts), dtype=float)
    return float(np.sum(pixels[sel] * signal[sel]) / np.sum(signal[sel]))


def main():
    points = [(laser_pixel(f), nm, s, label) for f, nm, s, label in LASERS]
    points += FLUORESCENCE_ANCHORS
    p = np.array([pt[0] for pt in points])
    lam = np.array([pt[1] for pt in points])
    sig = np.array([pt[2] for pt in points])

    coeffs_desc = np.polyfit(p, lam, DEGREE, w=1 / sig)
    resid = lam - np.polyval(coeffs_desc, p)
    coeffs = list(coeffs_desc[::-1]) + [0.0] * (6 - DEGREE - 1)   # ascending, 6 terms

    for (px, nm, s, label), r in zip(points, resid):
        print(f"{label:70s} pixel {px:7.2f}  {nm:6.1f} +- {s:3.1f} nm  residual {r:+5.2f} nm")
    wl = np.polyval(coeffs_desc, np.arange(256))
    print(f"coefficients (ascending): {coeffs[:DEGREE + 1]}")
    print(f"pixel 0 -> {wl[0]:.1f} nm, pixel 255 -> {wl[-1]:.1f} nm")

    OUT.write_text(json.dumps({
        "description": "C12666MA pixel to wavelength: nm = sum(c[i] * p**i), p = 0-based pixel index",
        "coefficients": [float(c) for c in coeffs],
        "pixel_index_base": 0,
        "valid_range_nm": [520, 745],
        "accuracy_nm": "about +-1 nm near 532 nm, a few nm up to 745 nm; below 532 nm the "
                       "quadratic is extrapolated (no reference line there yet)",
        "method": "weighted quadratic fit, see calibration/fit_wavelength.py",
        "points": [{"pixel": round(px, 2), "nm": round(nm, 1), "sigma_nm": s,
                    "residual_nm": round(float(r), 2), "source": label}
                   for (px, nm, s, label), r in zip(points, resid)],
        "date": date.today().isoformat(),
    }, indent=2) + "\n")
    print(f"written {OUT}")


if __name__ == "__main__":
    main()
