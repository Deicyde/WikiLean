#!/usr/bin/env python3
"""Load Brain serving datasets into the shared D1 database `wikilean-brain`, and prune them.

    brain_d1_import.py [global options] import --release-root DIR --python PATH
    brain_d1_import.py [global options] import --scratch SERVING SIDECAR
    brain_d1_import.py [global options] prune
    brain_d1_import.py [global options] supersede RELEASE_ID
    brain_d1_import.py [global options] verify --sample N (--release-root DIR | --scratch S T)...

Global options: --receipt-dir (or WIKILEAN_BRAIN_RECEIPT_DIR), one of --database-id UUID or
--wrangler-config PATH (the Brain database has no entry in wiki/wrangler.jsonc yet), and
--dry-run or --execute, one of which import, prune and supersede need. A dry run takes the
lock, reads the remote state, prints what it would do, and changes nothing.

The design is the D1 read-layer design, section 5. Rows belong to serving datasets, named by
the logical root of the sidecar build_serving_db.py writes; `releases` maps releases to them.

- import: take the promoter's PromotionLock on its receipt root and refuse while any promotion
  attempt is incomplete; check the serving file against its sidecar (refusing up front an FTS
  text too long for one statement or a row over D1's row limit) and the migration on D1;
  register the dataset (finishing an interrupted delete of it first) and the release as a
  candidate, then prune, so the prune can never delete the dataset this release reuses; skip
  the copy when the dataset is already ready; otherwise create the two FTS tables first (names
  falls back to parts, recorded in datasets, if D1 has no trigram tokenizer and --names-mode
  was not given), copy every table in seq ranges, each range a DELETE then one multi-row
  INSERT under the statement byte budget (rows too long for one statement are finished with
  conditional appends), read each range back and compare the digest of its copied columns
  with the local file before journaling it in import_ranges, then fill the FTS tables by rowid
  range the same way, check sampled MATCH queries (a failure empties that FTS table for the
  rerun) and finish with the state UPDATE. A resumed import copies only the seq gaps between
  journaled ranges. --names-mode may change while the dataset imports and no names range is
  journaled.
- --scratch takes a serving file and its sidecar with no frozen release (the step-2 trial
  import): it recomputes every digest and registers the release row scratch:<dataset id>.
- prune: deletes the release rows nothing protects, then every dataset no release references:
  state 'deleting', its journal rows, its FTS tables, its rows in delete_bounds ranges, and its
  datasets row last. An interrupted prune leaves a 'deleting' dataset the next prune finishes.
- supersede: marks a candidate superseded, so the next prune can delete it.
- verify: read-only and lock-free. Re-reads N random journaled ranges and compares them with
  the local serving file and with import_ranges, and runs the sampled MATCH queries.

Every statement reaches D1 through one function, D1.call, which runs
`wrangler d1 execute <db> --remote --json --command <sql>` (never --file: on a remote database
--file goes through the import API, which stops the database serving queries). Only 64-hex
dataset ids and fixed table names are pasted into SQL text; every value goes through
sql_literal.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from brain_deploy_journal import (
    EventJournal,
    JournalError,
    PromotionLock,
    list_incomplete_attempts,
    validate_target_receipt_root,
)
from brain_http import TransportError, probe_selector
from brain_promote_release import (
    PRODUCTION_ORIGIN,
    CommandRunner,
    PromotionError,
    RunResult,
    _node_environment,
    _env_float,
    _resolved_executable,
    extract_last_json_value,
    selector_from_probe,
    sha256_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT / "brain" / "tools", REPO_ROOT / "brain"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import build_serving_db as bsd  # noqa: E402
from authority_contracts import VerificationError, logical_json_root  # noqa: E402
from store import canonical_json  # noqa: E402

DATABASE = "wikilean-brain"
RESULT_SCHEMA = "wikilean.brain-d1-import-result/v1"
SUPPORTED_SCHEMA_VERSIONS = (1,)
SERVING_PATH = "brain/data/serving.sqlite3"
SIDECAR_PATH = "brain/data/serving-tables.json"
HASH_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SCRATCH_PREFIX = "scratch:"

STATEMENT_BYTES = 90_000      # D1 caps statement text at 100 KB (memory)
# one --command argument: macOS caps argv plus environment at 1 MiB, Linux each argument at
# 128 KiB (MAX_ARG_STRLEN)
CALL_BYTES = 120_000 if sys.platform.startswith("linux") else 400_000
RANGES_PER_CALL = 4
CALL_ATTEMPTS = 3
SIZE_CEILING = 9_000_000_000  # stop before the 10 GB database cap
SLOW_CALL_MS = 2_000          # a call whose statements took longer makes the importer pause
FTS_MERGE_PAGES = 500         # one ('merge', N) step after every FTS write call
FTS_CRISISMERGE = 64
FTS_SAMPLES = 30
MAX_JSON_INT = 2**53 - 1      # D1 answers in JSON; larger integers would not round-trip
ROW_BYTES = 2_000_000         # D1's largest row
HEX_TEXT = "CAST(X'' AS TEXT)"   # the wrapper of a string that holds NUL (see sql_literal)

# The D1 columns of every data table after `ds`, in the builder's column order. The local
# file has these plus LOCAL_ONLY, which D1 omits (they only fill the FTS tables).
D1_COLUMNS: dict[str, tuple[str, ...]] = {
    "cell_pages": ("seq", "id", "json"),
    "aliases": ("seq", "kind", "key", "owner"),
    "labels": ("seq", "ord", "id", "label", "aka", "f", "p", "libs", "xy"),
    "supercells": ("seq", "ord", "path", "label", "aka", "parent", "fa", "frontier", "n_cells",
                   "n_sub", "fhist", "kids", "syn", "json"),
    "name_keys": ("seq", "name_lc", "kind", "owner", "len16"),
    "hot_prefixes": ("seq", "prefix", "cells", "sups", "live_sups"),
    "cell_under": ("seq", "anc", "ord"),
    "synapse_traces": ("seq", "pair", "json"),
    "xrefs": ("seq", "page_ord", "node_ord", "page", "node"),
    "text_ids": ("seq", "node", "owner"),
}
LOCAL_ONLY: dict[str, tuple[str, ...]] = {"name_keys": ("name",), "text_ids": ("body",)}
DATA_TABLES = tuple(D1_COLUMNS)
# the per-dataset FTS tables: journal name -> (source table, source column)
FTS_TABLES: dict[str, tuple[str, str]] = {"names": ("name_keys", "name"), "texts": ("text_ids", "body")}
NAMES_MODES = ("trigram", "parts")
CONTROL_COLUMNS: dict[str, tuple[str, ...]] = {
    "datasets": ("ds", "dataset_id", "schema_version", "tables_json", "longest_name",
                 "sup_names_from", "names_mode", "state", "imported_at"),
    "releases": ("release_id", "seq", "ds", "manifest_sha256", "state", "registered_at"),
    "import_ranges": ("ds", "tbl", "lo", "hi", "digest", "verified_at"),
}
# The data tables and columns each schema_version has on D1, frozen: a later version adds its
# own entry and never edits one, so the pruner can always delete a dataset of an older version.
# The importer refuses any other version.
REQUIRED_COLUMNS: dict[int, dict[str, tuple[str, ...]]] = {
    1: {
        "datasets": ("ds", "dataset_id", "schema_version", "tables_json", "longest_name",
                     "sup_names_from", "names_mode", "state", "imported_at"),
        "releases": ("release_id", "seq", "ds", "manifest_sha256", "state", "registered_at"),
        "import_ranges": ("ds", "tbl", "lo", "hi", "digest", "verified_at"),
        "cell_pages": ("ds", "seq", "id", "json"),
        "aliases": ("ds", "seq", "kind", "key", "owner"),
        "labels": ("ds", "seq", "ord", "id", "label", "aka", "f", "p", "libs", "xy"),
        "supercells": ("ds", "seq", "ord", "path", "label", "aka", "parent", "fa", "frontier",
                       "n_cells", "n_sub", "fhist", "kids", "syn", "json"),
        "name_keys": ("ds", "seq", "name_lc", "kind", "owner", "len16"),
        "hot_prefixes": ("ds", "seq", "prefix", "cells", "sups", "live_sups"),
        "cell_under": ("ds", "seq", "anc", "ord"),
        "synapse_traces": ("ds", "seq", "pair", "json"),
        "xrefs": ("ds", "seq", "page_ord", "node_ord", "page", "node"),
        "text_ids": ("ds", "seq", "node", "owner"),
    },
}
TABLES_BY_VERSION: dict[int, tuple[str, ...]] = {
    version: tuple(t for t in columns if t not in CONTROL_COLUMNS)
    for version, columns in REQUIRED_COLUMNS.items()}
# the UNIQUE keys each table needs on D1: every range DELETE, readback and ON CONFLICT uses one
REQUIRED_KEYS: dict[int, dict[str, tuple[str, ...]]] = {
    version: {"datasets": ("dataset_id",), "releases": ("release_id",),
              "import_ranges": ("ds", "tbl", "lo"), **{t: ("ds", "seq") for t in tables}}
    for version, tables in TABLES_BY_VERSION.items()}
assert DATA_TABLES == bsd.DATA_TABLES, "the importer and the builder disagree on the tables"
assert REQUIRED_COLUMNS[SUPPORTED_SCHEMA_VERSIONS[-1]] == {
    **CONTROL_COLUMNS, **{t: ("ds", *c) for t, c in D1_COLUMNS.items()}}, \
    "D1_COLUMNS changed: add a schema_version to REQUIRED_COLUMNS instead of editing one"


class D1ImportError(RuntimeError):
    """A fail-closed import, prune or verify error."""


class D1CallError(D1ImportError):
    """A wrangler call failed on every attempt."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------------------------
# SQL text


