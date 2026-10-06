import math

import numpy as np
import pytest

from c12666ma.analysis import DarkReference, Region, YieldStatus, compute_yield
from c12666ma.calibration import WavelengthCalibration
from c12666ma.protocol import Frame

WL = WavelengthCalibration.from_file().wavelengths
INCIDENT = Region("incident_light", 440, 480)
FLUO = Region("fluorescence", 680, 760)
DARK_LEVEL = 320.0
NOISE = 10.0


def make_frame(counts, seq=0, high_gain=True, clocks=2000):
    return Frame(seq=seq, t_device_us=seq * 10_000, integration_us=clocks * 5,
                 integration_clocks=clocks, clk_hz=200_000, n_avg=1, high_gain=high_gain,
                 led=False, flags=0, adc_errors=0, counts=np.asarray(counts, float),
                 timestamp=1_700_000_000.0 + seq)


@pytest.fixture
def dark():
    rng = np.random.default_rng(1)
    frames = [make_frame(DARK_LEVEL + rng.normal(0, NOISE, 256), i) for i in range(20)]
    return DarkReference.from_frames(frames)


def spectrum(incident=0.0, fluo=0.0):
    """Dark level plus a flat signal of `incident` / `fluo` counts per pixel."""
    y = np.full(256, DARK_LEVEL)
    y[INCIDENT.mask(WL)] += incident
    y[FLUO.mask(WL)] += fluo
    return y


def test_calibration_file_is_monotonic_and_in_range():
    assert np.all(np.diff(WL) > 0)
    assert 300 < WL[0] < 360 and 780 < WL[-1] < 860
    assert INCIDENT.mask(WL).sum() > 10 and FLUO.mask(WL).sum() > 20


def test_calibration_matches_device_float32_copy():
    cal = WavelengthCalibration.from_file()
    device = [float(np.float32(c)) for c in cal.coefficients]      # what the Pico stores
    assert cal.same_as(device)
    assert cal.same_as(device[:3])                                  # trailing zeros optional
    assert not cal.same_as([c * 1.001 for c in device])
    assert not cal.same_as([0.0] * 6) and not cal.same_as(None)


def test_dark_noise_estimate(dark):
    assert dark.n_frames == 20
    assert np.median(dark.std) == pytest.approx(NOISE, rel=0.25)


def test_yield_is_ratio_of_region_sums(dark):
    res = compute_yield(make_frame(spectrum(incident=400, fluo=40)), dark, WL, INCIDENT, FLUO)
    assert res.status == YieldStatus.OK
    expected = (40 * FLUO.mask(WL).sum()) / (400 * INCIDENT.mask(WL).sum())
    assert res.value == pytest.approx(expected, rel=0.05)
    assert not res.saturated


def test_incident_without_fluorescence_gives_near_zero(dark):
    res = compute_yield(make_frame(spectrum(incident=400)), dark, WL, INCIDENT, FLUO)
    assert res.status == YieldStatus.OK
    assert abs(res.value) < 0.01


def test_fluorescence_without_incident_warns(dark):
    res = compute_yield(make_frame(spectrum(fluo=100)), dark, WL, INCIDENT, FLUO)
    assert res.status == YieldStatus.FLUO_WITHOUT_INCIDENT
    assert res.warning and math.isnan(res.value)


def test_no_signal_clamps_to_zero(dark):
    rng = np.random.default_rng(2)
    res = compute_yield(make_frame(DARK_LEVEL + rng.normal(0, NOISE, 256)), dark, WL,
                        INCIDENT, FLUO)
    assert res.status == YieldStatus.NO_SIGNAL and res.value == 0.0


def test_threshold_scales_with_k(dark):
    weak = make_frame(spectrum(incident=8))          # ~ 6 sigma over the region
    assert compute_yield(weak, dark, WL, INCIDENT, FLUO, k=3).status == YieldStatus.OK
    assert compute_yield(weak, dark, WL, INCIDENT, FLUO, k=50).status == YieldStatus.NO_SIGNAL


def test_needs_matching_dark(dark):
    frame = make_frame(spectrum(incident=400, fluo=40), clocks=4000)   # other integration
    res = compute_yield(frame, dark, WL, INCIDENT, FLUO)
    assert res.status == YieldStatus.NO_DARK and math.isnan(res.value)
    assert compute_yield(frame, None, WL, INCIDENT, FLUO).status == YieldStatus.NO_DARK


def test_saturation_flag(dark):
    y = spectrum(incident=3000, fluo=40)
    res = compute_yield(make_frame(y), dark, WL, INCIDENT, FLUO)
    assert res.saturated and res.status == YieldStatus.OK
    low_gain = make_frame(spectrum(incident=1700, fluo=40), high_gain=False)
    assert compute_yield(low_gain, None, WL, INCIDENT, FLUO).saturated


def test_dark_from_single_frame_estimates_noise():
    rng = np.random.default_rng(3)
    d = DarkReference.from_frames([make_frame(DARK_LEVEL + rng.normal(0, NOISE, 256))])
    assert np.median(d.std) == pytest.approx(NOISE, rel=0.3)


def test_dark_rejects_mixed_settings():
    with pytest.raises(ValueError):
        DarkReference.from_frames([make_frame(spectrum()), make_frame(spectrum(), clocks=4000)])
