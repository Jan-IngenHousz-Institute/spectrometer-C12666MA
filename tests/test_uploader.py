import json
import time

import numpy as np
import pytest

from c12666ma import uploader as up
from c12666ma.analysis import DarkReference
from c12666ma.openjii import OpenJIIError, UploadRecord
from c12666ma.protocol import FLAG_LATE, Frame
from c12666ma.uploader import Uploader, dark_row, frame_row


class FakeClient:
    """Duck-typed OpenJIIClient: offline until told otherwise."""

    def __init__(self):
        self.online = True
        self.uploads = {}            # upload_id -> UploadRecord
        self.calls = []
        self.outcome = "completed"

    def find_table(self, experiment_id, table_name):
        return "tbl-1" if self.uploads else None

    def upload_files(self, experiment_id, table_name, table_id, paths, upload_id=None):
        self.calls.append([p.name for p in paths])
        if not self.online:
            raise OpenJIIError("cannot reach api")
        uid = f"up-{len(self.uploads) + 1}"
        self.uploads[uid] = UploadRecord(uid, "running", "tbl-1", table_name, None, None)
        return {"uploadId": uid, "uploadTableId": "tbl-1", "uploadTableName": table_name,
                "files": [{"fileName": p.name, "filePath": "/x/" + p.name} for p in paths]}

    def upload_status(self, experiment_id, table_name, upload_ids):
        out = {}
        for uid in upload_ids:
            rec = self.uploads.get(uid)
            if rec is None:
                continue
            if rec.status == "running":
                rec.status = self.outcome
                rec.row_count = 5
                rec.error_code = None if self.outcome == "completed" else "no_rows"
            out[uid] = rec
        return out


def make_frame(seq, flags=0):
    return Frame(seq=seq, t_device_us=seq * 1_000_000, integration_us=100_000,
                 integration_clocks=20_000, clk_hz=200_000, n_avg=2, high_gain=True, led=False,
                 flags=flags, adc_errors=0, counts=np.full(256, 320.5), timestamp=1.7e9 + seq)


DEVICE = {"device_uid": "E664B012CF234E35", "device_name": "Fluor", "port": "COM42", "firmware": "v2.1"}
WL = np.linspace(327.15, 816.1, 256)


def test_frame_row_is_self_describing():
    row = frame_row(make_frame(7, FLAG_LATE), device=DEVICE, session_id="s1", t0=1.7e9,
                    wavelengths=WL, wl_coeffs=[327.15, 2.54, -0.00246], note="leaf 3")
    assert row["kind"] == "spectrum" and row["tick"] == 7 and row["t_s"] == 7.0
    assert row["timestamp_utc"].endswith("Z") and row["device_name"] == "Fluor"
    assert row["integration_ms"] == 100.0 and row["n_avg"] == 2 and row["gain"] == "high"
    assert row["flag_names"] == ["late"] and row["wavelength_coeffs"] == [327.15, 2.54, -0.00246]
    assert len(row["counts"]) == 256 and row["counts"][0] == 320.5 and len(row["wavelength_nm"]) == 256
    json.dumps(row, allow_nan=False)                 # strict JSON


def test_dark_row():
    dark = DarkReference.from_frames([make_frame(i) for i in range(3)])
    row = dark_row(dark, device=DEVICE, session_id="s1", wavelengths=WL, wl_coeffs=[1, 2])
    assert row["kind"] == "dark" and row["n_frames"] == 3 and len(row["counts_mean"]) == 256


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(up, "RETRY_MIN_S", 0.2)
    monkeypatch.setattr(up, "RETRY_MAX_S", 0.5)
    monkeypatch.setattr(up, "POLL_S", 0.1)


def wait(cond, timeout=5.0):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def write_rows(u, n, start=0):
    for i in range(start, start + n):
        u.writer.write(frame_row(make_frame(i), device=DEVICE, session_id="s", t0=1.7e9,
                                 wavelengths=WL, wl_coeffs=[1.0]))


def test_batches_upload_and_get_confirmed(fast, tmp_path):
    client = FakeClient()
    u = Uploader(client, "exp", "spectra", tmp_path / "uploads", log=lambda t: None)
    u.start()
    write_rows(u, 3)
    assert u.ledger.batches("open") == ["batch_000001.ndjson"]
    u.rotate_and_upload()
    assert wait(lambda: u.ledger.batches("completed") == ["batch_000001.ndjson"])
    assert client.calls == [["batch_000001.ndjson"]]
    lines = (tmp_path / "uploads" / "batch_000001.ndjson").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["tick"] == 0
    u.rotate_and_upload()                      # nothing new: no empty upload
    time.sleep(0.3)
    assert client.calls == [["batch_000001.ndjson"]]
    u.stop(wait_s=1)


def test_offline_keeps_batches_and_sends_them_together(fast, tmp_path):
    client = FakeClient()
    client.online = False
    u = Uploader(client, "exp", "spectra", tmp_path / "uploads", log=lambda t: None)
    u.start()
    for k in range(3):
        write_rows(u, 2, start=k * 2)
        u.rotate_and_upload()
        time.sleep(0.25)
    assert wait(lambda: not u.online and len(client.calls) >= 1)
    assert u.ledger.batches("pending") == [f"batch_00000{k}.ndjson" for k in (1, 2, 3)]
    client.online = True
    assert wait(lambda: u.ledger.batches("completed") == [f"batch_00000{k}.ndjson" for k in (1, 2, 3)])
    assert client.calls[-1] == ["batch_000001.ndjson", "batch_000002.ndjson", "batch_000003.ndjson"]
    assert u.online
    u.stop(wait_s=1)


