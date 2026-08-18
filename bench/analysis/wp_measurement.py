#!/usr/bin/env python3
"""WP measurement — brain_premises (tool #9) on MathlibMPR, one-shot.

HONESTY HEADER (read before citing any number):
  * ONE-SHOT, HELD-OUT: brain_premises was developed and tuned exclusively
    against LeanDojo Benchmark-4's premise split (BRIDGE-V2-BENCHMARKS.md
    discipline); MathlibMPR was never queried during development. The WP arm
    was run ONCE, post-deploy (2026-08-18), and scored as-is — no tuning
    against this benchmark, no reruns for a better number. Rows condemned by
    the MCP cold-start-race check (run_agent.py run_one) are harness
    failures, not model outcomes; any retried row is archived in
    bench/v2/runs/agent/race_condemned_archive/ with the same convention as
    the other arms.
  * POST-DEPLOY vs FROZEN ROWS: the N/F/W/WF/U comparison rows were produced
    earlier against the same pinned MPR task file
    (bench/v2/data/MathlibMPR.json, frenzymath/LeanSearch-v2@94f4888cbaf9,
    CC BY 4.0) and the same prompt/model/turn budget. W's allowedTools is
    PINNED to 8 explicit names (run_agent.py ARM-IDENTITY WARNING), so
    deploying brain_premises could not have changed W's toolset; WP's only
    delta vs W is the 9th explicit allowedTools entry. The Brain index the
    live server queries is the nightly build current at run time — the same
    live server the frozen W/WF/U rows hit at THEIR run times; index drift
    between those dates is uncontrolled and shared by every remote arm.
  * Scoring is byte-identical to bench/v2/score_retrieval.py (exact
    full-name match after norm(); group-Recall@10 = per-task mean fraction
    of gold premise GROUPS with >=1 member in the top 10).

Contrasts (per-task paired, 69 MPR tasks, no clustering — one task per PR):
  WP vs W  — does adding brain_premises close the Brain arm's gap?
  WP vs F  — does it reach formal-tools parity?
  WP vs U, WP vs WF — context vs the union arms.
Each contrast: exact two-sided sign test + paired Wilcoxon + task-resampled
bootstrap CI (B=10,000, seed 20260727) — the same machinery as
bench/analysis/retrieval_clustered.py (functions imported from it verbatim).

brain_premises usage (from the WP rows + full stream transcripts):
  * calls per run (tool_calls_by_name),
  * how often a brain_premises RESULT contained >=1 gold-group member,
  * provenance of every covered group's hit name — the chronological
    first-entry classification of retrieval_provenance.py (surfaced /
    guessed_verified / memory / in_query / written_unconfirmed), resolved
    pass (full untruncated streams), with surfaced-by-tool attribution —
    computed for WP and, as context, for W.

Usage: python3 bench/analysis/wp_measurement.py
Writes: bench/analysis/wp_measurement.json + .md
Deterministic: SEED = 20260727, B = 10000.
"""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                             # bench
V2 = BENCH / "v2"
sys.path.insert(0, str(V2))
sys.path.insert(0, str(HERE))
import score_retrieval as sr                    # noqa: E402  (exact scorer)
from retrieval_clustered import (               # noqa: E402  (same stats)
    sign_test, wilcoxon_test, wilson_ci, pct_ci, r4)
from retrieval_provenance import (              # noqa: E402  (same chronology)
    classify, events_from_stream, tool_fn, gold_patterns, found)

SEED = 20260727
B = 10_000
K = 10
ARMS = ["N", "F", "W", "WF", "U", "WP"]
PAIRS = [("WP", "W"), ("WP", "F"), ("WP", "U"), ("WP", "WF")]
MODEL = "claude-sonnet-5"
RUNS = V2 / "runs" / "agent" / "mpr"
PREMISES_TOOL = "brain_premises"


# ------------------------------------------------------------------- scoring

