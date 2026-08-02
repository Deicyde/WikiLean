#!/usr/bin/env python3
"""Bridge v2 report — bare-union (U) retrieval ablation.

WF = W ∪ F union tools + the evidence-based agent manual (and the manual is
tuned on logged test-set traffic). U = the IDENTICAL union toolset with NO
manual (bench/v2/run_agent.py arm "U"). This script isolates:

  WF − U  — the manual's marginal effect on the same toolset;
  U − F, U − W — the union's marginal effect without the manual.

Methodology is identical to bench/analysis/retrieval_clustered.py (its
helpers are imported directly, same SEED/B):
  * MathlibQR fair-810 — 171 declaration clusters; paired Wilcoxon
    signed-rank + exact sign test on per-declaration means (hit@10 and
    nDCG@10), declaration-resampled cluster bootstrap (B=10000) percentile
    95% CIs on the row-pooled ratio estimator and paired differences.
  * MathlibMPR — 69 PR tasks (no clustering); per-task group-recall@10,
    task bootstrap + paired exact sign test (Wilcoxon for reference).
Scoring reuses bench/v2/score_retrieval.py verbatim (exact full-name match).

Also reports U's per-style QR metrics alongside N/F/W/WF, U's total cost
from transcript_stats, and any permanently-failed rows.

Outputs (this directory): union_ablation.json, union_ablation.md
Usage: python3 bench/analysis/union_ablation.py
Deterministic: same SEED/B as retrieval_clustered.py.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                             # bench
V2 = BENCH / "v2"
sys.path.insert(0, str(V2))
sys.path.insert(0, str(HERE))
import score_retrieval as sr                    # noqa: E402  (reuse scorer)
from retrieval_clustered import (               # noqa: E402  (same stats)
    SEED, B, sign_test, wilcoxon_test, pct_ci, r4)

K = 10
ARMS = ["N", "F", "W", "U", "WF"]
PAIRS = [("WF", "U"), ("U", "F"), ("U", "W")]


# ---------------------------------------------------------------- QR fair-810

def analyze_qr(rng: np.random.Generator) -> dict:
    rows = sr.qr_rows()                                     # 810 fair rows
    ranked = {arm: sr.load_ranked(V2 / "runs" / "agent" / "qr810" / arm)
              for arm in ARMS}

    decls = sorted({r["gold"] for r in rows})
    d_idx = {d: i for i, d in enumerate(decls)}
    n_d = len(decls)                                        # 171

    n_rows = np.zeros(n_d)
    hit_sum = {a: np.zeros(n_d) for a in ARMS}
    ndcg_sum = {a: np.zeros(n_d) for a in ARMS}
    by_style: dict[str, dict[str, list[float]]] = {
        a: defaultdict(list) for a in ARMS}                 # style -> ndcg list
    by_style_hit: dict[str, dict[str, list[float]]] = {
        a: defaultdict(list) for a in ARMS}
    n_missing = {a: 0 for a in ARMS}

    for r in rows:
        i = d_idx[r["gold"]]
        n_rows[i] += 1
        for a in ARMS:
            lst = ranked[a].get(r["qid"])
            if lst is None:
                n_missing[a] += 1
                continue
            try:
                rank = lst[:K].index(r["gold"]) + 1
            except ValueError:
                rank = 0
            nd = 1.0 / np.log2(rank + 1) if rank else 0.0
            if rank:
                hit_sum[a][i] += 1.0
                ndcg_sum[a][i] += nd
            by_style[a][r["style"]].append(nd)
            by_style_hit[a][r["style"]].append(1.0 if rank else 0.0)
    assert all(v == 0 for v in n_missing.values()), f"missing rows: {n_missing}"

    hit_mean = {a: hit_sum[a] / n_rows for a in ARMS}
    ndcg_mean = {a: ndcg_sum[a] / n_rows for a in ARMS}

    out: dict = {
        "n_rows": len(rows), "n_declarations": n_d,
        "row_level": {a: {"recall@10": r4(hit_sum[a].sum() / n_rows.sum()),
                          "ndcg@10": r4(ndcg_sum[a].sum() / n_rows.sum())}
                      for a in ARMS},
        "per_style": {a: {s: {"recall@10": r4(np.mean(by_style_hit[a][s])),
                              "ndcg@10": r4(np.mean(by_style[a][s]))}
                          for s in sorted(by_style[a])} for a in ARMS},
    }

    for tkey, means in (("paired_tests_hit10", hit_mean),
                        ("paired_tests_ndcg10", ndcg_mean)):
        tests = {}
        for h, l in PAIRS:
            tests[f"{h}_vs_{l}"] = {
                "mean_diff": r4((means[h] - means[l]).mean()),
                "wilcoxon": wilcoxon_test(means[h], means[l]),
                "sign_test": sign_test(means[h], means[l]),
            }
        out[tkey] = tests

    idx = rng.integers(0, n_d, size=(B, n_d))
    tot = n_rows[idx].sum(axis=1)
    boot = {}
    for metric, sums in (("recall@10", hit_sum), ("ndcg@10", ndcg_sum)):
        per_arm = {a: sums[a][idx].sum(axis=1) / tot for a in ARMS}
        boot[metric] = {
            "arms": {a: {"point": out["row_level"][a][metric],
                         "ci95": [r4(v) for v in pct_ci(per_arm[a])]}
                     for a in ARMS},
            "diffs": {f"{h}_minus_{l}": {
                "point": r4(out["row_level"][h][metric]
                            - out["row_level"][l][metric]),
                "ci95": [r4(v) for v in pct_ci(per_arm[h] - per_arm[l])],
                "excludes_zero": bool(
                    np.percentile(per_arm[h] - per_arm[l], 2.5) > 0
                    or np.percentile(per_arm[h] - per_arm[l], 97.5) < 0)}
                for h, l in PAIRS},
        }
    out["cluster_bootstrap"] = {"B": B, "seed": SEED, "unit": "declaration",
                                **boot}
    return out


# ---------------------------------------------------------------- MPR

def analyze_mpr(rng: np.random.Generator) -> dict:
    rows = sr.mpr_rows()                                    # 69 PR tasks
    ranked = {arm: sr.load_ranked(V2 / "runs" / "agent" / "mpr" / arm)
              for arm in ARMS}

    per_task = {a: [] for a in ARMS}
    for r in rows:
        for a in ARMS:
            lst = ranked[a].get(r["qid"])
            assert lst is not None, f"mpr missing {r['qid']} arm {a}"
            top = set(lst[:K])
            cov = sum(1 for g in r["groups"] if top & set(g))
            per_task[a].append(cov / len(r["groups"]))
    per_task = {a: np.array(v) for a, v in per_task.items()}
    n_tasks = len(rows)

    idx = rng.integers(0, n_tasks, size=(B, n_tasks))
    boot_mean = {a: per_task[a][idx].mean(axis=1) for a in ARMS}

    arms = {a: {"group_recall@10_per_task_mean": r4(per_task[a].mean()),
                "per_task_mean_boot_ci95": [r4(v) for v in pct_ci(boot_mean[a])]}
            for a in ARMS}

    tests = {}
    for h, l in PAIRS:
        d = per_task[h] - per_task[l]
        tests[f"{h}_vs_{l}"] = {
            "mean_diff": r4(d.mean()),
            "diff_boot_ci95": [r4(v) for v in pct_ci(boot_mean[h] - boot_mean[l])],
            "excludes_zero": bool(
                np.percentile(boot_mean[h] - boot_mean[l], 2.5) > 0
                or np.percentile(boot_mean[h] - boot_mean[l], 97.5) < 0),
            "sign_test": sign_test(per_task[h], per_task[l]),
            "wilcoxon": wilcoxon_test(per_task[h], per_task[l]),
        }

    return {"n_tasks": n_tasks, "note": "one task per PR; no clustering",
            "arms": arms, "paired_tests": tests,
            "bootstrap": {"B": B, "seed": SEED, "unit": "task"}}


# ------------------------------------------------- race-row sensitivity
# The original F/W grids predated run_agent.py's cold-start-race condemnation:
# 194 rows whose MCP servers never attached ran de-facto arm N (F 175/810 qr
# + 15/69 mpr, W 2/810 + 2/69), deflating F and W. Those rows have since been
# ARCHIVED (runs/agent/race_condemned_archive/) and RERUN with the fixed
# harness — see bench/analysis/grid_repaired.py for before/after. This
# sensitivity (each contrast recomputed only on rows where the comparison arm
# made >= 1 mcp tool call) is retained as a residual guard; on the repaired
# grid it should nearly coincide with the primary contrasts.

def mcp_calls_by_qid(bench: str, arm: str) -> dict[str, int]:
    d = V2 / "runs" / "agent" / bench / arm / "claude-sonnet-5"
    out = {}
    for f in d.glob("*.json"):
        r = json.loads(f.read_text())
        out[r["qid"]] = sum(
            v for k, v in (r["transcript_stats"]["tool_calls_by_name"] or {})
            .items() if k.startswith("mcp__"))
    return out


def sensitivity(rng: np.random.Generator) -> dict:
    out: dict = {"note": "contrasts restricted to rows where the comparison "
                         "arm had >=1 mcp tool call (race rows dropped)"}
    qr_all = sr.qr_rows()
    mpr_all = sr.mpr_rows()
    for hi, other in (("U", "F"), ("U", "W"), ("WF", "F"), ("WF", "W")):
        qr_calls = mcp_calls_by_qid("qr810", other)
        mpr_calls = mcp_calls_by_qid("mpr", other)
        ranked = {a: sr.load_ranked(V2 / "runs" / "agent" / "qr810" / a)
                  for a in (hi, other)}

        # QR: per-declaration paired means on kept rows
        rows = [r for r in qr_all if qr_calls.get(r["qid"], 0) > 0]
        decls = sorted({r["gold"] for r in rows})
        d_idx = {d: i for i, d in enumerate(decls)}
        n_rows = np.zeros(len(decls))
        hit = {a: np.zeros(len(decls)) for a in (hi, other)}
        for r in rows:
            i = d_idx[r["gold"]]
            n_rows[i] += 1
            for a in (hi, other):
                lst = ranked[a][r["qid"]]
                try:
                    rank = lst[:K].index(r["gold"]) + 1
                except ValueError:
                    rank = 0
                hit[a][i] += 1.0 if rank else 0.0
        hm = {a: hit[a] / n_rows for a in (hi, other)}
        idx = rng.integers(0, len(decls), size=(B, len(decls)))
        tot = n_rows[idx].sum(axis=1)
        boot_d = (hit[hi][idx].sum(axis=1) - hit[other][idx].sum(axis=1)) / tot
        out[f"qr810_{hi}_vs_{other}"] = {
            "rows_kept": len(rows), "rows_dropped": len(qr_all) - len(rows),
            "n_declarations": len(decls),
            f"recall@10_{hi}": r4(hit[hi].sum() / n_rows.sum()),
            f"recall@10_{other}": r4(hit[other].sum() / n_rows.sum()),
            "diff_point": r4((hit[hi].sum() - hit[other].sum()) / n_rows.sum()),
            "diff_ci95": [r4(v) for v in pct_ci(boot_d)],
            "sign_test": sign_test(hm[hi], hm[other]),
            "wilcoxon": wilcoxon_test(hm[hi], hm[other]),
        }

        # MPR: per-task paired means on kept tasks
        rankedm = {a: sr.load_ranked(V2 / "runs" / "agent" / "mpr" / a)
                   for a in (hi, other)}
        tasks = [r for r in mpr_all if mpr_calls.get(r["qid"], 0) > 0]
        pt = {a: [] for a in (hi, other)}
        for r in tasks:
            for a in (hi, other):
                top = set(rankedm[a][r["qid"]][:K])
                pt[a].append(sum(1 for g in r["groups"] if top & set(g))
                             / len(r["groups"]))
        pt = {a: np.array(v) for a, v in pt.items()}
        idx = rng.integers(0, len(tasks), size=(B, len(tasks)))
        boot_d = pt[hi][idx].mean(axis=1) - pt[other][idx].mean(axis=1)
        out[f"mpr_{hi}_vs_{other}"] = {
            "tasks_kept": len(tasks), "tasks_dropped": len(mpr_all) - len(tasks),
            f"gr@10_{hi}": r4(pt[hi].mean()), f"gr@10_{other}": r4(pt[other].mean()),
            "diff_point": r4(pt[hi].mean() - pt[other].mean()),
            "diff_ci95": [r4(v) for v in pct_ci(boot_d)],
            "sign_test": sign_test(pt[hi], pt[other]),
            "wilcoxon": wilcoxon_test(pt[hi], pt[other]),
        }
    return out


# ---------------------------------------------------------------- U run audit

def audit_u_runs() -> dict:
    out = {}
    for bench, n_expect in (("qr810", 810), ("mpr", 69)):
        d = V2 / "runs" / "agent" / bench / "U" / "claude-sonnet-5"
        rows = [json.loads(f.read_text()) for f in sorted(d.glob("*.json"))]
        errs = [{"qid": r["qid"], "error": r["error"]}
                for r in rows if r.get("error")]
        cost = sum((r["transcript_stats"].get("cost_usd") or 0) for r in rows)
        turns = [r["transcript_stats"].get("turns") or 0 for r in rows]
        tools = [sum((r["transcript_stats"].get("tool_calls_by_name") or {})
                     .values()) for r in rows]
        out[bench] = {
            "n_rows": len(rows), "n_expected": n_expect,
            "n_error": len(errs), "errors": errs,
            "total_cost_usd": r4(cost),
            "mean_turns": r4(np.mean(turns)) if rows else None,
            "mean_tool_calls": r4(np.mean(tools)) if rows else None,
        }
    out["total_cost_usd"] = r4(sum(out[b]["total_cost_usd"]
                                   for b in ("qr810", "mpr")))
    return out


# ---------------------------------------------------------------- report

def write_md(res: dict, path: Path) -> None:
    qr, mpr, audit = res["qr810"], res["mpr"], res["u_run_audit"]
    L: list[str] = []
    A = L.append
    A("# Bridge v2 — bare-union (U) ablation")
    A("")
    A("U = the identical W ∪ F union toolset as WF, **no manual**. "
      "WF − U isolates the (test-set-tuned) manual's marginal effect; "
      "U − F / U − W isolate the union's effect without the manual. "
      f"Reproduce: `python3 bench/analysis/union_ablation.py` (seed {SEED}, "
      f"B={B:,}); stats identical to `retrieval_clustered.py` (imported), "
      "scoring identical to `bench/v2/score_retrieval.py`.")
    A("")
    A("## MathlibQR fair-810 — declaration-clustered")
    A("")
    A("| arm | R@10 (row) | R@10 95% CI | nDCG@10 (row) | nDCG@10 95% CI |")
    A("|---|---|---|---|---|")
    cb = qr["cluster_bootstrap"]
    for a in ARMS:
        r_ci = cb["recall@10"]["arms"][a]["ci95"]
        n_ci = cb["ndcg@10"]["arms"][a]["ci95"]
        A(f"| {a} | {qr['row_level'][a]['recall@10']:.4f} | "
          f"[{r_ci[0]:.4f}, {r_ci[1]:.4f}] | "
          f"{qr['row_level'][a]['ndcg@10']:.4f} | "
          f"[{n_ci[0]:.4f}, {n_ci[1]:.4f}] |")
    A("")
    A("### Per-style nDCG@10 (R@10 in parens)")
    A("")
    styles = sorted(next(iter(qr["per_style"].values())))
    A("| arm | " + " | ".join(s.replace("_", " ") for s in styles) + " |")
    A("|---" * (len(styles) + 1) + "|")
    for a in ARMS:
        cells = [f"{qr['per_style'][a][s]['ndcg@10']:.3f} "
                 f"({qr['per_style'][a][s]['recall@10']:.3f})"
                 for s in styles]
        A(f"| {a} | " + " | ".join(cells) + " |")
    A("")
    A("### Decisive paired contrasts (declaration-resampled, paired)")
    A("")
    A("| contrast | metric | diff | 95% CI | excl. 0 | Wilcoxon p | "
      "sign (+/-) | sign p |")
    A("|---|---|---|---|---|---|---|---|")
    for metric, tkey in (("R@10", "paired_tests_hit10"),
                         ("nDCG@10", "paired_tests_ndcg10")):
        mkey = "recall@10" if metric == "R@10" else "ndcg@10"
        for h, l in PAIRS:
            t = qr[tkey][f"{h}_vs_{l}"]
            d = cb[mkey]["diffs"][f"{h}_minus_{l}"]
            st = t["sign_test"]
            A(f"| {h} − {l} | {metric} | {d['point']:+.4f} | "
              f"[{d['ci95'][0]:+.4f}, {d['ci95'][1]:+.4f}] | "
              f"{'yes' if d['excludes_zero'] else 'no'} | "
              f"{t['wilcoxon']['p']:.2e} | {st['n_pos']}/{st['n_neg']} | "
              f"{st['p']:.2e} |")
    A("")
    A("## MathlibMPR — 69 PR tasks")
    A("")
    A("| arm | gR@10 (per-task mean) | boot 95% CI |")
    A("|---|---|---|")
    for a in ARMS:
        m = mpr["arms"][a]
        A(f"| {a} | {m['group_recall@10_per_task_mean']:.4f} | "
          f"[{m['per_task_mean_boot_ci95'][0]:.4f}, "
          f"{m['per_task_mean_boot_ci95'][1]:.4f}] |")
    A("")
    A("| contrast | mean diff | boot 95% CI | excl. 0 | sign (+/-) | "
      "sign p | Wilcoxon p |")
    A("|---|---|---|---|---|---|---|")
    for key, t in mpr["paired_tests"].items():
        st = t["sign_test"]
        A(f"| {key.replace('_vs_', ' − ')} | {t['mean_diff']:+.4f} | "
          f"[{t['diff_boot_ci95'][0]:+.4f}, {t['diff_boot_ci95'][1]:+.4f}] | "
          f"{'yes' if t['excludes_zero'] else 'no'} | "
          f"{st['n_pos']}/{st['n_neg']} | {st['p']:.2e} | "
          f"{t['wilcoxon']['p']:.2e} |")
    A("")
    A("## Race-row sensitivity (contrasts on attach-clean rows)")
    A("")
    sen = res["race_row_sensitivity"]
    A(sen["note"] + ". The 194 cold-start-race rows that originally deflated "
      "F/W (F: 175 qr + 15 mpr; W: 2 + 2) have been archived and rerun "
      "(see `grid_repaired.py`); this section is a residual guard and "
      "should nearly coincide with the primary contrasts above.")
    A("")
    A("| contrast | bench | kept | hi | other | diff | 95% CI | sign p | "
      "Wilcoxon p |")
    A("|---|---|---|---|---|---|---|---|---|")
    for hi, other in (("U", "F"), ("U", "W"), ("WF", "F"), ("WF", "W")):
        q = sen[f"qr810_{hi}_vs_{other}"]
        A(f"| {hi} − {other} | qr810 R@10 | {q['rows_kept']} rows "
          f"(−{q['rows_dropped']}) | {q[f'recall@10_{hi}']:.4f} | "
          f"{q[f'recall@10_{other}']:.4f} | {q['diff_point']:+.4f} | "
          f"[{q['diff_ci95'][0]:+.4f}, {q['diff_ci95'][1]:+.4f}] | "
          f"{q['sign_test']['p']:.2e} | {q['wilcoxon']['p']:.2e} |")
        m = sen[f"mpr_{hi}_vs_{other}"]
        A(f"| {hi} − {other} | mpr gR@10 | {m['tasks_kept']} tasks "
          f"(−{m['tasks_dropped']}) | {m[f'gr@10_{hi}']:.4f} | "
          f"{m[f'gr@10_{other}']:.4f} | {m['diff_point']:+.4f} | "
          f"[{m['diff_ci95'][0]:+.4f}, {m['diff_ci95'][1]:+.4f}] | "
          f"{m['sign_test']['p']:.2e} | {m['wilcoxon']['p']:.2e} |")
    A("")
    A("## U run audit")
    A("")
    for b in ("qr810", "mpr"):
        a_ = audit[b]
        A(f"- **{b}**: {a_['n_rows']}/{a_['n_expected']} rows, "
          f"{a_['n_error']} errors, cost ${a_['total_cost_usd']:.2f}, "
          f"mean turns {a_['mean_turns']}, mean tool calls "
          f"{a_['mean_tool_calls']}")
    A(f"- **total U cost**: ${audit['total_cost_usd']:.2f}")
    A("")
    path.write_text("\n".join(L) + "\n")


def main() -> int:
    rng = np.random.default_rng(SEED)
    res = {"seed": SEED, "B": B,
           "inputs": {"runs": "bench/v2/runs/agent/{qr810,mpr}/{N,F,W,U,WF}",
                      "gold": "bench/v2/data/{MathlibQR,MathlibQR_shared171,"
                              "MathlibMPR}.json"},
           "qr810": analyze_qr(rng), "mpr": analyze_mpr(rng),
           "race_row_sensitivity": sensitivity(rng),
           "u_run_audit": audit_u_runs()}
    (HERE / "union_ablation.json").write_text(json.dumps(res, indent=1) + "\n")
    write_md(res, HERE / "union_ablation.md")
    print(json.dumps({
        "qr_recall@10": {a: res["qr810"]["row_level"][a]["recall@10"]
                         for a in ARMS},
        "qr_ndcg@10": {a: res["qr810"]["row_level"][a]["ndcg@10"]
                       for a in ARMS},
        "mpr_gr@10": {a: res["mpr"]["arms"][a]["group_recall@10_per_task_mean"]
                      for a in ARMS},
        "u_total_cost_usd": res["u_run_audit"]["total_cost_usd"],
    }, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
