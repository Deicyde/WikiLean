#!/usr/bin/env python3
"""Lift the FULL LeanGraph formal dependency graph to QID→QID edges.

The live concept graph's formal edges come from a 9,999-pair extract
(mathlib_deps/mathlib_edges.tsv). TheoremGraph's math-graph ships the complete
elaborator-level Mathlib dependency graph (11.3M typed decl→decl edges), keyed by
statement UUID. This lifts it: (decl→decl dep) × (decl→QID) → (QID→QID) formal
edge, keeping only edges between concepts we actually map — so a richer,
formally-grounded edge set than the extract, with the witnessing decl-pairs +
edge_type kept as the evidence payload.

Weight semantics (brain/SCHEMA.md contract): `weight` counts DISTINCT
(src_decl, dep_decl) pairs witnessing the edge — never raw CSV rows (18.3% dup
inflation) and never once per (qa,qb) product. `w_types` splits that count into
{sig: sig+field+extends, def, proof} buckets (a pair can land in several).
`docref` rows are doc references, not dependencies — excluded entirely.

Inputs (cached, gitignored — from uw-math-ai/math-graph, CC-BY-4.0):
  catalog/.cache/statement_formal.csv    statement_id → decl_name (388k)
  catalog/.cache/formal_dependency.csv   src_id → dep_id (+ edge_type) (11.3M)
Plus a decl→[qid] map (default the live decl_to_qid.json; pass the rebuild's).

Usage:
  python3 catalog/lift_formal_edges.py                       # live decl_to_qid
  python3 catalog/lift_formal_edges.py --decl-to-qid X.json --out Y.json
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
STMT = CACHE / "statement_formal.csv"
DEP = CACHE / "formal_dependency.csv"
DEFAULT_D2Q = HERE / "mathlib_deps" / "decl_to_qid.json"
DEFAULT_OUT = HERE / "data" / "formal_edges_lifted.json"
MAX_WITNESS = 4
BUCKET = {"sig": "sig", "field": "sig", "extends": "sig", "def": "def", "proof": "proof"}
csv.field_size_limit(10 ** 9)


def lift(decl_to_qid: dict[str, list[str]]) -> list[dict]:
    interest = set(decl_to_qid)
    if not STMT.exists() or not DEP.exists():
        sys.exit(f"missing {STMT.name}/{DEP.name} in {CACHE} — download math-graph first")

    # statement_id → decl, ONLY for interest decls (keeps the id set small).
    id2decl: dict[str, str] = {}
    with STMT.open(newline="") as fh:
        for r in csv.DictReader(fh):
            d = r.get("decl_name")
            if d in interest:
                id2decl[r["statement_id"]] = d
    print(f"  interest statement ids: {len(id2decl)} (for {len(interest)} decls)", file=sys.stderr)

    # scan the 11.3M dep edges; keep only interest→interest.
    edges: dict[tuple[str, str], dict] = {}
    n = kept = docref = unbucketed = 0
    with DEP.open(newline="") as fh:
        for r in csv.DictReader(fh):
            n += 1
            a_decl = id2decl.get(r["src_id"])
            if a_decl is None:
                continue
            b_decl = id2decl.get(r["dep_id"])
            if b_decl is None or b_decl == a_decl:
                continue
            t = r.get("edge_type") or "dep"
            if t == "docref":
                docref += 1
                continue
            bucket = BUCKET.get(t)
            if bucket is None:
                unbucketed += 1  # counts toward weight but no w_types bucket
            pair = (a_decl, b_decl)
            for qa in decl_to_qid[a_decl]:
                for qb in decl_to_qid[b_decl]:
                    if qa == qb:
                        continue
                    kept += 1
                    e = edges.setdefault((qa, qb), {"decls": [], "pairs": set(),
                                                    "w": {"sig": set(), "def": set(), "proof": set()},
                                                    "types": set()})
                    e["types"].add(t)
                    if bucket is not None:
                        e["w"][bucket].add(pair)
                    if pair not in e["pairs"]:
                        e["pairs"].add(pair)
                        if len(e["decls"]) < MAX_WITNESS:
                            e["decls"].append([a_decl, b_decl])
    print(f"  scanned {n} dep rows, {kept} interest-pairs → {len(edges)} QID→QID edges "
          f"({docref} docref rows excluded, {unbucketed} unbucketed)", file=sys.stderr)

    return [{"from": a, "to": b, "source": "mathlib",
             "decls": v["decls"], "weight": len(v["pairs"]),
             "w_types": {k: len(s) for k, s in v["w"].items()},
             "edge_types": sorted(v["types"])}
            for (a, b), v in edges.items()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decl-to-qid", type=Path, default=DEFAULT_D2Q)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    d2q = json.loads(args.decl_to_qid.read_text())
    # accept {decl: qid} or {decl: [qids]}
    d2q = {k: (v if isinstance(v, list) else [v]) for k, v in d2q.items()}
    edges = lift(d2q)
    tmp = args.out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"n_edges": len(edges), "edges": edges}, ensure_ascii=False))
    tmp.replace(args.out)
    print(f"wrote {args.out.name}: {len(edges)} formal QID→QID edges "
          f"({args.out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
