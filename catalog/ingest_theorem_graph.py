#!/usr/bin/env python3
"""Ingest TheoremGraph's judge-affirmed (formal Mathlib decl ↔ informal arXiv
statement) matches as a per-QID arXiv literature layer for the concept graph.

Source: uw-math-ai/theorem-matching (HuggingFace) — the curated match table
behind TheoremGraph (arXiv:2606.25363). License **CC-BY-SA-4.0** (stricter than
the parent math-graph's CC-BY-4.0). We store only the LINK FACTS (decl ↔
arxiv_id / informal_ref / paper_title + the judges' verdicts), never the papers'
copyrightable text, and attribute the source; WikiLean's own data stays CC0.

Join: TheoremGraph gives (formal_decl → arXiv statement); WikiLean nodes give
(QID → Mathlib decl) via the concept graph's primary_decl AND every decl cited
in that article's annotations. We join on the shared Mathlib decl name, so each
concept gains "this result is stated as Thm X.Y in arXiv:… " links.

Anti-slop: `theorem_matching.csv` is the FULL candidate sweep (sim ≥ 0.8), ~80%
of which its own judges reject. We keep only the **paper-affirmed** slice
(GPT-5.4 judge label ∈ {exact, inexact} — the paper's own 47.7% bar), and carry
BOTH judges' labels + the similarity band per link so the UI can badge
exact-vs-inexact and a human can audit. LLMs propose, humans publish.

Run:  python3 catalog/ingest_theorem_graph.py            # from the cached CSV
      python3 catalog/ingest_theorem_graph.py --download # (re)fetch the 108MB CSV
      python3 catalog/ingest_theorem_graph.py --tier exact   # stricter slice
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = HERE / ".cache" / "theorem_matching.csv"
CONCEPT_GRAPH = DATA / "concept_graph.json"
ANNOT = HERE.parent / "site" / "annotations"
OUT = DATA / "theoremgraph_links.json"

DATASET = "uw-math-ai/theorem-matching"
RESOLVE = f"https://huggingface.co/datasets/{DATASET}/resolve/main/theorem_matching.csv"
UA = "WikiLean-theoremgraph-ingest/1.0 (https://wikilean.jackmccarthy.org; jack.mccarthy.1@stonybrook.edu)"

AFFIRM = {"exact", "inexact"}          # the paper's GPT-5.4 "match" bar (47.7% globally)
MAX_LINKS_PER_QID = 12                  # keep the map artifact + panel readable
csv.field_size_limit(10 ** 9)


def download() -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {DATASET} theorem_matching.csv (~108MB) …")
    subprocess.run(["curl", "-sS", "-L", "-m", "300", "-H", f"User-Agent: {UA}",
                    "-o", str(CACHE), RESOLVE], check=True)


def wikilean_decls() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """→ (primary: decl→{qid}, cited: decl→{qid}) over the concept layer."""
    g = json.loads(CONCEPT_GRAPH.read_text())
    primary: dict[str, set[str]] = collections.defaultdict(set)
    slug2qid = {n["slug"]: n["qid"] for n in g["nodes"] if n.get("slug")}
    for n in g["nodes"]:
        if n.get("primary_decl") and n.get("qid"):
            primary[n["primary_decl"]].add(n["qid"])
    cited: dict[str, set[str]] = collections.defaultdict(set)
    for f in glob.glob(str(ANNOT / "*.json")):
        if f.endswith(".agent1.json"):
            continue
        try:
            d = json.loads(Path(f).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        qid = slug2qid.get(d.get("slug"))
        if not qid:
            continue
        for a in (d.get("annotations") or []):
            dec = (a.get("mathlib") or {}).get("decl")
            if dec:
                cited[dec].add(qid)
    return primary, cited


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true", help="(re)fetch the CSV")
    ap.add_argument("--tier", choices=["affirmed", "exact"], default="affirmed",
                    help="affirmed = gpt54 ∈ {exact,inexact} (paper bar); exact = gpt54==exact")
    args = ap.parse_args()

    if args.download or not CACHE.exists():
        if not CACHE.exists() and not args.download:
            print(f"no cached CSV at {CACHE}; fetching (pass --download to force refresh)")
        download()

    primary, cited = wikilean_decls()
    all_decls = set(primary) | set(cited)
    print(f"WikiLean decls: {len(primary)} primary / {len(all_decls)} total (primary ∪ cited)")

    keep = (lambda lbl: lbl in AFFIRM) if args.tier == "affirmed" else (lambda lbl: lbl == "exact")

    # decl → best affirmed match (the CSV is already rank-1 per formal_decl).
    links: dict[str, list[dict]] = collections.defaultdict(list)
    seen: dict[str, set[tuple[str, str]]] = collections.defaultdict(set)
    n_rows = n_aff = n_joined = 0
    with CACHE.open(newline="") as fh:
        for r in csv.DictReader(fh):
            n_rows += 1
            if not keep(r.get("gpt54_label", "")):
                continue
            n_aff += 1
            decl = r["formal_decl"]
            if decl not in all_decls:
                continue
            link = {
                "decl": decl,
                "arxiv_id": r["arxiv_id"],
                "ref": r.get("informal_ref") or "",
                "title": r.get("paper_title") or "",
                "sim": round(float(r["sim"]), 3) if r.get("sim") else None,
                "gpt54": r.get("gpt54_label"),
                "deepseek": r.get("deepseek_label"),
            }
            for qid in (primary.get(decl, set()) | cited.get(decl, set())):
                key = (decl, r["arxiv_id"])
                if key in seen[qid]:
                    continue
                seen[qid].add(key)
                links[qid].append({**link, "primary": qid in primary.get(decl, set())})
                n_joined += 1

    # sort each concept's links: primary decl first, exact before inexact, then sim.
    order = {"exact": 0, "inexact": 1}
    for qid, ls in links.items():
        ls.sort(key=lambda x: (not x["primary"], order.get(x["gpt54"], 9), -(x["sim"] or 0)))
        del ls[MAX_LINKS_PER_QID:]

    out = {
        "_meta": {
            "source": "uw-math-ai/theorem-matching (TheoremGraph)",
            "paper": "arXiv:2606.25363",
            "license": "CC-BY-SA-4.0",
            "attribution": "Matches from TheoremGraph (Math-Graph / theorem-matching, "
                           "UW Math-AI), arXiv:2606.25363, CC-BY-SA-4.0. Stored as link "
                           "facts only; arXiv papers retain their own licenses.",
            "tier": args.tier,
            "affirm_labels": sorted(AFFIRM) if args.tier == "affirmed" else ["exact"],
            "generated_at": int(time.time()),
            "n_concepts": len(links),
            "n_links": sum(len(v) for v in links.values()),
        },
        "links": {q: links[q] for q in sorted(links)},
    }
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    tmp.replace(OUT)
    n_exact = sum(1 for v in links.values() for x in v if x["gpt54"] == "exact")
    print(f"rows={n_rows} affirmed={n_aff} joined_links={n_joined}")
    print(f"wrote {OUT.name}: {out['_meta']['n_links']} links across "
          f"{out['_meta']['n_concepts']} concepts ({n_exact} gpt54-exact) "
          f"[tier={args.tier}, {OUT.stat().st_size / 1024:.0f} KB]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
