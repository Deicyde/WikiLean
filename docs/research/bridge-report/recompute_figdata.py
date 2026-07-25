#!/usr/bin/env python3
"""Recompute every number the report figures use, asserting each against the
value printed in docs/research/BRIDGE-REPORT.md. Output: figures/figdata.json.

Sources (same as the report's §8 recomputation entry points):
  - bench/data/bridge_summary.json  (paired_matrix -> per-split success + McNemar)
  - bench/data/runs/<arm>/*.json    (halluc recount via score_bridge Oracle/extractor,
                                     cost + wall-clock per split)
  - bench/data/fresh_tasks.jsonl    (det x det2 -> 74-task primary subset)
  - bench/v2/score_retrieval.py     (QR-810 / MPR agent + system rows)
  - bench/v2/runs/sorrydb/**        (run rows x verify.jsonl, tasks_frozen filter)
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

# Repo root = nearest ancestor holding bench/ — works both in the WikiLean
# working repo (docs/research/bridge-report/) and in the preservation repo
# Deicyde/wikilean-bridge-experiment (report/bridge-report/).
REPO = next(p for p in Path(__file__).resolve().parents if (p / "bench").is_dir())
BENCH = REPO / "bench"
OUT = Path(__file__).resolve().parent / "figures" / "figdata.json"

sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH / "v2"))
import score_bridge  # noqa: E402
import score_retrieval  # noqa: E402

ARMS = ["A", "B", "C", "D", "E"]
mismatches: list[str] = []


def check(label: str, got, want, tol=0.0):
    ok = (abs(got - want) <= tol) if isinstance(want, float) else (got == want)
    tag = "OK " if ok else "MISMATCH"
    print(f"  [{tag}] {label}: got {got!r} want {want!r}")
    if not ok:
        mismatches.append(f"{label}: got {got!r} want {want!r}")


# ---------------------------------------------------------------- Tier 1 ----
print("== Tier 1: per-split success from paired_matrix ==")
summary = json.loads((BENCH / "data" / "bridge_summary.json").read_text())
pm = summary["paired_matrix"]
eval_ids = sorted(t for t in pm if not t.startswith("fresh_"))
fresh_ids = sorted(t for t in pm if t.startswith("fresh_"))
assert len(eval_ids) == 371 and len(fresh_ids) == 100

succ = {"eval": {}, "fresh": {}}
for arm in ARMS:
    succ["eval"][arm] = 100.0 * sum(pm[t][arm] for t in eval_ids) / len(eval_ids)
    succ["fresh"][arm] = 100.0 * sum(pm[t][arm] for t in fresh_ids) / len(fresh_ids)

# NOTE: D eval = 238/371 = 64.1509% — the md prints 64.1 via double rounding
# (0.6415 -> 0.641); correct 1-dp rounding is 64.2. Verified 2026-07-25.
for arm, want in zip(ARMS, [59.6, 57.1, 62.3, 64.2, 60.9]):
    check(f"eval success {arm}", round(succ["eval"][arm], 1), want)
for arm, want in zip(ARMS, [20.0, 22.0, 25.0, 42.0, 16.0]):
    check(f"fresh success {arm}", round(succ["fresh"][arm], 1), want)


def mcnemar(ids, x, y):
    b = sum(1 for t in ids if pm[t][x] and not pm[t][y])
    c = sum(1 for t in ids if pm[t][y] and not pm[t][x])
    n, k = b + c, min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** (n - 1) if n else 1.0
    return b, c, min(1.0, p)

print("== Tier 1: fresh McNemar ==")
for pair, want in [(("D", "E"), (32, 6)), (("D", "C"), (28, 11)), (("D", "A"), (29, 7))]:
    b, c, p = mcnemar(fresh_ids, *pair)
    check(f"fresh McNemar {pair[0]}v{pair[1]} discordant", (b, c), want)
    print(f"          exact p = {p:.2e}")
b, c, p = mcnemar(eval_ids, "D", "E")
check("eval McNemar DvE discordant", (b, c), (63, 51))
print(f"          exact p = {p:.3f}")

print("== Tier 1: 74-task both-determinate subset ==")
fresh_tasks = [json.loads(l) for l in (BENCH / "data" / "fresh_tasks.jsonl").read_text().splitlines()]
det_ids = [t["id"] for t in fresh_tasks if t.get("determinate") and t.get("det2")]
check("both-determinate n", len(det_ids), 74)
check("det subset D", sum(pm[t]["D"] for t in det_ids), 31)
check("det subset E", sum(pm[t]["E"] for t in det_ids), 14)
b, c, p = mcnemar(det_ids, "D", "E")
check("det McNemar DvE discordant", (b, c), (22, 5))
print(f"          exact p = {p:.4f}")

# ------------------------------------------- Tier 1: halluc per split -------
print("== Tier 1: hallucinated-decl rate per split (score_bridge oracle) ==")
oracle = score_bridge.Oracle(enabled=True)
assert oracle.decls, "declaration-data oracle missing"
halluc = {"eval": {}, "fresh": {}}
runs_w_halluc = {"eval": {}, "fresh": {}}
cost_folded, wall_folded = {}, {}  # the md's §3.2 cost bullet = folded 471-run means
for arm in ARMS:
    agg = {s: [0, 0, 0] for s in ("eval", "fresh")}  # cited, halluc, runs_w
    costs, walls = [], []
    for f in sorted((BENCH / "data" / "runs" / arm).glob("*.json")):
        row = json.loads(f.read_text())
        split = "fresh" if row["task_id"].startswith("fresh_") else "eval"
        cited = score_bridge.extract_cited(row.get("output_lean") or "")
        bad = [n for n in cited if oracle.classify(n) == "hallucinated"]
        agg[split][0] += len(cited)
        agg[split][1] += len(bad)
        agg[split][2] += bool(bad)
        ts = row.get("transcript_stats") or {}
        if ts.get("cost_usd") is not None:
            costs.append(ts["cost_usd"])
        if row.get("wall_s") is not None:
            walls.append(row["wall_s"])
    for s in ("eval", "fresh"):
        halluc[s][arm] = 100.0 * agg[s][1] / agg[s][0] if agg[s][0] else 0.0
        runs_w_halluc[s][arm] = agg[s][2]
    cost_folded[arm] = sum(costs) / len(costs)
    wall_folded[arm] = sum(walls) / len(walls)

# NOTE: C eval = 140/1315 = 10.6464% — the md prints 10.7; correct rounding 10.6.
for arm, want in zip(ARMS, [10.1, 11.0, 10.6, 5.9, 11.3]):
    check(f"eval halluc {arm}", round(halluc["eval"][arm], 1), want)
for arm, want in zip(ARMS, [21.2, 17.7, 20.9, 6.8, 26.3]):
    check(f"fresh halluc {arm}", round(halluc["fresh"][arm], 1), want)
for arm, want in zip(ARMS, [86, 101, 103, 57, 107]):
    check(f"eval runs-w-halluc {arm}", runs_w_halluc["eval"][arm], want)
for arm, want in zip(ARMS, [54, 48, 49, 23, 36]):
    check(f"fresh runs-w-halluc {arm}", runs_w_halluc["fresh"][arm], want)
# The md §3.2 bullet "Cost (recomputed from run rows)" = FOLDED means over all
# 471 runs/arm (verified exactly 2026-07-25); fresh-only means differ
# (e.g. E fresh-only $0.096 because 31 no-output runs are cheap).
for arm, want in zip(ARMS, [0.034, 0.048, 0.140, 0.121, 0.128]):
    check(f"folded mean cost {arm}", round(cost_folded[arm], 3), want)
for arm, want in [("C", 116), ("D", 89), ("E", 106)]:
    check(f"folded mean wall {arm}", round(wall_folded[arm]), want)

# ---------------------------------------------------------- Retrieval ------
print("== Retrieval: QR-810 + MPR (score_retrieval) ==")
runs_root = BENCH / "v2" / "runs"
qr, mpr = {}, {}
for sysname in ["N", "F", "W", "WF"]:
    qr[sysname] = score_retrieval.score_qr(score_retrieval.load_ranked(runs_root / "agent" / "qr810" / sysname))
    mpr[sysname] = score_retrieval.score_mpr(score_retrieval.load_ranked(runs_root / "agent" / "mpr" / sysname))
qr["system"] = score_retrieval.score_qr(score_retrieval.load_ranked(runs_root / "system" / "qr810" / "wikibrain"))
mpr["system"] = score_retrieval.score_mpr(score_retrieval.load_ranked(runs_root / "system" / "mpr" / "wikibrain"))

for s, want_r, want_n in [("system", 0.036, 0.031), ("N", 0.633, 0.598),
                          ("F", 0.831, 0.790), ("W", 0.816, 0.781), ("WF", 0.885, 0.839)]:
    check(f"QR R@10 {s}", round(qr[s]["recall@10"], 3), want_r)
    check(f"QR nDCG@10 {s}", round(qr[s]["ndcg@10"], 3), want_n)
for s, want in [("system", 0.000), ("N", 0.203), ("W", 0.272), ("F", 0.453), ("WF", 0.557)]:
    check(f"MPR gR@10 {s}", round(mpr[s]["group_recall@10"], 3), want)
# W special_case = 0.5225 exactly; md's 0.523 is half-up (float round gives .522)
check("QR special_case W", qr["W"]["ndcg@10_by_style"]["q4_special_case"], 0.5225, tol=0.0001)
check("QR special_case F", round(qr["F"]["ndcg@10_by_style"]["q4_special_case"], 3), 0.384)

# ------------------------------------------------------------ SorryDB ------
print("== SorryDB: run rows x verify.jsonl, tasks_frozen filter ==")
sdir = BENCH / "v2" / "runs" / "sorrydb"
frozen = {json.loads(l)["id"] for l in
          (BENCH / "v2" / "data" / "sorrydb" / "tasks_frozen.jsonl").read_text().splitlines()}
check("frozen tasks n", len(frozen), 171)
verdicts = defaultdict(dict)  # arm -> id -> verdict
for l in (sdir / "verify.jsonl").read_text().splitlines():
    r = json.loads(l)
    if r["id"] in frozen:
        verdicts[r["arm"]][r["id"]] = r["verdict"]

sorry_stats = {}
for arm, wants in [("N", dict(rows=168, noout=70, gaveup=40, cand=58, proved=2,
                              failed=48, unspl=5, nover=3, cost=58.97, cpp=29.48, wall=120)),
                   ("F", dict(rows=169, noout=15, gaveup=83, cand=71, proved=9,
                              failed=54, unspl=6, nover=2, cost=102.76, cpp=11.42, wall=198)),
                   ("WF", dict(rows=171, noout=11, gaveup=86, cand=74, proved=10,
                               failed=56, unspl=5, nover=3, cost=108.87, cpp=10.89, wall=183))]:
    rows = [json.loads(f.read_text())
            for f in sorted((sdir / arm / "claude-sonnet-5").glob("*.json"))]
    rows = [r for r in rows if r["id"] in frozen]
    noout = sum(1 for r in rows if r.get("error") or not r.get("proof"))
    gaveup = sum(1 for r in rows if r.get("gave_up") and not r.get("error"))
    cands = [r for r in rows if r.get("proof") and not r.get("gave_up") and not r.get("error")]
    vmap = verdicts[arm]
    proved = sum(1 for r in cands if vmap.get(r["id"]) == "proved")
    failed = sum(1 for r in cands if vmap.get(r["id"]) == "failed")
    unspl = sum(1 for r in cands if vmap.get(r["id"]) == "unspliceable")
    nover = sum(1 for r in cands if r["id"] not in vmap or vmap.get(r["id"]) == "env_broken")
    total_cost = sum((r.get("transcript_stats") or {}).get("cost_usd") or 0 for r in rows)
    mean_wall = sum(r.get("wall_s") or 0 for r in rows) / len(rows)
    check(f"sorrydb {arm} rows", len(rows), wants["rows"])
    check(f"sorrydb {arm} no-output", noout, wants["noout"])
    check(f"sorrydb {arm} gave-up", gaveup, wants["gaveup"])
    check(f"sorrydb {arm} candidates", len(cands), wants["cand"])
    check(f"sorrydb {arm} proved", proved, wants["proved"])
    check(f"sorrydb {arm} failed", failed, wants["failed"])
    check(f"sorrydb {arm} unspliceable", unspl, wants["unspl"])
    check(f"sorrydb {arm} no-verdict", nover, wants["nover"])
    check(f"sorrydb {arm} total cost", round(total_cost, 2), wants["cost"], tol=0.02)
    check(f"sorrydb {arm} cost/proved", round(total_cost / proved, 2), wants["cpp"], tol=0.02)
    check(f"sorrydb {arm} mean wall", round(mean_wall), wants["wall"], tol=1)
    sorry_stats[arm] = dict(rows=len(rows), no_output=noout, gave_up=gaveup,
                            candidates=len(cands), proved=proved, failed=failed,
                            unspliceable=unspl, no_verdict=nover,
                            total_cost=round(total_cost, 2),
                            cost_per_proved=round(total_cost / proved, 2),
                            proved_per_171=round(100.0 * proved / 171, 1),
                            mean_wall_s=round(mean_wall))

# ------------------------------------------------------------- output ------
fig = {
    "tier1": {"success": succ, "halluc": halluc, "runs_w_halluc": runs_w_halluc,
              "cost_folded": cost_folded, "wall_folded": wall_folded},
    "retrieval": {
        "qr_r10": {s: qr[s]["recall@10"] for s in qr},
        "qr_ndcg10": {s: qr[s]["ndcg@10"] for s in qr},
        "qr_by_style": {s: qr[s]["ndcg@10_by_style"] for s in qr},
        "mpr_gr10": {s: mpr[s]["group_recall@10"] for s in mpr},
        "anchors": {"qr_theoremgraph": 0.775, "qr_lsv2": 0.780,
                    "mpr_lsv2": 0.461, "mpr_diver": 0.380, "mpr_tg": 0.165},
    },
    "sorrydb": sorry_stats,
}
OUT.write_text(json.dumps(fig, indent=1))
print(f"\nwrote {OUT}")
if mismatches:
    print(f"\n*** {len(mismatches)} MISMATCHES ***")
    for m in mismatches:
        print(" -", m)
    sys.exit(1)
print("all checks passed")