def test_resume_after_restart(fast, tmp_path):
    client = FakeClient()
    client.online = False
    u = Uploader(client, "exp", "spectra", tmp_path / "uploads", log=lambda t: None)
    write_rows(u, 4)
    u.writer.rotate()
    write_rows(u, 1)                            # an open batch at the time of the crash
    u.writer.flush()
    del u

    u2 = Uploader(client, "exp", "spectra", tmp_path / "uploads", log=lambda t: None)
    # the batch left open by the crash is queued with its complete rows
    assert u2.ledger.batches("pending") == ["batch_000001.ndjson", "batch_000002.ndjson"]
    assert u2.ledger.batches("open") == []
    client.online = True
    u2.start()
    write_rows(u2, 1)                           # new rows go to a fresh batch, not the stale one
    u2.rotate_and_upload()
    assert wait(lambda: set(u2.ledger.batches("completed"))
                == {"batch_000001.ndjson", "batch_000002.ndjson", "batch_000003.ndjson"})
    u2.stop(wait_s=1)
    names = sorted(p.name for p in (tmp_path / "uploads").glob("batch_*.ndjson"))
    assert names == ["batch_000001.ndjson", "batch_000002.ndjson", "batch_000003.ndjson"]


def test_open_batch_is_recovered_after_a_crash(fast, tmp_path):
    client = FakeClient()
    client.online = False
    u = Uploader(client, "exp", "spectra", tmp_path / "uploads", log=lambda t: None)
    write_rows(u, 3)
    u.writer.flush()
    path = tmp_path / "uploads" / "batch_000001.ndjson"
    with open(path, "ab") as f:
        f.write(b'{"kind":"spectrum","tick":99,"cou')      # the power went while writing
    del u

    client.online = True
    u2 = Uploader(client, "exp", "spectra", tmp_path / "uploads", log=lambda t: None)
    assert u2.ledger.batches("pending") == ["batch_000001.ndjson"]
    info = u2.ledger.data["batches"]["batch_000001.ndjson"]
    assert info["rows"] == 3 and info["recovered"]
    assert len(path.read_text().splitlines()) == 3        # the broken line is gone
    u2.start()
    assert wait(lambda: u2.ledger.batches("completed") == ["batch_000001.ndjson"])
    u2.stop(wait_s=1)


def test_new_table_not_listed_yet_is_retried_without_going_offline(fast, tmp_path, monkeypatch):
    monkeypatch.setattr(up, "TABLE_WAIT_S", 0.2)
    client = FakeClient()
    orig = client.upload_files
    refusals = {"n": 2}

    def upload_files(experiment_id, table_name, table_id, paths, upload_id=None):
        if table_id is not None and refusals["n"] > 0:
            refusals["n"] -= 1
            raise OpenJIIError("upload failed (404): No upload table with id 'tbl-1' in this experiment")
        return orig(experiment_id, table_name, table_id, paths, upload_id)
    client.upload_files = upload_files
    logs = []
    u = Uploader(client, "exp", "spectra", tmp_path / "uploads", log=logs.append)
    u.start()
    write_rows(u, 1)
    u.rotate_and_upload()                       # creates the table
    assert wait(lambda: u.ledger.batches("completed") == ["batch_000001.ndjson"])
    write_rows(u, 1)
    u.rotate_and_upload()                       # append: refused twice, then accepted
    assert wait(lambda: u.ledger.batches("completed") == ["batch_000001.ndjson", "batch_000002.ndjson"])
    assert u.online and refusals["n"] == 0
    assert any("not listed on the platform yet" in l for l in logs)
    u.stop(wait_s=1)


def test_platform_failure_is_recorded_not_retried(fast, tmp_path):
    client = FakeClient()
    client.outcome = "failed"
    u = Uploader(client, "exp", "spectra", tmp_path / "uploads", log=lambda t: None)
    u.start()
    write_rows(u, 2)
    u.rotate_and_upload()
    assert wait(lambda: u.ledger.batches("failed") == ["batch_000001.ndjson"])
    assert u.ledger.data["batches"]["batch_000001.ndjson"]["error"] == "no_rows"
    assert len(client.calls) == 1
    u.stop(wait_s=1)


def test_interrupted_transfer_is_resent(fast, tmp_path):
    client = FakeClient()
    u = Uploader(client, "exp", "spectra", tmp_path / "uploads", log=lambda t: None)
    orig = client.upload_status

    def status(experiment_id, table_name, upload_ids):
        out = orig(experiment_id, table_name, upload_ids)
        for rec in out.values():
            if rec.upload_id == "up-1" and rec.status == "completed":
                rec.status, rec.error_code = "failed", "transfer_interrupted"
        return out
    client.upload_status = status
    u.start()
    write_rows(u, 2)
    u.rotate_and_upload()
    assert wait(lambda: client.calls == [["batch_000001.ndjson"], ["batch_000001.ndjson"]])
    assert wait(lambda: u.ledger.batches("completed") == ["batch_000001.ndjson"])
    u.stop(wait_s=1)
