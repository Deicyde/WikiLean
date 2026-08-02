#!/usr/bin/env python3
"""Bridge v2 revision — REVIEW-2 items §1 + §2: fresh-set cluster structure and
ONE coherent paired inference for the post-repair full-100 D-vs-E contrast.

REVIEW-2.md (docs/research/review/REVIEW-2.md) makes two demands:

  §1  The full-100 repaired result must carry a single coherent paired
      inferential method — "preferably a cluster-aware paired bootstrap for the
      risk difference, with its corresponding interval and p-value" — replacing
      the mismatched pair (paired-Wald CI barely excluding 0 vs exact McNemar
      p=.073) that the v2 draft interprets side by side.
  §2  The 100 fresh theorems are not independent: "they come from roughly 44
      commits and contain conspicuous sibling families". Required: commit- or
      file-clustered paired bootstrap; a family-collapsed sensitivity analysis;
      counts of distinct commits, files, and families.

What this script does (all deterministic, SEED below):

  (1) Cluster census over bench/data/fresh_tasks.jsonl: distinct source
      commits (`added_in.commit`), distinct source files (`module`), and
      name-stem sibling families (deterministic rule: within one module,
      connected components under "shared leading name token OR token-set
      Jaccard >= JACCARD_T"). Verifies the reviewer's "roughly 44 commits"
      claim and the three named families (AntitoneOn integral results,
      bounded-variation variants, monotonicity variants).
  (2) THE HEADLINE INFERENCE: commit-clustered paired bootstrap (B=10,000)
      for the D-vs-E risk difference on the post-repair full-100 outcomes
      (bench/analysis/bridge_summary_v2.json paired_matrix, i.e. the same
      per-task success bits behind the v2 draft's 42% vs 30%): RD, percentile
      95% CI, and a two-sided percentile-inversion bootstrap p — interval and
      p come from the SAME resampling distribution, so they cannot disagree
      the way the Wald CI and exact McNemar did.
  (3) The old numbers (paired-Wald CI, exact McNemar) recomputed for the
      supplement, with the one-sentence mismatch explanation.
  (4) The same clustered treatment for D-vs-C and D-vs-A, plus robustness
      rows: file-clustered, family-clustered, and unclustered bootstraps.
  (5) Family-collapsed sensitivity (one unit per family; majority-rule and
      any-success-rule collapses, plus a tie-free mean collapse): McNemar +
      RD per pair, and the same at file and commit granularity (strictly
      coarser collapses that bound any reasonable family definition).

Outcome provenance (unchanged from the v2 draft — this script only redoes the
INFERENCE): success = produced ∧ no error ∧ zero hallucinated citations ∧
typecheck ok, per-task bits from bench/analysis/bridge_summary_v2.json
paired_matrix (written by bench/analysis/score_e31_v2.py, which graded
bench/data/runs/{A..E}/fresh_*.json — E rows fresh_069..099 are the 2026-07-27
repair — with bench/score_bridge.py against the fresh-pinned toolchain).
Cluster labels: bench/data/fresh_tasks.jsonl `added_in.commit` / `module`.
Cross-checked against bench/analysis/part1_fresh100_v2.json (asserted equal).

Run:  python3 bench/analysis/fresh_clustered.py
Outputs: fresh_clustered.json, fresh_clustered.md (this directory).
Deterministic: SEED = 20260801, B = 10_000, fixed job order + per-job seeds.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from math import comb, sqrt
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent            # bench/analysis
BENCH = HERE.parent                               # bench/

TASKS_FILE = BENCH / "data" / "fresh_tasks.jsonl"
SUMMARY_V2 = HERE / "bridge_summary_v2.json"
PART1_V2 = HERE / "part1_fresh100_v2.json"        # cross-check only
OUT_JSON = HERE / "fresh_clustered.json"
OUT_MD = HERE / "fresh_clustered.md"

SEED = 20260801          # base seed; per-job seeds = SEED + fixed offset below
B = 10_000
Z = 1.959963984540054
ARMS = ["A", "B", "C", "D", "E"]
PAIRS = [("D", "E"), ("D", "C"), ("D", "A")]
JACCARD_T = 0.40         # sibling threshold on name-token Jaccard (same module)

# Fixed per-job seed offsets: (pair, cluster-level) -> offset. Frozen so that
# adding/reordering analyses can never silently change published numbers.
JOB_SEEDS = {
    ("D", "E", "commit"): 1,  ("D", "E", "module"): 2,
    ("D", "E", "family"): 3,  ("D", "E", "task"): 4,
    ("D", "C", "commit"): 11, ("D", "C", "module"): 12,
    ("D", "C", "family"): 13, ("D", "C", "task"): 14,
    ("D", "A", "commit"): 21, ("D", "A", "module"): 22,
    ("D", "A", "family"): 23, ("D", "A", "task"): 24,
    ("D", "E", "family_mean"): 31, ("D", "C", "family_mean"): 32,
    ("D", "A", "family_mean"): 33,
}


# --------------------------------------------------------------------------- #
# Statistics — mcnemar_exact / paired_rd_wald copied VERBATIM from
# bench/analysis/tier1_reanalysis.py (the functions that produced the v2-draft
# numbers) so the supplement's "old numbers" are byte-identical; asserted
# against part1_fresh100_v2.json below.
# --------------------------------------------------------------------------- #
def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar p: binomial(b+c, 1/2) doubled min-tail, capped."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired_rd_wald(b: int, c: int, n: int, z: float = Z) -> dict:
    """Absolute risk difference p_x - p_y for PAIRED binary data with the
    paired-Wald (matched-pairs) CI: diff=(b-c)/n, SE=sqrt(b+c-(b-c)^2/n)/n."""
    diff = (b - c) / n
    se = sqrt(max(0.0, (b + c) - (b - c) ** 2 / n)) / n
    return {"rd": diff, "se": se,
            "ci95": [diff - z * se, diff + z * se],
            "method": "paired Wald (matched pairs)"}


def wilson_ci(k: int, n: int, z: float = Z) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def cluster_boot_rd(diffs_by_cluster: list[np.ndarray], seed: int,
                    b: int = B) -> dict:
    """Cluster-resampled paired bootstrap for a risk difference.

    Point estimate: RD = (sum of per-task paired differences) / (n tasks) —
    the pooled ratio estimator, identical to (b - c)/n and to the 42%-30%=12pp
    arithmetic in the draft. Each replicate resamples G clusters with
    replacement and recomputes the SAME ratio over the resampled tasks, so
    within-cluster correlation propagates into the interval.

    p-value: two-sided percentile inversion with add-one correction,
    p = 2*min(P*(RD* <= 0), P*(RD* >= 0)) — the smallest alpha at which the
    two-sided percentile CI excludes 0. Interval and p share one distribution.
    """
    g = len(diffs_by_cluster)
    sums = np.array([a.sum() for a in diffs_by_cluster], dtype=float)
    ns = np.array([len(a) for a in diffs_by_cluster], dtype=float)
    n_tot = float(ns.sum())
    rd = float(sums.sum() / n_tot)

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, g, size=(b, g))
    reps = sums[idx].sum(axis=1) / ns[idx].sum(axis=1)

    lo_tail = (int((reps <= 0).sum()) + 1) / (b + 1)
    hi_tail = (int((reps >= 0).sum()) + 1) / (b + 1)
    p_boot = min(1.0, 2 * min(lo_tail, hi_tail))

    # Analytic cluster-robust SE (supplement/reference only): sandwich for the
    # ratio-mean with G/(G-1) small-sample factor.
    resid = sums - ns * rd
    se_cr = sqrt(g / (g - 1) * float((resid ** 2).sum())) / n_tot

    return {
        "n_clusters": g, "n_tasks": int(n_tot), "B": b, "seed": seed,
        "rd": round(rd, 6),
        "ci95_percentile": [round(float(np.percentile(reps, 2.5)), 6),
                            round(float(np.percentile(reps, 97.5)), 6)],
        "p_two_sided_percentile_inversion": round(p_boot, 6),
        "boot_se": round(float(reps.std(ddof=1)), 6),
        "cluster_robust_se_analytic": round(se_cr, 6),
    }


# --------------------------------------------------------------------------- #
# Family detection (deterministic name-stem rule)
# --------------------------------------------------------------------------- #
def name_tokens(decl: str) -> list[str]:
    """Lowercased tokens of a decl name: split on '.' and '_', drop '_root_',
    strip trailing primes. First token = the leading name stem."""
    toks: list[str] = []
    for part in decl.split("."):
        if part == "_root_":
            continue
        for t in part.split("_"):
            t = t.rstrip("'").lower()
            if t:
                toks.append(t)
    return toks


def detect_families(tasks: list[dict]) -> dict[str, list[str]]:
    """Sibling families: within one module, connected components under
    'shared leading token OR token-set Jaccard >= JACCARD_T'. Returns
    family_id -> [task ids] (family_id = first member's task id)."""
    by_module: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        by_module[t["module"]].append(t)

    parent: dict[str, str] = {t["id"]: t["id"] for t in tasks}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for mod_tasks in by_module.values():
        for i, a in enumerate(mod_tasks):
            ta = set(name_tokens(a["decl_name"]))
            la = name_tokens(a["decl_name"])[0]
            for b_ in mod_tasks[i + 1:]:
                tb = set(name_tokens(b_["decl_name"]))
                lb = name_tokens(b_["decl_name"])[0]
                jac = len(ta & tb) / len(ta | tb)
                if la == lb or jac >= JACCARD_T:
                    union(a["id"], b_["id"])

    fams: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        fams[find(t["id"])].append(t["id"])
    return {k: sorted(v) for k, v in sorted(fams.items())}


# --------------------------------------------------------------------------- #
# Collapsed-unit paired analysis
# --------------------------------------------------------------------------- #
def collapse_units(units: dict[str, list[str]], succ: dict[str, dict[str, bool]],
                   arm: str, rule: str) -> tuple[dict[str, bool], int]:
    """Collapse each unit (family/file/commit) to one binary outcome.
    rule='any': success iff any member task succeeded.
    rule='majority': success iff STRICTLY more than half succeeded
    (exact ties -> failure; tie count returned for transparency)."""
    out: dict[str, bool] = {}
    ties = 0
    for uid, members in units.items():
        k = sum(1 for m in members if succ[m][arm])
        n = len(members)
        if rule == "any":
            out[uid] = k > 0
        elif rule == "majority":
            if n > 1 and 2 * k == n:
                ties += 1
            out[uid] = 2 * k > n
        else:
            raise ValueError(rule)
    return out, ties


def paired_binary(x: dict[str, bool], y: dict[str, bool]) -> dict:
    ids = sorted(x)
    b = sum(1 for i in ids if x[i] and not y[i])
    c = sum(1 for i in ids if y[i] and not x[i])
    both = sum(1 for i in ids if x[i] and y[i])
    n = len(ids)
    wald = paired_rd_wald(b, c, n)
    return {"n_units": n, "x_only": b, "y_only": c, "both": both,
            "neither": n - b - c - both, "discordant": b + c,
            "p_mcnemar_exact": round(mcnemar_exact(b, c), 6),
            "rd": round(wald["rd"], 6),
            "rd_ci95_paired_wald": [round(v, 6) for v in wald["ci95"]]}


# --------------------------------------------------------------------------- #
def main() -> int:
    tasks = [json.loads(l) for l in TASKS_FILE.read_text().splitlines() if l]
    assert len(tasks) == 100, len(tasks)
    v2 = json.loads(SUMMARY_V2.read_text())
    pm = v2["paired_matrix"]
    succ = {t["id"]: {a: bool(pm[t["id"]][a]) for a in ARMS} for t in tasks}
    assert len(succ) == 100 and all(len(v) == 5 for v in succ.values())

    # ---- cross-check outcomes against part1_fresh100_v2.json ----------------
    part1 = json.loads(PART1_V2.read_text())
    for a in ARMS:
        k = sum(1 for t in succ.values() if t[a])
        assert k == part1["fresh_100_table_v2"][a]["k"], (a, k)
    de = part1["mcnemar_fresh_100_v2"]["D_vs_E"]
    b_de = sum(1 for t in succ.values() if t["D"] and not t["E"])
    c_de = sum(1 for t in succ.values() if t["E"] and not t["D"])
    assert (b_de, c_de) == (de["D_only"], de["E_only"]), (b_de, c_de)
    print(f"outcome cross-check OK: per-arm k and D/E discordants match "
          f"part1_fresh100_v2.json (b={b_de}, c={c_de})")

    # ---- (1) cluster census -------------------------------------------------
    commit_of = {t["id"]: t["added_in"]["commit"] for t in tasks}
    module_of = {t["id"]: t["module"] for t in tasks}
    commits = Counter(commit_of.values())
    modules = Counter(module_of.values())
    # hierarchy check: each module belongs to exactly one commit
    mod_commits = defaultdict(set)
    for t in tasks:
        mod_commits[t["module"]].add(t["added_in"]["commit"])
    nested = all(len(v) == 1 for v in mod_commits.values())

    families = detect_families(tasks)
    fam_of = {tid: fid for fid, mem in families.items() for tid in mem}
    fam_nested = all(len({commit_of[m] for m in mem}) == 1
                     for mem in families.values())
    multi_fams = {f: m for f, m in families.items() if len(m) > 1}
    id2decl = {t["id"]: (t["module"], t["decl_name"]) for t in tasks}

    def sizes_hist(counter_vals) -> dict[str, int]:
        return {str(s): c for s, c in sorted(Counter(counter_vals).items())}

    census = {
        "n_tasks": 100,
        "n_distinct_commits": len(commits),
        "reviewer_claim_roughly_44_commits": {
            "claim": "roughly 44", "true_value": len(commits),
            "verdict": "exactly correct" if len(commits) == 44 else "off"},
        "n_distinct_files_modules": len(modules),
        "n_families": len(families),
        "n_multi_member_families": len(multi_fams),
        "n_singleton_families": len(families) - len(multi_fams),
        "family_rule": ("within one module: connected components under "
                        f"(shared leading name token) OR (name-token Jaccard >= {JACCARD_T}); "
                        "tokens = decl name split on '.'/'_', '_root_' dropped, "
                        "primes stripped, lowercased"),
        "hierarchy": {"module_nested_in_commit": nested,
                      "family_nested_in_commit": fam_nested},
        "commit_size_hist": sizes_hist(commits.values()),
        "module_size_hist": sizes_hist(modules.values()),
        "family_size_hist": sizes_hist(len(m) for m in families.values()),
        "largest_commits": [{"commit": c[:12], "n": n}
                            for c, n in commits.most_common(5)],
        "multi_member_families": [
            {"family": fid, "n": len(mem), "module": id2decl[mem[0]][0],
             "commit": commit_of[mem[0]][:12],
             "members": [{"id": m, "decl": id2decl[m][1]} for m in mem]}
            for fid, mem in sorted(multi_fams.items(),
                                   key=lambda kv: -len(kv[1]))],
    }

    # reviewer's three named sibling examples, resolved against our rule
    fam_sizes = {fid: len(m) for fid, m in families.items()}
    reviewer_examples = {
        "AntitoneOn_integral_results": {
            "task_ids": [t["id"] for t in tasks
                         if t["decl_name"].startswith("AntitoneOn.")],
            "one_family_under_rule": len({fam_of[t["id"]] for t in tasks
                                          if t["decl_name"].startswith("AntitoneOn.")}) == 1},
        "bounded_variation_variants": {
            "task_ids": [t["id"] for t in tasks
                         if "BoundedVariation" in t["module"]],
            "n_families_under_rule": len({fam_of[t["id"]] for t in tasks
                                          if "BoundedVariation" in t["module"]}),
            "note": ("one file (Topology.EMetricSpace.BoundedVariation, "
                     "commit 49ed1b2d, 8 tasks); the name-stem rule splits it "
                     "into eVariationOn.* vs two loners, but file- and "
                     "commit-level collapses (reported below) treat all 8 as "
                     "one unit; fresh_095 (VariationOnFromTo) is a different "
                     "file AND commit, so it stays separate at every level")},
        "monotonicity_const_smul_variants": {
            "task_ids": [t["id"] for t in tasks
                         if t["decl_name"].endswith(".const_smul")],
            "one_family_under_rule": len({fam_of[t["id"]] for t in tasks
                                          if t["decl_name"].endswith(".const_smul")}) == 1},
    }
    census["reviewer_named_examples"] = reviewer_examples

    # ---- cluster groupings for the bootstraps -------------------------------
    def diffs_by(group_of: dict[str, str], x: str, y: str) -> list[np.ndarray]:
        by: dict[str, list[int]] = defaultdict(list)
        for tid in sorted(succ):
            by[group_of[tid]].append(int(succ[tid][x]) - int(succ[tid][y]))
        return [np.array(by[g], dtype=float) for g in sorted(by)]

    task_of = {tid: tid for tid in succ}          # unclustered = 100 clusters
    LEVELS = {"commit": commit_of, "module": module_of,
              "family": fam_of, "task": task_of}

    # ---- (2)+(4) clustered paired bootstraps --------------------------------
    boots: dict[str, dict[str, dict]] = defaultdict(dict)
    for x, y in PAIRS:
        for level, gof in LEVELS.items():
            boots[f"{x}_vs_{y}"][level] = cluster_boot_rd(
                diffs_by(gof, x, y), seed=SEED + JOB_SEEDS[(x, y, level)])

    headline = boots["D_vs_E"]["commit"]

    # Effect concentration: net paired difference contributed by each commit
    # cluster (explains WHY clustering widens the D-E interval).
    concentration: dict[str, dict] = {}
    for x, y in PAIRS:
        net_by_commit = Counter()
        for tid in succ:
            net_by_commit[commit_of[tid][:12]] += (int(succ[tid][x])
                                                   - int(succ[tid][y]))
        total = sum(net_by_commit.values())
        top = [{"commit": c, "net_paired_diff": n,
                "n_tasks": commits[next(k for k in commits if k.startswith(c))],
                "module_example": next(module_of[t] for t in succ
                                       if commit_of[t].startswith(c))}
               for c, n in net_by_commit.most_common(3)]
        concentration[f"{x}_vs_{y}"] = {
            "total_net_paired_diff": total,
            "top3_commit_contributions": top,
            "top2_share_of_net": (round(sum(t["net_paired_diff"]
                                            for t in top[:2]) / total, 4)
                                  if total else None),
        }

    # ---- (3) the old (mismatched) numbers, for the supplement ---------------
    old_wald = paired_rd_wald(b_de, c_de, 100)
    old = {
        "paired_wald_rd_ci95": [round(v, 6) for v in old_wald["ci95"]],
        "paired_wald_rd": round(old_wald["rd"], 6),
        "p_mcnemar_exact": round(mcnemar_exact(b_de, c_de), 6),
        "mismatch_explanation_one_sentence": (
            "The paired-Wald interval is a normal approximation over all 100 "
            "paired differences while exact McNemar conditions on only the 38 "
            "discordant pairs, and near the significance boundary these two "
            "approximations can land on opposite sides of alpha=.05 — which is "
            "why v2 replaces both with a single commit-clustered bootstrap "
            "whose interval and p-value come from the same resampling "
            "distribution."),
    }

    # ---- (5) family-collapsed sensitivity -----------------------------------
    def units_from(group_of: dict[str, str]) -> dict[str, list[str]]:
        u: dict[str, list[str]] = defaultdict(list)
        for tid in sorted(succ):
            u[group_of[tid]].append(tid)
        return dict(u)

    collapse_levels = {"family": units_from(fam_of),
                       "module": units_from(module_of),
                       "commit": units_from(commit_of)}
    collapsed: dict[str, dict] = {}
    for level, units in collapse_levels.items():
        lvl: dict[str, dict] = {"n_units": len(units)}
        for rule in ("majority", "any"):
            arm_out: dict[str, dict[str, bool]] = {}
            tie_ct: dict[str, int] = {}
            for a in ARMS:
                arm_out[a], tie_ct[a] = collapse_units(units, succ, a, rule)
            entry: dict[str, object] = {
                "rates": {a: {"k": sum(arm_out[a].values()),
                              "n": len(units),
                              "rate": round(sum(arm_out[a].values()) / len(units), 4)}
                          for a in ARMS},
                "majority_ties_counted_as_failure": (tie_ct if rule == "majority"
                                                     else None),
                "pairs": {f"{x}_vs_{y}": paired_binary(arm_out[x], arm_out[y])
                          for x, y in PAIRS},
            }
            lvl[rule] = entry
        collapsed[level] = lvl

    # tie-free mean collapse at family level: unit value = fraction of member
    # tasks succeeding (families weighted equally); commit-clustered bootstrap
    # over family units (families nest in commits — verified above).
    fam_ids = sorted(families)
    fam_commit = {fid: commit_of[families[fid][0]] for fid in fam_ids}
    mean_collapse: dict[str, dict] = {}
    for x, y in PAIRS:
        fam_diff = {fid: (np.mean([succ[m][x] for m in families[fid]])
                          - np.mean([succ[m][y] for m in families[fid]]))
                    for fid in fam_ids}
        by_c: dict[str, list[float]] = defaultdict(list)
        for fid in fam_ids:
            by_c[fam_commit[fid]].append(fam_diff[fid])
        arrs = [np.array(by_c[c], dtype=float) for c in sorted(by_c)]
        mean_collapse[f"{x}_vs_{y}"] = cluster_boot_rd(
            arrs, seed=SEED + JOB_SEEDS[(x, y, "family_mean")])
        mean_collapse[f"{x}_vs_{y}"]["note"] = (
            "units = families (equal weight), unit value = member success "
            "fraction; RD = mean over families of paired unit differences; "
            "commit-clustered bootstrap over family units")

    # ---- assemble -----------------------------------------------------------
    out = {
        "generated_by": "bench/analysis/fresh_clustered.py",
        "review_items": "REVIEW-2 §1 (one coherent paired inference) + §2 (cluster structure)",
        "seed": SEED, "B": B, "jaccard_threshold": JACCARD_T,
        "outcome_provenance": {
            "success_metric": v2["success_metric"],
            "per_task_outcomes": str(SUMMARY_V2)
            + " paired_matrix (post-repair; E fresh_069..099 = 2026-07-27 rerun)",
            "cluster_labels": str(TASKS_FILE) + " added_in.commit / module",
            "cross_checked_against": str(PART1_V2),
        },
        "census": census,
        "HEADLINE_D_vs_E_full100_commit_clustered": headline,
        "effect_concentration_by_commit": concentration,
        "old_numbers_for_supplement_D_vs_E": old,
        "clustered_bootstraps": {k: dict(v) for k, v in boots.items()},
        "family_collapsed_sensitivity": collapsed,
        "family_mean_collapse_commit_clustered": mean_collapse,
    }
    OUT_JSON.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT_JSON}")

    # ---- markdown report ----------------------------------------------------
    def ci_s(d: dict) -> str:
        lo, hi = d["ci95_percentile"]
        return f"{d['rd']:+.3f} [{lo:+.3f}, {hi:+.3f}] p={d['p_two_sided_percentile_inversion']:.4f}"

    lines: list[str] = []
    A = lines.append
    A("# Fresh-set cluster structure + coherent paired inference (REVIEW-2 §1–§2)")
    A("")
    A(f"Generated by `bench/analysis/fresh_clustered.py` (seed {SEED}, B={B:,}).")
    A("Outcomes: post-repair full-100 per-task success bits from")
    A("`bench/analysis/bridge_summary_v2.json` `paired_matrix` (metric:")
    A(f"`{v2['success_metric']}`), cross-checked against")
    A("`part1_fresh100_v2.json`. Cluster labels from `bench/data/fresh_tasks.jsonl`.")
    A("")
    A("## 1. Cluster census — the reviewer is right about the structure")
    A("")
    A(f"- **100 tasks** drawn from **{len(commits)} distinct source commits** "
      f"(reviewer said \"roughly 44\" — the true number is exactly **44**) and "
      f"**{len(modules)} distinct source files**.")
    A(f"- Name-stem sibling families (rule: same file AND shared leading name "
      f"token OR name-token Jaccard ≥ {JACCARD_T}): **{len(families)} families** "
      f"({len(multi_fams)} multi-member + {len(families) - len(multi_fams)} singletons).")
    A(f"- Hierarchy: every file belongs to one commit: **{nested}**; every "
      f"family nests in one commit: **{fam_nested}** — so commit is the "
      f"coarsest (most conservative) clustering level.")
    A(f"- Largest commit clusters: "
      + ", ".join(f"`{c['commit']}` (n={c['n']})" for c in census["largest_commits"]) + ".")
    A("")
    A("Reviewer's three named sibling groups, verified:")
    ae = reviewer_examples
    A(f"- **AntitoneOn integral results**: {len(ae['AntitoneOn_integral_results']['task_ids'])} "
      f"tasks (fresh_019–027, one file, one commit) — one family under the rule: "
      f"{ae['AntitoneOn_integral_results']['one_family_under_rule']}.")
    A(f"- **Bounded-variation variants**: {len(ae['bounded_variation_variants']['task_ids'])} "
      f"tasks in Topology.EMetricSpace.BoundedVariation (one file/commit); the "
      f"name-stem rule keeps {ae['bounded_variation_variants']['n_families_under_rule']} "
      f"sub-families, but the file- and commit-level collapses below treat all 8 as one unit.")
    A(f"- **Monotonicity `const_smul` variants**: "
      f"{len(ae['monotonicity_const_smul_variants']['task_ids'])} tasks "
      f"(fresh_005–007) — one family under the rule: "
      f"{ae['monotonicity_const_smul_variants']['one_family_under_rule']}.")
    A("")
    A("Multi-member families under the rule:")
    A("")
    A("| family | n | file (Mathlib.) | members |")
    A("|---|---|---|---|")
    for f in census["multi_member_families"]:
        mods = f["module"].replace("Mathlib.", "")
        mems = ", ".join(m["id"].replace("fresh_", "") for m in f["members"])
        A(f"| {f['family']} | {f['n']} | {mods} | {mems} |")
    A("")
    A("## 2. THE headline inference — commit-clustered paired bootstrap, D vs E")
    A("")
    h = headline
    A(f"On the post-repair full-100 set (D 42/100 = 42.0%, E 30/100 = 30.0%):")
    A("")
    A(f"> **RD (D − E) = {h['rd']:+.2f} ({100*h['rd']:+.0f}pp), 95% CI "
      f"[{h['ci95_percentile'][0]:+.3f}, {h['ci95_percentile'][1]:+.3f}], "
      f"two-sided bootstrap p = {h['p_two_sided_percentile_inversion']:.3f}** "
      f"(commit-clustered paired bootstrap, {h['n_clusters']} clusters, "
      f"B={h['B']:,}, seed {h['seed']}).")
    A("")
    A("This is ONE coherent paired inference: the percentile interval and the")
    A("percentile-inversion p-value are read off the same resampling")
    A("distribution, so they cannot disagree about alpha=.05, and the")
    A("resampling respects the commit-level dependence the reviewer flagged.")
    A("Suggested paper sentence: *\"D exceeded E by 12 percentage points, but")
    A("the commit-clustered matched comparison was inconclusive at this sample")
    A(f"size (95% CI [{h['ci95_percentile'][0]:+.3f}, {h['ci95_percentile'][1]:+.3f}], "
      f"p = {h['p_two_sided_percentile_inversion']:.3f}).\"*")
    A("")
    conc = concentration["D_vs_E"]
    t1, t2 = conc["top3_commit_contributions"][:2]

    def de_of(commit_prefix: str) -> tuple[int, int, int]:
        ids = [t for t in succ if commit_of[t].startswith(commit_prefix)]
        return (sum(succ[t]["D"] for t in ids),
                sum(succ[t]["E"] for t in ids), len(ids))

    d1, e1, n1 = de_of(t1["commit"])
    d2, e2, n2 = de_of(t2["commit"])
    A("**Why clustering widens this contrast** — the advantage is concentrated:")
    A(f"of D's net +{conc['total_net_paired_diff']} paired-task advantage over E, "
      f"**+{t1['net_paired_diff']} comes from a single commit** "
      f"(`{t1['commit']}`, the {n1}-task AntitoneOn sum–integral family, "
      f"where D went {d1}/{n1} and E {e1}/{n1}) and +{t2['net_paired_diff']} from "
      f"`{t2['commit']}` (the {n2}-task bounded-variation file, D {d2}/{n2} vs "
      f"E {e2}/{n2}) — i.e. {conc['top2_share_of_net']:.0%} of the net effect "
      f"sits in 2 of 44 commits, which is exactly the pseudoreplication the "
      f"reviewer flagged.")
    A("")
    A("### Old numbers (supplement only — replaced by the above)")
    A("")
    A(f"- Paired-Wald RD 95% CI: [{old['paired_wald_rd_ci95'][0]:+.4f}, "
      f"{old['paired_wald_rd_ci95'][1]:+.4f}] (barely excludes 0).")
    A(f"- Exact McNemar (38 discordant pairs): p = {old['p_mcnemar_exact']:.4f}.")
    A(f"- Why they disagreed: {old['mismatch_explanation_one_sentence']}")
    A("")
    A("## 3. All pairs × all clustering levels (paired bootstrap RD [95% CI] p)")
    A("")
    A("| pair | task-level (iid) | family-clustered | file-clustered | **commit-clustered (headline)** |")
    A("|---|---|---|---|---|")
    for x, y in PAIRS:
        r = boots[f"{x}_vs_{y}"]
        A(f"| {x} − {y} | {ci_s(r['task'])} | {ci_s(r['family'])} | "
          f"{ci_s(r['module'])} | **{ci_s(r['commit'])}** |")
    A("")
    A("Clustering widens D−E as the reviewer predicted (task-level p "
      f"{boots['D_vs_E']['task']['p_two_sided_percentile_inversion']:.3f} → commit-level p "
      f"{boots['D_vs_E']['commit']['p_two_sided_percentile_inversion']:.3f}); "
      "D−C and D−A survive clustering.")
    A("")
    A("## 4. Family-collapsed sensitivity (one unit per family/file/commit)")
    A("")
    A("Unit outcome = majority of member tasks (ties→failure; tie counts in the")
    A("json) or any-success. Exact McNemar + paired-Wald RD on the collapsed units.")
    A("")
    A("| level (units) | rule | D | E | D−E RD [CI] p | D−C RD [CI] p | D−A RD [CI] p |")
    A("|---|---|---|---|---|---|---|")
    for level in ("family", "module", "commit"):
        lv = collapsed[level]
        for rule in ("majority", "any"):
            e = lv[rule]
            def cell(pair: str) -> str:
                p = e["pairs"][pair]
                lo, hi = p["rd_ci95_paired_wald"]
                return (f"{p['rd']:+.3f} [{lo:+.3f}, {hi:+.3f}] "
                        f"p={p['p_mcnemar_exact']:.3f}")
            A(f"| {level} (n={lv['n_units']}) | {rule} | "
              f"{e['rates']['D']['k']}/{lv['n_units']} | "
              f"{e['rates']['E']['k']}/{lv['n_units']} | "
              f"{cell('D_vs_E')} | {cell('D_vs_C')} | {cell('D_vs_A')} |")
    A("")
    A("Tie-free mean collapse (families weighted equally, commit-clustered bootstrap):")
    A("")
    for x, y in PAIRS:
        m = mean_collapse[f"{x}_vs_{y}"]
        A(f"- {x} − {y}: RD {m['rd']:+.3f}, 95% CI "
          f"[{m['ci95_percentile'][0]:+.3f}, {m['ci95_percentile'][1]:+.3f}], "
          f"p = {m['p_two_sided_percentile_inversion']:.4f}")
    A("")
    A("## 5. Provenance of every headline number")
    A("")
    A("- Per-task outcomes (success bits, all five arms, 100 fresh tasks):")
    A("  `bench/analysis/bridge_summary_v2.json` → `paired_matrix` (produced by")
    A("  `bench/analysis/score_e31_v2.py` grading `bench/data/runs/{A..E}/fresh_*.json`;")
    A("  arm-E rows fresh_069..099 are the 2026-07-27 post-outage repair).")
    A("- Commit / file labels: `bench/data/fresh_tasks.jsonl` → `added_in.commit`, `module`.")
    A("- Old Wald/McNemar numbers: recomputed here with the verbatim")
    A("  `tier1_reanalysis.py` formulas and asserted against `part1_fresh100_v2.json`.")
    A(f"- Bootstrap: B={B:,}, base seed {SEED}, fixed per-job seed offsets in the script.")
    A("")
    OUT_MD.write_text("\n".join(lines))
    print(f"wrote {OUT_MD}")

    # console headline
    print("\nHEADLINE  D-vs-E full-100, commit-clustered paired bootstrap:")
    print(f"  RD {h['rd']:+.3f}  CI95 [{h['ci95_percentile'][0]:+.3f}, "
          f"{h['ci95_percentile'][1]:+.3f}]  p {h['p_two_sided_percentile_inversion']:.4f}")
    for x, y in PAIRS[1:]:
        r = boots[f"{x}_vs_{y}"]["commit"]
        print(f"  {x}-vs-{y}: RD {r['rd']:+.3f}  CI95 "
              f"[{r['ci95_percentile'][0]:+.3f}, {r['ci95_percentile'][1]:+.3f}]  "
              f"p {r['p_two_sided_percentile_inversion']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
