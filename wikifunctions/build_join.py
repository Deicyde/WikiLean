#!/usr/bin/env python3
"""
build_join.py — Measure the addressable set for "WikiLean specs for Wikifunctions".

Joins three sets on the Wikidata QID:
  A. Wikifunctions universe:  every Wikidata item that carries a `wikifunctionswiki`
     sitelink -> its function ZID.  (The sitelink IS the QID<->ZID bridge.)
  B. WikiLean *mapped* concepts:  ../catalog/data/concept_layer.jsonl  (QID -> Mathlib decl).
  C. WikiLean *tracked* math concepts:  ../catalog/data/wikidata_universe.jsonl (QID -> label),
     the broader "math concepts WikiLean knows about, mapped or not".

Inputs come from the upstream catalog (../catalog/data/); outputs go to ./data/:
  wikifunctions_universe.jsonl   one row per Wikifunctions-linked Wikidata item (A)
  wikifunctions_join.jsonl       one row per A-member, enriched with WikiLean status
  wikifunctions_join_summary.json  headline counts

Run from anywhere: paths are resolved relative to this file.
No external deps beyond the stdlib + network (SPARQL + WikiLambda fetch); shells out to curl.
"""
import json, sys, time, urllib.parse, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"                       # this project's data (outputs)
SRC = ROOT.parent / "catalog" / "data"     # upstream WikiLean catalog data (inputs)
UA = "WikiLean-research/1.0 (jack.mccarthy.1@stonybrook.edu)"
SPARQL = "https://query.wikidata.org/sparql"


def _get(url, accept="application/json"):
    # Shell out to curl: avoids Python's macOS CA-bundle issue and matches the
    # rest of the pipeline's network calls.
    out = subprocess.run(
        ["curl", "-sS", "--retry", "3", "--max-time", "120",
         "-H", f"User-Agent: {UA}", "-H", f"Accept: {accept}", url],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def fetch_wikifunctions_universe():
    """All (qid, zid) pairs where the Wikidata item has a wikifunctions sitelink."""
    q = (
        "SELECT ?item ?article WHERE { "
        "?article schema:about ?item ; "
        "schema:isPartOf <https://www.wikifunctions.org/> . }"
    )
    url = f"{SPARQL}?query={urllib.parse.quote(q)}&format=json"
    data = json.loads(_get(url, accept="application/sparql-results+json"))
    out = []
    for b in data["results"]["bindings"]:
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        zid = b["article"]["value"].rsplit("/", 1)[-1]
        out.append({"qid": qid, "zid": zid})
    return out


def load_concept_layer():
    """QID -> mapping record (primary_decl, module, status, confidence, ...)."""
    idx = {}
    fp = SRC / "concept_layer.jsonl"
    with open(fp) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            idx[r["qid"]] = r
    return idx


def load_universe():
    """QID -> {label, enwiki_slug} for every math concept WikiLean tracks."""
    idx = {}
    fp = SRC / "wikidata_universe.jsonl"
    with open(fp) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            idx[r["qid"]] = r
    return idx


# Heuristic: does the mapped decl have a usable *decidable/computable* characterization,
# i.e. can Lean's kernel act as a ground-truth oracle?  This is a first-pass classifier on
# decl-name shape; the workflow stage verifies it against real Mathlib.
DECIDABLE_HINTS = (
    "Nat.", "Int.", "Bool", "decide", "Decidable", ".gcd", "Coprime", "Prime",
    "Even", "Odd", "factorial", "choose", "Fib", "Finset", "List.", "divisor",
)


def decidable_guess(rec):
    if not rec:
        return "unknown"
    blob = " ".join(
        [rec.get("primary_decl", "") or ""]
        + [d.get("decl", "") for d in rec.get("secondary_decls", []) or []]
    )
    return "likely" if any(h in blob for h in DECIDABLE_HINTS) else "needs_check"


def main():
    print("Fetching Wikifunctions universe (Wikidata items with a wikifunctions sitelink)...",
          file=sys.stderr)
    universe_wf = fetch_wikifunctions_universe()
    print(f"  {len(universe_wf)} (qid -> zid) pairs", file=sys.stderr)

    with open(DATA / "wikifunctions_universe.jsonl", "w") as f:
        for row in sorted(universe_wf, key=lambda r: r["zid"]):
            f.write(json.dumps(row) + "\n")

    concept = load_concept_layer()
    tracked = load_universe()
    print(f"  WikiLean mapped concepts (concept_layer): {len(concept)}", file=sys.stderr)
    print(f"  WikiLean tracked math concepts (universe): {len(tracked)}", file=sys.stderr)

    join_rows = []
    n_mapped = n_tracked_only = n_untracked = 0
    for row in universe_wf:
        qid, zid = row["qid"], row["zid"]
        rec = concept.get(qid)
        tr = tracked.get(qid)
        if rec and rec.get("status") == "formalized" and rec.get("primary_decl"):
            bucket = "mapped"  # Wikifunctions fn + WikiLean Mathlib decl -> spec-able NOW
            n_mapped += 1
        elif rec or tr:
            bucket = "tracked_no_decl"  # math concept WikiLean knows, no usable decl yet
            n_tracked_only += 1
        else:
            bucket = "untracked"  # Wikifunctions fn WikiLean doesn't track (non-math or gap)
            n_untracked += 1
        join_rows.append({
            "qid": qid,
            "zid": zid,
            "bucket": bucket,
            "wikilean_label": (rec or {}).get("primary_title") or (tr or {}).get("label"),
            "primary_decl": (rec or {}).get("primary_decl"),
            "module": (rec or {}).get("module"),
            "confidence": (rec or {}).get("confidence"),
            "secondary_decls": (rec or {}).get("secondary_decls"),
            "decidable_guess": decidable_guess(rec) if bucket == "mapped" else None,
        })

    join_rows.sort(key=lambda r: (r["bucket"] != "mapped", r["zid"]))
    with open(DATA / "wikifunctions_join.jsonl", "w") as f:
        for row in join_rows:
            f.write(json.dumps(row) + "\n")

    mapped = [r for r in join_rows if r["bucket"] == "mapped"]
    summary = {
        "wikifunctions_universe": len(universe_wf),
        "wikilean_mapped_concepts": len(concept),
        "wikilean_tracked_concepts": len(tracked),
        "intersection_mapped": n_mapped,
        "intersection_tracked_no_decl": n_tracked_only,
        "untracked": n_untracked,
        "mapped_decidable_likely": sum(1 for r in mapped if r["decidable_guess"] == "likely"),
        "mapped_needs_check": sum(1 for r in mapped if r["decidable_guess"] == "needs_check"),
    }
    with open(DATA / "wikifunctions_join_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    print("\n--- ADDRESSABLE NOW (Wikifunctions fn + WikiLean Mathlib decl) ---")
    for r in mapped:
        print(f"  {r['zid']:>9}  {r['qid']:>10}  {r['decidable_guess']:>11}  "
              f"{r['primary_decl']}  ({r['wikilean_label']})")


if __name__ == "__main__":
    main()
