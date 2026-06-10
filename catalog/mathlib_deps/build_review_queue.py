#!/usr/bin/env python3
"""Build a human review queue for AI-tagged decl<->QID mappings.

Surfaces every (decl, QID) pair where the decl is mapped to more than one
distinct QID. Sorted by fan-out (most suspicious first), then by decl name.
Use this to cull AI false positives before they inflate the concept graph and
before any of these mappings get submitted to Wikidata via the "Mathlib
declaration" property.

Output:
  catalog/mathlib_deps/decl_review_queue.csv          # one row per (decl, qid)
  catalog/mathlib_deps/decl_review_queue_summary.json # fan-out distribution + top suspects
"""
from __future__ import annotations

import csv
import json
import urllib.parse
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECL_QID = HERE / "decl_to_qid.json"
CONCEPT = HERE.parent / "data" / "concept_layer.jsonl"
OUT_CSV = HERE / "decl_review_queue.csv"
OUT_JSON = HERE / "decl_review_queue_summary.json"


def main() -> None:
    decl_to_qids: dict[str, list[str]] = json.loads(DECL_QID.read_text())

    qid_rec: dict[str, dict] = {}
    qid_primary: dict[str, str] = {}
    qid_to_decls: dict[str, set[str]] = defaultdict(set)
    with CONCEPT.open() as fh:
        for line in fh:
            r = json.loads(line)
            qid = r["qid"]
            qid_rec[qid] = r
            if r.get("primary_decl"):
                qid_primary[qid] = r["primary_decl"]
                qid_to_decls[qid].add(r["primary_decl"])
            for s in r.get("secondary_decls") or []:
                if isinstance(s, dict) and s.get("decl"):
                    qid_to_decls[qid].add(s["decl"])

    multi = {d: sorted(set(qs)) for d, qs in decl_to_qids.items() if len(set(qs)) > 1}

    rows: list[dict] = []
    for decl, qids in sorted(multi.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        fanout = len(qids)
        for i, qid in enumerate(qids, start=1):
            rec = qid_rec.get(qid, {})
            label = rec.get("primary_title")
            if not label:
                titles = rec.get("titles") or []
                label = titles[0] if titles else qid
            other = sorted(d for d in qid_to_decls.get(qid, set()) if d != decl)
            other_str = ", ".join(other[:5]) + (f" (+{len(other) - 5} more)" if len(other) > 5 else "")
            slug = rec.get("article_slug") or ""
            rows.append({
                "decl": decl,
                "decl_fanout": fanout,
                "qid_index": f"{i}/{fanout}",
                "qid": qid,
                "qid_label": label,
                "qid_status": rec.get("status") or "",
                "qid_importance": rec.get("importance") or "",
                "is_primary_decl_of_qid": "TRUE" if qid_primary.get(qid) == decl else "FALSE",
                "qid_other_decls": other_str,
                "wikipedia_url": (
                    f"https://en.wikipedia.org/wiki/{urllib.parse.quote(slug)}"
                    if slug else ""
                ),
                "wikidata_url": f"https://www.wikidata.org/wiki/{qid}",
                "mathlib_doc_url": (
                    "https://leanprover-community.github.io/mathlib4_docs/find/?pattern="
                    + urllib.parse.quote(decl)
                ),
                "decision": "",
                "notes": "",
            })

    fields = list(rows[0].keys()) if rows else []
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    fan_dist: dict[int, int] = defaultdict(int)
    for qids in multi.values():
        fan_dist[len(qids)] += 1

    summary = {
        "total_decls_in_review_queue": len(multi),
        "total_pairs_in_review_queue": len(rows),
        "fanout_distribution": dict(sorted(fan_dist.items())),
        "top_20_suspects": [
            {"decl": d, "fanout": len(qs), "qids": qs}
            for d, qs in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:20]
        ],
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2))

    print(f"{len(multi)} multi-QID decls -> {len(rows)} pairs")
    print(f"  CSV     -> {OUT_CSV}")
    print(f"  summary -> {OUT_JSON}")
    print(f"\nfanout distribution:")
    for n, c in sorted(fan_dist.items()):
        print(f"  {n:>3} QIDs: {c} decls")


if __name__ == "__main__":
    main()
