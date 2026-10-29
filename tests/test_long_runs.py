import numpy as np
import pandas as pd

from c12666ma.analysis import DarkReference
from c12666ma.calibration import WavelengthCalibration
from c12666ma.protocol import Frame
from c12666ma.recorder import RotatingRecorder

WL = WavelengthCalibration.from_file().wavelengths


def make_frame(seq, value=320.0):
    return Frame(seq=seq, t_device_us=seq * 1_000_000, integration_us=10_000,
                 integration_clocks=2000, clk_hz=200_000, n_avg=1, high_gain=True, led=False,
                 flags=0, adc_errors=0, counts=np.full(256, value), timestamp=1.7e9 + seq)


def test_plot_history_stays_bounded():
    from c12666ma.gui import GrowingSeries      # needs a Qt binding, like the GUI
    s = GrowingSeries(max_points=1000)
    for i in range(10_000):
        s.append(float(i), float(i))
    assert s.n <= 1000
    assert s.x[0] == 0 and s.x[-1] == 9999       # still spans the whole run
    assert np.all(np.diff(s.x) > 0)


def test_every_part_repeats_the_latest_dark(tmp_path):
    clock = [0.0]
    rec = RotatingRecorder(tmp_path, "dev", WL, {"note": "x"}, include_yield=False,
                           part_hours=1.0, t0_epoch=1.7e9, clock=lambda: clock[0])
    rec.write_dark(DarkReference.from_frames([make_frame(i) for i in range(3)]))
    for seq in range(10):
        clock[0] = seq * 1000.0                    # 1000 s apart: a new part every 4 frames
        rec.write_frame(make_frame(seq))
    rec.close()
    assert [p.name for p in rec.paths] == ["dev_part001.csv", "dev_part002.csv", "dev_part003.csv"]
    seqs = []
    for p in rec.paths:
        df = pd.read_csv(p, comment="#")
        assert list(df.kind[:3]) == ["wavelength_nm", "dark", "dark_std"]
        seqs += list(df[df.kind == "spectrum"].seq.astype(int))
        assert "# part:" in p.read_text()
    assert seqs == list(range(10)) and rec.n_spectra == 10
