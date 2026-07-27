#!/usr/bin/env python3
"""Bridge v2 report — cluster-aware corrective reanalysis of the retrieval
benchmarks (MathlibQR fair-810, MathlibMPR) and SorryDB.

Why this exists: the naive analysis treats the 810 QR rows as independent,
but they are 171 declaration clusters (2-6 query styles per declaration).
This script redoes every inferential claim at the correct unit:

  (1) QR hit@10  — per-(declaration, arm) means over each declaration's rows,
      then across the 171 declarations: paired Wilcoxon signed-rank + exact
      sign test for F-vs-W, WF-vs-F, WF-vs-W; declaration-resampled cluster
      bootstrap (B=10000, fixed seed) percentile 95% CIs for each arm's R@10
      (row-pooled ratio estimator, so the point estimate matches the scorer)
      and for the paired pairwise differences.
  (2) Same cluster bootstrap for nDCG@10.
  (3) MPR group-recall@10 — 69 PR tasks, one task per PR (no clustering):
      Wilson 95% CIs (on the pooled group-level proportion; the per-task-mean
      metric additionally gets a task-resampled bootstrap CI) + paired exact
      sign tests WF-vs-F and WF-vs-W (Wilcoxon shown for reference).
  (4) SorryDB — per-repo x arm proved table (intention-to-treat over the 171
      frozen tasks); repo-clustered bootstrap CIs for each arm's proved rate
      and the WF-F difference; task-level exact McNemar as a non-clustered
      reference.

Reuses bench/v2/score_retrieval.py's qr_rows / mpr_rows / load_ranked / norm
so scoring is byte-identical to the headline scorer.

Outputs (same directory as this script):
  retrieval_clustered.json  — every number, machine-readable
  retrieval_clustered.md    — human-readable report

Usage: python3 bench/analysis/retrieval_clustered.py
Deterministic: SEED = 20260727, B = 10000.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import stats

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                             # bench
V2 = BENCH / "v2"
sys.path.insert(0, str(V2))
import score_retrieval as sr                    # noqa: E402  (reuse scorer)

SEED = 20260727
B = 10_000
K = 10
QR_ARMS = ["N", "F", "W", "WF"]
SDB_ARMS = ["N", "F", "WF"]
QR_PAIRS = [("F", "W"), ("WF", "F"), ("WF", "W")]


# ---------------------------------------------------------------- helpers

def wilson_ci(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score 95% interval for a binomial proportion."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def sign_test(x: np.ndarray, y: np.ndarray) -> dict:
    """Exact two-sided sign test on paired samples (zeros dropped)."""
    d = x - y
    n_pos = int((d > 0).sum())
    n_neg = int((d < 0).sum())
    n = n_pos + n_neg
    if n == 0:
        return {"n_pos": 0, "n_neg": 0, "n_nonzero": 0, "p": 1.0}
    p = stats.binomtest(min(n_pos, n_neg), n, 0.5, alternative="two-sided").pvalue
    return {"n_pos": n_pos, "n_neg": n_neg, "n_nonzero": n, "p": float(p)}


def wilcoxon_test(x: np.ndarray, y: np.ndarray) -> dict:
    """Paired Wilcoxon signed-rank (zeros dropped, scipy default 'wilcox')."""
    d = x - y
    nz = int((d != 0).sum())
    if nz == 0:
        return {"n_nonzero": 0, "W": None, "p": 1.0}
    res = stats.wilcoxon(x, y, zero_method="wilcox",
                         alternative="two-sided", method="auto")
    return {"n_nonzero": nz, "W": float(res.statistic), "p": float(res.pvalue)}


def pct_ci(a: np.ndarray) -> list[float]:
    lo, hi = np.percentile(a, [2.5, 97.5])
    return [float(lo), float(hi)]


def r4(x) -> float:
    return float(round(float(x), 4))


# ---------------------------------------------------------------- (1)+(2) QR

def analyze_qr(rng: np.random.Generator) -> dict:
    rows = sr.qr_rows()                                     # 810 fair rows
    ranked = {arm: sr.load_ranked(V2 / "runs" / "agent" / "qr810" / arm)
              for arm in QR_ARMS}

    decls = sorted({r["gold"] for r in rows})
    d_idx = {d: i for i, d in enumerate(decls)}
    n_d = len(decls)                                        # 171

    n_rows = np.zeros(n_d)
    hit_sum = {a: np.zeros(n_d) for a in QR_ARMS}
    ndcg_sum = {a: np.zeros(n_d) for a in QR_ARMS}
    n_missing = {a: 0 for a in QR_ARMS}

    for r in rows:
        i = d_idx[r["gold"]]
        n_rows[i] += 1
        for a in QR_ARMS:
            lst = ranked[a].get(r["qid"])
            if lst is None:                                 # guarded; 0 in data
                n_missing[a] += 1
                continue
            try:
                rank = lst[:K].index(r["gold"]) + 1
            except ValueError:
                rank = 0
            if rank:
                hit_sum[a][i] += 1.0
                ndcg_sum[a][i] += 1.0 / np.log2(rank + 1)
    assert all(v == 0 for v in n_missing.values()), f"missing rows: {n_missing}"
    assert int(n_rows.sum()) == len(rows)

    hit_mean = {a: hit_sum[a] / n_rows for a in QR_ARMS}    # per-decl means
    ndcg_mean = {a: ndcg_sum[a] / n_rows for a in QR_ARMS}

    out: dict = {
        "n_rows": len(rows), "n_declarations": n_d,
        "rows_per_declaration": {str(k): int(v) for k, v in
                                 zip(*np.unique(n_rows, return_counts=True))},
        "row_level": {a: {"recall@10": r4(hit_sum[a].sum() / n_rows.sum()),
                          "ndcg@10": r4(ndcg_sum[a].sum() / n_rows.sum())}
                      for a in QR_ARMS},
        "declaration_level_mean": {a: {"recall@10": r4(hit_mean[a].mean()),
                                       "ndcg@10": r4(ndcg_mean[a].mean())}
                                   for a in QR_ARMS},
    }

    # paired tests on per-declaration hit@10 means (171 pairs)
    tests = {}
    for hi_arm, lo_arm in QR_PAIRS:
        key = f"{hi_arm}_vs_{lo_arm}"
        tests[key] = {
            "metric": "per-declaration mean hit@10",
            "mean_diff": r4((hit_mean[hi_arm] - hit_mean[lo_arm]).mean()),
            "wilcoxon": wilcoxon_test(hit_mean[hi_arm], hit_mean[lo_arm]),
            "sign_test": sign_test(hit_mean[hi_arm], hit_mean[lo_arm]),
        }
    out["paired_tests_hit10"] = tests

    tests_nd = {}
    for hi_arm, lo_arm in QR_PAIRS:
        key = f"{hi_arm}_vs_{lo_arm}"
        tests_nd[key] = {
            "metric": "per-declaration mean nDCG@10",
            "mean_diff": r4((ndcg_mean[hi_arm] - ndcg_mean[lo_arm]).mean()),
            "wilcoxon": wilcoxon_test(ndcg_mean[hi_arm], ndcg_mean[lo_arm]),
            "sign_test": sign_test(ndcg_mean[hi_arm], ndcg_mean[lo_arm]),
        }
    out["paired_tests_ndcg10"] = tests_nd

    # declaration-resampled cluster bootstrap (row-pooled ratio estimator,
    # same declarations drawn for every arm => paired differences)
    idx = rng.integers(0, n_d, size=(B, n_d))
    tot = n_rows[idx].sum(axis=1)                           # (B,)
    boot = {}
    for metric, sums in (("recall@10", hit_sum), ("ndcg@10", ndcg_sum)):
        per_arm = {a: sums[a][idx].sum(axis=1) / tot for a in QR_ARMS}
        boot[metric] = {
            "arms": {a: {"point": out["row_level"][a][metric],
                         "ci95": [r4(v) for v in pct_ci(per_arm[a])]}
                     for a in QR_ARMS},
            "diffs": {f"{h}_minus_{l}": {
                "point": r4(out["row_level"][h][metric]
                            - out["row_level"][l][metric]),
                "ci95": [r4(v) for v in pct_ci(per_arm[h] - per_arm[l])],
                "excludes_zero": bool(np.percentile(per_arm[h] - per_arm[l], 2.5) > 0
                                      or np.percentile(per_arm[h] - per_arm[l], 97.5) < 0)}
                for h, l in QR_PAIRS},
        }
    out["cluster_bootstrap"] = {"B": B, "seed": SEED, "unit": "declaration",
                                **boot}
    return out


# ---------------------------------------------------------------- (3) MPR

def analyze_mpr(rng: np.random.Generator) -> dict:
    rows = sr.mpr_rows()                                    # 69 PR tasks
    ranked = {arm: sr.load_ranked(V2 / "runs" / "agent" / "mpr" / arm)
              for arm in QR_ARMS}

    per_task = {a: [] for a in QR_ARMS}                     # fraction per task
    pooled = {a: [0, 0] for a in QR_ARMS}                   # [covered, total]
    for r in rows:
        for a in QR_ARMS:
            lst = ranked[a].get(r["qid"])
            assert lst is not None, f"mpr missing {r['qid']} arm {a}"
            top = set(lst[:K])
            cov = sum(1 for g in r["groups"] if top & set(g))
            per_task[a].append(cov / len(r["groups"]))
            pooled[a][0] += cov
            pooled[a][1] += len(r["groups"])
    per_task = {a: np.array(v) for a, v in per_task.items()}
    n_tasks = len(rows)

    # task-resampled bootstrap for the per-task-mean metric (the scorer metric)
    idx = rng.integers(0, n_tasks, size=(B, n_tasks))
    boot_mean = {a: per_task[a][idx].mean(axis=1) for a in QR_ARMS}

    arms = {}
    for a in QR_ARMS:
        k, n = pooled[a]
        arms[a] = {
            "group_recall@10_per_task_mean": r4(per_task[a].mean()),
            "per_task_mean_boot_ci95": [r4(v) for v in pct_ci(boot_mean[a])],
            "pooled_groups_covered": k, "pooled_groups_total": n,
            "pooled_proportion": r4(k / n),
            "pooled_wilson_ci95": [r4(v) for v in wilson_ci(k, n)],
        }

    pairs = [("WF", "F"), ("WF", "W")]
    tests = {}
    for h, l in pairs:
        d = per_task[h] - per_task[l]
        tests[f"{h}_vs_{l}"] = {
            "metric": "per-task group-recall@10",
            "mean_diff": r4(d.mean()),
            "diff_boot_ci95": [r4(v) for v in pct_ci(boot_mean[h] - boot_mean[l])],
            "sign_test": sign_test(per_task[h], per_task[l]),
            "wilcoxon": wilcoxon_test(per_task[h], per_task[l]),
        }

    return {"n_tasks": n_tasks, "note": "one task per PR; no clustering",
            "arms": arms, "paired_tests": tests,
            "bootstrap": {"B": B, "seed": SEED, "unit": "task"}}


# ---------------------------------------------------------------- (4) SorryDB

def analyze_sorrydb(rng: np.random.Generator) -> dict:
    tasks = [json.loads(l) for l in
             (V2 / "data/sorrydb/tasks_frozen.jsonl").read_text().splitlines()
             if l.strip()]
    task_repo = {t["id"]: t["repo"] for t in tasks}
    repos = sorted({t["repo"] for t in tasks})
    n_frozen = len(tasks)                                   # 171

    verdicts: dict[tuple[str, str], str] = {}
    for l in (V2 / "runs/sorrydb/verify.jsonl").read_text().splitlines():
        if l.strip():
            v = json.loads(l)
            verdicts[(v["arm"], v["id"])] = v["verdict"]

    # run-row bookkeeping (ITT: any frozen task not proved counts as 0)
    run_ids = {}
    for a in SDB_ARMS:
        d = V2 / "runs" / "sorrydb" / a / "claude-sonnet-5"
        run_ids[a] = {json.loads(f.read_text())["id"] for f in d.glob("*.json")}

    proved = {a: {t["id"]: (verdicts.get((a, t["id"])) == "proved")
                  for t in tasks} for a in SDB_ARMS}

    # per-repo x arm table
    repo_tasks = defaultdict(list)
    for t in tasks:
        repo_tasks[t["repo"]].append(t["id"])
    table = {}
    for repo in repos:
        ids = repo_tasks[repo]
        table[repo] = {"n_tasks": len(ids),
                       **{a: int(sum(proved[a][i] for i in ids))
                          for a in SDB_ARMS}}
    totals = {a: int(sum(proved[a].values())) for a in SDB_ARMS}

    # repo-clustered bootstrap (resample the 10 repos with replacement)
    repo_n = np.array([len(repo_tasks[r]) for r in repos], dtype=float)
    repo_k = {a: np.array([table[r][a] for r in repos], dtype=float)
              for a in SDB_ARMS}
    idx = rng.integers(0, len(repos), size=(B, len(repos)))
    tot = repo_n[idx].sum(axis=1)
    rate = {a: repo_k[a][idx].sum(axis=1) / tot for a in SDB_ARMS}

    arms = {a: {"proved": totals[a], "n_frozen_tasks": n_frozen,
                "proved_rate": r4(totals[a] / n_frozen),
                "repo_boot_ci95": [r4(v) for v in pct_ci(rate[a])],
                "n_run_rows_on_frozen": len(run_ids[a] & set(task_repo)),
                "verify_verdict_counts": {}}
            for a in SDB_ARMS}
    from collections import Counter
    for a in SDB_ARMS:
        arms[a]["verify_verdict_counts"] = dict(Counter(
            verdicts[k] for k in verdicts if k[0] == a))

    d_wf_f = rate["WF"] - rate["F"]
    lo, hi = pct_ci(d_wf_f)
    # non-clustered reference: exact McNemar on the 171 paired tasks
    b = sum(1 for t in tasks if proved["WF"][t["id"]] and not proved["F"][t["id"]])
    c = sum(1 for t in tasks if proved["F"][t["id"]] and not proved["WF"][t["id"]])
    mcnemar_p = (stats.binomtest(min(b, c), b + c, 0.5).pvalue
                 if b + c else 1.0)

    return {
        "n_frozen_tasks": n_frozen, "n_repos": len(repos),
        "denominator": "intention-to-treat over the 171 frozen tasks "
                       "(gave-up / empty / unverified / missing rows = not proved)",
        "per_repo_table": table, "totals": totals, "arms": arms,
        "WF_minus_F": {"point": r4((totals["WF"] - totals["F"]) / n_frozen),
                       "repo_boot_ci95": [r4(lo), r4(hi)],
                       "excludes_zero": bool(lo > 0 or hi < 0),
                       "P_boot_diff_le_0": r4(float((d_wf_f <= 0).mean())),
                       "mcnemar_task_level": {"WF_only": b, "F_only": c,
                                              "exact_p": r4(mcnemar_p)}},
        "bootstrap": {"B": B, "seed": SEED, "unit": "repo"},
    }


# ---------------------------------------------------------------- report

def write_md(res: dict, path: Path) -> None:
    qr, mpr, sdb = res["qr810"], res["mpr"], res["sorrydb"]
    L = []
    A = L.append
    A("# Bridge v2 — cluster-aware retrieval reanalysis")
    A("")
    A(f"Reproduce: `python3 bench/analysis/retrieval_clustered.py` "
      f"(seed {SEED}, B={B:,} bootstrap resamples). Scoring reuses "
      f"`bench/v2/score_retrieval.py` verbatim (exact full-name match).")
    A("")
    A("## 1-2. MathlibQR fair-810 — declaration-clustered")
    A("")
    A(f"810 rows are **{qr['n_declarations']} declaration clusters** "
      "(2-6 query styles each); all inference below is at the declaration "
      "level. Row-level points match the headline scorer.")
    A("")
    A("| arm | R@10 (row) | R@10 95% CI (cluster boot) | nDCG@10 (row) | "
      "nDCG@10 95% CI |")
    A("|---|---|---|---|---|")
    cb = qr["cluster_bootstrap"]
    for a in QR_ARMS:
        r_ci = cb["recall@10"]["arms"][a]["ci95"]
        n_ci = cb["ndcg@10"]["arms"][a]["ci95"]
        A(f"| {a} | {qr['row_level'][a]['recall@10']:.4f} | "
          f"[{r_ci[0]:.4f}, {r_ci[1]:.4f}] | "
          f"{qr['row_level'][a]['ndcg@10']:.4f} | "
          f"[{n_ci[0]:.4f}, {n_ci[1]:.4f}] |")
    A("")
    A("### Pairwise differences (declaration-resampled, paired)")
    A("")
    A("| contrast | metric | diff | 95% CI | Wilcoxon p | sign test "
      "(+/-) | sign p |")
    A("|---|---|---|---|---|---|---|")
    for metric, tkey in (("R@10", "paired_tests_hit10"),
                         ("nDCG@10", "paired_tests_ndcg10")):
        mkey = "recall@10" if metric == "R@10" else "ndcg@10"
        for h, l in QR_PAIRS:
            t = qr[tkey][f"{h}_vs_{l}"]
            d = cb[mkey]["diffs"][f"{h}_minus_{l}"]
            st = t["sign_test"]
            A(f"| {h} − {l} | {metric} | {d['point']:+.4f} | "
              f"[{d['ci95'][0]:+.4f}, {d['ci95'][1]:+.4f}] | "
              f"{t['wilcoxon']['p']:.2e} | {st['n_pos']}/{st['n_neg']} | "
              f"{st['p']:.2e} |")
    A("")
    A("## 3. MathlibMPR — 69 PR tasks (no clustering)")
    A("")
    A("| arm | gR@10 (per-task mean) | boot 95% CI | pooled groups | "
      "Wilson 95% CI (pooled) |")
    A("|---|---|---|---|---|")
    for a in QR_ARMS:
        m = mpr["arms"][a]
        A(f"| {a} | {m['group_recall@10_per_task_mean']:.4f} | "
          f"[{m['per_task_mean_boot_ci95'][0]:.4f}, "
          f"{m['per_task_mean_boot_ci95'][1]:.4f}] | "
          f"{m['pooled_groups_covered']}/{m['pooled_groups_total']} | "
          f"[{m['pooled_wilson_ci95'][0]:.4f}, "
          f"{m['pooled_wilson_ci95'][1]:.4f}] |")
    A("")
    A("Wilson intervals apply to the pooled group-level proportion "
      "(groups within a PR treated as independent); the per-task-mean "
      "metric (the scorer's) carries the task-bootstrap CI.")
    A("")
    A("| contrast | mean diff | boot 95% CI | sign (+/-) | sign p | "
      "Wilcoxon p |")
    A("|---|---|---|---|---|---|")
    for key, t in mpr["paired_tests"].items():
        st = t["sign_test"]
        A(f"| {key.replace('_vs_', ' − ')} | {t['mean_diff']:+.4f} | "
          f"[{t['diff_boot_ci95'][0]:+.4f}, {t['diff_boot_ci95'][1]:+.4f}] | "
          f"{st['n_pos']}/{st['n_neg']} | {st['p']:.2e} | "
          f"{t['wilcoxon']['p']:.2e} |")
    A("")
    A("## 4. SorryDB — repo-clustered uncertainty")
    A("")
    A(f"Intention-to-treat over the {sdb['n_frozen_tasks']} frozen tasks in "
      f"{sdb['n_repos']} repos; a task counts as proved only with a "
      "verified `proved` verdict.")
    A("")
    A("| repo | n | N | F | WF |")
    A("|---|---|---|---|---|")
    for repo, row in sdb["per_repo_table"].items():
        A(f"| {repo} | {row['n_tasks']} | {row['N']} | {row['F']} | "
          f"{row['WF']} |")
    t = sdb["totals"]
    A(f"| **total** | **{sdb['n_frozen_tasks']}** | **{t['N']}** | "
      f"**{t['F']}** | **{t['WF']}** |")
    A("")
    A("| arm | proved | rate | repo-boot 95% CI |")
    A("|---|---|---|---|")
    for a in SDB_ARMS:
        m = sdb["arms"][a]
        A(f"| {a} | {m['proved']}/{m['n_frozen_tasks']} | "
          f"{m['proved_rate']:.4f} | [{m['repo_boot_ci95'][0]:.4f}, "
          f"{m['repo_boot_ci95'][1]:.4f}] |")
    A("")
    d = sdb["WF_minus_F"]
    mc = d["mcnemar_task_level"]
    A(f"**WF − F**: {d['point']:+.4f}, repo-clustered bootstrap 95% CI "
      f"[{d['repo_boot_ci95'][0]:+.4f}, {d['repo_boot_ci95'][1]:+.4f}] — "
      f"{'excludes' if d['excludes_zero'] else 'includes'} zero "
      f"(P(diff ≤ 0) = {d['P_boot_diff_le_0']:.3f}). Task-level exact "
      f"McNemar (non-clustered reference): {mc['WF_only']} WF-only vs "
      f"{mc['F_only']} F-only, p = {mc['exact_p']:.3f}.")
    A("")
    A("**Plain statement:** WF vs F on SorryDB is **not statistically "
      "distinguishable** at this sample size. The point difference is a "
      "single extra proof (10 vs 9 of 171); the repo-clustered bootstrap "
      "puts substantial mass at zero (the lower CI endpoint is exactly 0 "
      "because WF ≥ F in every repo, and "
      f"{d['P_boot_diff_le_0']:.0%} of resamples show no difference), and "
      "the task-level exact McNemar is p = "
      f"{mc['exact_p']:.2f}. Proofs are concentrated in 3 of 10 repos; "
      "even the N-vs-{F,WF} gap should be described cautiously with only "
      "10 repo clusters."
      if not d["excludes_zero"] else
      "**Plain statement:** the WF-F difference excludes zero under the "
      "repo-clustered bootstrap.")
    A("")
    path.write_text("\n".join(L) + "\n")


def main() -> int:
    rng = np.random.default_rng(SEED)
    res = {"seed": SEED, "B": B,
           "inputs": {
               "qr_runs": "bench/v2/runs/agent/qr810/{N,F,W,WF}",
               "mpr_runs": "bench/v2/runs/agent/mpr/{N,F,W,WF}",
               "sorrydb_runs": "bench/v2/runs/sorrydb/{N,F,WF} + verify.jsonl",
               "gold": "bench/v2/data/{MathlibQR,MathlibQR_shared171,"
                       "MathlibMPR}.json + data/sorrydb/tasks_frozen.jsonl"},
           "qr810": None, "mpr": None, "sorrydb": None}
    res["qr810"] = analyze_qr(rng)
    res["mpr"] = analyze_mpr(rng)
    res["sorrydb"] = analyze_sorrydb(rng)

    (HERE / "retrieval_clustered.json").write_text(
        json.dumps(res, indent=1) + "\n")
    write_md(res, HERE / "retrieval_clustered.md")
    print(json.dumps({
        "qr_recall@10": {a: res["qr810"]["row_level"][a]["recall@10"]
                         for a in QR_ARMS},
        "mpr_gr@10": {a: res["mpr"]["arms"][a]["group_recall@10_per_task_mean"]
                      for a in QR_ARMS},
        "sorrydb_proved": res["sorrydb"]["totals"],
        "sorrydb_WF_minus_F_ci": res["sorrydb"]["WF_minus_F"]["repo_boot_ci95"],
    }, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
