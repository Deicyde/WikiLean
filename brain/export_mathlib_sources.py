#!/usr/bin/env python3
"""Export an independently verified fresh Mathlib capture into private v3 sources.

Both source families remain upstream acquired datasets. This never relabels
downloaded Mathlib as WikiLean-curated Git content. Source-plan review remains
explicit; no tracked corpus, bot checkout, deployment or public policy changes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import mathlib_source_evidence as evidence


def verify_export(path):
    evidence.validate_module_origins()
    files, manifest = evidence.read_bundle(path, evidence.EXPORT_SCHEMA)
    capture = {name.removeprefix("acquisition/"): raw for name, raw in files.items() if name.startswith("acquisition/")}
    plan = evidence.validate_plan(evidence.parse(capture["plan.json"], "plan"))
    tool = evidence.parse(capture["tool.json"], "tool")
    raw = {spec[1]: capture["raw/" + spec[1]] for spec in evidence.request_specs(plan)}
    acquired = evidence.parse(capture["receipts/docs.json"], "receipt")["audit"]["acquired_at"]
    expected_capture = evidence.capture_files(plan, raw, tool, acquired)
    if capture != {name: data for name, data in expected_capture.items() if name != "manifest.json"}:
        raise evidence.EvidenceError("export acquisition closure differs")
    lineage = evidence.parse(files["evidence/source-lineage.json"], "lineage")
    normalization_profile = evidence.parse(files["normalization/tool-profile.json"], "normalization profile") \
        if "normalization/tool-profile.json" in files else None
    expected = evidence.build_export(plan, raw, tool, capture, lineage["audit"]["normalized_at"],
                                     normalizer=lineage["tool"], normalization_profile=normalization_profile)
    if expected != {**files, "manifest.json": evidence.canonical(manifest)}:
        raise evidence.EvidenceError("export independent normalization differs")
    for name in ("source", "docs"):
        source = evidence.parse(files[f"source-manifests/{name}.json"], "source manifest")
        evidence.contracts.verify_source_manifest_files(evidence.contracts.validate_source_manifest(source), path)
    return evidence.parse(files["normalization/facts.json"], "facts")


def export(capture_path, store):
    if capture_path == store or capture_path in store.parents or store in capture_path.parents:
        raise evidence.EvidenceError("capture and export store must be disjoint by ancestry")
    plan, raw, tool, capture = evidence.verify_capture(capture_path)
    profile = evidence.current_profile()
    when = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = evidence.build_export(plan, raw, tool, capture, when)
    if evidence.current_profile() != profile:
        raise evidence.EvidenceError("export implementation changed during normalization")
    target = evidence.publish(files, store, evidence.EXPORT_SCHEMA)
    verify_export(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("export", "verify"))
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--store", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "export":
            if args.capture is None or args.store is None or args.bundle is not None:
                parser.error("export requires --capture and --store")
            print(export(args.capture, args.store))
        else:
            if args.bundle is None or args.capture is not None or args.store is not None:
                parser.error("verify requires --bundle")
            print(evidence.canonical(verify_export(args.bundle)).decode())
    except (evidence.EvidenceError, evidence.contracts.VerificationError, OSError, ValueError) as exc:
        print(f"Mathlib source export failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
