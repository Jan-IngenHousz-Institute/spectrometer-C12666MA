import json

import pytest

from c12666ma import download
from c12666ma.download import parse_type, typed
from c12666ma.openjii import Settings
from tests.fake_openjii import EXPERIMENT, FIRST_ROW_ID, VALID_KEY, FakeOpenJII

SPECTRA_COLUMNS = [("kind", "STRING"), ("tick", "BIGINT"), ("high_gain", "BOOLEAN"), ("t_s", "DOUBLE"),
                   ("counts", "ARRAY<DOUBLE>"), ("flag_names", "ARRAY<STRING>"),
                   ("contributor", "STRUCT<id: STRING, name: STRING, avatar: STRING>"),
                   ("note", "STRING")]
SPECTRA = [{"kind": "spectrum", "tick": i, "high_gain": i % 2 == 0, "t_s": 60.5 * i,
            "counts": [300.0 + i, 301.0, 302.5], "flag_names": ["late"] if i == 3 else [],
            "contributor": {"id": "u1", "name": "Test User", "avatar": None},
            "note": None if i == 0 else "chamber 2"} for i in range(5)]
PROBE = ([("kind", "STRING")], [{"kind": "probe"}])


def with_ids(rows):
    return [{"id": FIRST_ROW_ID + 7919 * i, **r} for i, r in enumerate(rows)]


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "RETRY_S", 0)
    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set_api_key("custom", VALID_KEY)
    s.save()
    return path


def run(server, settings_path, *args):
    return download.main(["--yes", "--api-url", server.url, "--settings-file", str(settings_path),
                          "--experiment", EXPERIMENT["id"], *args])


def table_id(server, name):
    return next(i for i, t in server.data_tables.items() if t["name"] == name)


def test_downloads_every_table_into_one_json(settings_path, tmp_path):
    out = tmp_path / "out" / "exp.json"
    with FakeOpenJII(data_tables={"c12666ma_spectra": (SPECTRA_COLUMNS, SPECTRA),
                                  "c12666ma_probe": PROBE}) as server:
        assert run(server, settings_path, "-o", str(out)) == 0
    doc = json.loads(out.read_text())
    assert doc["experiment"]["id"] == EXPERIMENT["id"] and doc["experiment"]["name"] == "Test leaves"
    assert doc["environment"] == "custom" and doc["downloaded_at"]
    assert [t["name"] for t in doc["tables"]] == ["c12666ma_spectra", "c12666ma_probe"]
    spectra = doc["tables"][0]
    assert spectra["rows"] == with_ids(SPECTRA)           # text from the API back to its types
    assert spectra["row_count"] == 5 and spectra["type"] == "upload"
    assert spectra["columns"][:2] == [{"name": "id", "type": "BIGINT"}, {"name": "kind", "type": "STRING"}]
    assert doc["tables"][1]["rows"] == with_ids(PROBE[1])
    assert {q["orderBy"] for q in server.page_requests} == {"id"}
    assert [p.name for p in out.parent.iterdir()] == ["exp.json"]   # no temporary file left


def test_rows_uploaded_during_the_download_are_not_repeated(settings_path, tmp_path, monkeypatch):
    monkeypatch.setattr(download, "PAGE_SIZES", (2, 1))
    out = tmp_path / "exp.json"
    with FakeOpenJII(data_tables={"spectra": (SPECTRA_COLUMNS, SPECTRA)}) as server:
        tid = table_id(server, "spectra")
        # an upload after the first page puts a row before it: the later pages shift by one
        server.on_page = lambda t, page: page == 1 and server.insert_row(tid, SPECTRA[0])
        assert run(server, settings_path, "-o", str(out)) == 0
        assert [q["page"] for q in server.page_requests] == ["1", "2", "3", "4"]
    rows = json.loads(out.read_text())["tables"][0]["rows"]
    assert rows == with_ids(SPECTRA)


def test_a_page_too_large_is_read_in_smaller_pages(settings_path, tmp_path):
    out = tmp_path / "exp.json"
    with FakeOpenJII(data_tables={"spectra": (SPECTRA_COLUMNS, SPECTRA)}, max_page_rows=250) as server:
        assert run(server, settings_path, "-o", str(out)) == 0
        assert [q["pageSize"] for q in server.page_requests] == ["1000", "500", "250"]
    assert json.loads(out.read_text())["tables"][0]["rows"] == with_ids(SPECTRA)


