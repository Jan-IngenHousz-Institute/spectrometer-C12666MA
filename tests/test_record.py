import json

import pytest

from c12666ma import record, simulator
from c12666ma import session as session_mod
from c12666ma.openjii import Settings
from c12666ma.uploader import Uploader
from tests.fake_openjii import EXPERIMENT, VALID_KEY, FakeOpenJII


def test_parse_duration():
    assert record.parse_duration("90m") == 5400 and record.parse_duration("3d") == 3 * 86400
    assert record.parse_duration("36h") == 36 * 3600 and record.parse_duration("45") == 45
    assert record.parse_duration("0") == 0 and record.parse_duration("") == 0
    with pytest.raises(ValueError):
        record.parse_duration("soon")
    assert record.fmt_duration(900) == "15m" and record.fmt_duration(86400) == "1d"
    assert record.fmt_duration(90) == "90s" and record.fmt_duration(0) == "0"


def test_parse_device_spec():
    name, s = record.parse_device_spec("Fluor=high,100,4,on")
    assert name == "Fluor" and s == {"high_gain": True, "integration_us": 100_000, "n_avg": 4, "led": True}
    assert record.parse_device_spec("Top=low,5.175,1")[1]["integration_us"] == 5175
    with pytest.raises(ValueError):
        record.parse_device_spec("Top=medium,10,1")


@pytest.fixture
def no_hardware(monkeypatch):
    monkeypatch.setattr(session_mod, "find_devices", lambda: [])
    simulator.reset()
    yield
    simulator.reset()


def test_unattended_local_recording(no_hardware, tmp_path):
    settings = tmp_path / "settings.json"
    code = record.main(["--yes", "--no-upload", "--simulate", "2", "--all", "high,10,1",
                        "--period", "0.1", "--duration", "2s", "--folder", str(tmp_path / "data"),
                        "--prefix", "run", "--note", "unit test", "--settings-file", str(settings)])
    assert code == 0
    folders = list((tmp_path / "data").iterdir())
    assert len(folders) == 1 and folders[0].name.startswith("run_")
    manifest = json.loads((folders[0] / "session.json").read_text())
    assert manifest["stop_reason"] == "duration reached" and len(manifest["devices"]) == 2
    assert all(d["spectra"] >= 10 for d in manifest["devices"])
    assert all(d["settings"]["integration_us"] == 10_000 for d in manifest["devices"])
    remembered = json.loads(settings.read_text())
    assert remembered["period_s"] == 0.1 and len(remembered["devices"]) == 2


def test_unattended_recording_with_upload(no_hardware, tmp_path):
    settings_path = tmp_path / "settings.json"
    s = Settings(settings_path)
    s.set_api_key("custom", VALID_KEY)
    s.save()
    with FakeOpenJII() as server:
        code = record.main(["--yes", "--api-url", server.url, "--experiment", EXPERIMENT["id"],
                            "--table", "spectra", "--simulate", "2", "--all", "low,10,2",
                            "--period", "0.1", "--upload-every", "1s", "--duration", "3s",
                            "--folder", str(tmp_path / "data"), "--settings-file", str(settings_path)])
        assert code == 0
        folder = next((tmp_path / "data").iterdir())
        ledger = json.loads((folder / "uploads" / "ledger.json").read_text())
        batches = ledger["batches"]
        assert len(batches) >= 2
        assert all(b["status"] in ("completed", "uploaded") for b in batches.values())
        assert server.tables == {"spectra": server.uploads[0]["uploadTableId"]}
        assert dict(server.received[0]["fields"])["targetKind"] == "new"
        if len(server.received) > 1:
            assert dict(server.received[1]["fields"])["targetKind"] == "existing"
        rows = [json.loads(l) for _, _, content in server.received[0]["files"]
                for l in content.decode().splitlines()]
        assert rows and rows[0]["kind"] == "spectrum" and rows[0]["gain"] == "low"
        assert rows[0]["n_avg"] == 2 and len(rows[0]["counts"]) == 256
        assert rows[0]["session_id"] == folder.name
        assert {r["device_name"] for r in rows} == {"SIM-1", "SIM-2"}
        target = json.loads((folder / "uploads" / "target.json").read_text())
        assert target["experiment_id"] == EXPERIMENT["id"] and target["table_name"] == "spectra"
        # every uploaded row is in the local CSVs and vice versa
        import pandas as pd
        uploaded = sorted((json.loads(l)["device_name"], json.loads(l)["tick"])
                          for rec in server.received for _, _, content in rec["files"]
                          for l in content.decode().splitlines())
        local = []
        for p in folder.glob("SIM-*_part*.csv"):
            df = pd.read_csv(p, comment="#")
            name = p.name.split("_")[0]
            local += [(name, int(s)) for s in df[df.kind == "spectrum"].seq]
        assert uploaded == sorted(local)
        assert json.loads(settings_path.read_text())["experiment"]["name"] == "Test leaves"


def test_unattended_refuses_without_key(no_hardware, tmp_path):
    with FakeOpenJII() as server:
        code = record.main(["--yes", "--api-url", server.url, "--experiment", EXPERIMENT["id"],
                            "--simulate", "1", "--all", "high,10,1", "--period", "0.1",
                            "--duration", "1s", "--folder", str(tmp_path / "data"),
                            "--settings-file", str(tmp_path / "s.json")])
    assert code == 2


def test_resume_upload_command(tmp_path, monkeypatch):
    """A recording whose uploads never went out (no network) is sent later."""
    from c12666ma import uploader as up
    from c12666ma.openjii import Environment, OpenJIIClient
    monkeypatch.setattr(up, "POLL_S", 0.1)
    settings_path = tmp_path / "settings.json"
    s = Settings(settings_path)
    s.set_api_key("custom", VALID_KEY)
    s.save()
    folder = tmp_path / "data" / "run_2026"
    dead = OpenJIIClient(Environment("custom", "http://127.0.0.1:9", "http://127.0.0.1:9"), VALID_KEY)
    u = Uploader(dead, EXPERIMENT["id"], "spectra", folder / "uploads", log=lambda t: None)
    for i in range(3):
        u.writer.write({"kind": "spectrum", "tick": i, "counts": [1, 2, 3]})
    u.writer.rotate()
    u.writer.write({"kind": "spectrum", "tick": 3, "counts": [4]})
    u.writer.flush()                                   # still open when the laptop died
    with FakeOpenJII() as server:
        (folder / "uploads" / "target.json").write_text(json.dumps({
            "environment": "custom", "api_url": server.url, "experiment_id": EXPERIMENT["id"],
            "experiment_name": "Test leaves", "table_name": "spectra"}))
        code = record.main(["upload", str(folder), "--yes", "--settings-file", str(settings_path)])
        assert code == 0
        assert len(server.received) == 1
        sent = [fn for _, fn, _ in server.received[0]["files"]]
        assert sent == ["batch_000001.ndjson", "batch_000002.ndjson"]   # open batch recovered too
    ledger = json.loads((folder / "uploads" / "ledger.json").read_text())
    assert ledger["batches"]["batch_000001.ndjson"]["status"] == "completed"
    assert ledger["batches"]["batch_000002.ndjson"]["status"] == "completed"
    assert ledger["batches"]["batch_000002.ndjson"]["recovered"]
