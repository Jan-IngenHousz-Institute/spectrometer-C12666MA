"""openJII client: uploads recordings into an experiment's upload table, and
downloads an experiment's tables.

Authentication: openJII has no password or device-code flow for desktop
tools; a personal API key (``jii_...``, created in the web app under
Platform > Account > API keys) sent as ``x-api-key`` authenticates every
``/api/v1`` endpoint. The key is validated with ``GET /api/v1/auth/get-session``
and cached in the user's profile (``%APPDATA%\\c12666ma\\openjii.json``).

Ingestion: ``POST /api/v1/experiments/{id}/data/uploads`` (multipart) puts
files into a named *upload table* of the experiment. The first upload creates
the table (needs *manage* access), later ones append (*contribute* access).
Each row of an NDJSON file becomes one row of the table, nested values kept.
Every upload starts a Databricks job; ``GET .../data/uploads`` reports when
its rows are queryable, so uploads are batched (see uploader.py).

Download: ``GET /api/v1/experiments/{id}/tables`` lists the tables of the
experiment's Data page, and ``GET .../data?tableName=&page=&pageSize=`` reads
one of them up to 1000 rows at a time. The platform's file export (``POST
.../data/exports``, the Download button of the page) is not used: it runs a
Databricks job per table, and in production (November 2026) an export whose
job could not record its result is never listed as completed or failed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

USER_AGENT = "c12666ma-record"
UPLOAD_SOURCE_KIND = "ndjson"
TABLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


class OpenJIIError(RuntimeError):
    """API or authentication failure, with a message meant for the terminal."""


class PageTooLarge(OpenJIIError):
    """A page of a table holds more data than one read returns."""


@dataclass(frozen=True)
class Environment:
    key: str
    api_url: str
    web_url: str

    @property
    def api_keys_url(self) -> str:
        return f"{self.web_url}/en-US/platform/account/api-keys"

    def experiment_url(self, experiment_id: str) -> str:
        return f"{self.web_url}/en-US/platform/experiments/{experiment_id}"


ENVIRONMENTS = {
    "prod": Environment("prod", "https://api.openjii.org", "https://openjii.org"),
    "dev": Environment("dev", "https://api.dev.openjii.org", "https://dev.openjii.org"),
}


@dataclass
class Experiment:
    id: str
    name: str
    status: str = ""


@dataclass
class UploadRecord:
    upload_id: str
    status: str                  # receiving, queued, pending, running, completed, failed
    table_id: Optional[str]
    table_name: Optional[str]
    row_count: Optional[int]
    error_code: Optional[str]
    rejected_files: list = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed")


@dataclass
class Table:
    identifier: str              # 'raw_data', a macro id or an upload table id
    name: str                    # as the experiment's Data page shows it
    type: str                    # static, macro, upload
    rows: int


# ----------------------------------------------------------------- settings
def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "c12666ma"


class Settings:
    """What the recorder remembers between runs: API keys per environment, the
    last experiment, table name, folder and acquisition settings."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or config_dir() / "openjii.json"
        self.data: dict = {}
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)       # POSIX; Windows profiles are private already
        except OSError:
            pass

    def api_key(self, env: str) -> str:
        return (self.data.get("api_keys") or {}).get(env, "")

    def set_api_key(self, env: str, key: str) -> None:
        self.data.setdefault("api_keys", {})[env] = key

    def forget_api_key(self, env: str) -> None:
        (self.data.get("api_keys") or {}).pop(env, None)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value