def per_task_scores() -> tuple[list[dict], dict[str, np.ndarray], dict]:
    rows = sr.mpr_rows()                                    # 69 PR tasks
    ranked = {a: sr.load_ranked(RUNS / a) for a in ARMS}
    per_task = {a: [] for a in ARMS}
    pooled = {a: [0, 0] for a in ARMS}
    covered_groups: dict[tuple[str, str], list[tuple[list[str], str | None]]] = {}
    for r in rows:
        for a in ARMS:
            lst = ranked[a].get(r["qid"])
            assert lst is not None, f"mpr missing {r['qid']} arm {a}"
            top = lst[:K]
            top_set = set(top)
            cov = []
            for g in r["groups"]:
                hit = next((n for n in top if n in set(g)), None)
                cov.append((g, hit))
            n_cov = sum(1 for _, h in cov if h)
            covered_groups[(a, r["qid"])] = cov
            per_task[a].append(n_cov / len(r["groups"]) if r["groups"] else 0.0)
            pooled[a][0] += n_cov
            pooled[a][1] += len(r["groups"])
    return rows, {a: np.array(v) for a, v in per_task.items()}, \
        {"pooled": pooled, "covered": covered_groups}


# ------------------------------------------------------- premises usage stats

def row_path(arm: str, qid: str) -> Path:
    return RUNS / arm / MODEL / f"{qid}.json"


def premises_usage(rows: list[dict]) -> dict:
    calls = []
    zero_call_runs = []
    total_cost = 0.0
    total_wall = 0.0
    n_result_calls = 0
    n_result_calls_with_gold = 0
    tasks_with_gold_in_premises_result = 0
    tool_totals: Counter = Counter()
    for r in rows:
        row = json.loads(row_path("WP", r["qid"]).read_text())
        tc = (row.get("transcript_stats") or {}).get("tool_calls_by_name") or {}
        tool_totals.update(tc)
        n = tc.get(f"mcp__wikibrain__{PREMISES_TOOL}", 0)
        calls.append(n)
        if n == 0:
            zero_call_runs.append(r["qid"])
        total_cost += (row.get("transcript_stats") or {}).get("cost_usd") or 0.0
        total_wall += row.get("wall_s") or 0.0
        # full-stream check: did any brain_premises RESULT contain a gold name?
        evs = events_from_stream(row_path("WP", r["qid"]).with_name(
            f"{r['qid']}.stream.jsonl.gz"))
        golds = sorted({m for g in r["groups"] for m in g})
        pats = {m: gold_patterns(m) for m in golds}
        task_hit = False
        for ev in evs or []:
            if tool_fn(ev.tool) != PREMISES_TOOL:
                continue
            n_result_calls += 1
            if any(found(pats[m], ev.res) for m in golds):
                n_result_calls_with_gold += 1
                task_hit = True
        if task_hit:
            tasks_with_gold_in_premises_result += 1
    arr = np.array(calls)
    return {
        "n_runs": len(rows),
        "calls_per_run": {"mean": r4(arr.mean()), "median": float(np.median(arr)),
                          "min": int(arr.min()), "max": int(arr.max()),
                          "total": int(arr.sum()),
                          "runs_with_>=1_call": int((arr > 0).sum()),
                          "runs_with_0_calls": zero_call_runs},
        "results_containing_gold": {
            "premises_calls_total": n_result_calls,
            "premises_calls_with_>=1_gold_member": n_result_calls_with_gold,
            "tasks_where_premises_result_contained_gold":
                tasks_with_gold_in_premises_result},
        "tool_calls_total_by_name": dict(tool_totals.most_common()),
        "cost": {"total_usd": r4(total_cost),
                 "mean_usd_per_task": r4(total_cost / len(rows)),
                 "mean_wall_s": r4(total_wall / len(rows))},
    }


def hit_provenance(arm: str, rows: list[dict],
                   covered: dict) -> dict:
    """Chronological first-entry class for every covered group's hit name
    (resolved pass: full stream transcripts), retrieval_provenance.classify."""
    cls_counts: Counter = Counter()
    surf_tool: Counter = Counter()
    n_hits = 0
    for r in rows:
        row = json.loads(row_path(arm, r["qid"]).read_text())
        query = row.get("query") or ""
        evs = events_from_stream(row_path(arm, r["qid"]).with_name(
            f"{r['qid']}.stream.jsonl.gz"))
        for g, hit in covered[(arm, r["qid"])]:
            if hit is None:
                continue
            n_hits += 1
            c = classify(hit, query, evs or [])
            cls_counts[c["cls"]] += 1
            if c["cls"] == "surfaced":
                surf_tool[c["tool"]] += 1
    return {"n_covered_group_hits": n_hits,
            "by_class": {k: {"n": v, "frac": r4(v / n_hits)}
                         for k, v in cls_counts.most_common()},
            "surfaced_by_tool": dict(surf_tool.most_common())}


