#!/usr/bin/env python3
"""Verify a sealed observation or export its evidence-closed v3 source entry.

The source command requires an explicit reviewed license/redistribution policy.
It supplies a source entry, not a reviewed full-corpus source plan. Planning
input origins and revision coherence still require source-authority review.
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import wikidata_observation as observation  # noqa: E402


def source_entry(path: Path, *, root_name: str, license_policy: dict) -> dict:
    bundle = observation.verify_bundle(path)
    receipt, lineage = bundle["receipt"], bundle["lineage"]
    observation.contracts._expect_pattern(root_name, "root name", observation.contracts.NAME_RE, "a lowercase root name")
    def ref(relative):
        return {"root": root_name, **observation.member_ref(relative, observation.read_regular(path / relative))}
    objects = []
    for obj in [*receipt["outputs"], *lineage["outputs"]]:
        name = obj["object"]
        relative = "acquired.jsonl" if name == "wikidata_observation_raw" else observation.OUTPUTS[name.replace("_", "-")]
        objects.append({**ref(relative), "name": name,
                        "roles": ["raw" if name == "wikidata_observation_raw" else "normalized"],
                        "redistribution": license_policy["redistribution"]})
    # Keep these closed audit/configuration preimages with the real source pack,
    # rather than retaining only the lineage configuration digest.
    # The enclosing bundle manifest carries an audit-generation ID. Keep it
    # outside semantic source objects; v3 already seals exact receipt/lineage
    # evidence bytes separately without leaking audit clocks into source IDs.
    for name, relative in (("request_plan", "request-plan.json"), ("toolchain", "toolchain.json")):
        objects.append({**ref(relative), "name": name, "roles": ["receipt"],
                        "redistribution": license_policy["redistribution"]})
    preimages = []
    for index, request in enumerate(observation.requests_for(bundle["plan"])):
        preimages.append({**ref(f"requests/{index:06d}.form"), "parameters_sha256": observation.sha(request.parameters)})
    source = {
        "source": observation.SOURCE, "source_kind": "acquired_dataset",
        "pin": copy.deepcopy(receipt["pin"]), "license": copy.deepcopy(license_policy),
        "acquisition": copy.deepcopy(receipt["tool"]),
        "normalization": {"schema": lineage["normalization_schema"], "tool": copy.deepcopy(lineage["tool"]),
                          "inputs": [obj["object"] for obj in receipt["outputs"]],
                          "outputs": [obj["object"] for obj in lineage["outputs"]]},
        "objects": sorted(objects, key=lambda obj: obj["name"]),
        "evidence": {
            "acquisition_receipts": [{**ref("acquisition-receipt.json"),
                "acquisition_receipt_id": receipt["acquisition_receipt_id"]}],
            "normalization_lineage": {**ref("normalization-lineage.json"),
                "normalization_lineage_id": lineage["normalization_lineage_id"]},
            "request_parameter_preimages": sorted(preimages, key=lambda item: item["parameters_sha256"]),
        },
    }
    # Validate the exact prospective manifest against the normative v3 contract.
    import source_plan_contracts
    source_plan_contracts._source_manifest_from_plan(source, "wikidata observation source")
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("verify", "source"))
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expected-id")
    parser.add_argument("--root-name", default="wikidata_observation")
    parser.add_argument("--license-expression")
    parser.add_argument("--redistribution", choices=("allowed", "restricted", "link-only", "unknown"))
    args = parser.parse_args()
    try:
        bundle = observation.verify_bundle(args.bundle, expected_id=args.expected_id)
        if args.mode == "source":
            if args.license_expression is None or args.redistribution is None:
                parser.error("source export requires explicit reviewed --license-expression and --redistribution")
            result = source_entry(args.bundle, root_name=args.root_name,
                license_policy={"expression": args.license_expression, "redistribution": args.redistribution})
        else:
            result = {"bundle_id": bundle["bundle_id"], "observation_policy": observation.OBSERVATION_POLICY,
                      "acquisition_receipt_id": bundle["receipt"]["acquisition_receipt_id"],
                      "normalization_lineage_id": bundle["lineage"]["normalization_lineage_id"],
                      "bindings": observation.OUTPUTS}
        sys.stdout.buffer.write(observation.canonical(result))
        return 0
    except (observation.ObservationError, observation.contracts.VerificationError, OSError) as exc:
        print(f"Wikidata observation verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
