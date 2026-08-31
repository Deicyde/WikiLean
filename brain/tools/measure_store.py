#!/usr/bin/env python3
"""Emit a machine-readable health and bounded-query report for Brain SQLite.

The probe deliberately uses only Python's standard library and opens the database
with SQLite's read-only, immutable URI flags.  It does not import ``brain.store``:
that keeps it usable while diagnosing a broken application environment and lets it
inspect both the current schema-v2 database and additive schema-v2 revisions.
"""
from __future__ import annotations

import argparse
import json
import math
import resource
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


REPORT_SCHEMA = "wikilean.brain.store-metrics.v1"
BRAIN_APPLICATION_ID = 0x574C424E  # "WLBN"
DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "data" / "brain.sqlite3"
CORE_TABLES = ("nodes", "edges", "cells", "organ_owners", "synapses")


class MeasurementError(RuntimeError):
    """The database cannot be measured as a complete Brain projection."""


def _max_rss_bytes() -> int:
    """Return process high-water RSS in bytes (not a per-call delta)."""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _scalar(connection: sqlite3.Connection, sql: str) -> Any:
    row = connection.execute(sql).fetchone()
    return row[0] if row is not None else None


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # All callers pass constants or names read from sqlite_master.
    quoted = table.replace('"', '""')
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{quoted}")')
    }


def _index_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }


def open_immutable(database: Path) -> sqlite3.Connection:
    """Open ``database`` without creating it, journals, or mutable sidecars."""
    resolved = database.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise MeasurementError(f"not a regular SQLite file: {resolved}")
    uri = resolved.as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection
    except sqlite3.Error as exc:
        raise MeasurementError(f"cannot open {resolved}: {exc}") from exc


def _snapshot_identity(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = _table_names(connection)
    if "snapshot" not in tables:
        raise MeasurementError("missing snapshot table")
    available = _columns(connection, "snapshot")
    wanted = (
        "schema_version",
        "build_state",
        "snapshot_id",
        "base_snapshot_id",
        "projection_id",
        "release_id",
        "metadata_json",
    )
    selected = [name for name in wanted if name in available]
    if not selected:
        raise MeasurementError("snapshot table has no recognized columns")
    where = " WHERE singleton = 1" if "singleton" in available else ""
    row = connection.execute(
        f"SELECT {', '.join(selected)} FROM snapshot{where} LIMIT 1"
    ).fetchone()
    if row is None:
        raise MeasurementError("snapshot table is empty")
    identity = {name: row[name] for name in selected if name != "metadata_json"}
    metadata: dict[str, Any] = {}
    if "metadata_json" in selected and row["metadata_json"]:
        try:
            decoded = json.loads(row["metadata_json"])
            if isinstance(decoded, dict):
                metadata = decoded
        except (TypeError, json.JSONDecodeError):
            pass
    if not identity.get("release_id") and isinstance(metadata.get("release_id"), str):
        identity["release_id"] = metadata["release_id"]
        identity["release_id_source"] = "snapshot.metadata_json"
    elif identity.get("release_id"):
        identity["release_id_source"] = "snapshot.release_id"
    else:
        identity["release_id"] = None
        identity["release_id_source"] = None
    for key in ("snapshot_id", "base_snapshot_id", "projection_id"):
        identity.setdefault(key, None)
    snapshot_id = identity["snapshot_id"]
    base_id = identity["base_snapshot_id"]
    projection_id = identity["projection_id"]
    identity["snapshot_aliases_base"] = (
        snapshot_id == base_id if snapshot_id is not None and base_id is not None else None
    )
    identity["snapshot_aliases_projection"] = (
        snapshot_id == projection_id
        if snapshot_id is not None and projection_id is not None
        else None
    )
    if identity["snapshot_aliases_base"]:
        identity["snapshot_id_alias"] = "base_snapshot_id"
    elif identity["snapshot_aliases_projection"]:
        identity["snapshot_id_alias"] = "projection_id"
    elif snapshot_id is None:
        identity["snapshot_id_alias"] = None
    else:
        identity["snapshot_id_alias"] = "neither"
    return identity


def _database_metrics(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    page_size = int(_scalar(connection, "PRAGMA page_size"))
    page_count = int(_scalar(connection, "PRAGMA page_count"))
    freelist_count = int(_scalar(connection, "PRAGMA freelist_count"))
    return {
        "path": str(path.expanduser().resolve()),
        "file_bytes": path.expanduser().resolve().stat().st_size,
        "application_id": int(_scalar(connection, "PRAGMA application_id")),
        "user_version": int(_scalar(connection, "PRAGMA user_version")),
        "page_size_bytes": page_size,
        "page_count": page_count,
        "allocated_bytes": page_size * page_count,
        "freelist_pages": freelist_count,
        "freelist_bytes": page_size * freelist_count,
        "used_pages": page_count - freelist_count,
        "used_bytes": page_size * (page_count - freelist_count),
        "freelist_fraction": round(freelist_count / page_count, 8)
        if page_count
        else 0.0,
        "journal_mode": str(_scalar(connection, "PRAGMA journal_mode")),
        "auto_vacuum": int(_scalar(connection, "PRAGMA auto_vacuum")),
        "query_only": bool(_scalar(connection, "PRAGMA query_only")),
        "immutable": True,
        "read_only": True,
    }


def _counts(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = _table_names(connection)
    table_counts = {
        name: int(_scalar(connection, f'SELECT count(*) FROM "{name}"'))
        for name in CORE_TABLES
        if name in tables
    }
    artifact_counts: dict[str, int] = {}
    if "artifacts" in tables and {"name", "row_count"} <= _columns(
        connection, "artifacts"
    ):
        artifact_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT name, row_count FROM artifacts ORDER BY name"
            )
        }
    edge_streams: dict[str, int] = {}
    if "edges" in tables and "stream" in _columns(connection, "edges"):
        edge_streams = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT stream, count(*) FROM edges GROUP BY stream ORDER BY stream"
            )
        }
    return {
        "tables": table_counts,
        "artifacts": artifact_counts,
        "edges_by_stream": edge_streams,
    }


