import numpy as np
import pandas as pd

from c12666ma.analysis import DarkReference, Region, compute_yield
from c12666ma.calibration import WavelengthCalibration
from c12666ma.protocol import Frame
from c12666ma.recorder import Recorder

WL = WavelengthCalibration.from_file().wavelengths


def make_frame(seq, counts, n_avg=1):
    return Frame(seq=seq, t_device_us=5_000_000 + seq * 10_000, integration_us=10_000,
                 integration_clocks=2000, clk_hz=200_000, n_avg=n_avg, high_gain=True,
                 led=bool(seq % 2), flags=0, adc_errors=0, counts=np.asarray(counts, float),
                 timestamp=1_700_000_000.0 + seq * 0.01)


def read(path):
    return pd.read_csv(path, comment="#")


def test_recording_with_yield(tmp_path):
    dark = DarkReference.from_frames([make_frame(i, np.full(256, 320.0) + i % 3) for i in range(5)])
    rec = Recorder(tmp_path / "r.csv", WL, {"note": "unit test"}, include_yield=True)
    rec.write_dark(dark)
    inc, flu = Region("i", 440, 480), Region("f", 680, 760)
    for seq in range(3):
        counts = np.full(256, 321.0)
        counts[inc.mask(WL)] += 500
        counts[flu.mask(WL)] += 50 * seq
        frame = make_frame(seq, counts)
        rec.write_frame(frame, compute_yield(frame, dark, WL, inc, flu))
    rec.close()

    text = (tmp_path / "r.csv").read_text()
    assert "# note: unit test" in text
    df = read(tmp_path / "r.csv")
    assert list(df["kind"]) == ["wavelength_nm", "dark", "dark_std", "spectrum", "spectrum", "spectrum"]
    px = [f"px{i}" for i in range(256)]
    np.testing.assert_allclose(df.loc[0, px].astype(float), WL, atol=0.01)
    spectra = df[df.kind == "spectrum"]
    assert list(spectra["seq"].astype(int)) == [0, 1, 2]
    np.testing.assert_allclose(spectra["t_s"].astype(float), [0, 0.01, 0.02])
    assert spectra["rel_fluo_yield"].astype(float).is_monotonic_increasing
    assert set(spectra["yield_status"]) == {"ok"}
    first_incident_px = px[int(np.argmax(inc.mask(WL)))]
    assert float(df.loc[3, first_incident_px]) == 821.0
    assert float(df.loc[3, "px0"]) == 321.0
    assert spectra["timestamp"].str.contains(r"T\d\d:\d\d:\d\d\.\d{3}").all()


def test_recording_without_yield_and_averaged_counts(tmp_path):
    rec = Recorder(tmp_path / "s.csv", WL, {}, include_yield=False)
    rec.write_frame(make_frame(0, np.full(256, 300.25), n_avg=4))
    rec.close()
    df = read(tmp_path / "s.csv")
    assert "rel_fluo_yield" not in df.columns
    assert df.shape[1] == 9 + 256
    assert float(df.loc[1, "px10"]) == 300.25
