"""Download all the data of an openJII experiment into one JSON file.

    python -m c12666ma.download                     interactive: sign in, pick the
                                                    experiment and the file
    python -m c12666ma.download --experiment URL    that experiment (link, id or exact name)
    python -m c12666ma.download login               (re)enter the openJII API key
    python -m c12666ma.download --yes --experiment URL -o FILE [--table NAME ...]

Every table of the experiment's Data page is read through the API, up to 1000
rows at a time, and written into one file:

    {"experiment": {"id", "name", "url"}, "environment", "downloaded_at",
     "tables": [{"name", "identifier", "type", "columns": [{"name", "type"}],
                 "rows": [{...}, ...], "row_count"}, ...]}

The API returns the values as text; each is turned back into its column's type
(numbers, true/false, lists, objects), so the `counts` of a spectrum are again
a list of 256 numbers. Timestamps stay text. The rows are in the platform's
row-id order, not in time order. The API key is the one that
`python -m c12666ma.record` stores.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .openjii import (ENVIRONMENTS, Environment, Experiment, OpenJIIClient, OpenJIIError,
                      PageTooLarge, Settings, Table)
from .record import Abort, Prompter, choose_experiment, cmd_login, environment_for, sign_in

PAGE_SIZES = (1000, 500, 250, 125, 25, 5, 1)    # each divides the one before it
ATTEMPTS = 3
RETRY_S = 5.0
PROGRESS_EVERY = 10_000                         # rows

INT_TYPES = {"TINYINT", "BYTE", "SMALLINT", "SHORT", "INT", "INTEGER", "BIGINT", "LONG"}
FLOAT_TYPES = {"FLOAT", "DOUBLE", "REAL", "DECIMAL", "DEC", "NUMERIC"}
_TOKEN = re.compile(r"`(?:[^`]|``)*`|[A-Za-z_][A-Za-z0-9_]*|\d+|[<>(),:]")


# ==================================================================== types
def parse_type(text: str):
    """A column type as the platform names it, e.g. 'ARRAY<STRUCT<id: STRING>>',
    as ('ARRAY', t), ('MAP', key, value), ('STRUCT', {field: t}) or a name ('BIGINT')."""
    tokens = _TOKEN.findall(text or "")
    pos = 0

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1] if pos <= len(tokens) else ""

    def peek() -> str:
        return tokens[pos] if pos < len(tokens) else ""

    def one():
        name = take().upper()
        if name in ("ARRAY", "MAP", "STRUCT") and peek() == "<":
            take()
            if name == "ARRAY":
                t = ("ARRAY", one())
            elif name == "MAP":
                key = one()
                take()                                  # ,
                t = ("MAP", key, one())
            else:
                fields = {}
                while peek() not in (">", ""):
                    field = take()
                    if field.startswith("`"):
                        field = field[1:-1].replace("``", "`")
                    if peek() == ":":
                        take()
                    fields[field] = one()
                    while peek() not in (",", ">", ""):  # NOT NULL, COMMENT ...
                        take()
                    if peek() == ",":
                        take()
                t = ("STRUCT", fields)
            take()                                      # >
            return t
        if peek() == "(":                               # DECIMAL(10,2), VARCHAR(20)
            while take() not in (")", ""):
                pass
        return name

    return one()


def typed(value, t):
    """A value as the API returns it (mostly text) in type `t` of parse_type();
    unchanged if it does not read as that type."""
    if value is None:
        return None
    try:
        if isinstance(t, tuple):
            if isinstance(value, str):
                value = json.loads(value)
            if t[0] == "ARRAY" and isinstance(value, list):
                return [typed(v, t[1]) for v in value]
            if t[0] == "MAP" and isinstance(value, dict):
                return {k: typed(v, t[2]) for k, v in value.items()}
            if t[0] == "STRUCT" and isinstance(value, dict):
                return {k: typed(v, t[1].get(k, "STRING")) for k, v in value.items()}
            return value
        if not isinstance(value, str):
            return value
        if t in INT_TYPES:
            return int(value)
        if t in FLOAT_TYPES:
            return float(value)
        if t == "BOOLEAN" and value.lower() in ("true", "false"):
            return value.lower() == "true"
        if t == "VARIANT":
            return json.loads(value)
    except ValueError:                                  # incl. json.JSONDecodeError
        pass
    return value


# ================================================================= download
def default_output(experiment: Experiment, settings: Settings) -> Path:
    """<the recorder's folder>/<experiment>_openjii_<date>_<time>.json"""
    folder = Path(settings.get("folder", str(Path.home() / "C12666MA data")))
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", experiment.name.strip())[:40] or "experiment"
    return folder / f"{name}_openjii_{datetime.now():%Y-%m-%d_%H-%M-%S}.json"


def select_tables(tables: list[Table], wanted: list[str]) -> list[Table]:
    """The tables named in `wanted` (as shown on the Data page, or by id), or all."""
    if not wanted:
        return tables
    chosen: list[Table] = []
    for w in wanted:
        match = [t for t in tables if w in (t.name, t.identifier)]
        if not match:
            raise Abort(f"no table {w!r} in the experiment; it has: "
                        + (", ".join(t.name for t in tables) or "none"))
        chosen += [t for t in match if t not in chosen]
    return chosen