def sql_literal(value: object) -> str:
    """The one way a value enters SQL text: NULL, an integer, or a quoted string.

    A string is quoted with its single quotes doubled. NUL cannot appear in statement text
    (nor in a command-line argument), so a string holding NUL becomes one expression node,
    `CAST(X'<its UTF-8 in hex>' AS TEXT)`, whose value is the same TEXT (one node however many
    NULs, so no expression-depth limit applies). Non-ASCII stays as UTF-8 text; a lone surrogate,
    which has no UTF-8 form, is refused. Floats are refused: they belong inside JSON text
    (the digest rule), and integers beyond 2**53 would not come back exactly from D1's JSON."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        raise D1ImportError("refusing to write a boolean: the serving tables hold none")
    if isinstance(value, int):
        if abs(value) > MAX_JSON_INT:
            raise D1ImportError(f"integer {value} does not round-trip through D1's JSON")
        return str(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise D1ImportError(f"string has no UTF-8 form: {exc}") from None
        if "\0" in value:
            return f"CAST(X'{value.encode('utf-8').hex().upper()}' AS TEXT)"
        return "'" + value.replace("'", "''") + "'"
    raise D1ImportError(f"refusing to write a {type(value).__name__} value")


def literal_bytes(text: str) -> int:
    """len(sql_literal(text).encode()) without building it."""
    raw = utf8_len(text)
    if "\0" in text:
        return len(HEX_TEXT) + 2 * raw
    return 2 + raw + text.count("'")


def chunk_end(text: str, start: int, max_bytes: int) -> int:
    """The largest end such that sql_literal(text[start:end]) is at most max_bytes long.

    The cost only grows with end (a NUL switches to the hex form, which costs more than the
    quoted form of the same text), so the first character that does not fit ends the chunk."""
    raw = quotes = 0
    nul = False
    end = start
    while end < len(text):
        ch = text[end]
        raw_, quotes_, nul_ = raw + len(ch.encode("utf-8")), quotes + (ch == "'"), nul or ch == "\0"
        if (len(HEX_TEXT) + 2 * raw_ if nul_ else 2 + raw_ + quotes_) > max_bytes:
            break
        raw, quotes, nul = raw_, quotes_, nul_
        end += 1
    return end


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def dataset_hex(dataset_id: object) -> str:
    match = HASH_RE.fullmatch(str(dataset_id))
    if match is None:
        raise D1ImportError(f"not a dataset id: {dataset_id!r}")
    return match.group(1)


def fts_table(kind: str, hex_id: str) -> str:
    """names_<hex> or texts_<hex>: a fixed prefix and a checked 64-hex id, nothing else."""
    if kind not in FTS_TABLES or HEX_RE.fullmatch(hex_id) is None:
        raise D1ImportError(f"refusing FTS table name {kind}_{hex_id}")
    return f"{kind}_{hex_id}"


def checked_table(name: object) -> str:
    if name not in D1_COLUMNS and name not in CONTROL_COLUMNS:
        raise D1ImportError(f"not a Brain table: {name!r}")
    return str(name)


def name_parts(name: str) -> str:
    """A name split at capitals, dots, underscores and slashes, for names_mode 'parts'."""
    spaced = re.sub(r"(?<=[^\W_])(?=[A-Z])", " ", name)
    return " ".join(part for part in re.split(r"[._/\s]+", spaced) if part)


def fts_create(kind: str, hex_id: str, names_mode: str) -> str:
    table = fts_table(kind, hex_id)
    if kind == "texts":
        return f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(body, content='', contentless_delete=1)"
    if names_mode == "trigram":
        return (f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(name, content='', "
                "contentless_delete=1, tokenize='trigram')")
    if names_mode == "parts":
        return (f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(parts, content='', "
                "contentless_delete=1, prefix='2 3')")
    raise D1ImportError(f"unknown names_mode {names_mode!r}")


def fts_column(kind: str, names_mode: str) -> str:
    return "body" if kind == "texts" else ("name" if names_mode == "trigram" else "parts")


def fts_content(kind: str, names_mode: str, text: str) -> str:
    return name_parts(text) if kind == "names" and names_mode == "parts" else text


# ---------------------------------------------------------------------------------------------
# The local side: a serving file and its sidecar


@dataclass
class LocalDataset:
    serving: Path
    sidecar_path: Path
    sidecar: dict[str, Any]
    dataset_id: str
    db: sqlite3.Connection

    @property
    def hex(self) -> str:
        return dataset_hex(self.dataset_id)

    @property
    def schema_version(self) -> int:
        return int(self.sidecar["schema_version"])

    @property
    def tables(self) -> dict[str, dict[str, Any]]:
        return self.sidecar["tables"]

    @property
    def tables_json(self) -> str:
        return canonical_json(self.tables)

    def rows(self, table: str) -> int:
        if table in FTS_TABLES:
            table = FTS_TABLES[table][0]
        return int(self.tables[table]["rows"])

    def close(self) -> None:
        self.db.close()


def _int(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise D1ImportError(f"{label} must be an integer of at least {minimum}")
    return value


def validate_sidecar(sidecar: object) -> dict[str, Any]:
    if not isinstance(sidecar, dict):
        raise D1ImportError("the sidecar is not a JSON object")
    expected = {"schema", "schema_version", "tables", "longest_name", "sup_names_from", "aliases_counts"}
    if set(sidecar) != expected:
        raise D1ImportError(f"sidecar keys differ from {sorted(expected)}: {sorted(sidecar)}")
    if sidecar["schema"] != bsd.SIDECAR_SCHEMA:
        raise D1ImportError(f"sidecar schema {sidecar['schema']!r} is not {bsd.SIDECAR_SCHEMA}")
    version = _int(sidecar["schema_version"], "schema_version", minimum=1)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise D1ImportError(f"schema_version {version} is not one this importer supports "
                            f"({', '.join(map(str, SUPPORTED_SCHEMA_VERSIONS))})")
    _int(sidecar["longest_name"], "longest_name")
    _int(sidecar["sup_names_from"], "sup_names_from", minimum=1)
    tables = sidecar["tables"]
    if not isinstance(tables, dict) or set(tables) != set(DATA_TABLES):
        raise D1ImportError(f"sidecar tables differ from {list(DATA_TABLES)}")
    for name, meta in tables.items():
        if not isinstance(meta, dict) or set(meta) != {"rows", "digest", "delete_bounds"}:
            raise D1ImportError(f"sidecar table {name} is malformed")
        rows = _int(meta["rows"], f"{name}.rows")
        if not isinstance(meta["digest"], str) or HEX_RE.fullmatch(meta["digest"]) is None:
            raise D1ImportError(f"sidecar table {name} has a malformed digest")
        bounds = meta["delete_bounds"]
        if (not isinstance(bounds, list)
                or any(not isinstance(b, int) or isinstance(b, bool) or not 1 <= b <= rows for b in bounds)
                or bounds != sorted(set(bounds))):
            raise D1ImportError(f"sidecar table {name} has malformed delete_bounds")
    return sidecar


def open_local(serving: Path, sidecar_path: Path, *, full_check: bool) -> LocalDataset:
    """Open a serving file read-only, check it against its sidecar, and name its dataset.

    With full_check every table's digest, row count and delete bounds are recomputed from the
    file and compared with the sidecar (what release verification does once it knows the D1
    layout, step 4); without it only the file's own `tables` rows are compared."""
    try:
        sidecar = validate_sidecar(json.loads(sidecar_path.read_text(encoding="utf-8")))
        dataset_id = logical_json_root(sidecar_path)
    except (OSError, ValueError, VerificationError) as exc:
        raise D1ImportError(f"cannot read the sidecar {sidecar_path}: {exc}") from exc
    dataset_hex(dataset_id)
    try:
        db = sqlite3.connect(f"{serving.resolve(strict=True).as_uri()}?mode=ro&immutable=1", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise D1ImportError(f"cannot open the serving file {serving}: {exc}") from exc
    local = LocalDataset(serving, sidecar_path, sidecar, dataset_id, db)
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        if meta.get("schema_version") != str(sidecar["schema_version"]):
            raise D1ImportError(f"{serving} has schema_version {meta.get('schema_version')!r}, "
                                f"its sidecar {sidecar['schema_version']}")
        recorded = {name: {"rows": rows, "digest": digest, "delete_bounds": json.loads(bounds)}
                    for name, rows, digest, bounds in db.execute(
                        "SELECT name, rows, digest, delete_bounds FROM tables")}
        if recorded != sidecar["tables"]:
            raise D1ImportError(f"{serving} and {sidecar_path} describe different tables")
        for table in DATA_TABLES:
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            if columns != [*D1_COLUMNS[table], *LOCAL_ONLY.get(table, ())]:
                raise D1ImportError(f"{serving} table {table} has columns {columns}")
        if full_check:
            for table in DATA_TABLES:
                if bsd.digest_table(db, table) != sidecar["tables"][table]:
                    raise D1ImportError(f"{serving} table {table} does not match its sidecar digest")
    except (sqlite3.Error, ValueError) as exc:
        local.close()
        raise D1ImportError(f"cannot check the serving file {serving}: {exc}") from exc
    except BaseException:
        local.close()
        raise
    return local


@dataclass(frozen=True)
class ReleaseFiles:
    release_id: str
    manifest_sha256: str
    serving: Path
    sidecar: Path


def release_files(root: Path) -> ReleaseFiles:
    """The serving artifacts a frozen release.json binds, with the sidecar's logical root."""
    manifest_path = root / "release.json"
    try:
        body = manifest_path.read_bytes()
        manifest = json.loads(body)
    except (OSError, ValueError) as exc:
        raise D1ImportError(f"cannot read {manifest_path}: {exc}") from exc
    release_id = manifest.get("release_id") if isinstance(manifest, dict) else None
    if not isinstance(release_id, str) or HASH_RE.fullmatch(release_id) is None:
        raise D1ImportError(f"{manifest_path} has no release id")
    artifacts = {item.get("path"): item for item in manifest.get("artifacts") or []
                 if isinstance(item, dict)}
    serving, sidecar = artifacts.get(SERVING_PATH), artifacts.get(SIDECAR_PATH)
    if serving is None or sidecar is None:
        raise D1ImportError(f"release {release_id} does not bind {SERVING_PATH} and {SIDECAR_PATH}: "
                            "it is not a D1 release")
    sidecar_file = root / SIDECAR_PATH
    try:
        root_hash = logical_json_root(sidecar_file)
    except (OSError, VerificationError) as exc:
        raise D1ImportError(f"cannot read {sidecar_file}: {exc}") from exc
    if sidecar.get("logical_root") != root_hash:
        raise D1ImportError(f"release {release_id} binds a different sidecar than {sidecar_file}")
    return ReleaseFiles(release_id, sha256_bytes(body), root / SERVING_PATH, sidecar_file)


# ---------------------------------------------------------------------------------------------
# D1: the one place a statement leaves this process


@dataclass
class CallStats:
    calls: int = 0
    statements: int = 0
    largest_statement: int = 0
    rows_written: int = 0
    duration_ms: float = 0.0
    size_after_first: int | None = None
    size_after: int | None = None
    started: float = field(default_factory=time.monotonic)

    def summary(self) -> dict[str, object]:
        return {"calls": self.calls, "statements": self.statements,
                "largest_statement_bytes": self.largest_statement,
                "rows_written": self.rows_written, "d1_duration_ms": round(self.duration_ms, 3),
                "size_after_first": self.size_after_first, "size_after": self.size_after,
                "wall_seconds": round(time.monotonic() - self.started, 3)}


class D1:
    """Runs statements on the remote Brain database through `wrangler d1 execute --command`.

    The runner has CommandRunner.run's signature, so tests replace it with one that executes
    the --command text locally. Every write the importer sends is idempotent (a range is a
    DELETE then an INSERT, appends are conditional, journal rows are ON CONFLICT DO NOTHING),
    so a failed call, applied or not, is simply sent again."""

    def __init__(self, *, repo: Path, database: str, config: Path, runner: Any,
                 timeout: float = 900.0, statement_bytes: int = STATEMENT_BYTES,
                 call_bytes: int = CALL_BYTES, attempts: int = CALL_ATTEMPTS,
                 size_ceiling: int = SIZE_CEILING, slow_call_ms: float = SLOW_CALL_MS,
                 node: Path | None = None, wrangler: Path | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[dict[str, object]], None] | None = None) -> None:
        self.repo = repo
        self.wiki = repo / "wiki"
        self.database = database
        self.config = config
        self.runner = runner
        self.timeout = timeout
        self.statement_bytes = statement_bytes
        self.call_bytes = call_bytes
        self.attempts = max(1, attempts)
        self.size_ceiling = size_ceiling
        self.slow_call_ms = slow_call_ms
        self._node = node
        self._wrangler = wrangler
        self.sleep = sleep
        self.log = log or (lambda record: print(json.dumps(record, sort_keys=True), file=sys.stderr))
        self.stats = CallStats()
        self.last_label: dict[str, object] = {}

    def _tools(self) -> tuple[Path, Path]:
        if self._node is None:
            self._node = _resolved_executable("node")
        if self._wrangler is None:
            self._wrangler = self.wiki / "node_modules" / "wrangler" / "bin" / "wrangler.js"
            if self._wrangler.is_symlink() or not self._wrangler.is_file():
                raise D1ImportError(f"the installed Wrangler entry point is missing: {self._wrangler}")
        return self._node, self._wrangler

    def command(self, sql: str) -> list[str]:
        node, wrangler = self._tools()
        return [str(node), str(wrangler), "d1", "execute", self.database, "--remote", "--json",
                "--config", str(self.config), "--command", sql]

    def call(self, statements: Sequence[str], *, expect_results: bool = False,
             attempts: int | None = None, label: Mapping[str, object] | None = None) -> list[dict[str, Any]]:
        """Send statements as one call; return wrangler's per-statement results.

        Every call must answer one result per statement; with expect_results each must also
        carry a `results` list of rows (the reads index them). A call that fails is sent again,
        up to `attempts` times in all. `label` (phase, table, seq range) goes into every log
        record and into the error of a call that fails."""
        if not statements:
            return []
        label = dict(label or {})
        self.last_label = label
        where = f" ({' '.join(f'{key}={value}' for key, value in label.items())})" if label else ""
        for statement in statements:
            size = utf8_len(statement)
            if size > self.statement_bytes:
                raise D1ImportError(f"a {size}-byte statement exceeds the {self.statement_bytes}-byte "
                                    "budget: raise --statement-bytes")
            if "\0" in statement:
                raise D1ImportError("internal: NUL in statement text")
        sql = ";\n".join(statements) + ";"
        if utf8_len(sql) > self.call_bytes and len(statements) > 1:
            raise D1ImportError(f"internal: a {utf8_len(sql)}-byte call exceeds the call budget")
        args = self.command(sql)
        env = _node_environment(self._node, include_cloudflare=True) if self._node_is_real() else None
        detail = ""
        for attempt in range(1, (attempts or self.attempts) + 1):
            started = time.monotonic()
            try:
                result = self.runner.run(args, cwd=self.wiki, timeout=self.timeout, env=env)
            except OSError as exc:
                raise D1ImportError(f"cannot run wrangler with a {utf8_len(sql)}-byte call{where}: "
                                    f"{exc}; lower --call-bytes if the argument is too long") from exc
            parsed, detail = self._parse(result, expect_results=expect_results)
            self.stats.calls += 1
            record: dict[str, object] = {
                "call": self.stats.calls, "attempt": attempt, "statements": len(statements),
                "bytes": utf8_len(sql), "largest_statement": max(map(utf8_len, statements)),
                "wall_ms": round((time.monotonic() - started) * 1000, 3), "ok": parsed is not None,
                **label}
            if parsed is None:
                record["error"] = detail[-500:]
                self.log(record)
                if attempt < (attempts or self.attempts):
                    self.sleep(min(30, 2 ** attempt))
                continue
            if len(parsed) != len(statements):
                raise D1ImportError(f"wrangler answered {len(parsed)} results for "
                                    f"{len(statements)} statements{where}")
            self._account(parsed, statements, record)
            self.log(record)
            return parsed
        raise D1CallError(f"wrangler d1 execute failed {attempts or self.attempts} time(s){where}: "
                          f"{detail.strip()[-2000:]}")

    def _node_is_real(self) -> bool:
        return self._node is not None and self._node.is_absolute() and self._node.exists()

    @staticmethod
    def _parse(result: RunResult, *, expect_results: bool = False) -> tuple[list[dict[str, Any]] | None, str]:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace")
        if not result.ok:
            state = "timed out" if result.timed_out else f"returned {result.returncode}"
            return None, f"{state}: {detail}"
        text = (result.stdout or b"").decode("utf-8", errors="replace")
        try:
            value = json.loads(text)
        except ValueError:
            # Wrangler may print a banner before the JSON: take the first line that starts a
            # JSON array running to the end of the output (one decode per candidate line)
            decoder, value = json.JSONDecoder(), None
            for match in re.finditer(r"(?m)^\[", text):
                try:
                    candidate, end = decoder.raw_decode(text, match.start())
                except ValueError:
                    continue
                if not text[end:].strip():
                    value = candidate
                    break
            if value is None:
                return None, f"Wrangler D1 execute emitted no JSON array: {detail}"
        if (not isinstance(value, list) or not value
                or any(not isinstance(item, dict) or item.get("success") is not True
                       or not isinstance(item.get("results", []), list)
                       or any(not isinstance(row, dict) for row in item.get("results", []))
                       or (expect_results and "results" not in item) for item in value)):
            return None, f"not a successful D1 result: {detail}"
        return value, ""

    def _account(self, parsed: list[dict[str, Any]], statements: Sequence[str],
                 record: dict[str, object]) -> None:
        duration = written = 0.0
        size_after = None
        for item in parsed:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            try:
                duration += float(meta.get("duration") or 0)
                written += int(meta.get("rows_written") or 0)
            except (TypeError, ValueError) as exc:
                raise D1ImportError(f"wrangler answered malformed meta {meta}: {exc}") from exc
            if isinstance(meta.get("size_after"), int):
                size_after = meta["size_after"]
        stats = self.stats
        stats.statements += len(statements)
        stats.largest_statement = max(stats.largest_statement, *map(utf8_len, statements))
        stats.rows_written += int(written)
        stats.duration_ms += duration
        record.update({"duration_ms": round(duration, 3), "rows_written": int(written),
                       "size_after": size_after})
        if size_after is not None:
            if stats.size_after_first is None:
                stats.size_after_first = size_after
            stats.size_after = size_after
            if size_after > self.size_ceiling:
                raise D1ImportError(f"the database holds {size_after} bytes, over the "
                                    f"{self.size_ceiling}-byte ceiling: stopping")
        if duration > self.slow_call_ms:
            self.sleep(duration / 1000)

    def rows(self, statement: str) -> list[dict[str, Any]]:
        return self.call([statement], expect_results=True)[0].get("results") or []

    def calls(self, statements: Sequence[str], **kwargs: Any) -> list[dict[str, Any]]:
        """call() over as few calls as the call budget allows; the results in statement order."""
        results: list[dict[str, Any]] = []
        part: list[str] = []
        size = -1               # a call is sum(len(statement) + 2) - 1 bytes
        for statement in statements:
            cost = utf8_len(statement) + 2
            if part and size + cost > self.call_bytes:
                results += self.call(part, **kwargs)
                part, size = [], -1
            part.append(statement)
            size += cost
        return results + self.call(part, **kwargs)


