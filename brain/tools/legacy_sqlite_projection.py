#!/usr/bin/env python3
"""Index unchanged legacy graph files in SQLite v2, retaining the original index.

This is a private comparison preparation step. It verifies projection parity,
not a legacy execution, source authority, complete release or approved baseline.
Run it in the baseline's isolated workspace; it is not a hostile-same-UID sandbox.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import authority_contracts as contracts
import store

SCHEMA = "wikilean.legacy-sqlite-projection-plan/v1"
SEMANTIC_FILES = ("nodes.jsonl", "edges.jsonl", "edges_links.jsonl", "cells.jsonl",
                  "synapses.jsonl", "frontier.jsonl", "frontier_graph.json")
INPUT_FILES = frozenset((*SEMANTIC_FILES, "brain.sqlite3"))
PROGRAMS = {"projector": Path(__file__).resolve(), "store": Path(store.__file__).resolve(),
            "verifier": Path(contracts.__file__).resolve(),
            "environment": Path(contracts.execution_environment_contract.__file__).resolve()}
MAX_FILE_BYTES = 16 * 1024**3


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return contracts.canonical_json_bytes(value)


def plan_id(plan):
    return "sha256:" + sha(canonical({"domain": SCHEMA,
        "plan": {k: v for k, v in plan.items() if k != "plan_id"}}))


def validate_plan(plan, expected_id):
    require(isinstance(plan, dict) and set(plan) == {"schema", "plan_id", "inputs", "programs"},
            "projection plan fields differ")
    require(plan["schema"] == SCHEMA, "unknown projection plan schema")
    require(isinstance(plan["inputs"], dict) and set(plan["inputs"]) == INPUT_FILES,
            "plan must bind all seven semantic files and original SQLite")
    require(isinstance(plan["programs"], dict) and set(plan["programs"]) == set(PROGRAMS),
            "plan must bind the projector, store and independent verifier closure")
    for group in ("inputs", "programs"):
        for name, item in plan[group].items():
            require(isinstance(item, dict) and set(item) == {"sha256", "bytes"}, "invalid file identity: " + name)
            require(isinstance(item["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", item["sha256"]),
                    "invalid SHA-256: " + name)
            require(type(item["bytes"]) is int and 0 < item["bytes"] <= MAX_FILE_BYTES,
                    "invalid bounded byte count: " + name)
    require(expected_id == plan["plan_id"] == plan_id(plan), "projection plan differs from expected ID")
    return plan


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def open_absolute(path):
    """Reject symlinks in every path component before opening one regular file."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "an absolute literal path is required")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        result = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    return result


def measure(path, *, expected=None, copy_to=None):
    descriptor = open_absolute(path)
    target = None
    try:
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= MAX_FILE_BYTES,
                    "source must be a nonempty bounded regular file")
            if expected is not None:
                require(before.st_size == expected["bytes"], "source size differs from expected identity")
            if copy_to is not None:
                target = Path(copy_to).open("xb")
            digest = hashlib.sha256(); size = 0
            while chunk := source.read(1024 * 1024):
                digest.update(chunk); size += len(chunk)
                require(size <= MAX_FILE_BYTES, "source grew past bound")
                if target is not None:
                    target.write(chunk)
            require(signature(before) == signature(os.fstat(source.fileno())), "source changed while reading")
            result = {"sha256": digest.hexdigest(), "bytes": size}
            require(expected is None or result == expected, "source bytes differ from expected identity")
            if target is not None:
                target.flush(); os.fsync(target.fileno())
            return result
    finally:
        if target is not None:
            target.close()


LOADED_PROGRAMS = {name: measure(path) for name, path in PROGRAMS.items()}


def legacy_index_identity(path):
    descriptor = open_absolute(path)
    try:
        with os.fdopen(descriptor, "rb") as source:
            uri = (Path("/dev/fd") / str(source.fileno())).as_uri() + "?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                require(connection.execute("PRAGMA user_version").fetchone()[0] == 1,
                        "original SQLite must have legacy schema 1")
                # The exact ebac34dc writer predates the WLBN application marker.
                require(connection.execute("PRAGMA application_id").fetchone()[0] == 0,
                        "original SQLite application marker differs from the legacy writer")
                require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                        "original SQLite integrity check failed")
                row = connection.execute("SELECT schema_version,build_state,snapshot_id FROM snapshot WHERE singleton=1").fetchone()
                require(row is not None and row[:2] == (1, "complete"), "original SQLite build is incomplete")
                require(isinstance(row[2], str) and re.fullmatch(r"[a-f0-9]{64}", row[2]),
                        "invalid original SQLite identity")
                return {"schema_version": 1, "snapshot_id": row[2], "integrity": "ok",
                        "legacy_execution_verified": False}
    finally:
        # fdopen and the SQLite connection own their respective handles.
        pass


