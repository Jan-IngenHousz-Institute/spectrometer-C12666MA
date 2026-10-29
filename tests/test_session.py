import json
import time

import numpy as np
import pandas as pd
import pytest

from c12666ma import session as session_mod
from c12666ma import simulator
from c12666ma.session import AcquisitionSession


@pytest.fixture
def sess(monkeypatch):
    simulator.reset()
    simulator.create_units(3)
    monkeypatch.setattr(session_mod, "find_devices", lambda: [])     # no real hardware
    monkeypatch.setattr(session_mod, "RECONNECT_S", 0.5)
    s = AcquisitionSession(simulate=True)
    yield s
    s.close()
    simulator.reset()


def wait_for(cond, timeout=5.0):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def frames_by_device(s):
    out = {}
    for h, frame, _ in list(s.results):
        out.setdefault(h.uid, []).append(frame)
    return out


def test_detect_and_connect_all(sess):
    assert [i.port for i in sess.detect()] == ["SIM1", "SIM2", "SIM3"]
    assert sess.connect_all() == []
    assert [h.name for h in sess.connected] == ["SIM-1", "SIM-2", "SIM-3"]
    assert sess.detect() == []
    with pytest.raises(Exception):
        sess.connect("SIM1")                          # already connected


def test_min_period_and_centred_delays(sess):
    sess.connect_all()
    a, b, c = sess.connected
    sess.apply(a, integration_us=10_000)
    sess.apply(b, integration_us=40_000, n_avg=2)    # 80 ms window: the slowest
    sess.apply(c, integration_us=20_000)
    assert b.window_us == 80_000
    assert sess.min_period_us() == b.min_trigger_period_us + session_mod.HOST_MARGIN_US
    sess.set_period_us(1000)                          # too fast: clamped to the minimum
    assert sess.effective_period_us() == sess.min_period_us()
    d = sess.delays_us()
    assert d[b.uid] == 0 and d[a.uid] == 35_000 and d[c.uid] == 30_000
    for h in (a, b, c):                               # all windows share the same centre
        assert d[h.uid] + h.window_us / 2 == 40_000


def test_synchronised_frames_share_tick_and_centre(sess):
    sess.connect_all()
    a, b, c = sess.connected
    sess.apply(a, integration_us=10_000)
    sess.apply(b, integration_us=30_000)
    sess.apply(c, integration_us=20_000)
    sess.set_period_us(60_000)
    sess.start()
    assert sess.mode == "triggered"
    assert wait_for(lambda: all(h.frames >= 8 for h in (a, b, c)))
    sess.stop()
    by = frames_by_device(sess)
    common = set.intersection(*[{f.seq for f in fs} for fs in by.values()])
    assert len(common) >= 8
    for seq in sorted(common)[1:6]:
        centres = []
        for fs in by.values():
            f = next(f for f in fs if f.seq == seq)
            centres.append(f.timestamp + f.integration_us * f.n_avg / 2e6)
        assert max(centres) - min(centres) < 0.005   # centred within 5 ms
    starts = sorted(f.timestamp for f in by[b.uid])
    assert np.median(np.diff(starts)) == pytest.approx(0.060, abs=0.003)
    assert all(h.missed == 0 and h.busy == 0 for h in (a, b, c))


def test_single_device_fastest_is_continuous(sess):
    h = sess.connect("SIM1")
    sess.apply(h, integration_us=10_000)
    sess.start()
    assert sess.mode == "continuous"
    assert sess.min_period_us() == 10_000
    assert wait_for(lambda: h.frames >= 10)
    sess.set_period_us(50_000)                        # a set period switches to triggered
    assert sess.mode == "triggered"
    sess.stop()