def _analyze_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    if "sqlite_stat1" not in _table_names(connection):
        return {
            "present": False,
            "entry_count": 0,
            "tables": [],
            "indexes": [],
            "entries": [],
        }
    entries = [
        {"table": str(row[0]), "index": row[1], "stat": str(row[2])}
        for row in connection.execute(
            "SELECT tbl, idx, stat FROM sqlite_stat1 ORDER BY tbl, idx"
        )
    ]
    return {
        "present": bool(entries),
        "entry_count": len(entries),
        "tables": sorted({entry["table"] for entry in entries}),
        "indexes": sorted(
            entry["index"] for entry in entries if entry["index"] is not None
        ),
        "entries": entries,
    }


def _check(
    connection: sqlite3.Connection, pragma: str, check_limit: int
) -> dict[str, Any]:
    started = time.perf_counter_ns()
    messages = [
        str(row[0])
        for row in connection.execute(f"PRAGMA {pragma}({check_limit})")
    ]
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return {
        "ok": messages == ["ok"],
        "messages": messages,
        "duration_ms": round(elapsed_ms, 6),
        "error_limit": check_limit,
    }


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * fraction))
    return ordered[rank - 1]


def _latency_summary(values: Sequence[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 6),
        "p50": round(statistics.median(values), 6),
        "p95": round(_percentile(values, 0.95), 6),
        "mean": round(statistics.fmean(values), 6),
        "max": round(max(values), 6),
    }


def _plan(
    connection: sqlite3.Connection, sql: str, params: Sequence[Any]
) -> list[dict[str, Any]]:
    return [
        {"id": int(row[0]), "parent": int(row[1]), "detail": str(row[3])}
        for row in connection.execute("EXPLAIN QUERY PLAN " + sql, params)
    ]


def _probe(
    connection: sqlite3.Connection,
    *,
    sql: str,
    params: Sequence[Any],
    sample: str | None,
    limit: int,
    iterations: int,
    warmup: int,
    expected_indexes: Iterable[str] = (),
    base_tables: Iterable[str] = (),
) -> dict[str, Any]:
    query_plan = _plan(connection, sql, params)
    for _ in range(warmup):
        connection.execute(sql, params).fetchall()
    timings: list[float] = []
    rows_returned = 0
    for _ in range(iterations):
        started = time.perf_counter_ns()
        rows = connection.execute(sql, params).fetchall()
        timings.append((time.perf_counter_ns() - started) / 1_000_000)
        rows_returned = len(rows)
    details = [entry["detail"] for entry in query_plan]
    indexes = list(expected_indexes)
    used = [name for name in indexes if any(name in detail for detail in details)]
    scans = [
        detail
        for detail in details
        if any(
            f"SCAN {table}" in detail or f"SCAN TABLE {table}" in detail
            for table in base_tables
        )
    ]
    return {
        "status": "ok" if sample is not None else "empty",
        "sample_key": sample,
        "limit": limit,
        "iterations": iterations,
        "warmup_iterations": warmup,
        "rows_returned": rows_returned,
        "latency_ms": _latency_summary(timings),
        "plan": query_plan,
        "plan_summary": {
            "expected_indexes": indexes,
            "used_expected_indexes": used,
            "all_expected_indexes_used": set(used) == set(indexes),
            "base_table_full_scans": scans,
        },
    }