# ------------------------------------------------------------------- client
class OpenJIIClient:
    def __init__(self, env: Environment, api_key: str, timeout: float = 60.0) -> None:
        self.env = env
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json",
                                      "x-api-key": self.api_key})

    # ------------------------------------------------------------ plumbing
    def _request(self, method: str, path: str, **kw) -> tuple[int, object]:
        kw.setdefault("timeout", self.timeout)
        try:
            resp = self._session.request(method, f"{self.env.api_url}{path}", **kw)
        except requests.RequestException as exc:
            raise OpenJIIError(f"cannot reach {self.env.api_url}: {exc}") from exc
        try:
            payload = resp.json() if resp.content.strip() else None
        except ValueError:
            payload = {"raw": resp.text[:400]}
        return resp.status_code, payload

    @staticmethod
    def _error_text(payload) -> str:
        if isinstance(payload, dict):
            for key in ("message", "error", "detail", "raw"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
                if isinstance(value, dict) and isinstance(value.get("message"), str):
                    return value["message"]
        return json.dumps(payload)[:300] if payload is not None else "(no body)"

    # ---------------------------------------------------------------- auth
    def validate_key(self) -> dict:
        """The signed-in user, or OpenJIIError. better-auth answers 200 with a
        null body for an unauthenticated call, which is a failure here."""
        if not self.api_key:
            raise OpenJIIError("no API key")
        status, payload = self._request("GET", "/api/v1/auth/get-session", timeout=20)
        if status != 200 or not (isinstance(payload, dict) and payload.get("user")):
            raise OpenJIIError(f"the API key was rejected ({status}). Create one at "
                               f"{self.env.api_keys_url}")
        return payload["user"]

    # --------------------------------------------------------- experiments
    def list_experiments(self) -> list[Experiment]:
        """Active experiments the user is a member of (any shape the API returns)."""
        status, payload = self._request(
            "GET", "/api/v1/experiments",
            params={"filter": "member", "status": "active", "pageSize": 100})
        if status != 200:
            raise OpenJIIError(f"listing experiments failed ({status}): {self._error_text(payload)}")
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise OpenJIIError(f"unexpected experiments reply: {str(payload)[:200]}")
        return [Experiment(e["id"], e.get("name") or e["id"], e.get("status") or "")
                for e in items if isinstance(e, dict) and e.get("id")]

    def get_experiment(self, experiment_id: str) -> Experiment:
        status, payload = self._request("GET", f"/api/v1/experiments/{experiment_id}")
        if status != 200 or not isinstance(payload, dict):
            raise OpenJIIError(f"experiment {experiment_id}: {status} {self._error_text(payload)}")
        return Experiment(payload["id"], payload.get("name") or payload["id"],
                          payload.get("status") or "")

    def capabilities(self, experiment_id: str) -> dict:
        """{'canContribute': bool, 'canManage': bool, ...} for the signed-in user."""
        status, payload = self._request("GET", f"/api/v1/experiments/{experiment_id}/access")
        if status != 200 or not isinstance(payload, dict):
            raise OpenJIIError(f"experiment access {experiment_id}: {status} "
                               f"{self._error_text(payload)}")
        return payload.get("capabilities") or {}

    # ------------------------------------------------------------- uploads
    def list_uploads(self, experiment_id: str, table_name: Optional[str] = None) -> list[UploadRecord]:
        params = {"uploadTableName": table_name} if table_name else {}
        status, payload = self._request(
            "GET", f"/api/v1/experiments/{experiment_id}/data/uploads", params=params)
        if status != 200 or not isinstance(payload, dict):
            raise OpenJIIError(f"listing uploads failed ({status}): {self._error_text(payload)}")
        out = []
        for u in payload.get("uploads") or []:
            if isinstance(u, dict) and u.get("uploadId"):
                out.append(UploadRecord(u["uploadId"], u.get("status") or "", u.get("uploadTableId"),
                                        u.get("uploadTableName"), u.get("rowCount"),
                                        u.get("errorCode"), u.get("rejectedFiles") or []))
        return out

    def find_table(self, experiment_id: str, table_name: str) -> Optional[str]:
        """The upload table's id if an upload into it exists, else None."""
        for u in self.list_uploads(experiment_id, table_name):
            if u.table_id and u.table_name == table_name:
                return u.table_id
        return None

    def upload_files(self, experiment_id: str, table_name: str, table_id: Optional[str],
                     paths: list[Path], upload_id: Optional[str] = None) -> dict:
        """Upload NDJSON files into `table_name` (created if table_id is None).
        Returns {'uploadId', 'uploadTableId', 'uploadTableName', 'files'}."""
        if not TABLE_NAME_RE.match(table_name) or table_name.startswith("_"):
            raise OpenJIIError(f"invalid table name {table_name!r}: letters, digits and "
                               "underscores, starting with a letter, max 63 characters")
        upload_id = upload_id or str(uuid.uuid4())
        fields = {"sourceKind": UPLOAD_SOURCE_KIND, "uploadId": upload_id}
        if table_id:
            fields.update(targetKind="existing", uploadTableId=table_id)
        else:
            fields.update(targetKind="new", targetName=table_name)
        handles = []
        try:
            files = []
            for p in paths:
                f = open(p, "rb")
                handles.append(f)
                files.append(("files", (p.name, f, "application/x-ndjson")))
            # requests puts the text fields before the files in the multipart body,
            # which the backend requires (it resolves the target on the first file).
            status, payload = self._request(
                "POST", f"/api/v1/experiments/{experiment_id}/data/uploads",
                data=fields, files=files, timeout=max(self.timeout, 600))
        finally:
            for f in handles:
                f.close()
        if status != 201 or not isinstance(payload, dict):
            raise OpenJIIError(f"upload failed ({status}): {self._error_text(payload)}")
        payload.setdefault("uploadId", upload_id)
        return payload

    def upload_status(self, experiment_id: str, table_name: str,
                      upload_ids: set[str]) -> dict[str, UploadRecord]:
        return {u.upload_id: u for u in self.list_uploads(experiment_id, table_name)
                if u.upload_id in upload_ids}

    # ------------------------------------------------------------ download
    def list_tables(self, experiment_id: str) -> list[Table]:
        """The tables of the experiment's Data page."""
        status, payload = self._request("GET", f"/api/v1/experiments/{experiment_id}/tables")
        if status != 200 or not isinstance(payload, list):
            raise OpenJIIError(f"listing tables failed ({status}): {self._error_text(payload)}")
        return [Table(t["identifier"], t.get("displayName") or t["identifier"],
                      t.get("tableType") or "", t.get("totalRows") or 0)
                for t in payload if isinstance(t, dict) and t.get("identifier")]

    def read_table_page(self, experiment_id: str, table: str, page: int, page_size: int) -> dict:
        """One page of a table (by identifier), ordered by the row id so that the
        pages neither overlap nor skip rows. -> {'columns': [{'name', 'type_text'}],
        'rows': [{...}]}; values as the SQL warehouse returns them, mostly strings
        (see download.py). The warehouse may first have to start: a long timeout."""
        status, payload = self._request(
            "GET", f"/api/v1/experiments/{experiment_id}/data",
            params={"tableName": table, "page": page, "pageSize": page_size,
                    "orderBy": "id", "orderDirection": "ASC"}, timeout=max(self.timeout, 300))
        if status == 400 and ("PAGE_TOO_LARGE" in json.dumps(payload)
                              or "smaller page size" in self._error_text(payload)):
            raise PageTooLarge(f"page {page} of {page_size} rows of table {table} is too large")
        if (status != 200 or not isinstance(payload, list) or not payload
                or not isinstance(payload[0], dict) or not isinstance(payload[0].get("data"), dict)):
            raise OpenJIIError(f"reading table {table} failed ({status}): {self._error_text(payload)}")
        data = payload[0]["data"]
        return {"columns": data.get("columns") or [], "rows": data.get("rows") or []}


def parse_experiment_ref(text: str) -> str:
    """An experiment id from an id, or from a platform URL containing one."""
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                  text)
    return m.group(0).lower() if m else text.strip()
