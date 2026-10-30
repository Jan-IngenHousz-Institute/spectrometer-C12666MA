import json

import pytest

from c12666ma.openjii import Environment, OpenJIIClient, OpenJIIError, Settings, parse_experiment_ref
from tests.fake_openjii import EXPERIMENT, VALID_KEY, FakeOpenJII


@pytest.fixture
def server():
    with FakeOpenJII() as s:
        yield s


def client_for(server, key=VALID_KEY):
    return OpenJIIClient(Environment("test", server.url, server.url), key)


def test_key_validation(server):
    assert client_for(server).validate_key()["name"] == "Test User"
    with pytest.raises(OpenJIIError, match="rejected"):
        client_for(server, "jii_wrong").validate_key()
    with pytest.raises(OpenJIIError, match="no API key"):
        client_for(server, "").validate_key()


def test_unreachable_server_is_an_error():
    c = OpenJIIClient(Environment("x", "http://127.0.0.1:9", "http://127.0.0.1:9"), VALID_KEY)
    with pytest.raises(OpenJIIError, match="cannot reach"):
        c.list_experiments()


@pytest.mark.parametrize("shape", ["list", "items"])
def test_list_experiments_both_shapes(shape):
    with FakeOpenJII(list_shape=shape) as server:
        exps = client_for(server).list_experiments()
    assert [e.name for e in exps] == ["Test leaves", "Other"]
    assert exps[0].id == EXPERIMENT["id"]


def test_capabilities_and_table_lookup(server):
    c = client_for(server)
    assert c.capabilities(EXPERIMENT["id"]) == {"canContribute": True, "canManage": True}
    assert c.find_table(EXPERIMENT["id"], "spectra") is None


def test_upload_creates_then_appends(server, tmp_path):
    c = client_for(server)
    a = tmp_path / "batch_000001.ndjson"
    a.write_text('{"tick":1,"counts":[1,2]}\n{"tick":2,"counts":[3,4]}\n')
    b = tmp_path / "batch_000002.ndjson"
    b.write_text('{"tick":3,"counts":[5,6]}\n')

    first = c.upload_files(EXPERIMENT["id"], "spectra", None, [a])
    assert first["uploadTableName"] == "spectra" and first["uploadTableId"]
    assert [f["fileName"] for f in first["files"]] == ["batch_000001.ndjson"]
    fields = dict(server.received[0]["fields"])
    assert fields["targetKind"] == "new" and fields["targetName"] == "spectra"
    assert fields["sourceKind"] == "ndjson" and fields["uploadId"] == first["uploadId"]
    # the backend resolves the target on the first file: fields must come first
    body_order = [n for n, _ in server.received[0]["fields"]]
    assert body_order and server.received[0]["files"][0][2].startswith(b'{"tick":1')

    table_id = c.find_table(EXPERIMENT["id"], "spectra")
    assert table_id == first["uploadTableId"]
    second = c.upload_files(EXPERIMENT["id"], "spectra", table_id, [b])
    assert dict(server.received[1]["fields"])["targetKind"] == "existing"
    assert dict(server.received[1]["fields"])["uploadTableId"] == table_id

    status = c.upload_status(EXPERIMENT["id"], "spectra", {first["uploadId"], second["uploadId"]})
    assert status[first["uploadId"]].status in ("running", "completed")
    status = c.upload_status(EXPERIMENT["id"], "spectra", {first["uploadId"]})
    assert status[first["uploadId"]].finished and status[first["uploadId"]].row_count == 2


def test_upload_without_manage_access_is_refused(tmp_path):
    with FakeOpenJII(can_manage=False) as server:
        f = tmp_path / "b.ndjson"
        f.write_text('{"a":1}\n')
        with pytest.raises(OpenJIIError, match="management access"):
            client_for(server).upload_files(EXPERIMENT["id"], "spectra", None, [f])


def test_bad_table_name(server, tmp_path):
    f = tmp_path / "b.ndjson"
    f.write_text('{"a":1}\n')
    for bad in ("1abc", "_x", "a-b", "a" * 64):
        with pytest.raises(OpenJIIError, match="invalid table name"):
            client_for(server).upload_files(EXPERIMENT["id"], bad, None, [f])


def test_settings_cache_roundtrip(tmp_path):
    path = tmp_path / "openjii.json"
    s = Settings(path)
    s.set_api_key("prod", "jii_abc")
    s.set("experiment", {"id": "x", "name": "y"})
    s.save()
    s2 = Settings(path)
    assert s2.api_key("prod") == "jii_abc" and s2.get("experiment")["name"] == "y"
    s2.forget_api_key("prod")
    s2.save()
    assert Settings(path).api_key("prod") == ""
    assert json.loads(path.read_text())["experiment"] == {"id": "x", "name": "y"}


def test_parse_experiment_ref():
    uid = EXPERIMENT["id"]
    assert parse_experiment_ref(uid) == uid
    assert parse_experiment_ref(f"https://openjii.org/en-US/platform/experiments/{uid}/data?x=1") == uid
    assert parse_experiment_ref("  My experiment ") == "My experiment"
