#!/usr/bin/env python3
"""Read two exact Git input files and compare their experimental assertion view."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import assertion_kernel as kernel
from ingest.git_snapshot import read_text_snapshot

PILOT_PATHS = ("brain/data/container_links.jsonl", "brain/data/discovery_proposals.jsonl")


def project_rows(result):
    projected = {path: [] for path in PILOT_PATHS}
    for assertion in result.state["assertions"]:
        if not assertion["active"]:
            continue
        attributes = assertion["payload"]["attributes"]
        kernel.exact(attributes, {"source_path", "legacy_row"}, "pilot assertion attributes")
        kernel.require(attributes["source_path"] in projected, "unregistered pilot source")
        projected[attributes["source_path"]].append(attributes["legacy_row"])
    return {path: sorted(rows, key=kernel.canonical) for path, rows in projected.items()}


def import_rows(commit, files):
    kernel.require(set(files) == set(PILOT_PATHS), "pilot requires exactly both declared input files")
    operations, original = [], {}
    for path in sorted(files):
        original[path] = []
        counts = collections.Counter()
        for line in files[path].splitlines():
            if not line.strip():
                continue
            row = kernel.contracts.parse_artifact_json_bytes(line, location=path)
            kernel.require(isinstance(row, dict) and "_meta" not in row, "pilot rows must be actual curated contributions")
            raw = kernel.canonical(row); digest = hashlib.sha256(raw).hexdigest()
            counts[digest] += 1
            assertion_id = kernel.contracts.domain_hash("wikilean.experimental-legacy-assertion.v1",
                {"commit": commit, "path": path, "row_sha256": digest, "equivalent_occurrence": counts[digest]})
            if path.endswith("container_links.jsonl"):
                source = row.get("qid")
                kernel.require(isinstance(row.get("path"), str) and row["path"], "container contribution lacks path")
                destination = "path:" + row["path"].removeprefix("path:").replace(".", "/")
                kind = "formalizes"
            else:
                source, destination, kind = row.get("src"), row.get("dst"), row.get("kind")
            actor = {"kind": "git-snapshot", "commit": commit, "path": path, "row_sha256": digest}
            payload = {"src": source, "dst": destination, "kind": kind, "attributes": {"source_path": path, "legacy_row": row}}
            operations.append(kernel.make_operation("assert_relationship", assertion_id, payload, actor=actor))
            original[path].append(row)
    kernel.require(bool(operations), "pilot contribution set is empty")
    operations.sort(key=lambda op: op["operation_id"])
    fixture = kernel.make_fixture(kernel.empty_replay(), operations)
    ledger = {"schema": kernel.LEDGER_SCHEMA, "fixtures": [fixture]}
    result = kernel.replay_ledger(ledger)
    original = {path: sorted(rows, key=kernel.canonical) for path, rows in original.items()}
    projected = project_rows(result)
    kernel.require(projected == original, "pilot source contributions changed under assertion projection")
    parity = {path: {"rows": len(rows), "legacy_root": kernel.contracts.domain_hash("wikilean.experimental-legacy-contributions.v1", rows),
        "shadow_root": kernel.contracts.domain_hash("wikilean.experimental-legacy-contributions.v1", projected[path])} for path, rows in original.items()}
    return ledger, result, parity


def shadow(repository, expected_commit, *, git="/usr/bin/git"):
    files, tree = {}, None
    for path in PILOT_PATHS:
        snapshot = read_text_snapshot(repository, scope=path, git=git)
        kernel.require(snapshot.commit == expected_commit and len(snapshot.files) == 1 and snapshot.files[0].path == path,
            "pilot source is not the explicitly expected Git commit/path")
        kernel.require(tree in (None, snapshot.tree), "pilot input trees differ")
        tree = snapshot.tree
        files[path] = snapshot.files[0].text.encode("utf-8")
    ledger, result, parity = import_rows(expected_commit, files)
    report = {"schema": "wikilean.experimental-assertion-shadow/v1", "authority": False, "production_writes": False,
        "source_commit": expected_commit, "source_tree": tree, "source_files": [{"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} for path, raw in sorted(files.items())],
        "state_root": result.semantic_root, "chain_root": result.chain_root, "parity": parity,
        "full_graph_parity": "not evaluated; this check covers only the two pilot source contribution multisets"}
    return ledger, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        ledger, report = shadow(args.repository, args.commit)
        sys.stdout.buffer.write(kernel.canonical({"ledger": ledger, "report": report}) + b"\n")
    except (ValueError, OSError, RuntimeError) as exc:
        print("Experimental assertion shadow failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