# ----------------------------------------------------------------------- main

def main() -> int:
    rng = np.random.default_rng(SEED)
    rows, per_task, extra = per_task_scores()
    n_tasks = len(rows)

    idx = rng.integers(0, n_tasks, size=(B, n_tasks))
    boot_mean = {a: per_task[a][idx].mean(axis=1) for a in ARMS}

    arms = {}
    for a in ARMS:
        k, n = extra["pooled"][a]
        arms[a] = {
            "group_recall@10_per_task_mean": r4(per_task[a].mean()),
            "per_task_mean_boot_ci95": [r4(v) for v in pct_ci(boot_mean[a])],
            "pooled_groups_covered": k, "pooled_groups_total": n,
            "pooled_proportion": r4(k / n),
            "pooled_wilson_ci95": [r4(v) for v in wilson_ci(k, n)],
        }

    tests = {}
    for h, l in PAIRS:
        d = per_task[h] - per_task[l]
        bd = boot_mean[h] - boot_mean[l]
        lo, hi = np.percentile(bd, [2.5, 97.5])
        tests[f"{h}_vs_{l}"] = {
            "metric": "per-task group-recall@10",
            "mean_diff": r4(d.mean()),
            "diff_boot_ci95": [r4(lo), r4(hi)],
            "excludes_zero": bool(lo > 0 or hi < 0),
            "sign_test": sign_test(per_task[h], per_task[l]),
            "wilcoxon": wilcoxon_test(per_task[h], per_task[l]),
        }

    usage = premises_usage(rows)
    prov = {a: hit_provenance(a, rows, extra["covered"]) for a in ("WP", "W")}

    table = []
    for i, r in enumerate(rows):
        table.append({"qid": r["qid"], "n_groups": len(r["groups"]),
                      **{a: r4(per_task[a][i]) for a in ARMS},
                      "WP_minus_W": r4(per_task["WP"][i] - per_task["W"][i])})

    res = {
        "header": {
            "one_shot": "WP run once post-deploy (2026-08-18); MathlibMPR "
                        "held out from brain_premises development (LeanDojo "
                        "Benchmark-4 premise split only); no tuning, no "
                        "reruns for a better number",
            "gold": "bench/v2/data/MathlibMPR.json pinned "
                    "frenzymath/LeanSearch-v2@94f4888cbaf9 (CC BY 4.0)",
            "rows": "bench/v2/runs/agent/mpr/{N,F,W,WF,U,WP}/claude-sonnet-5",
            "scorer": "bench/v2/score_retrieval.py (exact full-name match)",
            "arm_identity": "W pinned to 8 explicit allowedTools; WP = W + "
                            "mcp__wikibrain__brain_premises (9th explicit "
                            "entry); same mcp-D remote server, prompt, model "
                            "claude-sonnet-5, turn budget; no manual",
        },
        "n_tasks": n_tasks,
        "arms": arms,
        "paired_tests": tests,
        "premises_usage": usage,
        "hit_provenance_resolved": prov,
        "bootstrap": {"B": B, "seed": SEED, "unit": "task"},
        "per_task": table,
    }
    (HERE / "wp_measurement.json").write_text(json.dumps(res, indent=1) + "\n")
    write_md(res, HERE / "wp_measurement.md")
    print(json.dumps({
        "gr@10": {a: arms[a]["group_recall@10_per_task_mean"] for a in ARMS},
        "tests": {k: {"mean_diff": v["mean_diff"], "sign_p": v["sign_test"]["p"],
                      "wilcoxon_p": v["wilcoxon"]["p"],
                      "ci": v["diff_boot_ci95"]} for k, v in tests.items()},
        "premises_calls_per_run_mean": usage["calls_per_run"]["mean"],
        "cost_total_usd": usage["cost"]["total_usd"],
    }, indent=1))
    return 0


