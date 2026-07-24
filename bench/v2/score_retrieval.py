#!/usr/bin/env python3
"""Bridge v2 — mechanical retrieval scoring (no judge anywhere).

Benchmarks (third-party gold, pinned frenzymath/LeanSearch-v2@94f4888cbaf9,
CC BY 4.0 — see bench/v2/data/):
  MathlibQR fair-810 — per query row, the gold is ONE declaration
    (`full_name`); metrics Recall@10 and nDCG@10 (gold at rank r contributes
    1/log2(r+1); single relevant item, so nDCG@10 == 1/log2(rank+1) or 0).
    Judge-free metrics only, per TheoremGraph fn.7.
  MathlibMPR — per query, gold is a set of premise GROUPS, each a set of
    interchangeable decl names; metric group-Recall@10 = mean over queries of
    (# groups with >=1 member in the top 10) / (# groups).

Run files (produced by run_system.py / run_agent.py):
  bench/v2/runs/<mode>/<bench>/<system>/<qid>.json
    {"qid": ..., "ranked": ["Full.Decl.Name", ...], ...}

Name matching is EXACT full-name string equality after stripping a leading
"decl:<Lib>:" prefix — no suffix matching ever (mathlib-decl-oracles memory).

Usage: python3 bench/v2/score_retrieval.py [--runs-root bench/v2/runs]
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
QFIELDS = ["q1a_lean", "q1b_latex", "q1c_natural",
           "q2_slogan", "q3_nickname", "q4_special_case"]


def norm(name: str) -> str:
    n = (name or "").strip().strip("`")
    if n.startswith("decl:"):
        n = n.split(":", 2)[2]
    return n


def qr_rows(fair_only: bool = True) -> list[dict]:
    qr = json.loads((DATA / "MathlibQR.json").read_text())
    shared = set(json.loads((DATA / "MathlibQR_shared171.json").read_text())
                 ["shared_declarations"])
    rows = []
    for r in qr:
        if fair_only and r["full_name"] not in shared:
            continue
        for f in QFIELDS:
            q = (r.get(f) or "").strip()
            if q:
                rows.append({"qid": f"{r['id']}__{f}", "query": q,
                             "gold": r["full_name"], "style": f,
                             "difficulty": r.get("difficulty")})
    return rows


def mpr_rows() -> list[dict]:
    mpr = json.loads((DATA / "MathlibMPR.json").read_text())
    return [{"qid": r["id"], "nl": r["NL_main_result"],
             "formal": r["formal_statement"], "main": r["formal_main_result"],
             "groups": [[norm(d) for d in g["docs"]] for g in r["premise_group"]]}
            for r in mpr]


def load_ranked(d: Path) -> dict[str, list[str]]:
    out = {}
    for f in sorted(d.glob("*.json")):
        try:
            row = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out[row.get("qid") or f.stem] = [norm(x) for x in (row.get("ranked") or [])]
    return out


def score_qr(ranked: dict[str, list[str]], k: int = 10) -> dict:
    rows = qr_rows()
    per_style: dict[str, list[float]] = defaultdict(list)
    rec, ndcg, missing = [], [], 0
    for r in rows:
        lst = ranked.get(r["qid"])
        if lst is None:
            missing += 1
            continue
        try:
            rank = lst[:k].index(r["gold"]) + 1
        except ValueError:
            rank = 0
        rec.append(1.0 if rank else 0.0)
        nd = 1.0 / math.log2(rank + 1) if rank else 0.0
        ndcg.append(nd)
        per_style[r["style"]].append(nd)
    n = len(rec)
    return {"n_scored": n, "n_missing": missing,
            "recall@10": round(sum(rec) / n, 4) if n else None,
            "ndcg@10": round(sum(ndcg) / n, 4) if n else None,
            "ndcg@10_by_style": {s: round(sum(v) / len(v), 4)
                                 for s, v in sorted(per_style.items())}}


def score_mpr(ranked: dict[str, list[str]], k: int = 10) -> dict:
    rows = mpr_rows()
    gr, missing = [], 0
    for r in rows:
        lst = ranked.get(r["qid"])
        if lst is None:
            missing += 1
            continue
        top = set(lst[:k])
        covered = sum(1 for g in r["groups"] if top & set(g))
        gr.append(covered / len(r["groups"]) if r["groups"] else 0.0)
    n = len(gr)
    return {"n_scored": n, "n_missing": missing,
            "group_recall@10": round(sum(gr) / n, 4) if n else None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-root", type=Path, default=HERE / "runs")
    args = ap.parse_args()
    print("published anchors — QR-810: TheoremGraph .775 R@10/.548 nDCG, "
          "LSv2+rerank .780/.623 · MPR: LSv2 .461 gR@10, DIVER .380, "
          "TheoremGraph-negative-transfer .165\n")
    for mode_dir in sorted(args.runs_root.iterdir()) if args.runs_root.exists() else []:
        for bench_dir in sorted(mode_dir.iterdir()):
            scorer = score_qr if bench_dir.name == "qr810" else \
                     score_mpr if bench_dir.name == "mpr" else None
            if scorer is None:
                continue
            for sys_dir in sorted(bench_dir.iterdir()):
                if not sys_dir.is_dir():
                    continue
                s = scorer(load_ranked(sys_dir))
                print(f"{mode_dir.name}/{bench_dir.name}/{sys_dir.name}: "
                      f"{json.dumps(s)}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