def test_a_failed_read_is_tried_again(settings_path, tmp_path):
    out = tmp_path / "exp.json"
    with FakeOpenJII(data_tables={"spectra": (SPECTRA_COLUMNS, SPECTRA)}, fail_reads=2) as server:
        assert run(server, settings_path, "-o", str(out)) == 0
    assert json.loads(out.read_text())["tables"][0]["row_count"] == 5
    with FakeOpenJII(data_tables={"spectra": (SPECTRA_COLUMNS, SPECTRA)}, fail_reads=3) as server:
        assert run(server, settings_path, "-o", str(tmp_path / "failed.json")) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["exp.json", "settings.json"]


def test_table_filter_and_empty_table(settings_path, tmp_path):
    with FakeOpenJII(data_tables={"c12666ma_spectra": (SPECTRA_COLUMNS, SPECTRA),
                                  "empty": (SPECTRA_COLUMNS, []), "other": PROBE}) as server:
        assert run(server, settings_path, "--table", "empty", "--table", "c12666ma_spectra",
                   "-o", str(tmp_path)) == 0
        assert table_id(server, "other") not in {q["tableName"] for q in server.page_requests}
    out = next(tmp_path.glob("Test_leaves_openjii_*.json"))           # a folder gets the default name
    doc = json.loads(out.read_text())
    assert [(t["name"], t["row_count"], len(t["rows"])) for t in doc["tables"]] == \
        [("empty", 0, 0), ("c12666ma_spectra", 5, 5)]
    assert len(doc["tables"][0]["columns"]) == len(SPECTRA_COLUMNS) + 1


def test_unknown_table_stops(settings_path, tmp_path):
    with FakeOpenJII(data_tables={"spectra": (SPECTRA_COLUMNS, SPECTRA)}) as server:
        assert run(server, settings_path, "--table", "nope", "-o", str(tmp_path / "x.json")) == 2
        assert not server.page_requests


def test_refuses_without_key(tmp_path):
    with FakeOpenJII(data_tables={"spectra": (SPECTRA_COLUMNS, SPECTRA)}) as server:
        code = download.main(["--yes", "--api-url", server.url, "--experiment", EXPERIMENT["id"],
                              "--settings-file", str(tmp_path / "s.json"), "-o", str(tmp_path / "x.json")])
    assert code == 2


def test_parse_type():
    assert parse_type("BIGINT") == "BIGINT" and parse_type("decimal(10,2)") == "DECIMAL"
    assert parse_type("ARRAY<DOUBLE>") == ("ARRAY", "DOUBLE")
    assert parse_type("MAP<STRING, ARRAY<INT>>") == ("MAP", "STRING", ("ARRAY", "INT"))
    # the annotations column of every platform table, as production names it
    annotations = parse_type(
        "ARRAY<STRUCT<id: STRING, rowId: STRING, type: STRING, content: STRUCT<text: STRING, "
        "flagType: STRING> NOT NULL, createdBy: STRING, createdByName: STRING, createdAt: TIMESTAMP, "
        "updatedAt: TIMESTAMP>>")
    fields = annotations[1][1]
    assert annotations[0] == "ARRAY" and list(fields)[:4] == ["id", "rowId", "type", "content"]
    assert fields["content"] == ("STRUCT", {"text": "STRING", "flagType": "STRING"})
    assert fields["updatedAt"] == "TIMESTAMP"
    assert parse_type("STRUCT<`odd name`: INT>") == ("STRUCT", {"odd name": "INT"})


def test_typed():
    assert typed("469637188033094612", "BIGINT") == 469637188033094612
    assert typed("327.1498214969004", "DOUBLE") == 327.1498214969004
    assert typed("true", "BOOLEAN") is True and typed("false", "BOOLEAN") is False
    assert typed('["1.0","2.5"]', ("ARRAY", "DOUBLE")) == [1.0, 2.5]
    assert typed('{"a":"3"}', ("MAP", "STRING", "INT")) == {"a": 3}
    assert typed('{"n":"7","x":null}', ("STRUCT", {"n": "INT"})) == {"n": 7, "x": None}
    assert typed('{"k":[1,2]}', "VARIANT") == {"k": [1, 2]}
    assert typed("2026-11-02 14:11:35", "TIMESTAMP") == "2026-11-02 14:11:35"
    assert typed(None, "BIGINT") is None and typed(5, "BIGINT") == 5
    assert typed("n/a", "BIGINT") == "n/a" and typed("[oops", ("ARRAY", "INT")) == "[oops"