def write_md(res: dict, path: Path) -> None:
    L = []
    A = L.append
    A("# WP measurement — brain_premises on MathlibMPR (one-shot)")
    A("")
    A("**Honesty header.** One-shot held-out evaluation, run once post-deploy "
      "(2026-08-18) and scored as-is — no tuning against MPR, no reruns for a "
      "better number (brain_premises was developed on LeanDojo Benchmark-4's "
      "premise split only). Gold pinned to "
      "frenzymath/LeanSearch-v2@94f4888cbaf9 (CC BY 4.0). Comparison arms are "
      "the frozen rows in `bench/v2/runs/agent/mpr/` (same prompt, model "
      "claude-sonnet-5, turn budget); W's toolset is pinned to 8 explicit "
      "names, WP adds only `mcp__wikibrain__brain_premises`. The live Brain "
      "index drifts nightly and run dates differ across arms — that drift is "
      "uncontrolled and shared by every remote arm. Scoring is byte-identical "
      "to `bench/v2/score_retrieval.py`.")
    A("")
    A(f"Reproduce: `python3 bench/analysis/wp_measurement.py` "
      f"(seed {res['bootstrap']['seed']}, B={res['bootstrap']['B']:,}).")
    A("")
    A("## Arms — group-Recall@10 over the 69 MPR tasks")
    A("")
    A("| arm | gR@10 (per-task mean) | boot 95% CI | pooled groups | "
      "Wilson 95% CI (pooled) |")
    A("|---|---|---|---|---|")
    for a in ARMS:
        m = res["arms"][a]
        A(f"| {a} | {m['group_recall@10_per_task_mean']:.4f} | "
          f"[{m['per_task_mean_boot_ci95'][0]:.4f}, "
          f"{m['per_task_mean_boot_ci95'][1]:.4f}] | "
          f"{m['pooled_groups_covered']}/{m['pooled_groups_total']} | "
          f"[{m['pooled_wilson_ci95'][0]:.4f}, "
          f"{m['pooled_wilson_ci95'][1]:.4f}] |")
    A("")
    A("## Paired contrasts (per-task, n=69)")
    A("")
    A("| contrast | mean diff | boot 95% CI | sign (+/−) | sign p | "
      "Wilcoxon p |")
    A("|---|---|---|---|---|---|")
    for key, t in res["paired_tests"].items():
        st = t["sign_test"]
        A(f"| {key.replace('_vs_', ' − ')} | {t['mean_diff']:+.4f} | "
          f"[{t['diff_boot_ci95'][0]:+.4f}, {t['diff_boot_ci95'][1]:+.4f}] | "
          f"{st['n_pos']}/{st['n_neg']} | {st['p']:.3g} | "
          f"{t['wilcoxon']['p']:.3g} |")
    A("")
    u = res["premises_usage"]
    c = u["calls_per_run"]
    g = u["results_containing_gold"]
    A("## brain_premises usage (WP arm)")
    A("")
    A(f"- calls per run: mean {c['mean']}, median {c['median']:.0f}, "
      f"range {c['min']}-{c['max']}, total {c['total']}; "
      f"{c['runs_with_>=1_call']}/{u['n_runs']} runs made >=1 call "
      f"(0-call runs: {c['runs_with_0_calls'] or 'none'})")
    A(f"- results containing gold: {g['premises_calls_with_>=1_gold_member']}"
      f"/{g['premises_calls_total']} brain_premises calls returned >=1 "
      f"gold-group member; {g['tasks_where_premises_result_contained_gold']}"
      f"/{u['n_runs']} tasks saw a gold member in a brain_premises result")
    A(f"- cost: ${u['cost']['total_usd']:.2f} total, "
      f"${u['cost']['mean_usd_per_task']:.3f}/task, mean wall "
      f"{u['cost']['mean_wall_s']:.0f}s")
    A(f"- all tool calls: `{json.dumps(u['tool_calls_total_by_name'])}`")
    A("")
    A("## Hit provenance (resolved pass, chronological first entry)")
    A("")
    A("For every covered gold group, the class of its top-ranked hit name "
      "(retrieval_provenance.py method, full stream transcripts):")
    A("")
    for a, p in res["hit_provenance_resolved"].items():
        A(f"### {a} — {p['n_covered_group_hits']} covered-group hits")
        A(f"- by class: `{json.dumps(p['by_class'])}`")
        A(f"- surfaced by tool: `{json.dumps(p['surfaced_by_tool'])}`")
        A("")
    A("## Per-task table")
    A("")
    A("| qid | groups | " + " | ".join(ARMS) + " | WP−W |")
    A("|---|---|" + "---|" * (len(ARMS) + 1))
    for t in res["per_task"]:
        A(f"| {t['qid']} | {t['n_groups']} | " +
          " | ".join(f"{t[a]:.3f}" for a in ARMS) +
          f" | {t['WP_minus_W']:+.3f} |")
    A("")
    path.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main())