class WranglerConfig(contextlib.AbstractContextManager):
    """A config naming the Brain database: the operator's, or a private one written for an id."""

    def __init__(self, *, config: Path | None, database_id: str | None, database: str) -> None:
        if (config is None) == (database_id is None):
            raise D1ImportError("pass exactly one of --wrangler-config and --database-id")
        if database_id is not None and UUID_RE.fullmatch(database_id) is None:
            raise D1ImportError("--database-id must be a lowercase D1 database UUID")
        self.config, self.database_id, self.database = config, database_id, database
        self._temporary: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        if self.config is not None:
            if not self.config.is_absolute() or not self.config.is_file():
                raise D1ImportError("--wrangler-config must name an existing absolute file")
            return self.config
        self._temporary = tempfile.TemporaryDirectory(prefix="brain-d1-import-")
        path = Path(self._temporary.name) / "wrangler.json"
        path.write_text(json.dumps({
            "name": "wikilean-brain-import",
            "compatibility_date": "2025-01-01",
            "d1_databases": [{"binding": "BRAIN_DB", "database_name": self.database,
                              "database_id": self.database_id}],
        }, indent=1) + "\n", encoding="utf-8")
        return path

    def __exit__(self, *exc: object) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()


# ---------------------------------------------------------------------------------------------
# Remote state and the protected set


@dataclass(frozen=True)
class ReleaseRow:
    release_id: str
    seq: int
    ds: int
    state: str
    dataset_id: str | None
    dataset_state: str | None


@dataclass(frozen=True)
class DatasetRow:
    ds: int
    dataset_id: str
    state: str
    schema_version: int
    tables_json: str
    longest_name: int
    sup_names_from: int
    names_mode: str | None
    imported_at: str | None


@dataclass(frozen=True)
class RemoteState:
    releases: tuple[ReleaseRow, ...]
    datasets: tuple[DatasetRow, ...]

    def release(self, release_id: str) -> ReleaseRow | None:
        return next((r for r in self.releases if r.release_id == release_id), None)

    def dataset(self, dataset_id: str) -> DatasetRow | None:
        return next((d for d in self.datasets if d.dataset_id == dataset_id), None)


@dataclass(frozen=True)
class Protection:
    releases: dict[str, str]     # release id -> why it is protected
    datasets: frozenset[int]     # ds of every dataset a protected release references


def live_selector(base_url: str, opener: Callable[..., object] | None = None) -> tuple[str | None, str | None]:
    """The live selector's current and previous release ids, fetched as the promoter does."""
    try:
        probe = probe_selector(base_url, opener=opener)
    except TransportError as exc:
        raise D1ImportError(f"cannot fetch the live selector: {exc}") from exc
    if probe.status != 200:
        raise D1ImportError(f"the live selector answered HTTP {probe.status}: refusing to work "
                            "without knowing what is live")
    try:
        state = selector_from_probe(probe, "sha256:" + "0" * 64,
                                    candidate_manifest_sha256="0" * 64, allow_first_deploy=False)
    except PromotionError as exc:
        raise D1ImportError(f"the live selector is malformed: {exc}") from exc
    return state.current_release_id, state.previous_release_id


def attempt_selector_releases(attempts: Iterable[EventJournal]) -> dict[str, str]:
    """Releases the staged and predeploy selectors of incomplete attempts name.

    These are protected until the attempt reaches its final_state; a terminal attempt protects
    nothing (the live selector is authoritative after it). Import and prune refuse while any
    attempt is incomplete, so in practice this only matters if that refusal is relaxed."""
    named: dict[str, str] = {}
    for journal in attempts:
        if journal.terminal or not journal.events:
            continue
        intent = journal.events[0].get("payload") or {}
        for part in ("staged_selector", "predeploy"):
            selector = intent.get(part) if isinstance(intent.get(part), dict) else {}
            for key in ("release_id", "previous_release_id"):
                value = selector.get(key)
                if isinstance(value, str) and value:
                    named.setdefault(value, f"named by the {part} of incomplete attempt {journal.attempt_id}")
    return named


