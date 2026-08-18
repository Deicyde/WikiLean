#!/usr/bin/env python3
"""Dev self-eval for the premise index (wiki/public/assets/premise-index/).

Simulates brain_premises' ranking on held-out theorems: sample N theorem
sources (seeded), give the ranker 3 same-file (same-module) neighbor theorems
as seeds, rank the union of the seeds' STORED premise lists by (multiplicity
across seeds desc, best stored rank asc, name asc), and score recall@20 of the
held-out theorem's true explicit deps from the MathNetwork edge data.

Truth sets reported:
  raw       every is_explicit=True dep of the target (self-loops dropped)
  filtered  raw minus the index's hub-drop list, gated on the decl oracle —
            the retrievable universe (headline: no tool reading this index can
            ever return a hub-dropped or non-oracle name)
  stored    the target's own stored top-K list (can neighbors reconstruct it?)

This is the DEV eval (LeanDojo/self-consistency side of the discipline line);
MathlibMPR stays untouched as the frozen eval — `--mpr-check` only counts how
many of its gold premises RESOLVE in the refreshed decl index (a coverage
fact, not a score).

Usage (from the repo root):
  python3 bench/analysis/premise_selfeval.py            # self-eval, seed 20260818
  python3 bench/analysis/premise_selfeval.py --mpr-check
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREMISE_DIR = ROOT / "wiki/public/assets/premise-index"
DECL_DIR = ROOT / "wiki/public/assets/decl-index"
DECL_DATA = ROOT / "catalog/.cache/declaration-data.json"
EDGES_CSV = ROOT / "catalog/.cache/mathnetwork/edges.csv"
MPR_JSON = ROOT / "bench/v2/data/MathlibMPR.json"

SEED = 20260818
HOLDOUT = 200
N_SEEDS = 3
AT_K = 20


def load_premise_index() -> tuple[dict, dict[str, list[str]]]:
    manifest = json.loads((PREMISE_DIR / "manifest.json").read_text())
    names: list[str] = []
    for i in range(manifest["chunks"]):
        names.extend(json.loads((PREMISE_DIR / "names" / f"{i}.json").read_text()))
    lists: dict[str, list[str]] = {}
    for key in manifest["shards"]:
        for src, ints in json.loads((PREMISE_DIR / f"{key}.json").read_text()).items():
            lists[src] = [names[i] for i in ints]
    return manifest, lists


def iter_edges():
    """Yield (source, target, is_explicit) rows.

    stdlib csv.reader (same as brain/build_rollups.py on this exact file):
    quoted fields at ANY position parse correctly — notation decls carry commas
    inside «» and can appear in the target column, where a hand-rolled
    first-field-only regex silently mangles the row.
    """
    with EDGES_CSV.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert header == ["source", "target", "is_explicit", "is_simplifier"], header
        for row in reader:
            if not row:
                continue
            yield row[0], row[1], row[2] == "True"


def rank_union(seed_lists: list[list[str]]) -> list[str]:
    """(multiplicity desc, best stored rank asc, name asc) — the brain_premises order.

    The name tie-break must collate like the Worker's int-index tie-break: the
    builder's name table is sorted with JS Array.sort() = UTF-16 CODE-UNIT
    order, which differs from Python's code-point order for astral-plane names
    (𝕜/𝟙 notation decls) — so compare on the UTF-16-BE encoding.
    """
    mult: dict[str, int] = defaultdict(int)
    best: dict[str, int] = {}
    for lst in seed_lists:
        for rank, name in enumerate(lst):
            mult[name] += 1
            if name not in best or rank < best[name]:
                best[name] = rank
    return sorted(mult, key=lambda n: (-mult[n], best[n], n.encode("utf-16-be")))


def self_eval(args: argparse.Namespace) -> None:
    manifest, lists = load_premise_index()
    decl_data = json.loads(DECL_DATA.read_text())["declarations"]
    hubs = {n for n, _ in manifest["hub_drop"]}
    print(f"premise index: {len(lists)} sources, pin {manifest['pin']['decl_index_etag']}")

    by_module: dict[str, list[str]] = defaultdict(list)
    for src in lists:
        info = decl_data.get(src)
        if info is None or info.get("kind") != "theorem":
            continue
        module = re.sub(r"\.html(#.*)?$", "", info["docLink"].removeprefix("./")).replace("/", ".")
        by_module[module].append(src)
    for mod in by_module:
        by_module[mod].sort()
    eligible = sorted(
        src for mod, srcs in by_module.items() if len(srcs) > N_SEEDS for src in srcs
    )
    print(f"eligible held-out pool: {len(eligible)} theorems in {sum(1 for s in by_module.values() if len(s) > N_SEEDS)} modules")

    rng = random.Random(args.seed)
    targets = rng.sample(eligible, args.holdout)
    mod_of = {}
    for mod, srcs in by_module.items():
        for s in srcs:
            mod_of[s] = mod
    seeds_of = {
        t: rng.sample([s for s in by_module[mod_of[t]] if s != t], N_SEEDS) for t in targets
    }

    truth_raw: dict[str, set[str]] = {t: set() for t in targets}
    want = set(targets)
    for src, tgt, explicit in iter_edges():
        if explicit and src in want and tgt != src:
            truth_raw[src].add(tgt)

    r_raw, r_filt, r_stored = [], [], []
    skipped = 0
    for t in targets:
        pred = rank_union([lists[s] for s in seeds_of[t]])[: args.at_k]
        pset = set(pred)
        raw = truth_raw[t]
        filt = {d for d in raw if d not in hubs and d in decl_data}
        stored = set(lists[t])
        if not raw:
            skipped += 1
            continue
        r_raw.append(len(pset & raw) / len(raw))
        if filt:
            r_filt.append(len(pset & filt) / len(filt))
        if stored:
            r_stored.append(len(pset & stored) / len(stored))

    print(f"\nself-eval: {len(targets)} held-out theorems (seed {args.seed}), "
          f"{N_SEEDS} same-module seeds each, recall@{args.at_k}; {skipped} skipped (no explicit deps)")
    print(f"  recall@{args.at_k} vs raw explicit deps:      {sum(r_raw) / len(r_raw):.4f}  (n={len(r_raw)})")
    print(f"  recall@{args.at_k} vs filtered explicit deps: {sum(r_filt) / len(r_filt):.4f}  (n={len(r_filt)})  ← headline")
    print(f"  recall@{args.at_k} vs target's stored top-{manifest['filters']['top_k']}:  {sum(r_stored) / len(r_stored):.4f}  (n={len(r_stored)})")


def norm_gold(d: str) -> str:
    d = d.strip().strip("`")
    return re.sub(r"^decl:[A-Za-z0-9]+:", "", d)


def mpr_check(_args: argparse.Namespace) -> None:
    decl_manifest = json.loads((DECL_DIR / "manifest.json").read_text())
    universe: set[str] = set()
    for key in decl_manifest["shards"]:
        universe.update(d for d, _ in json.loads((DECL_DIR / f"{key}.json").read_text()))
    golds = {norm_gold(d) for t in json.loads(MPR_JSON.read_text()) for g in t["premise_group"] for d in g["docs"]}
    hit = sorted(g for g in golds if g in universe)
    miss = sorted(golds - set(hit))
    print(f"decl index: {len(universe)} decls, etag {decl_manifest['source_sha_or_etag']}, built {decl_manifest['built_at']}")
    print(f"MathlibMPR gold premises: {len(hit)}/{len(golds)} resolve")
    if miss:
        print("missing:")
        for m in miss:
            print(f"  {m}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--holdout", type=int, default=HOLDOUT)
    ap.add_argument("--at-k", type=int, default=AT_K)
    ap.add_argument("--mpr-check", action="store_true", help="count MPR golds resolving in the decl index (no scoring)")
    args = ap.parse_args()
    if args.mpr_check:
        mpr_check(args)
    else:
        self_eval(args)


if __name__ == "__main__":
    main()
