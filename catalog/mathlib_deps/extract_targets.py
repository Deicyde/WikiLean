#!/usr/bin/env python3
"""Emit Lean target decls + decl->QID map from concept_layer.jsonl.

A target is any decl (primary_decl or any secondary_decls[].decl) that the
concept layer associates with a QID. The Lean extractor restricts dependency
edges to this set; the Python merger lifts decl->decl edges to QID->QID via
decl_to_qid.json.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONCEPT = HERE.parent / "data" / "concept_layer.jsonl"
TARGETS = HERE / "targets.txt"
DECL_QID = HERE / "decl_to_qid.json"


def main() -> None:
    decl_to_qids: dict[str, list[str]] = {}
    with CONCEPT.open() as fh:
        for line in fh:
            rec = json.loads(line)
            qid = rec.get("qid")
            if not qid:
                continue
            primary = rec.get("primary_decl")
            if primary:
                decl_to_qids.setdefault(primary, []).append(qid)
            for sd in rec.get("secondary_decls") or []:
                if isinstance(sd, dict) and sd.get("decl"):
                    decl_to_qids.setdefault(sd["decl"], []).append(qid)

    # Dedupe qid lists, preserve order.
    for d, qs in decl_to_qids.items():
        seen: set[str] = set()
        decl_to_qids[d] = [q for q in qs if not (q in seen or seen.add(q))]

    TARGETS.write_text("\n".join(sorted(decl_to_qids)) + "\n")
    DECL_QID.write_text(json.dumps(decl_to_qids, indent=2, sort_keys=True))
    total_pairs = sum(len(qs) for qs in decl_to_qids.values())
    print(f"{len(decl_to_qids)} distinct decls -> {total_pairs} decl/qid pairs")
    print(f"  -> {TARGETS}")
    print(f"  -> {DECL_QID}")


if __name__ == "__main__":
    main()
