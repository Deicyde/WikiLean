#!/usr/bin/env python3
"""Emit the WikiLean join fabric as a single CC0, vendorable artifact.

One line per (QID, Mathlib decl) mapping — the Stacks-tags-file design that let
Mathlib adopt @[stacks]: trivially diffable, trivially importable, no schema. A
provenance tier travels with every row so a consumer can filter to the trust
level they want (merged = human-reviewed & in mathlib master; ai = catalog
mapping, not yet upstream-reviewed).

This is the artifact the outreach notes (docs/outreach.md) offer to Theorem
Graph and LeanBridge, and the seed a Wikidata Mix'n'match catalog would import.

Output: catalog/data/join_fabric.tsv (+ .jsonl). Deterministic; run nightly or
on demand. CC0 — see the header written into the file.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
# Later files win on QID collision (same precedence as bot/pool.py CATALOG).
CATALOG = ["pilot_tagged.jsonl", "tier2_tagged.jsonl", "generated_candidates.jsonl",
           "mathlib_yaml_tagged.jsonl", "refresh_tagged.jsonl"]
MERGED = HERE.parent / "bot" / "data" / "tagged_in_master.txt"
OUT_TSV = DATA / "join_fabric.tsv"
OUT_JSONL = DATA / "join_fabric.jsonl"

TIER = {  # source file → provenance tier (most-trusted first)
    "mathlib_yaml_tagged.jsonl": "mathlib-maintainer",  # docs/1000.yaml
    "refresh_tagged.jsonl": "ai-verified",
    "generated_candidates.jsonl": "ai-verified",
    "tier2_tagged.jsonl": "ai",
    "pilot_tagged.jsonl": "ai",
}


def main() -> int:
    merged = {l.strip() for l in MERGED.read_text().splitlines() if l.strip().startswith("Q")} \
        if MERGED.exists() else set()
    rows: dict[str, dict] = {}  # keyed by (qid, decl) so later files override cleanly
    for name in CATALOG:
        f = DATA / name
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            decl = r.get("primary_decl")
            qid = r.get("primary_qid") or r.get("wikidata_qid")
            if not decl or not qid:
                continue
            module = next((d.get("module") for d in r.get("mathlib_decls", [])
                           if d.get("decl") == decl), None)
            rows[(qid, decl)] = {
                "qid": qid, "decl": decl, "module": module or "",
                "label": r.get("primary_qid_label") or r.get("title") or "",
                # A merged @[wikidata] tag in master is the human-reviewed gold tier,
                # regardless of which catalog file the mapping also appears in.
                "tier": "merged" if qid in merged else TIER.get(name, "ai"),
            }
    out = sorted(rows.values(), key=lambda r: (int(r["qid"][1:]), r["decl"]))
    header = ("# WikiLean join fabric — Wikidata QID ↔ Mathlib declaration.\n"
              "# Released CC0 (public domain). Source: wikilean.jackmccarthy.org\n"
              "# tier: merged = @[wikidata] tag reviewed & merged into mathlib master;\n"
              "#       mathlib-maintainer = from mathlib docs/1000.yaml; ai(-verified) = catalog mapping.\n"
              "# Columns: qid\\tdecl\\tmodule\\ttier\\tlabel\n")
    OUT_TSV.write_text(header + "".join(
        f"{r['qid']}\t{r['decl']}\t{r['module']}\t{r['tier']}\t{r['label']}\n" for r in out))
    OUT_JSONL.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
    tiers = {}
    for r in out:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    print(f"join_fabric: {len(out)} (QID, decl) mappings → {OUT_TSV.name} + {OUT_JSONL.name}; tiers {tiers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