def _sample(
    connection: sqlite3.Connection, table: str, column: str, order: str
) -> str | None:
    row = connection.execute(
        f'SELECT "{column}" FROM "{table}" ORDER BY {order} LIMIT 1'
    ).fetchone()
    return str(row[0]) if row is not None else None


def _hottest_edge_kind(connection: sqlite3.Connection) -> tuple[str, str] | None:
    """Select the highest-cardinality endpoint/kind pair outside timed probes."""
    row = connection.execute(
        "SELECT endpoint, kind FROM ("
        "SELECT src AS endpoint, kind FROM edges "
        "UNION ALL "
        "SELECT dst AS endpoint, kind FROM edges WHERE dst <> src"
        ") GROUP BY endpoint, kind "
        "ORDER BY count(*) DESC, endpoint, kind LIMIT 1"
    ).fetchone()
    return (str(row[0]), str(row[1])) if row is not None else None


def _query_metrics(
    connection: sqlite3.Connection, *, limit: int, iterations: int, warmup: int
) -> dict[str, Any]:
    tables = _table_names(connection)
    indexes = _index_names(connection)
    required = {"organ_owners", "edges", "synapses"}
    missing = sorted(required - tables)
    if missing:
        raise MeasurementError(
            "missing query table(s): " + ", ".join(missing)
        )

    no_match = "__wikilean_store_metrics_no_match__"
    owner = _sample(connection, "organ_owners", "organ_id", "organ_id")
    owner_sql = (
        "SELECT owner_id FROM organ_owners WHERE organ_id = ? LIMIT ?"
    )

    edge_sample = _hottest_edge_kind(connection)
    endpoint = edge_sample[0] if edge_sample else None
    edge_kind = edge_sample[1] if edge_sample else None
    src_hint = (
        " INDEXED BY edges_src_kind_idx"
        if "edges_src_kind_idx" in indexes
        else ""
    )
    dst_hint = (
        " INDEXED BY edges_dst_kind_idx"
        if "edges_dst_kind_idx" in indexes
        else ""
    )
    edge_sql = (
        "SELECT payload_json FROM ("
        f"SELECT payload_json, stream, ordinal FROM edges{src_hint} "
        "WHERE src = ? AND kind = ? "
        "UNION ALL "
        f"SELECT payload_json, stream, ordinal FROM edges{dst_hint} "
        "WHERE dst = ? AND src <> ? AND kind = ?"
        ") ORDER BY CASE stream WHEN 'main' THEN 0 ELSE 1 END, ordinal LIMIT ?"
    )

    synapse = _sample(connection, "synapses", "src", "ordinal")
    synapse_src_hint = (
        " INDEXED BY synapses_src_idx"
        if "synapses_src_idx" in indexes
        else ""
    )
    synapse_dst_hint = (
        " INDEXED BY synapses_dst_idx"
        if "synapses_dst_idx" in indexes
        else ""
    )
    synapse_sql = (
        "SELECT payload_json FROM ("
        f"SELECT payload_json, ordinal FROM synapses{synapse_src_hint} WHERE src = ? "
        "UNION ALL "
        f"SELECT payload_json, ordinal FROM synapses{synapse_dst_hint} "
        "WHERE dst = ? AND src <> ?"
        ") ORDER BY ordinal LIMIT ?"
    )

    return {
        "owner_lookup": _probe(
            connection,
            sql=owner_sql,
            params=(owner or no_match, limit),
            sample=owner,
            limit=limit,
            iterations=iterations,
            warmup=warmup,
            base_tables=("organ_owners",),
        ),
        "edge_neighborhood": _probe(
            connection,
            sql=edge_sql,
            params=(
                endpoint or no_match,
                edge_kind or no_match,
                endpoint or no_match,
                endpoint or no_match,
                edge_kind or no_match,
                limit,
            ),
            sample=(f"{endpoint}|{edge_kind}" if edge_sample else None),
            limit=limit,
            iterations=iterations,
            warmup=warmup,
            expected_indexes=("edges_src_kind_idx", "edges_dst_kind_idx"),
            base_tables=("edges",),
        ),
        "synapse_neighborhood": _probe(
            connection,
            sql=synapse_sql,
            params=(synapse or no_match,) * 3 + (limit,),
            sample=synapse,
            limit=limit,
            iterations=iterations,
            warmup=warmup,
            expected_indexes=("synapses_src_idx", "synapses_dst_idx"),
            base_tables=("synapses",),
        ),
    }