def table_pages(client: OpenJIIClient, experiment_id: str, table: Table):
    """Yields (columns, rows) for every page of `table`, the values typed. A row
    uploaded while the pages are read shifts the later pages by one, which repeats
    the last row of a page at the start of the next; the repeat is left out."""
    sizes = list(PAGE_SIZES)
    done = 0                        # rows read, repeats included: the next page's offset
    previous: set[str] = set()
    attempt = 1
    while True:
        try:
            page = client.read_table_page(experiment_id, table.identifier, done // sizes[0] + 1,
                                          sizes[0])
        except PageTooLarge:
            if len(sizes) == 1:
                raise
            sizes.pop(0)
            continue
        except OpenJIIError as exc:
            if attempt == ATTEMPTS:
                raise
            print(f"  {table.name}: {exc}; trying again")
            attempt += 1
            time.sleep(RETRY_S)
            continue
        attempt = 1
        columns = [{"name": c["name"], "type": c.get("type_text") or c.get("type_name") or ""}
                   for c in page["columns"]]
        types = {c["name"]: parse_type(c["type"]) for c in columns}
        rows, seen = [], set()
        for raw in page["rows"]:
            key = json.dumps(raw, sort_keys=True)
            seen.add(key)
            if key not in previous:
                rows.append({k: typed(v, types.get(k, "STRING")) for k, v in raw.items()})
        yield columns, rows
        done += len(page["rows"])
        if len(page["rows"]) < sizes[0]:
            return
        previous = seen


def write_json(out: Path, client: OpenJIIClient, experiment: Experiment, env: Environment,
               tables: list[Table]) -> dict[str, int]:
    """Writes the file page by page, so a large experiment never has to fit in
    memory. -> {identifier: rows written}"""
    head = {"experiment": {"id": experiment.id, "name": experiment.name,
                           "url": env.experiment_url(experiment.id)},
            "environment": env.key,
            "downloaded_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    written: dict[str, int] = {}
    tmp = out.with_name(out.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            # each dict is left open ([:-1] drops its "}") for what follows it
            f.write(json.dumps(head)[:-1] + ', "tables": [')
            for i, t in enumerate(tables):
                n = None
                for columns, rows in table_pages(client, experiment.id, t):
                    if n is None:                       # first page: the table's header
                        meta = {"name": t.name, "identifier": t.identifier, "type": t.type,
                                "columns": columns}
                        f.write(("," if i else "") + "\n" + json.dumps(meta)[:-1] + ', "rows": [')
                        n = 0
                    for row in rows:
                        f.write(("," if n else "") + "\n" + json.dumps(row))
                        n += 1
                    if n // PROGRESS_EVERY > (n - len(rows)) // PROGRESS_EVERY:
                        print(f"  {t.name}: {n} of {t.rows} rows...")
                f.write(f'\n], "row_count": {n}}}')
                written[t.identifier] = n
                print(f"  {t.name}: {n} rows")
            f.write("\n]}\n")
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return written


def run_download(args, prompter: Prompter) -> int:
    settings = Settings(args.settings_file)
    env = environment_for(args, settings)
    client, user = sign_in(settings, env, prompter)
    print(f"openJII ({env.key}): signed in as {user.get('name') or user.get('email') or user.get('id')}")
    experiment = choose_experiment(client, settings, prompter, args.experiment, "Download experiment")
    tables = select_tables(client.list_tables(experiment.id), args.table)
    print(f"\n{experiment.name}  ({env.experiment_url(experiment.id)})")
    if not tables:
        raise Abort(f"'{experiment.name}' has no data yet")
    for t in tables:
        print(f"  {t.name}  ({t.type} table, {t.rows} rows)")
    default = default_output(experiment, settings)
    out = Path(args.output or prompter.ask("Save to", str(default))).expanduser()
    if out.is_dir():
        out = out / default.name
    out.parent.mkdir(parents=True, exist_ok=True)

    print("\nDownloading...")
    written = write_json(out, client, experiment, env, tables)
    print(f"\nSaved {sum(written.values())} rows of {len(tables)} table(s) to {out}")
    short = [f"{t.name} ({written[t.identifier]} of {t.rows})" for t in tables
             if written[t.identifier] < t.rows]
    if short:
        print(f"Fewer rows than the platform lists: {', '.join(short)}. Compare with the "
              "experiment's Data page and download again.", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--settings-file", type=Path, help=argparse.SUPPRESS)
    common.add_argument("--yes", "-y", action="store_true",
                        help="ask nothing; use flags and remembered settings")
    p = argparse.ArgumentParser(prog="python -m c12666ma.download", parents=[common],
                                description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")
    login = sub.add_parser("login", parents=[common], help="(re)enter the openJII API key")
    login.add_argument("--env", choices=list(ENVIRONMENTS))
    p.add_argument("--env", choices=list(ENVIRONMENTS))
    p.add_argument("--experiment", help="experiment link, id or exact name")
    p.add_argument("--table", action="append", default=[], metavar="NAME",
                   help="only this table (name or id); repeat for several (default: all)")
    p.add_argument("--output", "-o", help="JSON file to write, or a folder for it")
    p.add_argument("--api-url", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    prompter = Prompter(args.yes)
    print(f"C12666MA openJII download {__version__}")
    try:
        if args.command == "login":
            return cmd_login(args, prompter)
        return run_download(args, prompter)
    except Abort as exc:
        print(f"\nStopped: {exc}", file=sys.stderr)
        return 2
    except OpenJIIError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped (Ctrl-C); no file written", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
