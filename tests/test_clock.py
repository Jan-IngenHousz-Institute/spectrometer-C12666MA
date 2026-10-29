import pytest

from c12666ma import device as device_mod
from c12666ma.device import Spectrometer

DRIFT_PPM = 40
OFFSET_US = 5_000_000


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0

    def perf_counter(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t + 1.7e9


def device_clock(t: float) -> float:
    return OFFSET_US + t * 1e6 * (1 + DRIFT_PPM * 1e-6)


def make_spec(ft: FakeTime, rtts) -> Spectrometer:
    spec = Spectrometer.__new__(Spectrometer)        # no serial port
    spec._clock = None
    spec._sync_points = []
    rtts = iter(rtts)

    def query_json(cmd):
        # slow pings are slow on the way out: the device reads its clock late, so
        # only the fastest ping's midpoint is accurate
        rtt = next(rtts)
        ft.t += 0.0005 + (rtt - 0.001)
        t_us = int(device_clock(ft.t))
        ft.t += 0.0005
        return {"t_us": t_us}

    spec.query_json = query_json
    return spec


def test_sync_clock_uses_fastest_ping_and_measures_drift(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(device_mod, "time", ft)
    spec = make_spec(ft, [0.004, 0.001, 0.006, 0.002, 0.003] * 10)

    assert spec.sync_clock() == pytest.approx(1.0)
    assert abs(spec.device_time_at(ft.t) - device_clock(ft.t)) < 50

    ft.t += 60                                        # a minute without re-sync:
    assert abs(spec.device_time_at(ft.t) - device_clock(ft.t)) > 2000   # 40 ppm = 2.4 ms

    spec.sync_clock()                                 # second sync measures the drift
    ft.t += 60
    assert abs(spec.device_time_at(ft.t) - device_clock(ft.t)) < 50

    t_dev = spec.device_time_at(ft.t)                 # device -> PC is the inverse
    assert spec.device_to_host_time(t_dev) == pytest.approx(ft.time(), abs=1e-4)
