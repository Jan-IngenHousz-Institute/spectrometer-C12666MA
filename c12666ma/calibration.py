"""Pixel -> wavelength conversion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

N_PIXELS = 256
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "calibration" / "wavelength_calibration.json"


@dataclass
class WavelengthCalibration:
    coefficients: list[float]      # nm = sum(c[i] * p**i), p = 0-based pixel index
    source: str

    @property
    def wavelengths(self) -> np.ndarray:
        p = np.arange(N_PIXELS, dtype=float)
        return np.polyval(list(reversed(self.coefficients)), p)

    @property
    def calibrated(self) -> bool:
        return self.source != "uncalibrated"

    def same_as(self, coeffs) -> bool:
        """True if `coeffs` are these coefficients, allowing for the device
        storing them as 32-bit floats."""
        a = np.zeros(6)
        b = np.zeros(6)
        a[:len(self.coefficients)] = self.coefficients
        b[:len(coeffs or [])] = coeffs or []
        return bool(np.allclose(a, b, rtol=1e-5, atol=1e-12))

    @classmethod
    def from_file(cls, path: Path = DEFAULT_FILE) -> "WavelengthCalibration":
        data = json.loads(Path(path).read_text())
        return cls([float(c) for c in data["coefficients"]], f"file {Path(path).name}")

    @classmethod
    def uncalibrated(cls) -> "WavelengthCalibration":
        """Nominal 340-780 nm spread linearly over the pixels."""
        return cls([340.0, 440.0 / (N_PIXELS - 1)], "uncalibrated")

    @property
    def approximate(self) -> bool:
        return self.source.startswith("approximate")

    @classmethod
    def for_device(cls, uid: str, device_coeffs=None) -> tuple["WavelengthCalibration", bool]:
        """Calibration for the unit with flash unique ID `uid`, and whether it
        should be written to the device (each sensor needs its own):
        1. a calibration file made for this unit ("unit_uid" in the JSON);
        2. a file without "unit_uid" (made before units were told apart);
        3. coefficients already stored on the device;
        4. another unit's file, marked approximate and never written to the device."""
        files = []
        for path in sorted(DEFAULT_FILE.parent.glob("wavelength_calibration*.json")):
            try:
                files.append((path, json.loads(path.read_text())))
            except (OSError, ValueError):
                pass
        uid = (uid or "").upper()
        for path, data in files:
            if str(data.get("unit_uid", "")).upper() == uid and uid:
                return cls([float(c) for c in data["coefficients"]], f"file {path.name}"), True
        for path, data in files:
            if "unit_uid" not in data:
                return cls([float(c) for c in data["coefficients"]], f"file {path.name}"), True
        if device_coeffs and any(device_coeffs[1:]):
            return cls([float(c) for c in device_coeffs], "device"), False
        if files:
            path, data = files[0]
            return cls([float(c) for c in data["coefficients"]],
                       f"approximate: {path.name} was made for unit {data['unit_uid']}"), False
        return cls.uncalibrated(), False

    @classmethod
    def best(cls, device_coeffs=None) -> "WavelengthCalibration":
        """Device-stored coefficients if set, else the repository file, else
        the nominal range."""
        if device_coeffs and any(device_coeffs[1:]):
            return cls([float(c) for c in device_coeffs], "device")
        if DEFAULT_FILE.exists():
            return cls.from_file()
        return cls.uncalibrated()