def project(source_dir, destination, plan, expected_id):
    validate_plan(plan, expected_id)
    require(plan["programs"] == LOADED_PROGRAMS, "plan programs differ from the loaded implementation")
    source_dir, destination = Path(source_dir), Path(destination)
    require(source_dir.is_absolute() and destination.is_absolute(), "absolute source/destination paths required")
    require(destination.parent.resolve(strict=True) == destination.parent and ".." not in destination.parts,
            "destination parent must be a real literal directory")
    for name, path in PROGRAMS.items():
        measure(path, expected=plan["programs"][name])
    # Inspect all expected inputs before creating a new directory. No source is modified.
    for name in sorted(INPUT_FILES):
        measure(source_dir / name, expected=plan["inputs"][name])
    old_identity = legacy_index_identity(source_dir / "brain.sqlite3")
    destination.mkdir(mode=0o700)  # Never overwrite or adopt an existing directory.
    data = destination / "brain/data"; data.mkdir(parents=True, mode=0o700)
    retained = destination / "retained"; retained.mkdir(mode=0o700)
    for name in sorted(INPUT_FILES):
        target = retained / name if name == "brain.sqlite3" else data / name
        measure(source_dir / name, expected=plan["inputs"][name], copy_to=target)
    # Recheck the exact retained legacy bytes, independent of the original path.
    retained_identity = measure(retained / "brain.sqlite3", expected=plan["inputs"]["brain.sqlite3"])
    require(legacy_index_identity(retained / "brain.sqlite3") == old_identity, "retained original SQLite changed")
    store.write_sqlite_from_jsonl(data / "brain.sqlite3", data,
        required_artifacts=set(store.DEFAULT_ARTIFACT_FILES))
    artifacts = {}
    for name in SEMANTIC_FILES:
        path = data / name
        identity = measure(path, expected=plan["inputs"][name])
        relative = "brain/data/" + name
        logical_format = "jsonl-rowset" if name.endswith(".jsonl") else "json"
        with path.open("rb") as handle:
            logical_root = contracts._artifact_logical_root_handle(handle, logical_format, relative)
        artifacts[relative] = {"path": relative, **identity,
                              "logical_root": logical_root, "logical_format": logical_format}
    with (data / "brain.sqlite3").open("rb") as handle:
        contracts._verify_sqlite_projection(handle, destination, artifacts, verify_static_closure=False)
    for name, path in PROGRAMS.items():
        measure(path, expected=plan["programs"][name])
    for name in INPUT_FILES:
        measure(source_dir / name, expected=plan["inputs"][name])
    for name in SEMANTIC_FILES:
        measure(data / name, expected=plan["inputs"][name])
    retained_identity = measure(retained / "brain.sqlite3", expected=plan["inputs"]["brain.sqlite3"])
    report = {"schema": "wikilean.legacy-sqlite-projection-result/v1", "plan_id": expected_id,
        "scope": "projection-only", "authority": False, "baseline_approved": False,
        "legacy_execution_verified": False, "static_release_verified": False,
        "semantic_bytes_preserved": True, "indexed_artifacts": list(store.DEFAULT_ARTIFACT_FILES),
        "pass_through_artifacts": ["frontier.jsonl", "frontier_graph.json"],
        "original_sqlite": {**retained_identity, **old_identity, "path": "retained/brain.sqlite3"},
        "new_sqlite": {**measure(data / "brain.sqlite3"), "schema_version": 2, "path": "brain/data/brain.sqlite3"},
        "programs": plan["programs"], "artifacts": artifacts,
        "runtime": {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version}}
    # Absence of this completion record identifies an interrupted/failed preparation.
    with (destination / "projection.json").open("xb") as handle:
        handle.write(canonical(report)); handle.flush(); os.fsync(handle.fileno())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-plan-id", required=True)
    parser.add_argument("--source-data", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with os.fdopen(open_absolute(args.plan), "rb") as handle:
            require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), "plan must be a regular file")
            raw = handle.read(64 * 1024 + 1)
        require(len(raw) <= 64 * 1024, "plan exceeds bound")
        plan = contracts.parse_json_bytes(raw, location="legacy SQLite projection plan")
        require(raw == canonical(plan), "plan must use canonical JSON bytes")
        report = project(args.source_data, args.destination, plan, args.expected_plan_id)
        sys.stdout.buffer.write(canonical(report))
    except (ValueError, OSError, sqlite3.Error, store.StoreError, contracts.VerificationError) as exc:
        print("Legacy SQLite projection failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
