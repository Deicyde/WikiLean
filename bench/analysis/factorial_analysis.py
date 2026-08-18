#!/usr/bin/env python3
"""Factorial Stage-3 — the PREREGISTERED analysis of the 2x2 (join x verifier).

Follows docs/research/BRIDGE-FACTORIAL.md (commit 3658bd58) section 5 EXACTLY.

Primary endpoint: grounded typecheck under the REPAIRED oracle, per-row bits
from bench/analysis/factorial_scored.json (Stage 1).

Primary analysis: commit-clustered paired bootstrap — clusters = the 44
`added_in.commit` values, B = 10,000, seed = 20260803, machinery =
bench/analysis/fresh_clustered.py::cluster_boot_rd (percentile 95% CI and
two-sided percentile-inversion p from the same resampling distribution).
With Y_a(t) in {0,1}:

  JOIN main effect     = mean_t ((Y_Dp + Y_J) - (Y_X + Y_Ep)) / 2
  VERIFIER main effect = mean_t ((Y_Dp + Y_X) - (Y_J + Y_Ep)) / 2
  Interaction          = mean_t (Y_Dp - Y_J) - (Y_X - Y_Ep)   [exploratory]

H-JOIN / H-VERIFIER supported iff effect > 0 with two-sided p < 0.05 (prereg
section 1; no multiplicity correction across the two preregistered main
effects; interaction and all secondaries exploratory).

Every bootstrap replicate resamples the 44 commits with replacement and
recomputes the per-task-mean statistic as (sum of resampled cluster sums) /
(sum of resampled cluster sizes) — cluster_boot_rd verbatim. The prereg fixes
ONE seed (20260803); every cluster_boot_rd call here uses it, so all endpoints
share the same deterministic cluster-resampling pattern.

Supporting descriptives (prereg s5): four per-arm rates with Wilson 95% CIs;
the six pairwise clustered RDs (labeled supporting/exploratory — the
preregistered confirmatory tests are ONLY the two main effects).

Secondary endpoints (exploratory, same clustered machinery):
  1. run-level repaired hallucination (lower is better)
  2. judge evaluated-equivalence (folded in once Stage 2 completes)
  3. conjunction: grounded typecheck AND judge evaluated
  4. turn/tool-use descriptives; decl_exists counts X vs Dp (manipulation
     check); informal-tool touches Ep/X
  5. exposure-stratified primary rates (own-module basis 51/49) and the
     3-task live-index-leak sensitivity (drop fresh_037/054/095)
  6. det2-subset sensitivity of the primary contrasts (prereg "74/100" =
     the both-annotator determinate set: `determinate` AND `det2`)
Plus the raw-oracle sensitivity of the primary (supplement).

Run:  python3 bench/analysis/factorial_analysis.py
Outputs: factorial_analysis.json + factorial_analysis.md (this directory).
Deterministic given the fixed seed. Idempotent; judge blocks appear when
bench/analysis/factorial_scored.json contains the Stage-2 fold.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent            # bench/analysis
BENCH = HERE.parent                               # bench/
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BENCH))
from fresh_clustered import cluster_boot_rd, wilson_ci  # noqa: E402

TASKS_FILE = BENCH / "data" / "fresh_tasks.jsonl"
SCORED = HERE / "factorial_scored.json"
EXPOSURE = HERE / "fresh_exposure.json"
OUT_JSON = HERE / "factorial_analysis.json"
OUT_MD = HERE / "factorial_analysis.md"

SEED = 20260803          # preregistered (BRIDGE-FACTORIAL.md section 5)
B = 10_000
ARMS = ["Ep", "X", "J", "Dp"]
LEAK_TASKS = {"fresh_037", "fresh_054", "fresh_095"}   # prereg section 3
PAIRWISE = [("Dp", "Ep"), ("Dp", "X"), ("Dp", "J"),
            ("X", "Ep"), ("J", "Ep"), ("J", "X")]


def per_task_effects(y: dict[str, dict[str, float]], tid: str) -> dict[str, float]:
    v = y[tid]
    return {
        "join": ((v["Dp"] + v["J"]) - (v["X"] + v["Ep"])) / 2.0,
        "verifier": ((v["Dp"] + v["X"]) - (v["J"] + v["Ep"])) / 2.0,
        "interaction": (v["Dp"] - v["J"]) - (v["X"] - v["Ep"]),
    }


def boot_effects(y: dict[str, dict[str, float]], ids: list[str],
                 commit_of: dict[str, str]) -> dict[str, dict]:
    """The three factorial effects, commit-clustered, seed 20260803."""
    out = {}
    for eff in ("join", "verifier", "interaction"):
        by: dict[str, list[float]] = defaultdict(list)
        for tid in sorted(ids):
            by[commit_of[tid]].append(per_task_effects(y, tid)[eff])
        arrs = [np.array(by[c], dtype=float) for c in sorted(by)]
        out[eff] = cluster_boot_rd(arrs, seed=SEED, b=B)
    return out


def boot_pair(y: dict[str, dict[str, float]], ids: list[str],
              commit_of: dict[str, str], x: str, z: str) -> dict:
    by: dict[str, list[float]] = defaultdict(list)
    for tid in sorted(ids):
        by[commit_of[tid]].append(y[tid][x] - y[tid][z])
    arrs = [np.array(by[c], dtype=float) for c in sorted(by)]
    return cluster_boot_rd(arrs, seed=SEED, b=B)


def arm_table(y: dict[str, dict[str, float]], ids: list[str]) -> dict:
    out = {}
    for a in ARMS:
        k = int(sum(y[t][a] for t in ids))
        n = len(ids)
        lo, hi = wilson_ci(k, n)
        out[a] = {"k": k, "n": n, "rate": round(k / n, 4),
                  "wilson95": [round(lo, 4), round(hi, 4)]}
    return out


def verdict_sentence(name: str, r: dict, alpha: float = 0.05) -> str:
    sig = r["p_two_sided_percentile_inversion"] < alpha
    pos = r["rd"] > 0
    if name == "interaction":
        return ("exploratory: " +
                ("a nonzero interaction is indicated" if sig else
                 "no detectable interaction at this sample size"))
    if sig and pos:
        return "SUPPORTED (effect > 0, p < 0.05)"
    if sig and not pos:
        return "REVERSED (effect < 0, p < 0.05) — hypothesis not supported"
    return ("NULL at this sample size (p >= 0.05); the CI is the precision "
            "statement")


def main() -> int:
    tasks = [json.loads(l) for l in TASKS_FILE.read_text().splitlines() if l]
    assert len(tasks) == 100
    commit_of = {t["id"]: t["added_in"]["commit"] for t in tasks}
    # prereg s3/s5.6 "det2 74/100" = the both-annotator determinate set
    # (annotator-1 `determinate` 86 AND annotator-2 `det2` 83 -> 74; the
    # "both_determinate_PRIMARY_set" of v3_gate_fixes.json s6)
    det2_ids = [t["id"] for t in tasks
                if t.get("determinate") and t.get("det2")]
    assert len(det2_ids) == 74, len(det2_ids)
    assert len({c for c in commit_of.values()}) == 44
    ids = sorted(commit_of)

    scored = json.loads(SCORED.read_text())
    pr = scored["per_row"]

    def bits(field: str) -> dict[str, dict[str, float]]:
        return {tid: {a: float(bool(pr[a][tid][field])) for a in ARMS}
                for tid in ids}

    y_rep = bits("grounded_tc_repaired")
    y_raw = bits("grounded_tc_raw")
    y_hal = bits("any_halluc_repaired")

    exposure = {r["id"]: bool(r["basename_in_own_module"])
                for r in json.loads(EXPOSURE.read_text())["per_task"]}
    exp_ids = [t for t in ids if exposure[t]]
    unexp_ids = [t for t in ids if not exposure[t]]
    assert (len(exp_ids), len(unexp_ids)) == (51, 49)

    out: dict = {
        "generated_by": "bench/analysis/factorial_analysis.py",
        "prereg": "docs/research/BRIDGE-FACTORIAL.md @ 3658bd58 (section 5 "
                  "followed exactly; alpha=0.05 two-sided; the two main "
                  "effects are the only confirmatory tests)",
        "outcome_source": str(SCORED),
        "seed": SEED, "B": B, "n_clusters": 44,
        "machinery": "fresh_clustered.cluster_boot_rd (verbatim import)",
    }

    # ---- PRIMARY -----------------------------------------------------------
    primary = boot_effects(y_rep, ids, commit_of)
    out["PRIMARY_grounded_tc_repaired"] = {
        "four_arm_table_wilson95": arm_table(y_rep, ids),
        "effects": primary,
        "verdicts": {
            "H_JOIN": verdict_sentence("join", primary["join"]),
            "H_VERIFIER": verdict_sentence("verifier", primary["verifier"]),
            "interaction": verdict_sentence("interaction",
                                            primary["interaction"]),
        },
        "pairwise_clustered_RDs_supporting": {
            f"{x}_vs_{z}": boot_pair(y_rep, ids, commit_of, x, z)
            for x, z in PAIRWISE},
    }

    # ---- raw-oracle sensitivity (supplement) -------------------------------
    out["sensitivity_raw_oracle"] = {
        "four_arm_table_wilson95": arm_table(y_raw, ids),
        "effects": boot_effects(y_raw, ids, commit_of),
    }

    # ---- secondary 1: run-level repaired hallucination ---------------------
    out["secondary_hallucination_repaired"] = {
        "note": "run cites >=1 repaired-oracle hallucinated name; LOWER is "
                "better, so a negative JOIN/VERIFIER effect favors the factor",
        "four_arm_table_wilson95": arm_table(y_hal, ids),
        "effects": boot_effects(y_hal, ids, commit_of),
    }

    # ---- secondaries 2+3: judge + conjunction (need Stage 2) ---------------
    if scored.get("judge"):
        y_j = bits("judge_evaluated")
        y_c = {tid: {a: float(bool(pr[a][tid]["grounded_tc_repaired"]
                                   and pr[a][tid]["judge_evaluated"]))
                     for a in ARMS} for tid in ids}
        out["secondary_judge_evaluated"] = {
            "four_arm_table_wilson95": arm_table(y_j, ids),
            "effects": boot_effects(y_j, ids, commit_of),
        }
        out["secondary_conjunction_grounded_and_evaluated"] = {
            "four_arm_table_wilson95": arm_table(y_c, ids),
            "effects": boot_effects(y_c, ids, commit_of),
        }
        out["judge_summary"] = scored["judge"]
    else:
        out["secondary_judge_evaluated"] = "PENDING Stage 2"
        out["secondary_conjunction_grounded_and_evaluated"] = "PENDING Stage 2"

    # ---- secondary 4: descriptives / manipulation checks -------------------
    pa = scored["per_arm"]
    out["secondary_descriptives"] = {
        a: {"mean_assistant_turns": pa[a]["mean_assistant_turns"],
            "mean_tool_calls": pa[a]["mean_tool_calls"],
            "capped_k": pa[a]["capped_k"],
            "produced": pa[a]["produced"],
            "typecheck_ok_k": pa[a]["typecheck_ok_k"],
            "cost_usd_total": pa[a]["cost_usd_total"],
            "decl_exists_calls_total": pa[a]["decl_exists_calls_total"],
            "runs_using_decl_exists": pa[a]["runs_using_decl_exists"],
            "runs_touching_informal": pa[a]["runs_touching_informal"]}
        for a in ARMS}
    out["manipulation_checks"] = {
        "verifier_usage": {
            "X": {"decl_exists_calls": pa["X"]["decl_exists_calls_total"],
                  "runs_using": pa["X"]["runs_using_decl_exists"]},
            "Dp": {"decl_exists_calls": pa["Dp"]["decl_exists_calls_total"],
                   "runs_using": pa["Dp"]["runs_using_decl_exists"]},
        },
        "informal_touches": {
            "Ep": pa["Ep"]["runs_touching_informal"],
            "X": pa["X"]["runs_touching_informal"],
        },
    }

    # ---- secondary 5: exposure strata + live-leak sensitivity --------------
    noleak_ids = [t for t in ids if t not in LEAK_TASKS]
    out["secondary_exposure_and_leak"] = {
        "own_module_exposed_51": {
            "four_arm_table_wilson95": arm_table(y_rep, exp_ids),
            "effects": boot_effects(y_rep, exp_ids, commit_of)},
        "unexposed_49": {
            "four_arm_table_wilson95": arm_table(y_rep, unexp_ids),
            "effects": boot_effects(y_rep, unexp_ids, commit_of)},
        "drop_3_live_index_leaks": {
            "dropped": sorted(LEAK_TASKS),
            "four_arm_table_wilson95": arm_table(y_rep, noleak_ids),
            "effects": boot_effects(y_rep, noleak_ids, commit_of)},
    }

    # ---- secondary 6: det2 subset ------------------------------------------
    out["secondary_det2_subset"] = {
        "n": len(det2_ids),
        "four_arm_table_wilson95": arm_table(y_rep, det2_ids),
        "effects": boot_effects(y_rep, det2_ids, commit_of),
    }

    OUT_JSON.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT_JSON}")

    # ---- markdown ----------------------------------------------------------
    def eff_s(r: dict) -> str:
        lo, hi = r["ci95_percentile"]
        return (f"{r['rd']:+.3f} [{lo:+.3f}, {hi:+.3f}] "
                f"p={r['p_two_sided_percentile_inversion']:.4f}")

    L: list[str] = []
    A = L.append
    A("# The preregistered 2x2 factorial — analysis")
    A("")
    A(f"Prereg `docs/research/BRIDGE-FACTORIAL.md` @ 3658bd58; seed {SEED}, "
      f"B={B:,}, 44 commit clusters; machinery `cluster_boot_rd` (verbatim).")
    A("Primary outcome: grounded typecheck, repaired oracle.")
    A("")
    A("## Four-arm table (primary outcome)")
    A("")
    A("| arm | cell | k/n | rate | Wilson 95% |")
    A("|---|---|---|---|---|")
    cells = {"Ep": "join-, ver-", "X": "join-, ver+",
             "J": "join+, ver-", "Dp": "join+, ver+"}
    tab = out["PRIMARY_grounded_tc_repaired"]["four_arm_table_wilson95"]
    for a in ARMS:
        r = tab[a]
        A(f"| {a} | {cells[a]} | {r['k']}/{r['n']} | {r['rate']:.3f} "
          f"| [{r['wilson95'][0]:.3f}, {r['wilson95'][1]:.3f}] |")
    A("")
    A("## Preregistered effects (commit-clustered paired bootstrap)")
    A("")
    for eff, label in (("join", "JOIN main effect (confirmatory)"),
                       ("verifier", "VERIFIER main effect (confirmatory)"),
                       ("interaction", "Interaction (exploratory)")):
        r = primary[eff]
        v = out["PRIMARY_grounded_tc_repaired"]["verdicts"][
            {"join": "H_JOIN", "verifier": "H_VERIFIER",
             "interaction": "interaction"}[eff]]
        A(f"- **{label}**: {eff_s(r)} -> {v}")
    A("")
    A("## Six pairwise contrasts (supporting descriptives, exploratory)")
    A("")
    for x, z in PAIRWISE:
        r = out["PRIMARY_grounded_tc_repaired"][
            "pairwise_clustered_RDs_supporting"][f"{x}_vs_{z}"]
        A(f"- {x} - {z}: {eff_s(r)}")
    A("")
    A("## Raw-oracle sensitivity (supplement)")
    A("")
    rtab = out["sensitivity_raw_oracle"]["four_arm_table_wilson95"]
    A("Rates: " + ", ".join(f"{a} {rtab[a]['k']}/100" for a in ARMS) + ".")
    for eff in ("join", "verifier", "interaction"):
        A(f"- {eff}: {eff_s(out['sensitivity_raw_oracle']['effects'][eff])}")
    A("")
    A("## Secondary: run-level repaired hallucination (lower better)")
    A("")
    htab = out["secondary_hallucination_repaired"]["four_arm_table_wilson95"]
    A("Rates: " + ", ".join(f"{a} {htab[a]['k']}/100" for a in ARMS) + ".")
    for eff in ("join", "verifier", "interaction"):
        A(f"- {eff}: "
          f"{eff_s(out['secondary_hallucination_repaired']['effects'][eff])}")
    A("")
    if scored.get("judge"):
        A("## Secondary: judge evaluated-equivalence")
        A("")
        jtab = out["secondary_judge_evaluated"]["four_arm_table_wilson95"]
        A("Rates: " + ", ".join(f"{a} {jtab[a]['k']}/100" for a in ARMS) + ".")
        for eff in ("join", "verifier", "interaction"):
            A(f"- {eff}: "
              f"{eff_s(out['secondary_judge_evaluated']['effects'][eff])}")
        A("")
        A("## Secondary: conjunction (grounded typecheck AND evaluated)")
        A("")
        ctab = out["secondary_conjunction_grounded_and_evaluated"][
            "four_arm_table_wilson95"]
        A("Rates: " + ", ".join(f"{a} {ctab[a]['k']}/100" for a in ARMS) + ".")
        for eff in ("join", "verifier", "interaction"):
            A(f"- {eff}: {eff_s(out['secondary_conjunction_grounded_and_evaluated']['effects'][eff])}")
        A("")
    else:
        A("## Secondary: judge + conjunction — PENDING Stage 2")
        A("")
    A("## Manipulation checks")
    A("")
    mc = out["manipulation_checks"]
    A(f"- decl_exists usage: X {mc['verifier_usage']['X']['decl_exists_calls']} "
      f"calls / {mc['verifier_usage']['X']['runs_using']} runs; "
      f"Dp {mc['verifier_usage']['Dp']['decl_exists_calls']} calls / "
      f"{mc['verifier_usage']['Dp']['runs_using']} runs.")
    A(f"- informal-tool touches: Ep {mc['informal_touches']['Ep']}/100, "
      f"X {mc['informal_touches']['X']}/100 runs.")
    A("")
    A("## Sensitivity cuts (primary outcome, exploratory)")
    A("")
    for key, label in (("own_module_exposed_51", "exposed 51"),
                       ("unexposed_49", "unexposed 49"),
                       ("drop_3_live_index_leaks", "drop 3 leak tasks")):
        blk = out["secondary_exposure_and_leak"][key]
        t = blk["four_arm_table_wilson95"]
        A(f"- **{label}** — rates "
          + ", ".join(f"{a} {t[a]['k']}/{t[a]['n']}" for a in ARMS)
          + "; join " + eff_s(blk["effects"]["join"])
          + "; verifier " + eff_s(blk["effects"]["verifier"]) + ".")
    d2 = out["secondary_det2_subset"]
    t = d2["four_arm_table_wilson95"]
    A(f"- **determinacy subset (both-annotator det2, n={d2['n']})** — rates "
      + ", ".join(f"{a} {t[a]['k']}/{t[a]['n']}" for a in ARMS)
      + "; join " + eff_s(d2["effects"]["join"])
      + "; verifier " + eff_s(d2["effects"]["verifier"]) + ".")
    A("")
    OUT_MD.write_text("\n".join(L))
    print(f"wrote {OUT_MD}")

    p = primary
    print("\nPREREGISTERED EFFECTS (grounded typecheck, repaired oracle):")
    for eff in ("join", "verifier", "interaction"):
        print(f"  {eff:12s} {eff_s(p[eff])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
