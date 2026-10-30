"""Unattended recording with several spectrometers, uploaded to openJII.

    python -m c12666ma.record                 interactive: sign in, pick the experiment,
                                              set each spectrometer, record
    python -m c12666ma.record login           (re)enter the openJII API key
    python -m c12666ma.record upload FOLDER   resume the uploads of an earlier recording
    python -m c12666ma.record --yes ...       no questions: every answer from the flags
                                              and the remembered settings (for Task
                                              Scheduler or a batch file)

Spectra are written locally first (one CSV series per spectrometer, see the
README) and, every upload interval, sent as NDJSON rows into an upload table
of the experiment. Without network the rows queue up on disk and go out when
the connection is back; a stopped run resumes with `upload FOLDER`.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__
from .device import DeviceError
from .openjii import (ENVIRONMENTS, Environment, Experiment, OpenJIIClient, OpenJIIError, Settings,
                      parse_experiment_ref)
from .session import AcquisitionSession, DeviceHandle
from .uploader import Uploader, dark_row, frame_row

DEFAULT_TABLE = "c12666ma_spectra"
DEFAULT_PERIOD_S = 60.0
DEFAULT_UPLOAD_S = 15 * 60.0
STATUS_EVERY_S = 60.0


class Abort(Exception):
    pass


# ================================================================ prompting
class Prompter:
    """input() with defaults; refuses to ask when running unattended (--yes)."""

    def __init__(self, unattended: bool) -> None:
        self.unattended = unattended or not sys.stdin.isatty()

    def ask(self, question: str, default: Optional[str] = None, *, secret: bool = False,
            validate=None) -> str:
        if self.unattended:
            if default is None:
                raise Abort(f"{question}: no value given and running unattended (--yes)")
            return default
        while True:
            suffix = f" [{default}]" if default not in (None, "") else ""
            try:
                raw = (getpass.getpass if secret else input)(f"{question}{suffix}: ")
            except EOFError:
                raise Abort("input closed") from None
            value = raw.strip() or (default or "")
            if validate is None:
                return value
            try:
                return validate(value)
            except (ValueError, Abort) as exc:
                print(f"  {exc}")

    def yes_no(self, question: str, default: bool) -> bool:
        if self.unattended:
            return default
        value = self.ask(f"{question} (y/n)", "y" if default else "n")
        return value.lower().startswith("y")

    def choose(self, question: str, options: list[str], default: Optional[int] = None) -> int:
        """Index of the chosen option (1-based list on screen)."""
        if self.unattended:
            if default is None:
                raise Abort(f"{question}: nothing chosen and running unattended")
            return default
        for i, text in enumerate(options, 1):
            print(f"  {i:2d}. {text}")

        def validate(v):
            n = int(v)
            if not 1 <= n <= len(options):
                raise ValueError(f"enter a number from 1 to {len(options)}")
            return n - 1
        return self.ask(question, None if default is None else str(default + 1), validate=validate)


def parse_duration(text: str) -> float:
    """'90m', '36h', '3d', '3600' (seconds) -> seconds; 0 = until stopped."""
    text = str(text).strip().lower()
    if text in ("", "0", "none", "forever"):
        return 0.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", text)
    if not m:
        raise ValueError(f"cannot read duration {text!r} (examples: 90m, 36h, 3d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def fmt_duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds <= 0:
        return "0"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"


def parse_device_spec(spec: str) -> tuple[str, dict]:
    """'Fluor=high,100,1[,on]' -> ('Fluor', {high_gain, integration_us, n_avg, led})"""
    name, _, rest = spec.partition("=")
    parts = [p.strip().lower() for p in rest.split(",")]
    if not name or len(parts) < 3:
        raise ValueError(f"--device needs NAME=gain,integration_ms,averages[,led]: {spec!r}")
    if parts[0] not in ("high", "low"):
        raise ValueError(f"gain must be high or low: {spec!r}")
    settings = {"high_gain": parts[0] == "high", "integration_us": round(float(parts[1]) * 1000),
                "n_avg": int(parts[2])}
    if len(parts) > 3:
        settings["led"] = parts[3] in ("on", "1", "true", "yes")
    return name.strip(), settings


# ================================================================= openJII
def sign_in(settings: Settings, env: Environment, prompter: Prompter,
            force: bool = False) -> tuple[OpenJIIClient, dict]:
    """A client with a valid key for `env`; asks for the key when needed."""
    key = "" if force else settings.api_key(env.key)
    while True:
        if key:
            client = OpenJIIClient(env, key)
            try:
                user = client.validate_key()
                settings.set_api_key(env.key, key)
                settings.save()
                return client, user
            except OpenJIIError as exc:
                print(f"openJII ({env.key}): {exc}")
                settings.forget_api_key(env.key)
                settings.save()
                key = ""
        if prompter.unattended:
            raise Abort(f"no valid openJII API key for {env.key}; run "
                        f"`python -m c12666ma.record login --env {env.key}` once")
        print(f"\nSign in to openJII ({env.key}): create a personal API key at\n  {env.api_keys_url}")
        if prompter.yes_no("Open that page in the browser?", True):
            webbrowser.open(env.api_keys_url)
        key = prompter.ask("Paste the API key (hidden)", secret=True)


def choose_experiment(client: OpenJIIClient, settings: Settings, prompter: Prompter,
                      requested: Optional[str]) -> Experiment:
    remembered = settings.get("experiment") or {}
    if requested:
        ref = parse_experiment_ref(requested)
        if re.fullmatch(r"[0-9a-f-]{36}", ref):
            return client.get_experiment(ref)
        matches = [e for e in client.list_experiments() if e.name.lower() == requested.lower()]
        if len(matches) != 1:
            raise Abort(f"experiment {requested!r}: {len(matches)} active experiments with that "
                        "name; give its id or URL")
        return matches[0]
    experiments = client.list_experiments()
    if prompter.unattended:
        if remembered.get("id"):
            return client.get_experiment(remembered["id"])
        raise Abort("no experiment given (--experiment) and none remembered")
    if not experiments:
        print("You are not a member of any active experiment.")
        ref = prompter.ask("Experiment id or URL")
        return client.get_experiment(parse_experiment_ref(ref))
    options = [f"{e.name}  ({e.id})" for e in experiments] + ["another one (paste its id or URL)"]
    default = next((i for i, e in enumerate(experiments) if e.id == remembered.get("id")), None)
    print("\nActive experiments you are a member of:")
    choice = prompter.choose("Record into experiment", options, default)
    if choice < len(experiments):
        return experiments[choice]
    return client.get_experiment(parse_experiment_ref(prompter.ask("Experiment id or URL")))


def check_access(client: OpenJIIClient, experiment: Experiment, table: str) -> Optional[str]:
    """Returns the existing table id (None if the first upload will create it),
    or raises Abort when the account cannot upload."""
    caps = client.capabilities(experiment.id)
    if not caps.get("canContribute"):
        raise Abort(f"your account cannot contribute data to '{experiment.name}'")
    table_id = client.find_table(experiment.id, table)
    if table_id is None and not caps.get("canManage"):
        raise Abort(f"table '{table}' does not exist in '{experiment.name}' and your account "
                    "cannot create one (needs manage access); ask the owner, or choose an "
                    "existing table name")
    return table_id


# ================================================================ recording
def describe_device(h: DeviceHandle) -> str:
    s = h.settings
    return (f"{h.name:10s} {h.port:6s} id {h.uid}  gain {'high' if s.high_gain else 'low':4s}  "
            f"integration {s.integration_us / 1000:g} ms  averages {s.n_avg}  "
            f"LED {'on' if s.led else 'off'}")


def configure_devices(session: AcquisitionSession, settings: Settings, prompter: Prompter,
                      specs: list[str], all_spec: Optional[str]) -> None:
    by_name = {}
    for spec in specs:
        name, values = parse_device_spec(spec)
        by_name[name.lower()] = values
    remembered = settings.get("devices") or {}
    for h in session.connected:
        values = by_name.get(h.name.lower()) or by_name.get(h.uid.lower())
        if values is None and all_spec:
            values = parse_device_spec("all=" + all_spec)[1]
        if values is None and not prompter.unattended:
            print(f"\n{h.name} on {h.port} (id {h.uid})")
            last = remembered.get(h.uid) or {}
            gain = prompter.ask("  gain (high/low)", last.get("gain", "high" if h.settings.high_gain else "low"),
                                validate=lambda v: v.lower() if v.lower() in ("high", "low") else _bad("high or low"))
            integ = prompter.ask("  integration time in ms", str(last.get("integration_ms", h.settings.integration_us / 1000)),
                                 validate=lambda v: float(v) if float(v) > 0 else _bad("a positive number"))
            avg = prompter.ask("  averages (1-16)", str(last.get("n_avg", h.settings.n_avg)),
                               validate=lambda v: int(v) if 1 <= int(v) <= 16 else _bad("1 to 16"))
            led = prompter.yes_no("  LED on during the recording?", bool(last.get("led", h.settings.led)))
            values = {"high_gain": gain == "high", "integration_us": round(integ * 1000),
                      "n_avg": avg, "led": led}
        elif values is None:
            last = remembered.get(h.uid)
            if last:
                values = {"high_gain": last["gain"] == "high",
                          "integration_us": round(last["integration_ms"] * 1000),
                          "n_avg": int(last["n_avg"]), "led": bool(last.get("led", False))}
        if values:
            session.apply(h, **values)
        s = h.settings
        remembered[h.uid] = {"name": h.name, "gain": "high" if s.high_gain else "low",
                             "integration_ms": s.integration_us / 1000, "n_avg": s.n_avg,
                             "led": s.led}
    settings.set("devices", remembered)


def _bad(expected: str):
    raise ValueError(f"enter {expected}")


def environment_for(args, settings: Settings) -> Environment:
    if getattr(args, "api_url", None):           # tests: a fake server
        return Environment("custom", args.api_url.rstrip("/"), args.api_url.rstrip("/"))
    return ENVIRONMENTS[args.env or settings.get("environment", "prod")]


def run_recording(args, prompter: Prompter) -> int:
    settings = Settings(args.settings_file)
    client = experiment = table_id = None
    table = ""
    env = environment_for(args, settings)

    # ---- openJII
    if not args.no_upload:
        client, user = sign_in(settings, env, prompter)
        who = user.get("name") or user.get("email") or user.get("id")
        print(f"openJII ({env.key}): signed in as {who}")
        settings.set("environment", env.key)
        experiment = choose_experiment(client, settings, prompter, args.experiment)
        settings.set("experiment", {"id": experiment.id, "name": experiment.name})
        table = args.table or (settings.get("table_name") if prompter.unattended else None) \
            or prompter.ask("Upload table name in the experiment",
                            settings.get("table_name", DEFAULT_TABLE))
        table_id = check_access(client, experiment, table)
        settings.set("table_name", table)
        print(f"Experiment: {experiment.name}  ({env.experiment_url(experiment.id)})\n"
              f"Table: {table} ({'exists' if table_id else 'will be created'})")
        settings.save()

    # ---- spectrometers
    session = AcquisitionSession(simulate=args.simulate > 0)
    if args.simulate:
        from . import simulator
        simulator.create_units(args.simulate)
    try:
        errors = session.connect_all()
        for e in errors:
            print(f"  {e}")
        if not session.connected:
            raise Abort("no spectrometer found on USB")
        old = [h.name for h in session.connected if "min_trigger_period_us" not in h.status]
        if old:
            raise Abort(f"{', '.join(old)}: firmware too old; copy firmware/main.py to the Pico")
        print(f"\n{len(session.connected)} spectrometer(s):")
        for h in session.connected:
            print("  " + describe_device(h))
        configure_devices(session, settings, prompter, args.device, args.all)
        print("\nSettings:")
        for h in session.connected:
            print("  " + describe_device(h))
            if h.calibration.approximate:
                print(f"    warning: {h.name} has no calibration of its own; its wavelengths are "
                      "those of another unit")

        # ---- timing, files
        min_s = session.min_period_us() / 1e6
        if len(session.connected) == 1:
            session.set_period_us(1)          # a period: one frame per tick, not continuous
            min_s = session.min_period_us() / 1e6
        period = args.period if args.period is not None else float(prompter.ask(
            f"Period between spectra in seconds (minimum {min_s:.2f})",
            str(settings.get("period_s", DEFAULT_PERIOD_S)),
            validate=lambda v: float(v) if float(v) >= min_s - 1e-6 else _bad(f"at least {min_s:.2f}")))
        period = max(period, min_s)
        session.set_period_us(round(period * 1e6))
        settings.set("period_s", period)
        upload_every = DEFAULT_UPLOAD_S
        if client is not None:
            if args.upload_every is not None:
                upload_every = args.upload_every
            else:
                upload_every = prompter.ask(
                    "Upload to openJII every (e.g. 15m, 1h)",
                    fmt_duration(settings.get("upload_every_s", DEFAULT_UPLOAD_S)),
                    validate=lambda v: parse_duration(v) if parse_duration(v) >= 60 else _bad("at least 1m"))
            settings.set("upload_every_s", upload_every)
        folder = Path(args.folder or prompter.ask(
            "Local folder for the recording", settings.get("folder", str(Path.home() / "C12666MA data"))))
        settings.set("folder", str(folder))
        if args.duration is not None:
            duration = parse_duration(args.duration)
        else:
            duration = prompter.ask("Duration (e.g. 36h, 3d; 0 = until stopped)",
                                    fmt_duration(settings.get("duration_s", 0)), validate=parse_duration)
        settings.set("duration_s", duration)
        note = args.note if args.note is not None else prompter.ask("Note for the file header",
                                                                    settings.get("note", ""))
        settings.set("note", note)
        settings.save()
        prefix = args.prefix or (experiment.name if experiment else "C12666MA")

        if args.dark or (args.dark is None and prompter.yes_no(
                "\nTake a dark spectrum first (cover the sensors)?", False)):
            n = args.dark or 20
            if not prompter.unattended:
                input(f"Cover the sensors, then press Enter to take {n} dark frames per device... ")
            session.take_dark(n, led_off=True)
            t_end = time.time() + n * period + 30
            while time.time() < t_end and not all(
                    h.processor.dark is not None for h in session.connected):
                time.sleep(0.5)
            print("  dark: " + ", ".join(f"{h.name} {'ok' if h.processor.dark else 'FAILED'}"
                                        for h in session.connected))
            session.stop()
            if not prompter.unattended:
                input("Uncover the sensors and press Enter to start recording... ")

        # ---- go
        rec = session.start_recording(folder, prefix=prefix, note=note, include_yield=False,
                                      duration_s=duration, part_hours=args.part_hours)
        print(f"\nRecording to {rec.folder}")
        uploader = None
        if client is not None:
            uploader = Uploader(client, experiment.id, table, rec.folder / "uploads",
                                log=lambda t: session.log(t))
            uploader.table_id = table_id
            (rec.folder / "uploads" / "target.json").write_text(json.dumps({
                "environment": env.key, "api_url": env.api_url, "experiment_id": experiment.id,
                "experiment_name": experiment.name, "table_name": table}, indent=2), encoding="utf-8")
            uploader.start()
            for h in session.handles:
                if h.processor.dark is not None:
                    uploader.writer.write(dark_row(h.processor.dark, device=device_fields(h),
                                                   session_id=rec.folder.name,
                                                   wavelengths=h.processor.wavelengths,
                                                   wl_coeffs=coeffs(h)))
                # rows are written where the CSV rows are, so both hold the same frames
                h.processor.extra_writer = _row_writer(uploader, h, rec.folder.name, rec.t0, note)
        print(f"Period {period:g} s" + (f", upload every {fmt_duration(upload_every)}" if uploader else
                                        ", no upload") + (f", for {fmt_duration(duration)}" if duration else
                                                          ", until Ctrl-C") + "\n")
        return main_loop(session, rec, uploader, upload_every, duration, note)
    finally:
        session.close()


def device_fields(h: DeviceHandle) -> dict:
    return {"device_uid": h.uid, "device_name": h.name, "port": h.port,
            "firmware": str(h.status.get("fw", ""))}


def coeffs(h: DeviceHandle) -> list:
    c = list(h.calibration.coefficients)
    while len(c) > 2 and c[-1] == 0:
        c.pop()
    return c


def _row_writer(uploader: Uploader, h: DeviceHandle, session_id: str, t0: float, note: str):
    device = device_fields(h)
    wl_coeffs = coeffs(h)

    def write(frame) -> None:
        uploader.writer.write(frame_row(frame, device=device, session_id=session_id, t0=t0,
                                        wavelengths=h.processor.wavelengths,
                                        wl_coeffs=wl_coeffs, note=note))
    return write


def main_loop(session: AcquisitionSession, rec, uploader: Optional[Uploader],
              upload_every: float, duration: float, note: str) -> int:
    next_upload = time.monotonic() + upload_every
    next_status = time.monotonic() + STATUS_EVERY_S
    next_flush = time.monotonic() + 10
    last_event = 0.0
    results = session.results
    try:
        while session.recording is rec:
            now = time.monotonic()
            results.clear()                           # the GUI's queue; not needed here
            if uploader is not None and now >= next_upload:
                uploader.rotate_and_upload()
                next_upload = now + upload_every
            if uploader is not None and now >= next_flush:
                uploader.writer.flush()
                next_flush = now + 10
            for t, line in list(session.events):
                if t > last_event:
                    print(line)
                    last_event = t
            if now >= next_status:
                print(status_line(session, rec, uploader))
                next_status = now + STATUS_EVERY_S
            time.sleep(0.5)
        reason = rec.stop_reason or "stopped"
        print(f"\nRecording ended: {reason}")
    except KeyboardInterrupt:
        print("\nStopping (Ctrl-C)...")
        session.stop_recording("stopped by user (Ctrl-C)")
    for h in session.handles:
        h.processor.extra_writer = None
    if uploader is not None:
        print("Uploading the last batch and waiting for the platform's confirmation...")
        uploader.stop(wait_s=120, confirm_s=90)
        s = uploader.ledger.summary()
        print(f"Uploads: {s['batches']}")
        if uploader.pending:
            print(f"{uploader.pending} batch(es) could not be uploaded"
                  + (f" ({uploader.last_error})" if uploader.last_error else "")
                  + f". They are kept locally; send them later with:\n"
                  f"  python -m c12666ma.record upload \"{rec.folder}\"")
        elif uploader.awaiting_confirmation:
            print(f"{uploader.awaiting_confirmation} batch(es) were sent but not confirmed yet "
                  "(usually a minute). Nothing to resend; to check later:\n"
                  f"  python -m c12666ma.record upload \"{rec.folder}\"")
    print(f"Local files: {rec.folder}  ({rec.n_spectra} spectra)")
    return 0


def status_line(session: AcquisitionSession, rec, uploader: Optional[Uploader]) -> str:
    parts = [datetime.now().strftime("%H:%M:%S"), f"tick {session.ticks_sent}"]
    for h in session.handles:
        state = "" if h.connected else " LOST"
        parts.append(f"{h.name} {h.frames}{state}" + (f" (missed {h.missed})" if h.missed else ""))
    parts.append(f"{rec.n_spectra} spectra on disk")
    if uploader is not None:
        s = uploader.ledger.summary()["batches"]
        done = s.get("completed", 0)
        waiting = s.get("pending", 0)
        sent = s.get("uploaded", 0)
        text = f"uploads {done} confirmed"
        if sent:
            text += f", {sent} sent"
        if waiting:
            text += f", {waiting} waiting"
        if not uploader.online:
            text += " (OFFLINE, keeping local)"
        if uploader.last_upload_at:
            text += f", last {datetime.fromtimestamp(uploader.last_upload_at).strftime('%H:%M')}"
        parts.append(text)
    return " · ".join(parts)


# ================================================================= commands
def cmd_login(args, prompter: Prompter) -> int:
    settings = Settings(args.settings_file)
    env = ENVIRONMENTS[args.env or settings.get("environment", "prod")]
    client, user = sign_in(settings, env, prompter, force=True)
    settings.set("environment", env.key)
    settings.save()
    print(f"openJII ({env.key}): signed in as {user.get('name') or user.get('email')}; "
          f"key saved in {settings.path}")
    return 0


def cmd_upload(args, prompter: Prompter) -> int:
    """Resume the uploads of a recording folder."""
    folder = Path(args.folder)
    target_file = folder / "uploads" / "target.json"
    if not target_file.exists():
        raise Abort(f"{folder} has no uploads/target.json (was it recorded with upload enabled?)")
    target = json.loads(target_file.read_text(encoding="utf-8"))
    settings = Settings(args.settings_file)
    env = ENVIRONMENTS.get(target["environment"]) or Environment(
        target["environment"], target["api_url"], target["api_url"])
    client, user = sign_in(settings, env, prompter)
    uploader = Uploader(client, target["experiment_id"], target["table_name"],
                        folder / "uploads", log=print)
    pending = uploader.pending
    print(f"{target['experiment_name']}: {pending} batch(es) to upload, "
          f"{uploader.awaiting_confirmation} awaiting confirmation")
    if not pending and not uploader.awaiting_confirmation:
        return 0
    uploader.start()
    try:
        while uploader.pending or uploader.awaiting_confirmation:
            time.sleep(5)
            if not uploader.online and uploader.pending:
                print(f"  offline ({uploader.last_error}); still trying, Ctrl-C to stop")
    except KeyboardInterrupt:
        print("stopped; run the same command again later")
    uploader.stop(wait_s=0)
    print(f"Uploads: {uploader.ledger.summary()['batches']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--settings-file", type=Path, help=argparse.SUPPRESS)
    common.add_argument("--yes", "-y", action="store_true",
                        help="ask nothing; use flags and remembered settings")
    p = argparse.ArgumentParser(prog="python -m c12666ma.record", parents=[common],
                                description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")
    login = sub.add_parser("login", parents=[common], help="(re)enter the openJII API key")
    login.add_argument("--env", choices=list(ENVIRONMENTS))
    up = sub.add_parser("upload", parents=[common], help="resume the uploads of a recording folder")
    up.add_argument("folder")
    rec = p
    rec.add_argument("--env", choices=list(ENVIRONMENTS))
    rec.add_argument("--experiment", help="experiment id, URL or exact name")
    rec.add_argument("--table", help=f"upload table name (default {DEFAULT_TABLE})")
    rec.add_argument("--no-upload", action="store_true", help="record locally only")
    rec.add_argument("--device", action="append", default=[], metavar="NAME=gain,ms,avg[,led]",
                     help="settings of one spectrometer, e.g. Fluor=high,100,1,off")
    rec.add_argument("--all", metavar="gain,ms,avg[,led]", help="settings for every spectrometer")
    rec.add_argument("--period", type=float, help="seconds between spectra")
    rec.add_argument("--upload-every", type=parse_duration, help="e.g. 15m, 1h")
    rec.add_argument("--folder", help="local folder for the recording")
    rec.add_argument("--prefix", help="recording folder name prefix (default: experiment name)")
    rec.add_argument("--duration", help="e.g. 36h, 3d (default: until Ctrl-C)")
    rec.add_argument("--part-hours", type=float, default=24.0, help="new local file every N hours")
    rec.add_argument("--note", help="note for the file headers")
    rec.add_argument("--dark", type=int, nargs="?", const=20, default=None, metavar="N",
                     help="take N dark frames per device first (default 20)")
    rec.add_argument("--simulate", type=int, default=0, metavar="N", help=argparse.SUPPRESS)
    rec.add_argument("--api-url", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    prompter = Prompter(getattr(args, "yes", False))
    print(f"C12666MA recorder {__version__}")
    try:
        if args.command == "login":
            return cmd_login(args, prompter)
        if args.command == "upload":
            return cmd_upload(args, prompter)
        return run_recording(args, prompter)
    except Abort as exc:
        print(f"\nStopped: {exc}", file=sys.stderr)
        return 2
    except (DeviceError, OpenJIIError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
