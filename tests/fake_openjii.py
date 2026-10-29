"""A stand-in for the openJII API, enough for the recorder: API-key check,
experiments, access, upload tables. Uploads become 'completed' when they are
listed the second time, like the platform's job finishing."""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VALID_KEY = "jii_test_key_123"
EXPERIMENT = {"id": "11111111-2222-4333-8444-555555555555", "name": "Test leaves", "status": "active"}
OTHER = {"id": "66666666-7777-4888-9999-000000000000", "name": "Other", "status": "active"}


def parse_multipart(body: bytes, content_type: str) -> tuple[list[tuple[str, str]], list[tuple[str, str, bytes]]]:
    """-> ([(field, value)], [(field, filename, content)]) in body order."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type).group(1).encode()
    fields, files = [], []
    for part in body.split(b"--" + boundary)[1:]:
        if part.strip() in (b"", b"--"):
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        content = content[:-2] if content.endswith(b"\r\n") else content
        disp = re.search(rb'name="([^"]*)"(?:; filename="([^"]*)")?', head)
        name = disp.group(1).decode()
        if disp.group(2) is not None:
            files.append((name, disp.group(2).decode(), content))
        else:
            fields.append((name, content.decode()))
    return fields, files


class FakeOpenJII:
    def __init__(self, *, can_manage: bool = True, can_contribute: bool = True,
                 list_shape: str = "list", fail_uploads: bool = False) -> None:
        self.can_manage = can_manage
        self.can_contribute = can_contribute
        self.list_shape = list_shape
        self.fail_uploads = fail_uploads
        self.uploads: list[dict] = []          # records as the API lists them
        self.received: list[dict] = []         # {fields, files}
        self.tables: dict[str, str] = {}       # name -> id
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # quiet
                pass

            def _send(self, status, payload):
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth(self) -> bool:
                return self.headers.get("x-api-key") == VALID_KEY

            def do_GET(self):
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                server_ref.requests.append(("GET", url.path))
                if url.path == "/api/v1/auth/get-session":
                    return self._send(200, {"user": {"id": "u1", "name": "Test User"}} if self._auth() else None)
                if not self._auth():
                    return self._send(401, {"message": "unauthorized"})
                if url.path == "/api/v1/experiments":
                    items = [EXPERIMENT, OTHER]
                    return self._send(200, items if server_ref.list_shape == "list"
                                      else {"items": items, "total": 2})
                m = re.fullmatch(r"/api/v1/experiments/([^/]+)(/.*)?", url.path)
                if m and m.group(1) in (EXPERIMENT["id"], OTHER["id"]):
                    exp = EXPERIMENT if m.group(1) == EXPERIMENT["id"] else OTHER
                    rest = m.group(2) or ""
                    if rest == "":
                        return self._send(200, exp)
                    if rest == "/access":
                        return self._send(200, {"experiment": exp, "hasAccess": True, "isAdmin": False,
                                                "capabilities": {"canContribute": server_ref.can_contribute,
                                                                 "canManage": server_ref.can_manage},
                                                "membershipStatus": "member"})
                    if rest == "/data/uploads":
                        with server_ref.lock:
                            out = []
                            for u in server_ref.uploads:
                                if q.get("uploadTableName") and u["uploadTableName"] != q["uploadTableName"]:
                                    continue
                                if u["status"] == "running":
                                    u["status"] = "failed" if server_ref.fail_uploads else "completed"
                                    u["errorCode"] = "no_rows" if server_ref.fail_uploads else None
                                elif u["status"] == "queued":
                                    u["status"] = "running"
                                out.append(dict(u))
                        return self._send(200, {"uploads": out})
                return self._send(404, {"message": "not found"})

            def do_POST(self):
                url = urlparse(self.path)
                server_ref.requests.append(("POST", url.path))
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                if not self._auth():
                    return self._send(401, {"message": "unauthorized"})
                m = re.fullmatch(r"/api/v1/experiments/([^/]+)/data/uploads", url.path)
                if not m:
                    return self._send(404, {"message": "not found"})
                fields, files = parse_multipart(body, self.headers["Content-Type"])
                f = dict(fields)
                if not files:
                    return self._send(400, {"message": "Upload received no files"})
                if f.get("sourceKind") != "ndjson":
                    return self._send(400, {"message": f"Unsupported source kind: {f.get('sourceKind')}"})
                with server_ref.lock:
                    if f.get("targetKind") == "new":
                        if not server_ref.can_manage:
                            return self._send(403, {"message": "Creating a new data table requires experiment management access"})
                        if f["targetName"] in server_ref.tables:
                            return self._send(409, {"message": "table exists"})
                        table_id = str(uuid.uuid4())
                        server_ref.tables[f["targetName"]] = table_id
                        table_name = f["targetName"]
                    else:
                        table_id = f.get("uploadTableId")
                        names = [n for n, i in server_ref.tables.items() if i == table_id]
                        if not names:
                            return self._send(404, {"message": "unknown table"})
                        table_name = names[0]
                    accepted = [(n, fn, c) for n, fn, c in files if fn.lower().endswith((".ndjson", ".jsonl"))]
                    upload_id = f.get("uploadId") or str(uuid.uuid4())
                    rows = sum(len([l for l in c.splitlines() if l.strip()]) for _, _, c in accepted)
                    server_ref.received.append({"fields": fields, "files": files, "uploadId": upload_id})
                    server_ref.uploads.append({
                        "uploadId": upload_id, "experimentId": m.group(1), "uploadTableId": table_id,
                        "uploadTableName": table_name, "sourceKind": "ndjson", "status": "queued",
                        "fileCount": len(accepted), "rowCount": rows, "rejectedFiles": [],
                        "errorCode": None, "createdBy": "u1", "createdAt": "2026-01-01T00:00:00Z",
                        "completedAt": None})
                self._send(201, {"uploadId": upload_id, "uploadTableId": table_id,
                                 "uploadTableName": table_name,
                                 "files": [{"fileName": fn, "filePath": "/x/" + fn} for _, fn, _ in accepted]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