def measure_database(
    database: Path,
    *,
    release_id: str | None = None,
    release_id_source: str = "argument",
    limit: int = 100,
    iterations: int = 7,
    warmup: int = 2,
    check_limit: int = 100,
) -> dict[str, Any]:
    """Measure a Brain SQLite projection without mutating it."""
    started = time.perf_counter_ns()
    if limit < 1 or iterations < 1 or warmup < 0 or check_limit < 1:
        raise ValueError("limit/iterations/check-limit must be positive; warmup may be zero")
    path = Path(database)
    connection = open_immutable(path)
    try:
        identity = _snapshot_identity(connection)
        embedded_release = identity.get("release_id")
        warnings: list[str] = []
        if release_id is not None:
            if embedded_release is not None and embedded_release != release_id:
                warnings.append(
                    "provided release_id differs from the ID embedded in snapshot"
                )
            identity["release_id"] = release_id
            identity["release_id_source"] = release_id_source
        elif identity["release_id"] is None:
            warnings.append(
                "release_id is external to this projection; pass --release-id or "
                "--release-manifest to bind the report to a release"
            )

        database_metrics = _database_metrics(connection, path)
        counts = _counts(connection)
        analyze = _analyze_stats(connection)
        quick = _check(connection, "quick_check", check_limit)
        integrity = _check(connection, "integrity_check", check_limit)
        queries = _query_metrics(
            connection, limit=limit, iterations=iterations, warmup=warmup
        )

        if not analyze["present"]:
            warnings.append("sqlite_stat1 is absent or empty; run ANALYZE in the builder")
        for name, probe in queries.items():
            if not probe["plan_summary"]["all_expected_indexes_used"]:
                warnings.append(f"{name} did not use every expected endpoint index")
            if probe["plan_summary"]["base_table_full_scans"]:
                warnings.append(f"{name} performs a base-table scan")

        identity_ok = (
            identity.get("build_state") in (None, "complete")
            and identity.get("projection_id") is not None
            and identity.get("base_snapshot_id") is not None
            and identity.get("snapshot_id_alias") != "neither"
        )
        checks = {
            "application_id": {
                "ok": database_metrics["application_id"] == BRAIN_APPLICATION_ID,
                "expected": BRAIN_APPLICATION_ID,
                "actual": database_metrics["application_id"],
            },
            "identity": {"ok": identity_ok},
            "quick_check": quick,
            "integrity_check": integrity,
        }
        return {
            "schema": REPORT_SCHEMA,
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "ok": all(check["ok"] for check in checks.values()),
            "identity": identity,
            "database": database_metrics,
            "counts": counts,
            "analyze": analyze,
            "checks": checks,
            "queries": queries,
            "warnings": warnings,
            "duration_ms": round(
                (time.perf_counter_ns() - started) / 1_000_000, 6
            ),
            "max_rss_bytes": _max_rss_bytes(),
        }
    except sqlite3.Error as exc:
        raise MeasurementError(f"cannot measure {path}: {exc}") from exc
    finally:
        connection.close()


def _release_from_manifest(path: Path) -> str:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasurementError(f"cannot read release manifest {path}: {exc}") from exc
    release_id = document.get("release_id") if isinstance(document, dict) else None
    if not isinstance(release_id, str) or not release_id:
        raise MeasurementError(f"release manifest {path} has no string release_id")
    return release_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    release = parser.add_mutually_exclusive_group()
    release.add_argument("--release-id")
    release.add_argument("--release-manifest", type=Path)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--iterations", type=int, default=7)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--check-limit", type=int, default=100)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        release_id = args.release_id
        release_source = "argument"
        if args.release_manifest is not None:
            release_id = _release_from_manifest(args.release_manifest)
            release_source = str(args.release_manifest.resolve())
        report = measure_database(
            args.database,
            release_id=release_id,
            release_id_source=release_source,
            limit=args.limit,
            iterations=args.iterations,
            warmup=args.warmup,
            check_limit=args.check_limit,
        )
    except (MeasurementError, OSError, ValueError) as exc:
        error = {"schema": REPORT_SCHEMA, "ok": False, "error": str(exc)}
        print(json.dumps(error, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
