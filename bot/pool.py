#!/usr/bin/env python3
"""Deterministic candidate-tag pool selector (no LLM).

Walks the ranked most-used QID list and keeps the ones that have an unambiguous,
high-confidence Mathlib counterpart in the WikiLean catalog and are NOT already
tagged or in flight. These are the "unreviewed" tags for the queue / next batch.

Sources (all in-repo): catalog/data/{pilot,tier2}_tagged.jsonl,
bot/data/most_used_qids.json, bot/data/tagged_in_master.txt.
"""
import json, sys, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = [ROOT / "catalog/data/pilot_tagged.jsonl", ROOT / "catalog/data/tier2_tagged.jsonl"]
MOST_USED = ROOT / "bot/data/most_used_qids.json"
TAGGED = ROOT / "bot/data/tagged_in_master.txt"


def module_to_file(module):
    return module.replace(".", "/") + ".lean" if module else None


def load_catalog():
    cat = {}
    for f in CATALOG:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            q, pd = r.get("wikidata_qid"), r.get("primary_decl")
            if not q or not pd:
                continue
            file, conf = None, None
            for d in r.get("mathlib_decls", []):
                if d.get("decl") == pd:
                    file, conf = module_to_file(d.get("module")), d.get("confidence")
                    break
            cat[q] = {"decl": pd, "file": file, "label": r.get("title", ""), "confidence": conf}
    return cat


def candidates(n=25, exclude=(), require_high=True):
    cat = load_catalog()
    excl = set(exclude)
    if TAGGED.exists():
        excl |= {l.strip() for l in TAGGED.read_text().splitlines() if l.strip().startswith("Q")}
    order = list(json.loads(MOST_USED.read_text()).keys())
    out = []
    for q in order:
        if q in excl or q not in cat:
            continue
        c = cat[q]
        if require_high and c.get("confidence") != "high":
            continue
        out.append({"qid": q, "label": c["label"], "decl": c["decl"], "file": c["file"], "status": "unreviewed"})
        if len(out) >= n:
            break
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=25)
    ap.add_argument("--exclude", default="", help="comma-separated qids to skip (in-flight)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cands = candidates(args.n, [q.strip() for q in args.exclude.split(",") if q.strip()])
    if args.json:
        print(json.dumps(cands, indent=1))
    else:
        print(f"{len(cands)} candidate(s):")
        for c in cands:
            print(f"  {c['qid']:12} {c['label'][:34]:34} -> {c['decl']}")