def test_recording_files_and_rotation(sess, tmp_path):
    sess.connect_all()
    for h in sess.connected:
        sess.apply(h, integration_us=10_000)
    sess.set_period_us(50_000)
    rec = sess.start_recording(tmp_path, prefix="test run", note="unit test",
                               part_hours=1.0 / 3600)        # new part every second
    time.sleep(2.6)
    sess.stop_recording()
    files = sorted(p.name for p in rec.folder.glob("*_part*.csv"))
    assert len(files) == 9                                    # 3 devices x 3 parts
    assert {f.split("_part")[0] for f in files} == {f"SIM-{i}_{u}" for i, u in
                                                    zip((1, 2, 3), [h.uid[-6:] for h in sess.connected])}
    manifest = json.loads((rec.folder / "session.json").read_text())
    assert manifest["stopped"] and manifest["stop_reason"] == "stopped by user"
    assert manifest["mode"] == "triggered" and len(manifest["devices"]) == 3
    assert manifest["measurements_file"] == "measurements.csv"
    measurements = pd.read_csv(rec.folder / "measurements.csv", comment="#")
    assert len(measurements) >= 40
    assert measurements["seq"].is_monotonic_increasing
    assert measurements["t_s"].astype(float).is_monotonic_increasing
    assert (measurements["incoming_light_device"] == "SIM-1").all()
    assert (measurements["fluorescence_device"] == "SIM-2").all()
    assert "recording started" in (rec.folder / "events.log").read_text()
    seqs = []
    for h in sess.connected:
        parts = sorted(rec.folder.glob(f"{h.name}_*.csv"))
        df = pd.concat([pd.read_csv(p, comment="#") for p in parts])
        assert "rel_fluo_yield" not in df.columns
        spectra = df[df.kind == "spectrum"]
        assert len(spectra) == rec.recorders[h.uid].n_spectra
        assert spectra["t_s"].astype(float).is_monotonic_increasing
        seqs.append(set(spectra["seq"].astype(int)))
    assert len(set.intersection(*seqs)) >= 40                 # same ticks in every file


def test_unplugged_device_reconnects_and_counts_missed_ticks(sess):
    sess.connect_all()
    for h in sess.connected:
        sess.apply(h, integration_us=10_000)
    sess.set_period_us(50_000)
    sess.start()
    a = sess.connected[0]
    assert wait_for(lambda: a.frames >= 5)
    a.spec.simulate_unplug(1.0)
    assert wait_for(lambda: not a.connected, 2)
    assert wait_for(lambda: a.connected and a.reconnects == 1, 6)
    n = a.frames
    assert wait_for(lambda: a.frames >= n + 5, 3)
    sess.stop()
    assert a.missed >= 10                                     # ~1-2 s of 50 ms ticks
    assert all(h.missed == 0 for h in sess.connected[1:])
    log = "\n".join(line for _, line in sess.events)
    assert "connection lost" in log and "reconnected" in log


def test_timed_recording_stops_itself(sess, tmp_path):
    h = sess.connect("SIM1")
    sess.apply(h, integration_us=10_000)
    sess.set_period_us(50_000)
    rec = sess.start_recording(tmp_path, duration_s=1.0, include_yield=False)
    assert wait_for(lambda: sess.recording is None, 4)
    assert rec.stop_reason == "duration reached"


def test_late_triggers_run_back_to_back_until_the_queue_is_full(sess):
    h = sess.connect("SIM1")
    sess.apply(h, integration_us=20_000)
    h.spec.start_stream(on_frame=h.processor.on_frame, triggered=True)
    for k in range(18):            # all due now: one runs, 16 wait, the 18th is refused
        h.spec.trigger(k)
    assert wait_for(lambda: h.frames == 17, 3)
    time.sleep(0.1)
    assert h.frames == 17 and h.busy == 1
    frames = sorted((f for hh, f, _ in sess.results if hh is h), key=lambda f: f.seq)
    assert [f.seq for f in frames] == list(range(17))
    assert all(f.flags & 4 for f in frames[1:])               # FLAG_LATE
    gaps = [b.t_device_us - a.t_device_us for a, b in zip(frames, frames[1:])]
    assert gaps == pytest.approx([20_000 + 5_175] * 16, abs=50)   # back to back
    h.spec.stop_stream()