def protected_set(state: RemoteState, live: tuple[str | None, str | None],
                  attempts: Mapping[str, str] | None = None,
                  extra: Mapping[str, str] | None = None) -> Protection:
    """The releases pruning must keep (section 5, Pruning), and their datasets."""
    current, previous = live
    why: dict[str, str] = {}
    if current:
        why[current] = "live current"
    if previous:
        why.setdefault(previous, "live previous")
    for release_id, reason in (attempts or {}).items():
        why.setdefault(release_id, reason)
    for release_id, reason in (extra or {}).items():
        why.setdefault(release_id, reason)
    current_row = state.release(current) if current else None
    for row in state.releases:
        if row.state != "candidate":
            continue
        if current_row is None or row.seq > current_row.seq:
            why.setdefault(row.release_id, "candidate newer than the live current")
        if row.dataset_state == "importing":
            why.setdefault(row.release_id, "candidate whose dataset is importing")
    datasets = frozenset(row.ds for row in state.releases if row.release_id in why)
    return Protection(why, datasets)


# ---------------------------------------------------------------------------------------------
# Ranges


@dataclass
class Range:
    table: str              # a data table, or names / texts
    lo: int
    hi: int
    digest: str
    primary: list[str]      # DELETE, then INSERT
    appends: list[str] = field(default_factory=list)

    @property
    def call_size(self) -> int:
        """Bytes its statements add to a call: each one and its ";\\n" separator (the last
        statement of a call ends in ";" alone, so a call is sum(call_size) - 1 bytes)."""
        return sum(utf8_len(s) + 2 for s in self.primary)


def gaps(ranges: Sequence[tuple[int, int]], rows: int) -> list[tuple[int, int]]:
    """The seq spans of 1..rows no recorded range covers."""
    result, nxt = [], 1
    for lo, hi in ranges:
        if lo > nxt:
            result.append((nxt, lo - 1))
        nxt = hi + 1
    if nxt <= rows:
        result.append((nxt, rows))
    return result


