"""Batched upload of a recording to an openJII experiment.

Rows (one JSON object per spectrum) are appended to the current batch file
``<recording>/uploads/batch_NNNNNN.ndjson``. At every upload interval the
batch is closed and queued. A worker thread uploads queued batches (several
at once if a backlog built up offline), then polls the platform until it
reports the rows queryable or failed. ``uploads/ledger.json`` records the
state of every batch, so a run that stopped (crash, power loss, no network)
resumes its pending uploads with ``python -m c12666ma.record upload <folder>``.

Acquisition and the local CSV files never depend on this: the worker only
reads closed batch files.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .openjii import OpenJIIClient, OpenJIIError
from .protocol import FLAG_ADC_ERROR, FLAG_LATE, FLAG_LED_CHANGED, FLAG_RESTART, Frame

MAX_BATCH_BYTES = 40_000_000       # platform limit is 100 MB per NDJSON file
MAX_FILES_PER_UPLOAD = 100
RETRY_MIN_S = 30.0
RETRY_MAX_S = 600.0
POLL_S = 30.0
TABLE_WAIT_S = 60.0                # new table not listed yet: try again after this
POLL_GIVE_UP_S = 3 * 3600.0        # the platform itself gives up after 1 h

FLAG_NAMES = {FLAG_LED_CHANGED: "led_changed", FLAG_RESTART: "restart",
              FLAG_LATE: "late", FLAG_ADC_ERROR: "adc_error"}


def frame_row(frame: Frame, *, device: dict, session_id: str, t0: float,
              wavelengths: np.ndarray, wl_coeffs: list, note: str = "") -> dict:
    """One self-describing row for the experiment table."""
    ts = datetime.fromtimestamp(frame.timestamp, tz=timezone.utc)
    return {
        "kind": "spectrum",
        "timestamp_utc": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "timestamp_local": ts.astimezone().isoformat(timespec="milliseconds"),
        "unix_time": round(frame.timestamp, 3),
        "t_s": round(frame.timestamp - t0, 3),
        "tick": frame.seq,
        "session_id": session_id,
        "note": note,
        **device,
        "integration_ms": frame.integration_us / 1000.0,
        "integration_us": frame.integration_us,
        "n_avg": frame.n_avg,
        "gain": "high" if frame.high_gain else "low",
        "high_gain": bool(frame.high_gain),
        "led": bool(frame.led),
        "flags": int(frame.flags),
        "flag_names": [name for bit, name in FLAG_NAMES.items() if frame.flags & bit],
        "adc_errors": int(frame.adc_errors),
        "clk_hz": frame.clk_hz,
        "n_pixels": int(len(frame.counts)),
        "wavelength_coeffs": [float(c) for c in wl_coeffs],
        "wavelength_nm": [round(float(w), 2) for w in wavelengths],
        "counts": [float(c) if c != int(c) else int(c) for c in frame.counts.tolist()],
    }


def dark_row(dark, *, device: dict, session_id: str, wavelengths: np.ndarray, wl_coeffs: list) -> dict:
    clocks, clk_hz, n_avg, high_gain = dark.settings_key
    ts = datetime.fromtimestamp(dark.timestamp, tz=timezone.utc)
    return {
        "kind": "dark",
        "timestamp_utc": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "timestamp_local": ts.astimezone().isoformat(timespec="milliseconds"),
        "unix_time": round(dark.timestamp, 3),
        "session_id": session_id,
        **device,
        "integration_us": round(clocks * 1e6 / clk_hz),
        "n_avg": n_avg,
        "gain": "high" if high_gain else "low",
        "high_gain": bool(high_gain),
        "n_frames": dark.n_frames,
        "clk_hz": clk_hz,
        "wavelength_coeffs": [float(c) for c in wl_coeffs],
        "wavelength_nm": [round(float(w), 2) for w in wavelengths],
        "counts_mean": [round(float(v), 3) for v in dark.mean],
        "counts_std": [round(float(v), 3) for v in dark.std],
    }


class Ledger:
    """uploads/ledger.json: {batch file name: {status, rows, bytes, upload_id, ...}}."""

    STATES = ("open", "pending", "uploaded", "completed", "failed", "rejected")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: dict = {"batches": {}, "uploads": {}}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def batches(self, status: Optional[str] = None) -> list[str]:
        with self.lock:
            names = sorted(self.data["batches"])
            if status is None:
                return names
            return [n for n in names if self.data["batches"][n].get("status") == status]

    def update(self, name: str, **fields) -> None:
        with self.lock:
            self.data["batches"].setdefault(name, {}).update(fields)
            self.save()

    def record_upload(self, upload_id: str, **fields) -> None:
        with self.lock:
            self.data["uploads"].setdefault(upload_id, {}).update(fields)
            self.save()

    def summary(self) -> dict:
        with self.lock:
            counts: dict = {}
            rows: dict = {}
            for b in self.data["batches"].values():
                s = b.get("status", "?")
                counts[s] = counts.get(s, 0) + 1
                rows[s] = rows.get(s, 0) + int(b.get("rows", 0))
            return {"batches": counts, "rows": rows}


class BatchWriter:
    """Appends rows to the current batch file; `rotate()` closes it for upload."""

    def __init__(self, folder: Path, ledger: Ledger) -> None:
        self.folder = folder
        self.ledger = ledger
        self.lock = threading.Lock()
        self._file = None
        self._name = ""
        self._rows = 0
        self._bytes = 0
        self._recover_open()
        self._index = self._next_index()

    def _recover_open(self) -> None:
        """A batch left open by a crash or power loss: keep its complete rows
        (a half-written last line would make the platform reject the whole
        file) and queue it."""
        for name in self.ledger.batches("open"):
            path = self.folder / name
            good = b""
            rows = 0
            try:
                for line in path.read_bytes().splitlines(keepends=True):
                    try:
                        json.loads(line)
                    except ValueError:
                        break
                    good += line
                    rows += 1
            except OSError:
                rows = 0
            if rows == 0:
                path.unlink(missing_ok=True)
                with self.ledger.lock:
                    self.ledger.data["batches"].pop(name, None)
                    self.ledger.save()
                continue
            path.write_bytes(good)
            self.ledger.update(name, status="pending", rows=rows, bytes=len(good),
                               closed=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               recovered=True)

    def _next_index(self) -> int:
        indices = [int(n[6:12]) for n in self.ledger.batches() if n.startswith("batch_")]
        return (max(indices) + 1) if indices else 1

    def _open(self) -> None:
        self._name = f"batch_{self._index:06d}.ndjson"
        self._index += 1
        self._file = open(self.folder / self._name, "a", encoding="utf-8")
        self._rows = 0
        self._bytes = 0
        self.ledger.update(self._name, status="open", rows=0, bytes=0,
                           opened=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def write(self, row: dict) -> None:
        line = json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n"
        with self.lock:
            if self._file is None:
                self._open()
            self._file.write(line)
            self._rows += 1
            self._bytes += len(line)
            if self._bytes >= MAX_BATCH_BYTES:
                self._close()

    def flush(self) -> None:
        with self.lock:
            if self._file is not None:
                self._file.flush()
                self.ledger.update(self._name, rows=self._rows, bytes=self._bytes)

    def _close(self) -> None:
        if self._file is None:
            return
        self._file.close()
        self._file = None
        if self._rows == 0:
            (self.folder / self._name).unlink(missing_ok=True)
            with self.ledger.lock:
                self.ledger.data["batches"].pop(self._name, None)
                self.ledger.save()
        else:
            self.ledger.update(self._name, status="pending", rows=self._rows, bytes=self._bytes,
                               closed=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def rotate(self) -> Optional[str]:
        """Close the current batch (if it has rows); returns its name."""
        with self.lock:
            name = self._name if self._file is not None and self._rows else None
            self._close()
            return name

    @property
    def current_rows(self) -> int:
        return self._rows


class Uploader:
    """Worker thread: uploads pending batches, then follows them to completion."""

    def __init__(self, client: OpenJIIClient, experiment_id: str, table_name: str,
                 folder: Path, log: Callable[[str], None] = print) -> None:
        self.client = client
        self.experiment_id = experiment_id
        self.table_name = table_name
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.folder / "ledger.json")
        self.writer = BatchWriter(self.folder, self.ledger)
        self.log = log
        self.table_id: Optional[str] = None
        self.last_upload_at: Optional[float] = None
        self.last_error: str = ""
        self.online = True
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._retry_s = RETRY_MIN_S
        self._next_try = 0.0
        self._next_poll = 0.0
        # batches that were uploaded but whose rows the platform has not confirmed
        self._following: dict[str, tuple[list[str], float]] = {}
        for name in self.ledger.batches("uploaded"):
            info = self.ledger.data["batches"][name]
            uid = info.get("upload_id")
            if uid:
                names, t = self._following.get(uid, ([], time.time()))
                self._following[uid] = (names + [name], t)

    # ------------------------------------------------------------ control
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="c12666ma-upload", daemon=True)
        self._thread.start()

    def rotate_and_upload(self) -> None:
        """Close the current batch and wake the worker."""
        if self.writer.rotate():
            self._next_try = 0.0
            self._wake.set()

    def stop(self, wait_s: float = 120.0, confirm_s: float = 90.0) -> None:
        """Close the batch, give the worker `wait_s` to upload what is pending and
        `confirm_s` more for the platform to confirm the rows, then stop."""
        self.writer.rotate()
        self._next_try = 0.0
        self._wake.set()
        t_end = time.monotonic() + wait_s
        while time.monotonic() < t_end and self.ledger.batches("pending") and self.online:
            time.sleep(0.5)
        t_end = time.monotonic() + confirm_s
        while time.monotonic() < t_end and self._following and self.online:
            self._next_poll = 0.0
            self._wake.set()
            time.sleep(2.0)
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    @property
    def pending(self) -> int:
        return len(self.ledger.batches("pending"))

    @property
    def awaiting_confirmation(self) -> int:
        return sum(len(n) for n, _ in self._following.values())

    # ------------------------------------------------------------- worker
    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if self.ledger.batches("pending") and now >= self._next_try:
                    self._upload_pending()
                if self._following and now >= self._next_poll:
                    self._poll()
                    self._next_poll = now + POLL_S
            except OpenJIIError as exc:
                if "No upload table with id" in str(exc):
                    self._defer_for_table()
                else:
                    self._fail(str(exc))
            except Exception as exc:                 # never let the worker die
                self._fail(f"{type(exc).__name__}: {exc}")
            self._wake.wait(1.0)
            self._wake.clear()

    def _fail(self, text: str) -> None:
        self.last_error = text
        self.online = False
        self._next_try = time.monotonic() + self._retry_s
        self.log(f"upload: {text}; retrying in {self._retry_s:.0f} s "
                 f"({self.pending} batch(es) waiting)")
        self._retry_s = min(self._retry_s * 2, RETRY_MAX_S)

    def _defer_for_table(self) -> None:
        """A freshly created table is listed by the platform a few minutes after
        its first upload completes; appending before that is refused (404)."""
        self.last_error = "upload table not listed on the platform yet"
        self._next_try = time.monotonic() + TABLE_WAIT_S
        self.log(f"upload: the table is not listed on the platform yet (new table); "
                 f"retrying in {TABLE_WAIT_S:.0f} s ({self.pending} batch(es) waiting)")

    def _upload_pending(self) -> None:
        names = self.ledger.batches("pending")[:MAX_FILES_PER_UPLOAD]
        paths = [self.folder / n for n in names]
        if self.table_id is None:
            self.table_id = self.client.find_table(self.experiment_id, self.table_name)
        rows = sum(int(self.ledger.data["batches"][n].get("rows", 0)) for n in names)
        result = self.client.upload_files(self.experiment_id, self.table_name, self.table_id, paths)
        upload_id = result["uploadId"]
        if result.get("uploadTableId"):
            self.table_id = result["uploadTableId"]
        accepted = {f.get("fileName") for f in result.get("files") or []}
        ok = [n for n in names if n in accepted]
        refused = [n for n in names if n not in accepted]
        for n in ok:
            self.ledger.update(n, status="uploaded", upload_id=upload_id,
                               uploaded=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        for n in refused:
            self.ledger.update(n, status="rejected", upload_id=upload_id,
                               error="not accepted by the platform")
        self.ledger.record_upload(upload_id, batches=names, rows=rows, status="uploaded",
                                  table_id=self.table_id)
        self._following[upload_id] = (ok, time.time())
        self.online = True
        self.last_error = ""
        self.last_upload_at = time.time()
        self._retry_s = RETRY_MIN_S
        self.log(f"upload: sent {len(ok)} batch(es), {rows} rows, upload {upload_id[:8]}"
                 + (f"; {len(refused)} file(s) refused" if refused else ""))

    def _poll(self) -> None:
        status = self.client.upload_status(self.experiment_id, self.table_name,
                                           set(self._following))
        for upload_id, (names, t_sent) in list(self._following.items()):
            rec = status.get(upload_id)
            if rec is not None and rec.finished:
                state = "completed" if rec.status == "completed" else "failed"
                for n in names:
                    self.ledger.update(n, status=state, error=rec.error_code or "")
                self.ledger.record_upload(upload_id, status=rec.status, row_count=rec.row_count,
                                          error_code=rec.error_code,
                                          rejected_files=rec.rejected_files)
                del self._following[upload_id]
                if state == "completed":
                    self.log(f"upload {upload_id[:8]}: {rec.row_count} rows queryable on openJII")
                elif rec.error_code == "transfer_interrupted":
                    for n in names:                   # the files never fully arrived: resend
                        self.ledger.update(n, status="pending")
                    self._next_try = 0.0
                    self._wake.set()
                    self.log(f"upload {upload_id[:8]}: transfer interrupted, will resend")
                else:
                    self.log(f"upload {upload_id[:8]} FAILED on the platform: {rec.error_code} "
                             f"{rec.rejected_files or ''} (the local files are kept)")
            elif time.time() - t_sent > POLL_GIVE_UP_S:
                for n in names:
                    self.ledger.update(n, status="failed", error="no confirmation")
                del self._following[upload_id]
                self.log(f"upload {upload_id[:8]}: no confirmation after "
                         f"{POLL_GIVE_UP_S / 3600:.0f} h; check it on the platform")
