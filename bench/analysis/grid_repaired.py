#!/usr/bin/env python3
"""Bridge v2 report — the race-repaired retrieval grid, before/after.

194 rows of the original agent grid (F: 175/810 qr810 + 15/69 mpr; W: 2 + 2)
ran with NO tools attached (MCP cold-start race — init 'pending', 0 mcp
calls, de-facto arm N). They were archived verbatim under
bench/v2/runs/agent/race_condemned_archive/ and rerun with the fixed harness
(bench/v2/run_agent.py: condemnation + first-wave stagger + MCP_TIMEOUT;
the 4 W stragglers against the original local worker per run_campaign.sh).
N (no MCP by design), U and WF audit race-free before and after.

This script scores BOTH grids — "before" reconstructed by overlaying the
archived ranked lists onto the current rows at the manifest qids — and runs
the final contrast set on the repaired grid with the same clustered
methodology as retrieval_clustered.py (helpers imported, same SEED/B):

  WF − F, WF − U, U − F, F − W on MathlibQR fair-810 (declaration-clustered
  paired Wilcoxon + exact sign test + declaration-resampled cluster
  bootstrap) and on MathlibMPR (per-task paired, task bootstrap).

Outputs (this directory): grid_repaired.json, grid_repaired.md
Usage: python3 bench/analysis/grid_repaired.py
Deterministic: same SEED/B as retrieval_clustered.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
V2 = BENCH / "v2"
sys.path.insert(0, str(V2))
sys.path.insert(0, str(HERE))
import score_retrieval as sr                    # noqa: E402
from retrieval_clustered import (               # noqa: E402
    SEED, B, sign_test, wilcoxon_test, pct_ci, r4)

K = 10
ARMS = ["N", "F", "W", "U", "WF"]
PAIRS = [("WF", "F"), ("WF", "U"), ("U", "F"), ("F", "W")]
ARCH = V2 / "runs" / "agent" / "race_condemned_archive"


def load_grid(bench: str, before: bool) -> dict[str, dict[str, list[str]]]:
    ranked = {a: sr.load_ranked(V2 / "runs" / "agent" / bench / a)
              for a in ARMS}
    if before:
        man = json.loads((ARCH / "manifest.json").read_text())
        for key, qids in man.items():
            b, arm = key.split("/")
            if b != bench:
                continue
            arch = sr.load_ranked(ARCH / b / arm)
            for q in qids:
                ranked[arm][q] = arch.get(q, [])
    return ranked


def qr_tables(ranked) -> tuple[dict, dict, np.ndarray, dict]:
    """Per-arm row-level points + per-declaration hit/ndcg sums."""
    rows = sr.qr_rows()
    decls = sorted({r["gold"] for r in rows})
    d_idx = {d: i for i, d in enumerate(decls)}
    n_rows = np.zeros(len(decls))
    hit = {a: np.zeros(len(decls)) for a in ARMS}
    ndcg = {a: np.zeros(len(decls)) for a in ARMS}
    for r in rows:
        i = d_idx[r["gold"]]
        n_rows[i] += 1
        for a in ARMS:
            lst = ranked[a].get(r["qid"])
            assert lst is not None, f"qr missing {r['qid']} arm {a}"
            try:
                rank = lst[:K].index(r["gold"]) + 1
            except ValueError:
                rank = 0
            if rank:
                hit[a][i] += 1.0
                ndcg[a][i] += 1.0 / np.log2(rank + 1)
    points = {a: {"recall@10": r4(hit[a].sum() / n_rows.sum()),
                  "ndcg@10": r4(ndcg[a].sum() / n_rows.sum())} for a in ARMS}
    return points, hit, n_rows, ndcg


def mpr_tables(ranked) -> tuple[dict, dict]:
    rows = sr.mpr_rows()
    per_task = {a: [] for a in ARMS}
    for r in rows:
        for a in ARMS:
            lst = ranked[a].get(r["qid"])
            assert lst is not None, f"mpr missing {r['qid']} arm {a}"
            top = set(lst[:K])
            per_task[a].append(sum(1 for g in r["groups"] if top & set(g))
                               / len(r["groups"]))
    per_task = {a: np.array(v) for a, v in per_task.items()}
    points = {a: {"group_recall@10": r4(per_task[a].mean())} for a in ARMS}
    return points, per_task


def main() -> int:
    rng = np.random.default_rng(SEED)
    res: dict = {"seed": SEED, "B": B,
                 "manifest": json.loads((ARCH / "manifest.json").read_text())}

    # ---- before/after arm tables
    before_qr, _, _, _ = qr_tables(load_grid("qr810", before=True))
    before_mpr, _ = mpr_tables(load_grid("mpr", before=True))
    rk_qr = load_grid("qr810", before=False)
    rk_mpr = load_grid("mpr", before=False)
    after_qr, hit, n_rows, ndcg = qr_tables(rk_qr)
    after_mpr, per_task = mpr_tables(rk_mpr)
    res["arm_tables"] = {"before": {"qr810": before_qr, "mpr": before_mpr},
                         "after": {"qr810": after_qr, "mpr": after_mpr}}

    # ---- final contrasts on the repaired grid
    n_d = len(n_rows)
    hm = {a: hit[a] / n_rows for a in ARMS}
    nm = {a: ndcg[a] / n_rows for a in ARMS}
    idx = rng.integers(0, n_d, size=(B, n_d))
    tot = n_rows[idx].sum(axis=1)
    boot_hit = {a: hit[a][idx].sum(axis=1) / tot for a in ARMS}
    boot_ndcg = {a: ndcg[a][idx].sum(axis=1) / tot for a in ARMS}
    idx_m = rng.integers(0, len(per_task["N"]), size=(B, len(per_task["N"])))
    boot_mpr = {a: per_task[a][idx_m].mean(axis=1) for a in ARMS}

    contrasts: dict = {}
    for h, l in PAIRS:
        d_hit = boot_hit[h] - boot_hit[l]
        d_nd = boot_ndcg[h] - boot_ndcg[l]
        d_m = boot_mpr[h] - boot_mpr[l]
        contrasts[f"{h}_vs_{l}"] = {
            "qr810_recall@10": {
                "point": r4(after_qr[h]["recall@10"] - after_qr[l]["recall@10"]),
                "ci95": [r4(v) for v in pct_ci(d_hit)],
                "excludes_zero": bool(np.percentile(d_hit, 2.5) > 0
                                      or np.percentile(d_hit, 97.5) < 0),
                "wilcoxon": wilcoxon_test(hm[h], hm[l]),
                "sign_test": sign_test(hm[h], hm[l])},
            "qr810_ndcg@10": {
                "point": r4(after_qr[h]["ndcg@10"] - after_qr[l]["ndcg@10"]),
                "ci95": [r4(v) for v in pct_ci(d_nd)],
                "excludes_zero": bool(np.percentile(d_nd, 2.5) > 0
                                      or np.percentile(d_nd, 97.5) < 0),
                "wilcoxon": wilcoxon_test(nm[h], nm[l]),
                "sign_test": sign_test(nm[h], nm[l])},
            "mpr_group_recall@10": {
                "point": r4(float(per_task[h].mean() - per_task[l].mean())),
                "ci95": [r4(v) for v in pct_ci(d_m)],
                "excludes_zero": bool(np.percentile(d_m, 2.5) > 0
                                      or np.percentile(d_m, 97.5) < 0),
                "wilcoxon": wilcoxon_test(per_task[h], per_task[l]),
                "sign_test": sign_test(per_task[h], per_task[l])},
        }
    res["contrasts_repaired"] = contrasts
    res["methods"] = {
        "qr810": "171 declaration clusters; paired tests on per-declaration "
                 "means; declaration-resampled cluster bootstrap (row-pooled "
                 "ratio estimator)",
        "mpr": "69 PR tasks; paired tests on per-task group-recall@10; "
               "task-resampled bootstrap"}

    (HERE / "grid_repaired.json").write_text(json.dumps(res, indent=1) + "\n")

    # ---- markdown
    L: list[str] = []
    A = L.append
    A("# Bridge v2 — the race-repaired agent grid")
    A("")
    A("194 original rows (F: 175 qr810 + 15 mpr; W: 2 + 2) ran with no tools "
      "attached (MCP cold-start race, de-facto arm N); originals preserved in "
      "`bench/v2/runs/agent/race_condemned_archive/`, rows rerun with the "
      "fixed harness. N/U/WF audit race-free before and after. Reproduce: "
      f"`python3 bench/analysis/grid_repaired.py` (seed {SEED}, B={B:,}).")
    A("")
    A("## Arm tables, before → after")
    A("")
    A("| arm | qr810 R@10 | qr810 nDCG@10 | mpr gR@10 |")
    A("|---|---|---|---|")
    for a in ARMS:
        bq, aq = before_qr[a], after_qr[a]
        bm, am = before_mpr[a], after_mpr[a]
        def cell(b, x):
            return f"{b:.4f} → **{x:.4f}**" if abs(b - x) > 5e-5 else f"{x:.4f}"
        A(f"| {a} | {cell(bq['recall@10'], aq['recall@10'])} | "
          f"{cell(bq['ndcg@10'], aq['ndcg@10'])} | "
          f"{cell(bm['group_recall@10'], am['group_recall@10'])} |")
    A("")
    A("## Final contrasts (repaired grid)")
    A("")
    A("| contrast | metric | diff | 95% CI | excl. 0 | Wilcoxon p | "
      "sign (+/-) | sign p |")
    A("|---|---|---|---|---|---|---|---|")
    for key, c in contrasts.items():
        name = key.replace("_vs_", " − ")
        for mkey, mname in (("qr810_recall@10", "qr R@10"),
                            ("qr810_ndcg@10", "qr nDCG@10"),
                            ("mpr_group_recall@10", "mpr gR@10")):
            t = c[mkey]
            st = t["sign_test"]
            A(f"| {name} | {mname} | {t['point']:+.4f} | "
              f"[{t['ci95'][0]:+.4f}, {t['ci95'][1]:+.4f}] | "
              f"{'yes' if t['excludes_zero'] else 'no'} | "
              f"{t['wilcoxon']['p']:.2e} | {st['n_pos']}/{st['n_neg']} | "
              f"{st['p']:.2e} |")
    A("")
    (HERE / "grid_repaired.md").write_text("\n".join(L) + "\n")

    print(json.dumps({"after_qr810_R@10": {a: after_qr[a]["recall@10"]
                                           for a in ARMS},
                      "after_mpr_gR@10": {a: after_mpr[a]["group_recall@10"]
                                          for a in ARMS}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