class RowDigest:
    """store.digest_rows, fed one row at a time."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()

    def add(self, row: Mapping[str, Any]) -> None:
        self._hash.update(canonical_json(dict(row)).encode("utf-8") + b"\n")

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def digest_of(rows: Iterable[Mapping[str, Any]]) -> str:
    digest = RowDigest()
    for row in rows:
        digest.add(row)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# The importer


@dataclass
class Options:
    execute: bool = False
    statement_bytes: int = STATEMENT_BYTES
    call_bytes: int = CALL_BYTES
    ranges_per_call: int = RANGES_PER_CALL
    fts_merge_pages: int = FTS_MERGE_PAGES
    fts_samples: int = FTS_SAMPLES


class Importer:
    def __init__(self, d1: D1, *, options: Options,
                 selector: Callable[[], tuple[str | None, str | None]],
                 receipt_root: Path | None = None) -> None:
        self.d1 = d1
        self.options = options
        self.selector = selector
        self.receipt_root = receipt_root
        self.actions: list[dict[str, object]] = []

    # -- the lock -------------------------------------------------------------------------------

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """The promoter's lock on its receipt root, refusing while any attempt is incomplete."""
        if self.receipt_root is None:
            raise D1ImportError("import, prune and supersede need --receipt-dir")
        try:
            with PromotionLock(self.receipt_root):
                incomplete = list_incomplete_attempts(self.receipt_root)
                if incomplete:
                    raise D1ImportError("incomplete promotion attempt(s) block D1 import and prune: "
                                        + ", ".join(j.attempt_id for j in incomplete)
                                        + "; reconcile each first with `bash site/ops/brain-promote-release.sh "
                                        "--receipt-dir <receipt dir> --reconcile-attempt <attempt id> "
                                        "--approval-note ...` (docs/BRAIN-RELEASE-RUNBOOK.md)")
                yield
        except JournalError as exc:
            raise D1ImportError(str(exc)) from exc

    def _attempts(self) -> dict[str, str]:
        if self.receipt_root is None:
            return {}
        return attempt_selector_releases(list_incomplete_attempts(self.receipt_root))

    # -- reads ----------------------------------------------------------------------------------

    def preflight(self, local: LocalDataset) -> None:
        """Refuse, before any write, a dataset whose import could never finish (local, read-only):
        an FTS text too long for one statement in either names mode (FTS rows cannot be appended
        to), or a row larger than D1's row limit."""
        budget = self.options.statement_bytes
        for kind, (source, column) in FTS_TABLES.items():
            modes = NAMES_MODES if kind == "names" else NAMES_MODES[:1]
            headers = {m: utf8_len(f"INSERT INTO {fts_table(kind, local.hex)}(rowid, {fts_column(kind, m)}) VALUES ")
                       for m in modes}
            # a literal costs at most 4 bytes per byte of text (parts at most doubles a name, the
            # hex form doubles again), plus its wrapper: only longer texts need the exact check
            floor = (budget - max(headers.values()) - 64) // 4
            for seq, text in local.db.execute(f"SELECT seq, {column} FROM {source} "
                                              f"WHERE length(CAST({column} AS BLOB)) > ?", (floor,)):
                for mode, header in headers.items():
                    literal = f"({sql_literal(seq)}, {sql_literal(fts_content(kind, mode, text))})"
                    if header + utf8_len(literal) > budget:
                        raise D1ImportError(f"{source} seq {seq}: its {kind} text does not fit one "
                                            f"{budget}-byte statement"
                                            + (f" (names_mode {mode})" if kind == "names" else "")
                                            + ", and FTS rows cannot be appended to: raise --statement-bytes")
        for table, columns in D1_COLUMNS.items():
            size = " + ".join(f"coalesce(length(CAST({c} AS BLOB)), 0)" for c in columns)
            row = local.db.execute(f"SELECT seq, {size} FROM {table} WHERE {size} > ? LIMIT 1",
                                   (ROW_BYTES,)).fetchone()
            if row is not None:
                raise D1ImportError(f"{table} seq {row[0]} holds {row[1]} bytes, over D1's {ROW_BYTES}-byte row limit")

    def require_schema(self, schema_version: int) -> None:
        """Refuse unless the migration for schema_version is on D1 (read-only): every
        column, and a UNIQUE index on exactly each key the importer's statements rely on (a
        table that predates the migration keeps its old shape under CREATE TABLE IF NOT EXISTS,
        and without its (ds, seq) key every range statement would scan the whole table)."""
        required = REQUIRED_COLUMNS.get(schema_version)
        if required is None:
            raise D1ImportError(f"schema_version {schema_version} is not one this importer supports")
        keys = REQUIRED_KEYS[schema_version]
        tables = list(required)
        results = self.d1.calls(
            [f"SELECT name FROM pragma_table_info({sql_literal(checked_table(table))})" for table in tables]
            + [f"SELECT il.name AS idx, ii.name AS col FROM pragma_index_list({sql_literal(checked_table(table))}) "
               "AS il, pragma_index_info(il.name) AS ii WHERE il.\"unique\" = 1 ORDER BY il.name, ii.seqno"
               for table in keys], expect_results=True)
        present = {table: {str(row.get("name")) for row in result.get("results") or []}
                   for table, result in zip(tables, results)}
        missing = [f"{table}.{column}" for table, columns in required.items()
                   for column in columns if column not in present.get(table, set())]
        for table, result in zip(keys, results[len(tables):]):
            indexes: dict[str, list[str]] = {}
            for row in result.get("results") or []:
                indexes.setdefault(str(row.get("idx")), []).append(str(row.get("col")))
            if list(keys[table]) not in indexes.values():
                missing.append(f"{table} UNIQUE({', '.join(keys[table])})")
        if missing:
            raise D1ImportError("wiki/migrations-brain/0001_brain.sql is not applied to the Brain "
                                f"database: missing {', '.join(missing[:8])}"
                                + (" ..." if len(missing) > 8 else ""))

    def remote_state(self) -> RemoteState:
        releases, datasets = self.d1.call([
            "SELECT r.release_id, r.seq, r.ds, r.state, d.dataset_id, d.state AS dataset_state "
            "FROM releases r LEFT JOIN datasets d ON d.ds = r.ds ORDER BY r.seq",
            "SELECT ds, dataset_id, state, schema_version, tables_json, longest_name, sup_names_from, "
            "names_mode, imported_at FROM datasets ORDER BY ds",
        ], expect_results=True)
        try:
            return RemoteState(
                tuple(ReleaseRow(str(r["release_id"]), int(r["seq"]), int(r["ds"]), str(r["state"]),
                                 r.get("dataset_id"), r.get("dataset_state"))
                      for r in releases.get("results") or []),
                tuple(DatasetRow(int(d["ds"]), str(d["dataset_id"]), str(d["state"]),
                                 int(d["schema_version"]), str(d["tables_json"]),
                                 int(d["longest_name"]), int(d["sup_names_from"]), d.get("names_mode"),
                                 d.get("imported_at"))
                      for d in datasets.get("results") or []))
        except (KeyError, TypeError, ValueError) as exc:
            raise D1ImportError(f"the Brain database answered malformed control rows: {exc}") from exc

    def coverage(self, ds: int, rows: Mapping[str, int]) -> dict[str, list[tuple[int, int, str]]]:
        """Journaled ranges per table, checked to be sorted, disjoint and inside 1..rows."""
        recorded: dict[str, list[tuple[int, int, str]]] = {name: [] for name in rows}
        for row in self.d1.rows(f"SELECT tbl, lo, hi, digest FROM import_ranges WHERE ds = {sql_literal(ds)} "
                                "ORDER BY tbl, lo"):
            name, lo, hi, digest = row.get("tbl"), row.get("lo"), row.get("hi"), row.get("digest")
            if name not in recorded or not isinstance(lo, int) or not isinstance(hi, int):
                raise D1ImportError(f"import_ranges holds a malformed row for ds {ds}: {row}")
            recorded[name].append((lo, hi, str(digest)))
        for name, ranges in recorded.items():
            nxt = 1
            for lo, hi, _ in ranges:
                if lo < nxt or hi < lo or hi > rows[name]:
                    raise D1ImportError(f"import_ranges for ds {ds} table {name} overlap or leave "
                                        f"1..{rows[name]}: [{lo}, {hi}]")
                nxt = hi + 1
        return recorded

    # -- prune ----------------------------------------------------------------------------------

    def prune_plan(self, state: RemoteState, protection: Protection) -> tuple[list[str], list[DatasetRow]]:
        releases = [r.release_id for r in state.releases if r.release_id not in protection.releases]
        datasets = [d for d in state.datasets if d.ds not in protection.datasets]
        for d in state.datasets:
            if d.state == "deleting" and d.ds in protection.datasets:
                raise D1ImportError(f"dataset {d.dataset_id} is being deleted but a protected "
                                    "release references it; resolve by hand")
        unknown = [d.dataset_id for d in datasets if d.schema_version not in TABLES_BY_VERSION]
        if unknown:
            raise D1ImportError(f"dataset(s) {', '.join(unknown)} have a schema_version this importer "
                                "does not know: prune with the importer that wrote them")
        for d in datasets:      # every delete plan is checked before the first delete
            self._delete_bounds(d)
        return releases, datasets

    def prune(self, *, extra: Mapping[str, str] | None = None) -> dict[str, object]:
        state = self.remote_state()
        protection = protected_set(state, self.selector(), self._attempts(), extra)
        releases, datasets = self.prune_plan(state, protection)
        report: dict[str, object] = {
            "protected": dict(sorted(protection.releases.items())),
            "delete_releases": releases,
            "delete_datasets": [d.dataset_id for d in datasets],
        }
        if not self.options.execute:
            return report
        for chunk in self._within_budget("DELETE FROM releases WHERE release_id IN (", ", ", ")",
                                         [sql_literal(r) for r in releases]):
            self.d1.call([chunk], label={"phase": "prune", "table": "releases"})
        for dataset in datasets:
            self.delete_dataset(dataset)
        return report

    def delete_dataset(self, dataset: DatasetRow) -> None:
        """Delete one unreferenced dataset, each step its own call, journal rows before data."""
        ds = sql_literal(dataset.ds)
        hex_id = dataset_hex(dataset.dataset_id)
        tables = self._delete_bounds(dataset)
        label = {"phase": "delete", "dataset_id": dataset.dataset_id}
        self.d1.call([f"UPDATE datasets SET state = 'deleting' WHERE ds = {ds}"], label=label)
        self.d1.call([f"DELETE FROM import_ranges WHERE ds = {ds}"], label=label)
        self.d1.call([f"DROP TABLE IF EXISTS {fts_table(kind, hex_id)}" for kind in FTS_TABLES], label=label)
        for table, (rows, bounds) in tables.items():
            lo = 1
            for hi in [*bounds, rows]:
                if hi >= lo:
                    self.d1.call([f"DELETE FROM {checked_table(table)} WHERE ds = {ds} AND seq BETWEEN "
                                  f"{sql_literal(lo)} AND {sql_literal(hi)}"],
                                 label={**label, "table": table, "lo": lo, "hi": hi})
                    lo = hi + 1
        self.d1.call([f"DELETE FROM datasets WHERE ds = {ds} AND state = 'deleting'"], label=label)

    @staticmethod
    def _delete_bounds(dataset: DatasetRow) -> dict[str, tuple[int, list[int]]]:
        """The delete bounds of each data table of the dataset's own schema_version."""
        version_tables = TABLES_BY_VERSION.get(dataset.schema_version)
        if version_tables is None:
            raise D1ImportError(f"dataset {dataset.dataset_id} has schema_version {dataset.schema_version}, "
                                "which this importer does not know: prune with the importer that wrote it")
        try:
            tables = json.loads(dataset.tables_json)
            if not isinstance(tables, dict) or set(tables) != set(version_tables):
                raise ValueError(f"its tables are not those of schema_version {dataset.schema_version}")
            result = {}
            for table in version_tables:
                meta = tables[table]
                rows, bounds = meta["rows"], meta["delete_bounds"]
                _int(rows, f"{table}.rows")
                if (not isinstance(bounds, list) or any(not isinstance(b, int) or isinstance(b, bool) for b in bounds)
                        or bounds != sorted(bounds)):
                    raise ValueError(f"{table}.delete_bounds")
                result[table] = (rows, list(bounds))
            return result
        except (KeyError, TypeError, ValueError, D1ImportError) as exc:
            raise D1ImportError(f"dataset {dataset.dataset_id} has malformed tables_json: {exc}") from exc

    # -- supersede ------------------------------------------------------------------------------

    def supersede(self, release_id: str) -> dict[str, object]:
        current, previous = self.selector()
        if release_id in (current, previous):
            raise D1ImportError(f"{release_id} is the live {'current' if release_id == current else 'previous'} "
                                "release: refusing to supersede it")
        row = self.remote_state().release(release_id)
        if row is None:
            raise D1ImportError(f"no release row {release_id}")
        report: dict[str, object] = {"release_id": release_id, "state": row.state}
        if row.state == "superseded":
            report["changed"] = False
            return report
        report["changed"] = self.options.execute
        if self.options.execute:
            self.d1.call([f"UPDATE releases SET state = 'superseded' WHERE release_id = {sql_literal(release_id)}"])
        return report

    # -- import ---------------------------------------------------------------------------------

    def import_dataset(self, local: LocalDataset, release_id: str, manifest_sha256: str,
                       names_mode: str | None = None) -> dict[str, object]:
        """Register, prune and copy one dataset. names_mode None lets the importer choose:
        trigram, or parts if D1 has no trigram tokenizer (the choice is recorded in datasets)."""
        if names_mode is not None and names_mode not in NAMES_MODES:
            raise D1ImportError(f"names_mode must be one of {NAMES_MODES}")
        self.preflight(local)
        self.require_schema(local.schema_version)
        state = self.remote_state()
        live = self.selector()
        attempts = self._attempts()
        existing = state.dataset(local.dataset_id)
        switch = False
        if existing is not None:
            self._check_same_dataset(existing, local)
            if existing.state != "deleting" and names_mode and existing.names_mode != names_mode:
                self._check_names_mode_switch(state, existing, local, names_mode)
                switch = True
        row = state.release(release_id)
        if row is not None and row.dataset_id is not None and row.dataset_id != local.dataset_id:
            raise D1ImportError(f"release row {release_id} references dataset {row.dataset_id}, "
                                f"not {local.dataset_id}")
        protection = protected_set(state, live, attempts)
        others = sorted(r.release_id for r in state.releases
                        if r.state == "candidate" and r.release_id in protection.releases
                        and r.release_id not in live and r.release_id != release_id
                        and r.dataset_id != local.dataset_id)
        if others:
            raise D1ImportError("another protected candidate references a different dataset: "
                                + ", ".join(others) + "; supersede it first with `brain_d1_import.py "
                                "<global options> --execute supersede <release id>`")
        report: dict[str, object] = {"release_id": release_id, "dataset_id": local.dataset_id,
                                     "dataset_state": existing.state if existing else None}
        if switch:
            report["names_mode"] = f"{existing.names_mode} -> {names_mode}"
        mine = {release_id: "the release being imported"}
        if not self.options.execute:
            report["prune"] = self._dry_prune(state, live, attempts, mine, local.dataset_id)
            if existing is not None and existing.state == "ready":
                report["copy"] = "skip: the dataset is ready"
            else:
                done = (self.coverage(existing.ds, self._row_counts(local))
                        if existing is not None and existing.state == "importing" else {})
                report["copy"] = {name: local.rows(name) - sum(hi - lo + 1 for lo, hi, _ in done.get(name, []))
                                  for name in self._row_counts(local)}
            return report

        # register: finish an interrupted delete of this dataset, then the rows
        if existing is not None and existing.state == "deleting":
            self.delete_dataset(existing)
            existing = None
        if existing is None:
            mode = names_mode or "trigram"
            self.d1.call([
                "INSERT INTO datasets(dataset_id, schema_version, tables_json, longest_name, "
                "sup_names_from, names_mode, state) VALUES ("
                + ", ".join(sql_literal(v) for v in (
                    local.dataset_id, local.schema_version, local.tables_json,
                    local.sidecar["longest_name"], local.sidecar["sup_names_from"], mode))
                + ", 'importing') ON CONFLICT(dataset_id) DO NOTHING"])
            state = self.remote_state()
            existing = state.dataset(local.dataset_id)
            if existing is None:
                raise D1ImportError(f"dataset {local.dataset_id} was not registered")
            self._check_same_dataset(existing, local)
        elif switch:
            existing = self._set_names_mode(existing, names_mode)
        self._register_release(state, release_id, existing.ds, manifest_sha256)

        report["prune"] = self.prune(extra=mine)
        existing = self.remote_state().dataset(local.dataset_id)
        if existing is None:
            raise D1ImportError(f"dataset {local.dataset_id} disappeared during the prune")
        if existing.state == "ready":
            report["copy"] = "skip: the dataset is ready"
            report["imported"] = False
            return report
        if existing.state != "importing":
            raise D1ImportError(f"dataset {local.dataset_id} is {existing.state}, not importing")
        report["copy"] = self.copy(local, existing, names_forced=names_mode is not None)
        report["imported"] = True
        return report

    def _dry_prune(self, state: RemoteState, live: tuple[str | None, str | None],
                   attempts: Mapping[str, str], mine: Mapping[str, str],
                   dataset_id: str) -> dict[str, object]:
        protection = protected_set(state, live, attempts, mine)
        releases, datasets = self.prune_plan(state, protection)
        return {"protected": dict(sorted(protection.releases.items())), "delete_releases": releases,
                "delete_datasets": [d.dataset_id for d in datasets if d.dataset_id != dataset_id]}

    def _check_names_mode_switch(self, state: RemoteState, existing: DatasetRow, local: LocalDataset,
                                 names_mode: str) -> None:
        """A dataset's names_mode may change only while it imports and no names range is journaled."""
        why = None
        if existing.state != "importing":
            why = f"it is {existing.state}"
        elif self.coverage(existing.ds, self._row_counts(local))["names"]:
            why = "names ranges are already copied"
        if why is not None:
            releases = " ".join(r.release_id for r in state.releases if r.ds == existing.ds) or "<release id>"
            raise D1ImportError(f"dataset {local.dataset_id} is recorded with names_mode {existing.names_mode}, "
                                f"not {names_mode}, and {why}: to change it, run `brain_d1_import.py "
                                f"<global options> --execute supersede {releases}`, then `... --execute prune`, "
                                "and import again")

    def _set_names_mode(self, dataset: DatasetRow, names_mode: str) -> DatasetRow:
        """Drop the names table (it holds no journaled range), then record the new mode."""
        label = {"phase": "names-mode", "dataset_id": dataset.dataset_id}
        self.d1.call([f"DROP TABLE IF EXISTS {fts_table('names', dataset_hex(dataset.dataset_id))}"], label=label)
        self.d1.call([f"UPDATE datasets SET names_mode = {sql_literal(names_mode)} "
                      f"WHERE ds = {sql_literal(dataset.ds)} AND state = 'importing'"], label=label)
        row = self.remote_state().dataset(dataset.dataset_id)
        if row is None or row.state != "importing" or row.names_mode != names_mode:
            raise D1ImportError(f"dataset {dataset.dataset_id} left 'importing' while its names_mode changed")
        self.actions.append({"names_mode": names_mode})
        return row

    @staticmethod
    def _check_same_dataset(row: DatasetRow, local: LocalDataset) -> None:
        mine = (local.schema_version, local.tables_json, local.sidecar["longest_name"],
                local.sidecar["sup_names_from"])
        theirs = (row.schema_version, row.tables_json, row.longest_name, row.sup_names_from)
        if mine != theirs:
            raise D1ImportError(f"the datasets row for {local.dataset_id} disagrees with its sidecar")

    def _register_release(self, state: RemoteState, release_id: str, ds: int, manifest_sha256: str) -> None:
        """Insert the release as a candidate with a new seq, or set it back to one.

        A release already registered as the newest candidate of this dataset keeps its row, so
        a retried or resumed registration changes nothing."""
        top = max((r.seq for r in state.releases), default=0)
        row = state.release(release_id)
        if row is not None and row.state == "candidate" and row.ds == ds and row.seq == top:
            return
        seq = top + 1
        values = ", ".join(sql_literal(v) for v in (release_id, seq, ds, manifest_sha256, "candidate", now()))
        self.d1.call([
            f"INSERT INTO releases(release_id, seq, ds, manifest_sha256, state, registered_at) VALUES ({values}) "
            "ON CONFLICT(release_id) DO UPDATE SET seq = excluded.seq, ds = excluded.ds, "
            "manifest_sha256 = excluded.manifest_sha256, state = 'candidate', "
            "registered_at = excluded.registered_at"])

    @staticmethod
    def _row_counts(local: LocalDataset) -> dict[str, int]:
        return {name: local.rows(name) for name in (*DATA_TABLES, *FTS_TABLES)}

    # -- copy -----------------------------------------------------------------------------------

    def copy(self, local: LocalDataset, dataset: DatasetRow, *, names_forced: bool = False) -> dict[str, object]:
        """Create the FTS tables, copy every data table, fill the FTS tables, check, finish.

        The FTS tables come first, so a tokenizer or option D1 lacks fails before any data
        range is written. names_forced says the operator chose the mode (no fallback)."""
        names_mode = dataset.names_mode
        if names_mode not in NAMES_MODES:
            raise D1ImportError(f"dataset {dataset.dataset_id} has names_mode {names_mode!r}")
        counts = self._row_counts(local)
        recorded = self.coverage(dataset.ds, counts)
        hex_id = local.hex
        names_mode = self._create_fts(local, dataset, names_mode,
                                      fallback=not names_forced and not recorded["names"])
        copied: dict[str, int] = {}
        for table in DATA_TABLES:
            spans = gaps([(lo, hi) for lo, hi, _ in recorded[table]], counts[table])
            copied[table] = self._copy_spans(dataset.ds, spans,
                                             lambda a, b, t=table: self._plan_table(local, dataset.ds, t, a, b),
                                             lambda r: self._read_table(dataset.ds, r),
                                             name=table, total=counts[table])
        for kind in FTS_TABLES:
            spans = gaps([(lo, hi) for lo, hi, _ in recorded[kind]], counts[kind])
            copied[kind] = self._copy_spans(
                dataset.ds, spans, lambda a, b, k=kind: self._plan_fts(local, k, names_mode, a, b),
                lambda r, k=kind: self._read_fts(k, hex_id, r), name=kind, total=counts[kind])
        bad = self.check_fts_samples(local, names_mode)
        if bad:
            # a count check cannot see wrong FTS content: forget that index's ranges and drop it,
            # so the rerun refills only the FTS table instead of failing this check forever
            kinds = sorted({kind for kind, _ in bad})
            label = {"phase": "fts-reset", "dataset_id": local.dataset_id}
            for kind in kinds:
                self.d1.call([f"DELETE FROM import_ranges WHERE ds = {sql_literal(dataset.ds)} "
                              f"AND tbl = {sql_literal(kind)}"], label=label)
                self.d1.call([f"DROP TABLE IF EXISTS {fts_table(kind, hex_id)}"], label=label)
            raise D1ImportError(f"{bad[0][1]}: the {' and '.join(kinds)} index was emptied; rerun the "
                                "import to refill it")
        self.finish(local, dataset)
        return copied

    @staticmethod
    def _fts_setup(kind: str, hex_id: str, names_mode: str) -> list[str]:
        table = fts_table(kind, hex_id)
        return [fts_create(kind, hex_id, names_mode),
                f"INSERT INTO {table}({table}, rank) VALUES ('automerge', 0)",
                f"INSERT INTO {table}({table}, rank) VALUES ('crisismerge', {FTS_CRISISMERGE})"]

    def _create_fts(self, local: LocalDataset, dataset: DatasetRow, names_mode: str, *, fallback: bool) -> str:
        """Create both FTS tables and return the names mode in force. If D1 refuses the trigram
        tokenizer and the importer chose the mode (fallback), names falls back to parts and the
        datasets row records it (the design, section 4)."""
        hex_id = local.hex
        label = {"phase": "fts-create", "dataset_id": local.dataset_id}
        try:
            self.d1.call(self._fts_setup("names", hex_id, names_mode), label=label)
        except D1CallError as exc:
            if names_mode != "trigram" or "tokenizer" not in str(exc):
                raise
            if not fallback:
                raise D1ImportError(f"{exc}; D1 has no trigram tokenizer: rerun with --names-mode parts") from exc
            self._set_names_mode(dataset, "parts")
            names_mode = "parts"
            self.d1.call(self._fts_setup("names", hex_id, names_mode), label=label)
        self.d1.call(self._fts_setup("texts", hex_id, names_mode), label=label)
        return names_mode

    def _copy_spans(self, ds: int, spans: list[tuple[int, int]], plan: Callable[[int, int], Iterator[Range]],
                    read: Callable[[Range], str], *, name: str, total: int) -> int:
        """Write, read back and journal every range of every span; return the rows copied."""
        copied = 0
        done = total - sum(b - a + 1 for a, b in spans)

        def land(group: list[Range]) -> int:
            nonlocal done
            rows = self._land(ds, group, read)
            done += rows
            self.d1.log({"progress": name, "rows_done": done, "rows_total": total})
            return rows

        for a, b in spans:
            group: list[Range] = []
            for item in plan(a, b):
                if group and (len(group) >= self.options.ranges_per_call
                              or sum(r.call_size for r in group) + item.call_size - 1 > self.options.call_bytes):
                    copied += land(group)
                    group = []
                group.append(item)
            if group:
                copied += land(group)
        return copied

    def _land(self, ds: int, group: list[Range], read: Callable[[Range], str]) -> int:
        pending = list(group)
        for attempt in range(self.d1.attempts):
            label = {"phase": "copy", "table": pending[0].table, "lo": pending[0].lo, "hi": pending[-1].hi}
            self._write(pending, label)
            readback = self.d1.call([read(r) for r in pending], expect_results=True, label=label)
            bad = []
            for item, result in zip(pending, readback):
                if self._remote_digest(item, result.get("results") or []) != item.digest:
                    bad.append(item)
            good = [r for r in pending if r not in bad]
            self._journal(ds, good, label)
            if not bad:
                return sum(r.hi - r.lo + 1 for r in group)
            self.actions.append({"rewrite": [(r.table, r.lo, r.hi) for r in bad], "attempt": attempt + 1})
            pending = bad
        raise D1ImportError("ranges still differ from the local file after "
                            f"{self.d1.attempts} writes: {[(r.table, r.lo, r.hi) for r in pending]}")

    def _write(self, ranges: list[Range], label: Mapping[str, object]) -> None:
        self.d1.call([s for r in ranges for s in r.primary], label=label)
        appends = [s for r in ranges for s in r.appends]
        call: list[str] = []
        for statement in appends:
            if call and (len(call) >= self.options.ranges_per_call
                         or sum(utf8_len(s) + 2 for s in call) + utf8_len(statement) + 1 > self.options.call_bytes):
                self.d1.call(call, label=label)
                call = []
            call.append(statement)
        self.d1.call(call, label=label)

    def _journal(self, ds: int, ranges: list[Range], label: Mapping[str, object] | None = None) -> None:
        if not ranges:
            return
        stamp = now()
        rows = ["(" + ", ".join(sql_literal(v) for v in (ds, r.table, r.lo, r.hi, r.digest, stamp)) + ")"
                for r in ranges]
        self.d1.call(self._within_budget("INSERT INTO import_ranges(ds, tbl, lo, hi, digest, verified_at) VALUES ",
                                         ", ", " ON CONFLICT(ds, tbl, lo) DO NOTHING", rows), label=label)

    def _within_budget(self, head: str, sep: str, tail: str, items: list[str]) -> list[str]:
        """head + items joined by sep + tail, split into as few statements as the budget allows."""
        statements: list[str] = []
        part: list[str] = []
        fixed = utf8_len(head) + utf8_len(tail)
        size = fixed
        for item in items:
            cost = utf8_len(item) + (utf8_len(sep) if part else 0)
            if part and size + cost > self.options.statement_bytes:
                statements.append(head + sep.join(part) + tail)
                part, size, cost = [], fixed, utf8_len(item)
            part.append(item)
            size += cost
        if part:
            statements.append(head + sep.join(part) + tail)
        return statements

    @staticmethod
    def _remote_digest(item: Range, rows: list[dict[str, Any]]) -> str:
        if item.table in FTS_TABLES:
            count = rows[0].get("n") if rows else None
            return item.digest if count == item.hi - item.lo + 1 else f"count {count}"
        return digest_of(rows)

    # table ranges

    def _plan_table(self, local: LocalDataset, ds: int, table: str, a: int, b: int) -> Iterator[Range]:
        columns = D1_COLUMNS[table]
        ds_lit = sql_literal(ds)
        header = f"INSERT INTO {table}(ds, {', '.join(columns)}) VALUES "
        budget = self.options.statement_bytes
        cursor = local.db.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE seq BETWEEN ? AND ? "
                                  "ORDER BY seq", (a, b))
        tuples: list[str] = []
        size = utf8_len(header)
        lo = hi = None
        digest = RowDigest()

        def flush() -> Range:
            return Range(table, lo, hi, digest.hexdigest(), [
                f"DELETE FROM {table} WHERE ds = {ds_lit} AND seq BETWEEN {sql_literal(lo)} AND {sql_literal(hi)}",
                header + ", ".join(tuples)])

        expected = a
        for values in cursor:
            seq = values[0]
            if seq != expected:
                raise D1ImportError(f"{local.serving} table {table} has seq {seq} where {expected} belongs")
            expected += 1
            row = dict(zip(columns, values))
            literal = "(" + ", ".join([ds_lit, *(sql_literal(v) for v in values)]) + ")"
            cost = utf8_len(literal)
            if utf8_len(header) + cost > budget:
                if tuples:
                    yield flush()
                    tuples, size, digest = [], utf8_len(header), RowDigest()
                yield self._long_row(table, columns, values, ds_lit, header, digest_of([row]))
                continue
            if tuples and size + 2 + cost > budget:
                yield flush()
                tuples, size, digest = [], utf8_len(header), RowDigest()
            if not tuples:
                lo = seq
            tuples.append(literal)
            size += cost + (2 if len(tuples) > 1 else 0)
            hi = seq
            digest.add(row)
        if expected != b + 1:
            raise D1ImportError(f"{local.serving} table {table} ends before seq {b}")
        if tuples:
            yield flush()

    def _long_row(self, table: str, columns: Sequence[str], values: Sequence[object], ds_lit: str,
                  header: str, digest: str) -> Range:
        """One row too long for one statement: an INSERT carrying the first chunk of its long
        text columns, then appends that each match only the exact prefix already written."""
        budget = self.options.statement_bytes
        literals = [sql_literal(v) for v in values]
        by_size = sorted((i for i, v in enumerate(values) if isinstance(v, str)),
                         key=lambda i: -utf8_len(literals[i]))
        chunked: list[int] = []

        def insert(parts: list[str]) -> str:
            return header + "(" + ", ".join([ds_lit, *parts]) + ")"

        parts = list(literals)
        while utf8_len(insert(parts)) > budget:
            if len(chunked) == len(by_size):
                raise D1ImportError(f"{table} seq {values[0]}: even without its text the row exceeds "
                                    f"the {budget}-byte statement budget")
            chunked.append(by_size[len(chunked)])
            parts[chunked[-1]] = "''"
        written = {i: 0 for i in chunked}
        room = budget - utf8_len(insert(parts))
        for i in chunked:          # the first chunks, as far as the INSERT has room
            text = str(values[i])
            end = chunk_end(text, 0, room + 2)
            if end:
                parts[i] = sql_literal(text[:end])
                room -= utf8_len(parts[i]) - 2
                written[i] = end
        seq = sql_literal(values[0])
        statements = [f"DELETE FROM {table} WHERE ds = {ds_lit} AND seq = {seq}", insert(parts)]
        appends = []
        for i in chunked:
            column, text, pos = columns[i], str(values[i]), written[i]
            prefix_bytes = utf8_len(text[:pos])
            while pos < len(text):
                skeleton = (f"UPDATE {table} SET {column} = {column} || ",
                            f" WHERE ds = {ds_lit} AND seq = {seq} AND "
                            f"length(CAST({column} AS BLOB)) = {sql_literal(prefix_bytes)}")
                end = chunk_end(text, pos, budget - utf8_len(skeleton[0]) - utf8_len(skeleton[1]))
                if end == pos:
                    raise D1ImportError(f"{table} seq {values[0]}: the statement budget cannot hold an append")
                appends.append(skeleton[0] + sql_literal(text[pos:end]) + skeleton[1])
                prefix_bytes += utf8_len(text[pos:end])
                pos = end
        return Range(table, int(values[0]), int(values[0]), digest, statements, appends)

    def _read_table(self, ds: int, item: Range) -> str:
        return (f"SELECT {', '.join(D1_COLUMNS[item.table])} FROM {item.table} WHERE ds = {sql_literal(ds)} "
                f"AND seq BETWEEN {sql_literal(item.lo)} AND {sql_literal(item.hi)} ORDER BY seq")

    # FTS ranges

    def _plan_fts(self, local: LocalDataset, kind: str, names_mode: str, a: int, b: int) -> Iterator[Range]:
        source, column = FTS_TABLES[kind]
        table = fts_table(kind, local.hex)
        header = f"INSERT INTO {table}(rowid, {fts_column(kind, names_mode)}) VALUES "
        merge = f"INSERT INTO {table}({table}, rank) VALUES ('merge', {sql_literal(self.options.fts_merge_pages)})"
        budget = self.options.statement_bytes
        tuples: list[str] = []
        size = utf8_len(header)
        lo = hi = None
        digest = RowDigest()

        def flush() -> Range:
            return Range(kind, lo, hi, digest.hexdigest(), [
                f"DELETE FROM {table} WHERE rowid BETWEEN {sql_literal(lo)} AND {sql_literal(hi)}",
                header + ", ".join(tuples), merge])

        expected = a
        for seq, text in local.db.execute(f"SELECT seq, {column} FROM {source} WHERE seq BETWEEN ? AND ? "
                                          "ORDER BY seq", (a, b)):
            if seq != expected:
                raise D1ImportError(f"{local.serving} table {source} has seq {seq} where {expected} belongs")
            expected += 1
            content = fts_content(kind, names_mode, text)
            literal = f"({sql_literal(seq)}, {sql_literal(content)})"
            cost = utf8_len(literal)
            if utf8_len(header) + cost > budget:
                raise D1ImportError(f"{source} seq {seq}: its {kind} text does not fit one "
                                    f"{budget}-byte statement, and FTS rows cannot be appended to")
            if tuples and size + 2 + cost > budget:
                yield flush()
                tuples, size, digest = [], utf8_len(header), RowDigest()
            if not tuples:
                lo = seq
            tuples.append(literal)
            size += cost + (2 if len(tuples) > 1 else 0)
            hi = seq
            digest.add({"rowid": seq, "text": content})
        if expected != b + 1:
            raise D1ImportError(f"{local.serving} table {source} ends before seq {b}")
        if tuples:
            yield flush()

    @staticmethod
    def _read_fts(kind: str, hex_id: str, item: Range) -> str:
        return (f"SELECT count(*) AS n FROM {fts_table(kind, hex_id)}_docsize "
                f"WHERE id BETWEEN {sql_literal(item.lo)} AND {sql_literal(item.hi)}")

    # FTS sample queries

    def fts_queries(self, local: LocalDataset, names_mode: str) -> list[tuple[str, str]]:
        """Up to options.fts_samples (index kind, MATCH phrase) pairs from evenly spaced rows."""
        per_kind = max(1, self.options.fts_samples // len(FTS_TABLES))
        queries: list[tuple[str, str]] = []
        for kind, (source, column) in FTS_TABLES.items():
            rows = local.rows(source)
            if rows == 0:
                continue
            picks = sorted({1 + i * (rows - 1) // max(1, per_kind - 1) for i in range(per_kind)})
            for seq in picks:
                (text,) = local.db.execute(f"SELECT {column} FROM {source} WHERE seq = ?", (seq,)).fetchone()
                if kind == "names" and names_mode == "trigram":
                    middle = len(text) // 2
                    term = text[max(0, middle - 3):middle + 3]
                    if len(term.strip()) < 3 or "\0" in term:
                        continue
                else:
                    words = re.findall(r"[^\W_]{3,}", fts_content(kind, names_mode, text))
                    if not words:
                        continue
                    term = max(words, key=len)
                queries.append((kind, '"' + term.replace('"', '""') + '"'))
        return queries[: self.options.fts_samples]

    def check_fts_samples(self, local: LocalDataset, names_mode: str) -> list[tuple[str, str]]:
        """Sampled MATCH queries (read-only): the (kind, message) of each that answers other
        rowids on D1 than on the local file."""
        queries = self.fts_queries(local, names_mode)
        if not queries:
            return []
        statements = [f"SELECT rowid FROM {fts_table(kind, local.hex)} WHERE {fts_table(kind, local.hex)} "
                      f"MATCH {sql_literal(phrase)} ORDER BY rowid LIMIT 50" for kind, phrase in queries]
        remote = self.d1.calls(statements, expect_results=True, label={"phase": "fts-samples"})
        bad: list[tuple[str, str]] = []
        with local_fts(local, names_mode) as reference:
            for (kind, phrase), result in zip(queries, remote):
                want = [row[0] for row in reference.execute(
                    f"SELECT rowid FROM {kind} WHERE {kind} MATCH ? ORDER BY rowid LIMIT 50", (phrase,))]
                got = [row.get("rowid") for row in result.get("results") or []]
                if got != want:
                    bad.append((kind, f"{kind} MATCH {phrase} answers {got[:5]} on D1 and {want[:5]} locally"))
        return bad

    # -- finish ---------------------------------------------------------------------------------

    def finish(self, local: LocalDataset, dataset: DatasetRow) -> None:
        """Summed coverage of every table, then the state UPDATE with its change count."""
        counts = self._row_counts(local)
        recorded = self.coverage(dataset.ds, counts)
        for name, rows in counts.items():
            covered = sum(hi - lo + 1 for lo, hi, _ in recorded[name])
            if covered != rows:
                raise D1ImportError(f"import_ranges cover {covered} of {rows} rows of {name}")
        stamp = now()
        update = (f"UPDATE datasets SET state = 'ready', imported_at = {sql_literal(stamp)} "
                  f"WHERE ds = {sql_literal(dataset.ds)} AND state = 'importing'")
        for _ in range(self.d1.attempts):
            try:
                result = self.d1.call([update], attempts=1)
            except D1CallError:
                # the call may have applied: only this run's timestamp proves it did
                row = self.remote_state().dataset(local.dataset_id)
                if row is not None and row.state == "ready" and row.imported_at == stamp:
                    return
                if row is not None and row.state == "importing":
                    continue
                raise D1ImportError(f"dataset {local.dataset_id} left 'importing' during the import")
            meta = result[0].get("meta")
            changes = meta.get("changes") if isinstance(meta, dict) else None
            if changes != 1:
                raise D1ImportError(f"the final UPDATE changed {changes} rows, not 1: dataset "
                                    f"{local.dataset_id} left 'importing' during the import")
            return
        raise D1CallError("the final UPDATE failed on every attempt")

    # -- verify ---------------------------------------------------------------------------------

    def verify(self, locals_: Sequence[LocalDataset], sample: int, rng: random.Random) -> dict[str, object]:
        """Re-read `sample` random journaled ranges of each dataset (read-only, no lock)."""
        checked: list[dict[str, object]] = []
        mismatches: list[str] = []
        for local in locals_:
            dataset = self.remote_state().dataset(local.dataset_id)
            if dataset is None or dataset.state != "ready":
                raise D1ImportError(f"dataset {local.dataset_id} is "
                                    f"{dataset.state if dataset else 'absent'} on D1, not ready")
            self._check_same_dataset(dataset, local)
            if dataset.names_mode not in NAMES_MODES:
                raise D1ImportError(f"dataset {local.dataset_id} has names_mode {dataset.names_mode!r}")
            recorded = self.coverage(dataset.ds, self._row_counts(local))
            journaled = [(name, lo, hi, digest) for name, ranges in recorded.items() for lo, hi, digest in ranges]
            picks = rng.sample(journaled, min(sample, len(journaled)))
            planned: list[tuple[Range, str]] = []
            for name, lo, hi, digest in picks:
                item = (self._exact_fts(local, name, dataset.names_mode, lo, hi) if name in FTS_TABLES
                        else self._exact_range(local, name, lo, hi))
                planned.append((item, digest))
                if item.digest != digest:
                    mismatches.append(f"{local.dataset_id} {name} [{lo}, {hi}]: the local file differs "
                                      "from import_ranges")
            for start in range(0, len(planned), max(1, self.options.ranges_per_call)):
                part = planned[start:start + max(1, self.options.ranges_per_call)]
                readers = [self._read_fts(r.table, local.hex, r) if r.table in FTS_TABLES
                           else self._read_table(dataset.ds, r) for r, _ in part]
                for (item, _), result in zip(part, self.d1.call(readers, expect_results=True)):
                    if self._remote_digest(item, result.get("results") or []) != item.digest:
                        mismatches.append(f"{local.dataset_id} {item.table} [{item.lo}, {item.hi}]: D1 differs "
                                          "from the local file")
            mismatches += [f"{local.dataset_id} {message}"
                           for _, message in self.check_fts_samples(local, dataset.names_mode)]
            checked.append({"dataset_id": local.dataset_id, "ranges": len(picks)})
        return {"checked": checked, "mismatches": mismatches, "ok": not mismatches}

    @staticmethod
    def _exact_range(local: LocalDataset, table: str, lo: int, hi: int) -> Range:
        columns = D1_COLUMNS[table]
        rows = [dict(zip(columns, values)) for values in local.db.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE seq BETWEEN ? AND ? ORDER BY seq", (lo, hi))]
        return Range(table, lo, hi, digest_of(rows), [])

    @staticmethod
    def _exact_fts(local: LocalDataset, kind: str, names_mode: str | None, lo: int, hi: int) -> Range:
        if names_mode not in NAMES_MODES:
            raise D1ImportError(f"names_mode {names_mode!r} is not one this importer knows")
        source, column = FTS_TABLES[kind]
        digest = RowDigest()
        for seq, text in local.db.execute(f"SELECT seq, {column} FROM {source} WHERE seq BETWEEN ? AND ? "
                                          "ORDER BY seq", (lo, hi)):
            digest.add({"rowid": seq, "text": fts_content(kind, names_mode, text)})
        return Range(kind, lo, hi, digest.hexdigest(), [])


@contextlib.contextmanager
def local_fts(local: LocalDataset, names_mode: str) -> Iterator[sqlite3.Connection]:
    """A connection whose tables `names` and `texts` index the local file as D1's would.

    The serving file's own FTS tables are trigram names and texts; for names_mode 'parts' a
    private parts index is built from the file's names."""
    if names_mode == "trigram":
        yield local.db
        return
    with tempfile.TemporaryDirectory(prefix="brain-d1-parts-") as scratch:
        db = sqlite3.connect(Path(scratch) / "parts.sqlite3")
        try:
            db.execute("CREATE VIRTUAL TABLE names USING fts5(parts, content='', contentless_delete=1, prefix='2 3')")
            db.executemany("INSERT INTO names(rowid, parts) VALUES (?, ?)",
                           ((seq, name_parts(name)) for seq, name in
                            local.db.execute("SELECT seq, name FROM name_keys ORDER BY seq")))
            db.execute("CREATE VIRTUAL TABLE texts USING fts5(body, content='', contentless_delete=1)")
            db.executemany("INSERT INTO texts(rowid, body) VALUES (?, ?)",
                           local.db.execute("SELECT seq, body FROM text_ids ORDER BY seq"))
            yield db
        finally:
            db.close()


# ---------------------------------------------------------------------------------------------
# Command line


def verify_release_root(root: Path, *, repo: Path, python: Path, runner: Any, timeout: float) -> ReleaseFiles:
    """Run brain/tools/verify_release.py on a frozen release, as the promoter does."""
    files = release_files(root)
    result = runner.run([str(python), str(repo / "brain" / "tools" / "verify_release.py"),
                         "--manifest", str(root / "release.json"), "--root", str(root)],
                        cwd=repo, timeout=timeout, env=None)
    if not result.ok:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")[-2000:]
        raise D1ImportError(f"release verification failed: {detail.strip()}")
    try:
        verified = extract_last_json_value(result.stdout, "release verification")
    except PromotionError as exc:
        raise D1ImportError(str(exc)) from exc
    if not isinstance(verified, dict) or verified.get("ok") is not True \
            or verified.get("release_id") != files.release_id:
        raise D1ImportError("the release verifier did not confirm the release id")
    return files


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--receipt-dir", type=Path,
                        default=Path(os.environ["WIKILEAN_BRAIN_RECEIPT_DIR"])
                        if os.environ.get("WIKILEAN_BRAIN_RECEIPT_DIR") else None,
                        help="the promoter's receipt root, whose PromotionLock import, prune and supersede take")
    parser.add_argument("--database", default=DATABASE)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database-id", help="the D1 database UUID of wikilean-brain (issue #150 creates it)")
    target.add_argument("--wrangler-config", type=Path, help="an absolute Wrangler config that binds the database")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true",
                       help="read the remote state and report; import, prune and supersede need this or --execute")
    modes.add_argument("--execute", action="store_true", help="mutate")
    parser.add_argument("--statement-bytes", type=int, default=STATEMENT_BYTES)
    parser.add_argument("--call-bytes", type=int, default=CALL_BYTES,
                        help=f"bytes of one --command argument (default {CALL_BYTES}; at most 131071 on Linux)")
    parser.add_argument("--ranges-per-call", type=int, default=RANGES_PER_CALL)
    parser.add_argument("--call-attempts", type=int, default=CALL_ATTEMPTS)
    parser.add_argument("--size-ceiling", type=int, default=SIZE_CEILING)
    parser.add_argument("--slow-call-ms", type=float, default=SLOW_CALL_MS)
    parser.add_argument("--fts-merge-pages", type=int, default=FTS_MERGE_PAGES)
    parser.add_argument("--command-timeout", type=float,
                        default=_env_float("WIKILEAN_BRAIN_COMMAND_TIMEOUT", 900))
    commands = parser.add_subparsers(dest="command", required=True)

    importing = commands.add_parser("import", help="import one dataset and register its release")
    source = importing.add_mutually_exclusive_group(required=True)
    source.add_argument("--release-root", type=Path, help="a frozen D1 release directory")
    source.add_argument("--scratch", nargs=2, type=Path, metavar=("SERVING", "SIDECAR"),
                        help="a serving file and its sidecar, registered as scratch:<dataset id>")
    importing.add_argument("--python", type=Path, help="the interpreter for verify_release.py")
    importing.add_argument("--names-mode", choices=NAMES_MODES,
                           help="force a names mode; by default the importer takes trigram, or parts if D1 "
                                "has no trigram tokenizer. It may change until a names range is copied")

    commands.add_parser("prune", help="delete what no protected release references")
    superseding = commands.add_parser("supersede", help="mark a candidate release superseded")
    superseding.add_argument("release_id")

    verifying = commands.add_parser("verify", help="re-read sampled ranges (read-only, no lock)")
    verifying.add_argument("--sample", type=int, required=True)
    verifying.add_argument("--release-root", type=Path, action="append", default=[])
    verifying.add_argument("--scratch", nargs=2, type=Path, action="append", default=[],
                           metavar=("SERVING", "SIDECAR"))
    verifying.add_argument("--seed", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (args.statement_bytes < 1_000 or args.call_bytes < args.statement_bytes + 1_024
            or args.ranges_per_call < 1):
        parser.error("need --statement-bytes >= 1000, --call-bytes >= --statement-bytes + 1024 "
                     "and --ranges-per-call >= 1")
    if args.command == "import" and args.release_root is not None and args.python is None:
        parser.error("import --release-root needs --python for verify_release.py")
    if sys.platform.startswith("linux") and args.call_bytes > 131_071:
        parser.error("Linux caps one argument at 131071 bytes plus its NUL (MAX_ARG_STRLEN): lower --call-bytes")
    if args.command == "verify" and not (args.release_root or args.scratch):
        parser.error("verify needs --release-root or --scratch")
    if args.command == "verify" and (args.sample < 0 or args.execute):
        parser.error("verify is read-only and needs --sample >= 0")
    if args.command in {"import", "prune", "supersede"} and args.receipt_dir is None:
        parser.error("--receipt-dir or WIKILEAN_BRAIN_RECEIPT_DIR is required")
    if args.command in {"import", "prune", "supersede"} and not (args.dry_run or args.execute):
        parser.error(f"{args.command} needs --dry-run or --execute")
    return args


def run(args: argparse.Namespace, *, runner: Any | None = None,
        selector: Callable[[], tuple[str | None, str | None]] | None = None,
        base_url: str = PRODUCTION_ORIGIN, node: Path | None = None, wrangler: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[dict[str, object]], None] | None = None,
        progress: dict[str, Any] | None = None) -> dict[str, object]:
    """Run one command. `progress` receives the D1 client as progress["d1"], so a caller can
    report its call stats and last label even when the command fails."""
    runner = runner or CommandRunner()
    repo = args.repo_root.resolve(strict=True)
    options = Options(execute=args.execute, statement_bytes=args.statement_bytes,
                      call_bytes=args.call_bytes, ranges_per_call=args.ranges_per_call,
                      fts_merge_pages=args.fts_merge_pages)
    receipt_root = None
    if args.command != "verify":
        try:
            receipt_root = validate_target_receipt_root(args.receipt_dir, repo, base_url)
        except JournalError as exc:
            raise D1ImportError(str(exc)) from exc
    with WranglerConfig(config=args.wrangler_config, database_id=args.database_id,
                        database=args.database) as config:
        d1 = D1(repo=repo, database=args.database, config=config, runner=runner,
                timeout=args.command_timeout, statement_bytes=args.statement_bytes,
                call_bytes=args.call_bytes, attempts=args.call_attempts, size_ceiling=args.size_ceiling,
                slow_call_ms=args.slow_call_ms, node=node, wrangler=wrangler, sleep=sleep, log=log)
        if progress is not None:
            progress["d1"] = d1
        importer = Importer(d1, options=options, selector=selector or (lambda: live_selector(base_url)),
                            receipt_root=receipt_root)
        result: dict[str, object] = {"schema": RESULT_SCHEMA, "command": args.command,
                                     "execute": args.execute, "database": args.database}
        if args.command == "verify":
            locals_ = []
            try:
                for root in args.release_root:
                    files = release_files(root)
                    locals_.append(open_local(files.serving, files.sidecar, full_check=False))
                for serving, sidecar in args.scratch:
                    locals_.append(open_local(serving, sidecar, full_check=False))
                rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()
                result.update(importer.verify(locals_, args.sample, rng))
            finally:
                for local in locals_:
                    local.close()
        elif args.command == "import":
            if args.scratch:
                serving, sidecar = args.scratch
                local = open_local(serving, sidecar, full_check=True)
                release_id = SCRATCH_PREFIX + local.dataset_id
                manifest_sha256 = sha256_bytes(sidecar.read_bytes())
            else:
                files = verify_release_root(args.release_root, repo=repo, python=args.python,
                                            runner=runner, timeout=args.command_timeout)
                local = open_local(files.serving, files.sidecar, full_check=True)
                release_id, manifest_sha256 = files.release_id, files.manifest_sha256
            try:
                with importer.locked():
                    result.update(importer.import_dataset(local, release_id, manifest_sha256, args.names_mode))
            finally:
                local.close()
        elif args.command == "prune":
            with importer.locked():
                result.update(importer.prune())
        else:
            with importer.locked():
                result.update(importer.supersede(args.release_id))
        result["calls"] = d1.stats.summary()
        result["ok"] = result.get("ok", True)
        return result


def main(argv: Sequence[str] | None = None, **run_options: Any) -> int:
    progress: dict[str, Any] = {}

    def fail(error: str) -> None:
        record: dict[str, object] = {"schema": RESULT_SCHEMA, "ok": False, "error": error}
        d1 = progress.get("d1")
        if d1 is not None:
            record["calls"] = d1.stats.summary()
            record["last_call"] = d1.last_label
        print(json.dumps(record, sort_keys=True, default=str), file=sys.stderr)

    try:
        args = parse_args(argv)
        result = run(args, progress=progress, **run_options)
    except KeyboardInterrupt:
        fail("interrupted: rerun the same command; a resumed import copies only what is not journaled")
        return 130
    except (D1ImportError, PromotionError, JournalError, TransportError, OSError, ValueError,
            sqlite3.Error) as exc:
        fail(f"{type(exc).__name__}: {exc}" if not isinstance(exc, D1ImportError) else str(exc))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
