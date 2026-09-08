#!/usr/bin/env python3
"""Create a reviewable exact Wikidata plan from explicit local selector inputs.

This command is offline and never rewrites corpus files. The prior Brain node
file must be supplied explicitly, or explicitly omitted, so an old build can
never become an undisclosed acquisition input. Review this plan and its input
origins before acquisition/source-plan authority.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import wikidata_observation as observation  # noqa: E402


def _selected(raw: bytes, name: str) -> list[str]:
    def parse_corpus(data: bytes):
        try:
            return observation.contracts.parse_artifact_json_bytes(data, location=name)
        except observation.contracts.VerificationError as exc:
            raise observation.ObservationError(f"{name}: invalid corpus JSON") from exc

    if name in {"concept-layer", "prior-brain-nodes", "universe-extension"}:
        rows = [parse_corpus(line) for line in raw.splitlines() if line.strip()]
    else:
        value = parse_corpus(raw)
        if name == "wikidata-crossrefs":
            if not isinstance(value, dict) or not isinstance(value.get("xrefs"), dict):
                raise observation.ObservationError("crossrefs must contain an xrefs object")
            selected = list(value["xrefs"])
            return observation.qids(sorted(set(selected), key=lambda q: (len(q), q)), name)
        if not isinstance(value, list):
            raise observation.ObservationError("grounding must be an array")
        rows = value
    selected = set()
    for row in rows:
        if not isinstance(row, dict):
            raise observation.ObservationError(f"{name} contains a non-object row")
        if name == "prior-brain-nodes":
            if row.get("type") != "concept":
                continue
            value = row.get("id")
        else:
            value = row.get("qid")
        if value is not None:
            observation.qids([value], name)
            selected.add(value)
    return sorted(selected, key=lambda q: (len(q), q))


def build_plan(paths: dict[str, Path | None], *, volume_floors: dict | None = None) -> dict:
    if set(paths) != set(observation.SELECTION_NAMES):
        raise observation.ObservationError("supply all five selection inputs, with explicit prior-node absence")
    inputs = []
    for name in observation.SELECTION_NAMES:
        path = paths[name]
        if path is None:
            inputs.append({"name": name, "state": "absent", "sha256": None, "bytes": 0, "qids": []})
        else:
            raw = observation.read_regular(path)
            inputs.append({"name": name, "state": "present", "sha256": observation.sha(raw),
                           "bytes": len(raw), "qids": _selected(raw, name)})
    by_name = {item["name"]: item for item in inputs}
    union = lambda names: sorted({q for name in names for q in by_name[name]["qids"]}, key=lambda q: (len(q), q))
    edge_qids = union(("concept-layer", "prior-brain-nodes"))
    description_qids = union(("grounding", "universe-extension", "wikidata-crossrefs"))
    floors = volume_floors if volume_floors is not None else {
        "universe": 1, "universe_classes": {cls: 1 for cls in observation.CLASSES},
        "universe_labels": 0, "universe_slugs": 0,
        "edges": min(50, len(edge_qids) // 2) if len(edge_qids) >= 50 else 0,
        "edge_labels": 0, "description_qids": len(description_qids),
        "descriptions": min(50, len(description_qids)) if len(description_qids) >= 50 else 0,
    }
    return observation.validate_plan({"schema": observation.PLAN_SCHEMA_V2,
        "observation_policy": observation.OBSERVATION_POLICY, "selection_inputs": inputs,
        "edge_qids": edge_qids, "description_qids": description_qids, "volume_floors": floors})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("concept-layer", "grounding", "universe-extension", "wikidata-crossrefs"):
        parser.add_argument("--" + name, required=True, type=Path)
    previous = parser.add_mutually_exclusive_group(required=True)
    previous.add_argument("--prior-brain-nodes", type=Path)
    previous.add_argument("--without-prior-brain-nodes", action="store_true")
    parser.add_argument("--volume-floors", type=Path, required=True,
                        help="explicit reviewed canonical count floors, including previous-generation coverage")
    args = parser.parse_args()
    try:
        floors = observation.parse(observation.read_regular(args.volume_floors), "reviewed floors", canonical_required=True)
        plan = build_plan({name: getattr(args, name.replace("-", "_")) for name in observation.SELECTION_NAMES},
                          volume_floors=floors)
        sys.stdout.buffer.write(observation.canonical(plan))
        return 0
    except (observation.ObservationError, OSError) as exc:
        print(f"Wikidata observation planning failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
