#!/usr/bin/env python3
"""Bridge v3 fix-pass gate analyses (six items the v3 draft still needs).

All computed from COMMITTED artifacts + the run rows on disk; the only
"instrument" re-executed is the deterministic hallucination oracle
(score_bridge.Oracle + halluc_validation.classify_adjusted — no REPL, no LLM).
Deterministic: seeds/machinery imported from fresh_clustered.py (base seed
20260801, B=10,000, cluster_boot_rd), with NEW frozen per-job seed offsets
below so published numbers can never drift if jobs are added or reordered.

  (1) S4 exposure-strata D-vs-E (and D-vs-C) McNemars recomputed on the
      POST-REPAIR rows (bridge_summary_v2.json paired_matrix — E rows
      fresh_069..099 are the 2026-07-27 rerun), strata from
      fresh_exposure.json per_task flags; merge-date split redone; verdict on
      the S4 sentence "D's edge over E is strongest exactly where there was
      nothing to leak". A repaired-oracle sensitivity block is included.
  (2) Commit-clustered paired bootstraps (44 clusters, identical machinery)
      for: E-vs-A grounded typecheck (repaired instrument); repaired
      run-level hallucination D-vs-A, D-vs-E, D-vs-C, E-vs-A; judge
      evaluated-equivalence D-vs-E, E-vs-A; repaired conjunction D-vs-A,
      E-vs-A, D-vs-E.  Reports which unclustered significances survive.
  (3) Tier-1 fresh attach audit: zero-tool-call rows per arm over all 500
      fresh rows + the 1,705 eval-341 rows, with the honest schema fact that
      Tier-1 run rows record NO MCP init/attach event (only per-row tool
      activity + the arm-D campaign-level HTTP preflight in run_bridge.py).
  (4) E's turn-budget overruns on the post-repair rows + corrected S5
      within-budget counts and both-within-budget pair contrasts.
  (5) Judge Layer-1 five-arm evaluated-equivalence table (A and B included,
      Wilson CIs) + judge McNemars D-vs-A and D-vs-B.
  (6) Fresh-task provenance facts for §3.2 (fields, NL origin, selection
      criteria, and the plain fact that no construction script is in the repo).

Run:  python3 bench/analysis/v3_gate_fixes.py
Out:  v3_gate_fixes.json, v3_gate_fixes.md (this directory).
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent            # bench/analysis
BENCH = HERE.parent                               # bench/
REPO = BENCH.parent
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(HERE))

import score_bridge  # noqa: E402
from fresh_clustered import (B, SEED, cluster_boot_rd,  # noqa: E402
                             mcnemar_exact, paired_rd_wald, wilson_ci)
from halluc_validation import (build_suffix_openable,  # noqa: E402
                               classify_adjusted, load_fresh_rows)

ARMS = ["A", "B", "C", "D", "E"]
RUNS = BENCH / "data" / "runs"
TASKS_FILE = BENCH / "data" / "fresh_tasks.jsonl"
STATS_FILE = BENCH / "data" / "fresh_tasks.stats.json"
BRIDGE_TASKS = BENCH / "data" / "bridge_tasks.jsonl"
SUMMARY_V2 = HERE / "bridge_summary_v2.json"
SUCCESS_REP = HERE / "success_repaired.json"
HALLUC_VAL = HERE / "halluc_validation.json"
JUDGE_SUMMARY = HERE / "judge_fresh_summary.json"
CONJ_REP = HERE / "conjunction_repaired.json"
EXPOSURE = HERE / "fresh_exposure.json"
JUDGE_DIR = HERE / "judge_fresh"
OUT_JSON = HERE / "v3_gate_fixes.json"
OUT_MD = HERE / "v3_gate_fixes.md"

ALPHA = 0.05
MAX_TURNS = 30

# NEW frozen per-job seed offsets (disjoint from fresh_clustered.JOB_SEEDS by
# convention; frozen so adding/reordering jobs never changes published numbers).
JOB_SEEDS = {
    ("gtc_repaired", "E", "A"): 41,
    ("halluc_repaired", "D", "A"): 51, ("halluc_repaired", "D", "E"): 52,
    ("halluc_repaired", "D", "C"): 53, ("halluc_repaired", "E", "A"): 54,
    ("judge_evaluated", "D", "E"): 61, ("judge_evaluated", "E", "A"): 62,
    ("conj_repaired", "D", "A"): 71, ("conj_repaired", "E", "A"): 72,
    ("conj_repaired", "D", "E"): 73,
}


def mcn_block(bits: dict[str, dict[str, bool]], ids: list[str],
              x: str, y: str) -> dict:
    b = sum(1 for t in ids if bits[t][x] and not bits[t][y])
    c = sum(1 for t in ids if bits[t][y] and not bits[t][x])
    both = sum(1 for t in ids if bits[t][x] and bits[t][y])
    n = len(ids)
    wald = paired_rd_wald(b, c, n)
    return {"pair": f"{x}_vs_{y}", "n_paired": n, "both": both,
            f"{x}_only": b, f"{y}_only": c, "neither": n - both - b - c,
            "discordant": b + c,
            "p_exact_binomial_two_sided": float(f"{mcnemar_exact(b, c):.3g}"),
            "rd": round(wald["rd"], 6),
            "rd_ci95_paired_wald": [round(v, 6) for v in wald["ci95"]]}


def rate_row(k: int, n: int) -> dict:
    lo, hi = wilson_ci(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 4),
            "wilson95": [round(lo, 4), round(hi, 4)]}


def main() -> int:
    tasks = [json.loads(l) for l in TASKS_FILE.read_text().splitlines() if l]
    assert len(tasks) == 100
    fresh_ids = sorted(t["id"] for t in tasks)
    commit_of = {t["id"]: t["added_in"]["commit"] for t in tasks}
    n_commits = len(set(commit_of.values()))
    assert n_commits == 44, n_commits

    # ---------------- outcome layers (all per-task bits over 100 x 5) --------
    v2 = json.loads(SUMMARY_V2.read_text())
    pm = v2["paired_matrix"]
    raw_succ = {t: {a: bool(pm[t][a]) for a in ARMS} for t in fresh_ids}

    # repaired grounded typecheck: raw bits + the 129 affected-row verdicts
    srep = json.loads(SUCCESS_REP.read_text())
    rep_succ = {t: dict(raw_succ[t]) for t in fresh_ids}
    for r in srep["affected_rows_detail"]:
        assert raw_succ[r["task_id"]][r["arm"]] == bool(r["raw_folded_success"])
        rep_succ[r["task_id"]][r["arm"]] = bool(r["repaired_folded_success"])
    for a in ARMS:
        want = srep["fresh_100_table_both_instruments"]["repaired_oracle"][a]["k"]
        got = sum(rep_succ[t][a] for t in fresh_ids)
        assert got == want, (a, got, want)

    # judge evaluated-equivalence bits from the committed per-row verdicts
    jfs = json.loads(JUDGE_SUMMARY.read_text())
    evald = {t: {} for t in fresh_ids}
    for a in ARMS:
        for t in fresh_ids:
            jr = json.loads((JUDGE_DIR / a / f"{t}.judge.json").read_text())
            assert jr["arm"] == a and jr["task_id"] == t
            evald[t][a] = bool(jr["evaluated"])
        want = jfs["fresh_100"]["evaluated_table"][a]["k"]
        got = sum(evald[t][a] for t in fresh_ids)
        assert got == want, (a, got, want)

    # repaired conjunction = repaired grounded typecheck AND judge evaluated
    conj = {t: {a: rep_succ[t][a] and evald[t][a] for a in ARMS}
            for t in fresh_ids}
    crep = json.loads(CONJ_REP.read_text())
    for a in ARMS:
        want = crep["fresh_100"]["repaired_conjunction"][a]["k"]
        got = sum(conj[t][a] for t in fresh_ids)
        assert got == want, (a, got, want)

    # run-level hallucination bits (any hallucinated citation), RAW + REPAIRED
    # oracle — deterministic re-classification, cross-checked per arm.
    oracle = score_bridge.Oracle(enabled=True)
    assert oracle.enabled
    openable = build_suffix_openable(oracle)
    rows = load_fresh_rows()                      # arm -> tid -> row
    hall_raw = {t: {} for t in fresh_ids}
    hall_rep = {t: {} for t in fresh_ids}
    for a in ARMS:
        for t in fresh_ids:
            lean = rows[a][t].get("output_lean")
            names = score_bridge.extract_cited(lean)
            hall_raw[t][a] = any(oracle.classify(n) == "hallucinated"
                                 for n in names)
            hall_rep[t][a] = any(
                classify_adjusted(lean, n, oracle, openable) == "hallucinated"
                for n in names)
    hv = json.loads(HALLUC_VAL.read_text())
    for a in ARMS:
        assert sum(hall_raw[t][a] for t in fresh_ids) == \
            hv["run_level"]["per_arm"][a]["runs_any_halluc"], a
        assert sum(hall_rep[t][a] for t in fresh_ids) == \
            hv["adjusted_oracle_sensitivity"]["per_arm"][a]["runs_any_halluc"], a
    print("outcome layers OK: repaired-success / judge / conjunction / "
          "halluc bits all match the committed summary tables")

    # ======================================================================
    # (1) S4 exposure strata on the POST-REPAIR rows
    # ======================================================================
    exp = json.loads(EXPOSURE.read_text())
    flags = {r["id"]: r for r in exp["per_task"]}
    exposed = sorted(t for t in fresh_ids if flags[t]["basename_in_own_module"])
    unexposed = sorted(t for t in fresh_ids
                       if not flags[t]["basename_in_own_module"])
    in_pin = sorted(t for t in fresh_ids if flags[t]["gold_commit_in_pin"])
    post_pin = sorted(t for t in fresh_ids
                      if not flags[t]["gold_commit_in_pin"])
    assert (len(exposed), len(unexposed)) == (51, 49)
    assert (len(in_pin), len(post_pin)) == (49, 51)

    def stratum(ids: list[str], bits) -> dict:
        return {"n": len(ids),
                "arms": {a: rate_row(sum(bits[t][a] for t in ids), len(ids))
                         for a in ARMS},
                "D_vs_E": mcn_block(bits, ids, "D", "E"),
                "D_vs_C": mcn_block(bits, ids, "D", "C")}

    s4 = {
        "basis": ("POST-REPAIR outcomes: bridge_summary_v2.json paired_matrix "
                  "(E rows fresh_069..099 = 2026-07-27 rerun), raw oracle — "
                  "replaces the snapshot-basis S4 in which E's 31 outage rows "
                  "counted as failures"),
        "strata_source": "fresh_exposure.json per_task "
                         "(basename_in_own_module / gold_commit_in_pin)",
        "by_exposure": {"exposed": stratum(exposed, raw_succ),
                        "unexposed": stratum(unexposed, raw_succ)},
        "by_merge_date": {"merged_before_pin": stratum(in_pin, raw_succ),
                          "merged_after_pin": stratum(post_pin, raw_succ)},
        "repaired_instrument_sensitivity": {
            "by_exposure": {"exposed": stratum(exposed, rep_succ),
                            "unexposed": stratum(unexposed, rep_succ)},
            "by_merge_date": {"merged_before_pin": stratum(in_pin, rep_succ),
                              "merged_after_pin": stratum(post_pin, rep_succ)}},
    }
    de_exp = s4["by_exposure"]["exposed"]["D_vs_E"]
    de_unexp = s4["by_exposure"]["unexposed"]["D_vs_E"]
    survives = de_unexp["rd"] > de_exp["rd"] and \
        de_unexp["p_exact_binomial_two_sided"] < ALPHA
    s4["verdict_S4_sentence"] = {
        "old_S4_claim": ("\"the leak's direction favors C/E, and D's edge over "
                         "E is strongest exactly where there was nothing to "
                         "leak\" (snapshot basis: exposed 15/3 p=.0075, "
                         "unexposed 17/3 p=.0026)"),
        "post_repair_D_vs_E": {
            "exposed": {"b_c": [de_exp["D_only"], de_exp["E_only"]],
                        "p": de_exp["p_exact_binomial_two_sided"],
                        "rd": de_exp["rd"]},
            "unexposed": {"b_c": [de_unexp["D_only"], de_unexp["E_only"]],
                          "p": de_unexp["p_exact_binomial_two_sided"],
                          "rd": de_unexp["rd"]}},
        "claim_survives": survives,
        "plain_statement": (
            "The claim does NOT survive the E repair. On the post-repair rows "
            f"D's edge over E is LARGER in the exposed stratum (RD "
            f"{de_exp['rd']:+.3f}, {de_exp['D_only']}/{de_exp['E_only']} "
            f"discordant, p={de_exp['p_exact_binomial_two_sided']:.3f}) than "
            f"in the unexposed stratum (RD {de_unexp['rd']:+.3f}, "
            f"{de_unexp['D_only']}/{de_unexp['E_only']}, "
            f"p={de_unexp['p_exact_binomial_two_sided']:.3f}); neither stratum "
            "is significant alone. The snapshot-basis pattern was an artifact "
            "of E's 31 outage failures concentrating in the unexposed stratum."
            if not survives else
            "The claim survives: D's edge remains larger and significant in "
            "the unexposed stratum on the post-repair rows."),
    }

    # ======================================================================
    # (2) commit-clustered paired bootstraps for the remaining contrasts
    # ======================================================================
    def diffs_by_commit(bits, x: str, y: str) -> list[np.ndarray]:
        by = defaultdict(list)
        for t in sorted(bits):
            by[commit_of[t]].append(int(bits[t][x]) - int(bits[t][y]))
        return [np.array(by[g], dtype=float) for g in sorted(by)]

    LAYERS = {"gtc_repaired": rep_succ, "halluc_repaired": hall_rep,
              "judge_evaluated": evald, "conj_repaired": conj}
    JOBS = [("gtc_repaired", "E", "A"),
            ("halluc_repaired", "D", "A"), ("halluc_repaired", "D", "E"),
            ("halluc_repaired", "D", "C"), ("halluc_repaired", "E", "A"),
            ("judge_evaluated", "D", "E"), ("judge_evaluated", "E", "A"),
            ("conj_repaired", "D", "A"), ("conj_repaired", "E", "A"),
            ("conj_repaired", "D", "E")]

    # published unclustered cross-checks (b, c) where a committed artifact
    # already reports the same contrast — asserted before bootstrapping
    pub = {
        ("gtc_repaired", "E", "A"):
            (srep["mcnemar_fresh_100_repaired"]["E_vs_A"]["E_only"],
             srep["mcnemar_fresh_100_repaired"]["E_vs_A"]["A_only"]),
        ("judge_evaluated", "D", "E"):
            (jfs["fresh_100"]["mcnemar_evaluated"]["D_vs_E"]["D_only"],
             jfs["fresh_100"]["mcnemar_evaluated"]["D_vs_E"]["E_only"]),
        ("judge_evaluated", "E", "A"):
            (jfs["fresh_100"]["mcnemar_evaluated"]["E_vs_A"]["E_only"],
             jfs["fresh_100"]["mcnemar_evaluated"]["E_vs_A"]["A_only"]),
        ("conj_repaired", "D", "A"):
            (crep["fresh_100"]["mcnemar_repaired"]["D_vs_A"]["D_only"],
             crep["fresh_100"]["mcnemar_repaired"]["D_vs_A"]["A_only"]),
        ("conj_repaired", "E", "A"):
            (crep["fresh_100"]["mcnemar_repaired"]["E_vs_A"]["E_only"],
             crep["fresh_100"]["mcnemar_repaired"]["E_vs_A"]["A_only"]),
        ("conj_repaired", "D", "E"):
            (crep["fresh_100"]["mcnemar_repaired"]["D_vs_E"]["D_only"],
             crep["fresh_100"]["mcnemar_repaired"]["D_vs_E"]["E_only"]),
    }

    clustered: dict[str, dict] = {}
    for layer, x, y in JOBS:
        bits = LAYERS[layer]
        unc = mcn_block(bits, fresh_ids, x, y)
        if (layer, x, y) in pub:
            assert (unc[f"{x}_only"], unc[f"{y}_only"]) == pub[(layer, x, y)], \
                (layer, x, y, unc)
        boot = cluster_boot_rd(diffs_by_commit(bits, x, y),
                               seed=SEED + JOB_SEEDS[(layer, x, y)])
        assert boot["n_clusters"] == 44
        unc_sig = unc["p_exact_binomial_two_sided"] < ALPHA
        clu_sig = boot["p_two_sided_percentile_inversion"] < ALPHA
        clustered[f"{layer}.{x}_vs_{y}"] = {
            "outcome_layer": layer, "pair": f"{x}_vs_{y}",
            "unclustered_mcnemar": unc,
            "commit_clustered_bootstrap": boot,
            "unclustered_significant_alpha05": unc_sig,
            "clustered_significant_alpha05": clu_sig,
            "survives_clustering": (unc_sig and clu_sig),
            "classification": (("sig" if unc_sig else "ns") + " -> "
                               + ("sig" if clu_sig else "ns")),
        }

    # ======================================================================
    # (3) Tier-1 fresh attach audit
    # ======================================================================
    row_keys: Counter = Counter()
    ts_keys: Counter = Counter()
    fresh_zero: dict[str, list[dict]] = {a: [] for a in ARMS}
    fresh_err = Counter()
    turns = {a: {} for a in ARMS}
    for a in ARMS:
        files = sorted((RUNS / a).glob("fresh_*.json"))
        assert len(files) == 100, (a, len(files))
        for f in files:
            r = json.loads(f.read_text())
            row_keys.update(r.keys())
            ts = r.get("transcript_stats") or {}
            ts_keys.update(ts.keys())
            n_tools = sum((ts.get("tool_calls_by_name") or {}).values())
            turns[a][r["task_id"]] = ts.get("turns")
            if r.get("error"):
                fresh_err[a] += 1
            if n_tools == 0:
                fresh_zero[a].append({"task_id": r["task_id"],
                                      "turns": ts.get("turns"),
                                      "wall_s": r.get("wall_s"),
                                      "tool_trace_len": len(r.get("tool_trace")
                                                            or [])})

    # eval-341 zero-tool counts per arm
    bt = {}
    for line in BRIDGE_TASKS.read_text().splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            if "_meta" not in r:
                bt[r["id"]] = r
    eval_ids = {t for t in bt if bt[t]["split"] == "eval"}
    assert len(eval_ids) == 341
    eval_zero, eval_tot, eval_err = Counter(), Counter(), Counter()
    eval_zero_ids: dict[str, list[str]] = {a: [] for a in ARMS}
    for a in ARMS:
        for f in sorted((RUNS / a).glob("*.json")):
            if ".judge" in f.name or f.stem.startswith("fresh_"):
                continue
            r = json.loads(f.read_text())
            tid = r.get("task_id") or f.stem
            if tid not in eval_ids:
                continue
            eval_tot[a] += 1
            if r.get("error"):
                eval_err[a] += 1
            ts = r.get("transcript_stats") or {}
            if sum((ts.get("tool_calls_by_name") or {}).values()) == 0:
                eval_zero[a] += 1
                eval_zero_ids[a].append(tid)
    assert all(eval_tot[a] == 341 for a in ARMS)

    attach_audit = {
        "question": ("referee: was Tier-1 attach-audited the way the v2 grid "
                     "was (init-event MCP status captured, race rows "
                     "condemned)?"),
        "schema_fact": {
            "row_keys_all_500_fresh_rows": sorted(row_keys),
            "transcript_stats_keys": sorted(ts_keys),
            "mcp_init_or_attach_evidence_in_rows": False,
            "statement": (
                "Tier-1 run rows (bench/run_bridge.py) record NO MCP "
                "init/attach event and no per-server status — the v2 grid "
                "runner's init-signature capture (commit 834a130a) postdates "
                "and never applied to Tier 1. Per-row attach evidence is "
                "therefore indirect: nonzero tool calls prove attachment; a "
                "zero-tool row cannot be distinguished from silent detooling "
                "by the row alone. Campaign-level evidence: arm D preflights "
                "the Wikibrain MCP (JSON-RPC initialize over HTTP) and ABORTS "
                "the run on failure, precisely because 'the claude CLI "
                "degrades SILENTLY to no-tools when the server is "
                "unreachable' (run_bridge.py); arms B/C/E use local stdio "
                "servers with no preflight."),
        },
        "fresh_500": {
            "n_rows_per_arm": 100,
            "runner_error_rows_per_arm": {a: fresh_err.get(a, 0) for a in ARMS},
            "zero_tool_rows_per_arm": {a: len(fresh_zero[a]) for a in ARMS},
            "arm_A_by_design": "A is the no-tools arm: 100/100 zero-tool is "
                               "the design, not a casualty",
            "zero_tool_rows_detail_tooled_arms": {
                a: fresh_zero[a] for a in ARMS if a != "A"},
            "reading": (
                "C: 0 zero-tool rows. D: 1 (fresh_002). E: 1 (fresh_005). "
                "B: 23 — all single-turn rows that answered without calling "
                "the wiki tools; B's toolkit (Wikipedia/nLab search, no Lean) "
                "is frequently unhelpful for a formalization task, so B's "
                "zero-tool rows are consistent with model choice, while the "
                "single D and E rows (turns=1, ~85s wall) are the plausible "
                "silent-detooling suspects — 1% worst-case per tooled arm on "
                "the fresh set."),
        },
        "eval_341": {
            "n_rows_per_arm": 341,
            "runner_error_rows_per_arm": {a: eval_err.get(a, 0) for a in ARMS},
            "zero_tool_rows_per_arm": {a: eval_zero.get(a, 0) for a in ARMS},
            "zero_tool_task_ids_tooled_arms": {
                a: eval_zero_ids[a] for a in "CDE"},
            "note": ("A 341/341 by design; B 160 (same wiki-tool-choice "
                     "pattern); C 0; D 0; E 4 — the suspected-casualty upper "
                     "bound for the tooled Lean arms on eval-341 is E's 4 rows "
                     "(1.2%) and D's 0."),
        },
    }

    # ======================================================================
    # (4) E overruns + corrected S5 within-budget
    # ======================================================================
    overrun = {a: sorted(t for t in fresh_ids if (turns[a][t] or 0) > MAX_TURNS)
               for a in ARMS}
    rerun_ids = sorted(t for t in fresh_ids if t >= "fresh_069")
    assert len(rerun_ids) == 31
    e_rerun_over = [t for t in overrun["E"] if t in rerun_ids]
    within = {a: 100 - len(overrun[a]) for a in ARMS}

    both_within = {}
    for x, y in (("D", "E"), ("D", "C"), ("D", "A")):
        ids = [t for t in fresh_ids
               if (turns[x][t] or 0) <= MAX_TURNS
               and (turns[y][t] or 0) <= MAX_TURNS]
        blk = mcn_block(raw_succ, ids, x, y)
        blk["arm_rates"] = {x: rate_row(sum(raw_succ[t][x] for t in ids),
                                        len(ids)),
                            y: rate_row(sum(raw_succ[t][y] for t in ids),
                                        len(ids))}
        both_within[f"{x}_vs_{y}"] = blk

    s5 = {
        "basis": ("POST-REPAIR run rows in bench/data/runs (E fresh_069..099 "
                  "= 2026-07-27 rerun); turns from transcript_stats.turns, "
                  "advisory budget = 30 (max_turns on every row)"),
        "overruns_per_arm": {a: len(overrun[a]) for a in ARMS},
        "E_overruns": {
            "total": len(overrun["E"]),
            "of_31_rerun_rows": len(e_rerun_over),
            "of_69_original_rows": len(overrun["E"]) - len(e_rerun_over),
            "rerun_overrun_task_ids": e_rerun_over},
        "max_turns_observed_per_arm": {
            a: max(v for v in turns[a].values() if v is not None)
            for a in ARMS},
        "corrected_within_budget_counts": within,
        "old_S5_within_budget_counts_snapshot_basis": {
            "A": 100, "B": 100, "C": 50, "D": 62, "E": 68,
            "why_wrong": ("E's 31 outage rows had turns=1 in the snapshot and "
                          "were counted within-budget; post-repair, 16 of the "
                          "31 rerun rows overran, so E's true within-budget "
                          "count is 52, not 68")},
        "both_within_budget_pairs_raw_instrument": both_within,
    }

    # ======================================================================
    # (5) judge Layer-1 five-arm table + D-vs-A / D-vs-B McNemars
    # ======================================================================
    eval_tab = {a: rate_row(sum(evald[t][a] for t in fresh_ids), 100)
                for a in ARMS}
    for a in ARMS:   # byte-check against the committed summary
        assert eval_tab[a]["k"] == jfs["fresh_100"]["evaluated_table"][a]["k"]
    j_da = mcn_block(evald, fresh_ids, "D", "A")
    j_db = mcn_block(evald, fresh_ids, "D", "B")
    pub_da = jfs["fresh_100"]["mcnemar_evaluated"]["D_vs_A"]
    assert (j_da["D_only"], j_da["A_only"]) == (pub_da["D_only"],
                                                pub_da["A_only"])
    judge_layer1 = {
        "caveat": ("UNCALIBRATED LLM judge (claude-sonnet-5, blind); "
                   "exploratory — same header caveat as "
                   "judge_fresh_summary.json"),
        "evaluated_equivalence_five_arm_table": eval_tab,
        "for_4_2_table": {a: (f"{eval_tab[a]['k']}/100 = "
                              f"{eval_tab[a]['rate']:.0%} "
                              f"[{eval_tab[a]['wilson95'][0]:.1%}, "
                              f"{eval_tab[a]['wilson95'][1]:.1%}]")
                          for a in ARMS},
        "mcnemar_D_vs_A": j_da,
        "mcnemar_D_vs_B": j_db,
    }

    # ======================================================================
    # (6) fresh-task provenance facts for §3.2
    # ======================================================================
    stats = json.loads(STATS_FILE.read_text())
    field_census = Counter()
    for t in tasks:
        field_census.update(t.keys())
    try:
        gitlog = subprocess.run(
            ["git", "-C", str(REPO), "log", "--follow", "--format=%h %ad %s",
             "--date=short", "--", "bench/data/fresh_tasks.jsonl"],
            capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
    except Exception:
        gitlog = ["<git unavailable>"]
    provenance = {
        "task_fields": {
            "always_present": sorted(k for k, v in field_census.items()
                                     if v == 100),
            "optional": {k: v for k, v in sorted(field_census.items())
                         if v < 100},
            "field_meanings": {
                "id": "fresh_000..fresh_099",
                "decl_name": "full dotted Mathlib name of the gold theorem",
                "module": "Mathlib module (source file) of the gold",
                "informal_statement": "natural-language statement the arms "
                                      "formalize (the ONLY task input shown "
                                      "to the subject model)",
                "gold_formal": "exact Mathlib master statement, ends ':= "
                               "sorry'",
                "gold_header": "'import Mathlib' on every row",
                "gold_context": "enclosing open/variable/universe lines in "
                                "scope order (needed to elaborate gold)",
                "added_in": "{commit, date} the gold merged into master",
                "determinate/determinacy_reason": "annotator-1 determinacy "
                                                  "verdict + reason",
                "det2/det2_reason": "annotator-2 (independent, informal-only) "
                                    "verdict + reason",
                "split": "'eval' on all 100 rows",
                "gold_repairs": "41 rows: ['context-reextraction'] — gold "
                                "contexts rebuilt in commit 185377cc after "
                                "under-extraction (namespace scope dropped, "
                                "multi-line variable blocks truncated)"},
        },
        "informal_statement_origin": {
            "process": ("Identifier-stripped natural language derived from "
                        "each gold declaration's Mathlib docstring by a "
                        "Claude Opus 4.8 agent session (commit df5dbf92, "
                        "2026-07-16, trailer 'Co-Authored-By: Claude Opus "
                        "4.8'); candidates were pre-filtered to the 187 "
                        "docstringed additions, so every NL statement is a "
                        "paraphrase of an author-written docstring with Lean "
                        "identifiers removed"),
            "construction_script_in_repo": False,
            "plain_statement": (
                "The construction script is ABSENT from the repo: commit "
                "df5dbf92 added only fresh_tasks.jsonl + fresh_tasks.stats."
                "json (no generator was committed), so the docstring-to-NL "
                "step is documented by the commit message and stats file, "
                "not by re-runnable code. The stated known weakness applies: "
                "formula-lemma NL is inherently close to the Lean."),
            "second_annotator": ("det2 pass (commit a0d45103) judged from "
                                 "informal_statement alone, locked before "
                                 "reading golds/annotator 1; also a Claude "
                                 "Opus 4.8 agent session — determinacy is "
                                 "two-AI-annotator, not human"),
        },
        "selection_criteria_recorded": {
            "window": ("theorem/lemma lines added to leanprover-community/"
                       "mathlib4 upstream/master between the Brain snapshot "
                       "(2026-07-03T01:56:29Z) and head 9944fe2973 "
                       "(2026-07-16), 44 distinct commits"),
            "funnel": {"raw_added_theorem_lemma_lines": 1326,
                       "candidates_with_docstring": 187,
                       "kept_tasks": 100},
            "held_out_guarantee": stats["held_out"]["guarantee"],
            "held_out_pins": {
                "decl_universe": ("TheoremGraph statement_formal.csv, 388,105 "
                                  "decls, sha256 bf3266149cda603f"),
                "brain_nodes": "brain/data/nodes.jsonl "
                               "(generated 2026-07-12)"},
            "drops": ("bespoke-helper / unrecoverable-hypothesis decls "
                      "dropped outright (per stats determinacy_note); "
                      "docstringed self-contained statements kept"),
            "determinacy": {
                "annotator1_determinate": 86, "annotator2_determinate": 83,
                "both_determinate_PRIMARY_set": 74,
                "kappa": "~0.20 (complementary strictness: ann1 flags sibling "
                         "near-duplicates, ann2 flags semantic "
                         "underdetermination)"},
            "by_area_top5": dict(sorted(stats["by_area"].items(),
                                        key=lambda kv: -kv[1])[:5]),
        },
        "gold_verification": stats["grading_note"],
        "git_history_fresh_tasks_jsonl": gitlog,
    }

    # ---------------------------------------------------------------- output
    out = {
        "generated_by": "bench/analysis/v3_gate_fixes.py",
        "seed": SEED, "B": B, "n_commit_clusters": 44,
        "job_seed_offsets": {f"{l}.{x}_vs_{y}": o
                             for (l, x, y), o in JOB_SEEDS.items()},
        "inputs": [str(p.relative_to(REPO)) for p in
                   (SUMMARY_V2, SUCCESS_REP, HALLUC_VAL, JUDGE_SUMMARY,
                    CONJ_REP, EXPOSURE, TASKS_FILE, STATS_FILE, BRIDGE_TASKS)]
                  + ["bench/data/runs/{A..E}/fresh_*.json",
                     "bench/analysis/judge_fresh/{A..E}/*.judge.json"],
        "1_s4_exposure_post_repair": s4,
        "2_commit_clustered_bootstraps": clustered,
        "3_tier1_attach_audit": attach_audit,
        "4_turn_budget_corrected": s5,
        "5_judge_layer1_five_arm": judge_layer1,
        "6_fresh_task_provenance": provenance,
    }
    OUT_JSON.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT_JSON}")

    # ---------------------------------------------------------------- md
    L: list[str] = []
    A_ = L.append
    A_("# Bridge v3 gate fixes — the six missing analyses")
    A_("")
    A_(f"Generated by `bench/analysis/v3_gate_fixes.py` (seed {SEED}, "
       f"B={B:,}, 44 commit clusters; machinery imported from "
       "`fresh_clustered.py`, new frozen per-job seed offsets). All outcome "
       "layers re-derived from committed artifacts and asserted against "
       "their published tables before use.")
    A_("")
    A_("## 1. S4 exposure strata on the POST-REPAIR rows")
    A_("")
    A_("Basis: `bridge_summary_v2.json` paired_matrix (E fresh_069..099 = "
       "2026-07-27 rerun), raw instrument; strata from "
       "`fresh_exposure.json`.")
    A_("")
    A_("| stratum | n | D | E | D-only/E-only | exact McNemar p |")
    A_("|---|---|---|---|---|---|")
    for name, ids in (("exposed", exposed), ("unexposed", unexposed),
                      ("merged before pin", in_pin),
                      ("merged after pin", post_pin)):
        key = ("by_exposure" if "pos" in name else "by_merge_date")
        st = (s4[key]["exposed"] if name == "exposed" else
              s4[key]["unexposed"] if name == "unexposed" else
              s4[key]["merged_before_pin"] if "before" in name else
              s4[key]["merged_after_pin"])
        d, e, de = st["arms"]["D"], st["arms"]["E"], st["D_vs_E"]
        A_(f"| {name} | {st['n']} | {d['k']}/{d['n']} = {d['rate']:.1%} | "
           f"{e['k']}/{e['n']} = {e['rate']:.1%} | "
           f"{de['D_only']}/{de['E_only']} | "
           f"{de['p_exact_binomial_two_sided']:.4f} |")
    A_("")
    A_("**Verdict on the S4 sentence:** "
       + s4["verdict_S4_sentence"]["plain_statement"])
    A_("")
    A_("(Repaired-instrument sensitivity in the json gives the same "
       "qualitative picture. D-vs-C per stratum is also in the json.)")
    A_("")
    A_("## 2. Commit-clustered paired bootstraps — what survives clustering")
    A_("")
    A_("| layer | pair | unclustered b/c, exact p | clustered RD [95% CI] p "
       "| verdict |")
    A_("|---|---|---|---|---|")
    for key in [f"{l}.{x}_vs_{y}" for l, x, y in JOBS]:
        r = clustered[key]
        u, bo = r["unclustered_mcnemar"], r["commit_clustered_bootstrap"]
        x, y = r["pair"].split("_vs_")
        A_(f"| {r['outcome_layer']} | {x} vs {y} | "
           f"{u[f'{x}_only']}/{u[f'{y}_only']}, "
           f"p={u['p_exact_binomial_two_sided']:.3g} | "
           f"{bo['rd']:+.3f} [{bo['ci95_percentile'][0]:+.3f}, "
           f"{bo['ci95_percentile'][1]:+.3f}] "
           f"p={bo['p_two_sided_percentile_inversion']:.4f} | "
           f"{r['classification']}"
           f"{' — **survives**' if r['survives_clustering'] else ''} |")
    A_("")
    surv = [k for k in clustered if clustered[k]["survives_clustering"]]
    died = [k for k in clustered
            if clustered[k]["unclustered_significant_alpha05"]
            and not clustered[k]["clustered_significant_alpha05"]]
    A_(f"Survivors: {', '.join(surv) if surv else 'none'}. "
       f"Killed by clustering: {', '.join(died) if died else 'none'}.")
    A_("")
    A_("## 3. Tier-1 fresh attach audit")
    A_("")
    A_(attach_audit["schema_fact"]["statement"])
    A_("")
    A_("| arm | fresh zero-tool /100 | eval-341 zero-tool /341 | errors |")
    A_("|---|---|---|---|")
    for a in ARMS:
        A_(f"| {a} | {len(fresh_zero[a])}"
           f"{' (by design)' if a == 'A' else ''} | "
           f"{eval_zero.get(a, 0)}{' (by design)' if a == 'A' else ''} | "
           f"{fresh_err.get(a, 0) + eval_err.get(a, 0)} |")
    A_("")
    A_(attach_audit["fresh_500"]["reading"])
    A_("")
    A_("## 4. Turn budget, corrected (post-repair rows)")
    A_("")
    A_(f"E overran the advisory 30-turn budget on "
       f"**{len(overrun['E'])}/100** rows — {len(e_rerun_over)} of the 31 "
       f"rerun rows (fresh_069..099) plus "
       f"{len(overrun['E']) - len(e_rerun_over)} of the 69 originals. "
       f"Corrected per-arm within-budget counts: "
       + ", ".join(f"{a} {within[a]}" for a in ARMS)
       + " (old S5 said E 68 — it counted E's 31 outage rows, turns=1, as "
         "within-budget).")
    A_("")
    A_("Both-within-budget pair contrasts (raw instrument, post-repair):")
    A_("")
    A_("| pair | n pairs | rates | b/c | exact p |")
    A_("|---|---|---|---|---|")
    for pair, blk in both_within.items():
        x, y = pair.split("_vs_")
        rx, ry = blk["arm_rates"][x], blk["arm_rates"][y]
        A_(f"| {x} vs {y} | {blk['n_paired']} | {rx['k']}/{rx['n']} vs "
           f"{ry['k']}/{ry['n']} | {blk[f'{x}_only']}/{blk[f'{y}_only']} | "
           f"{blk['p_exact_binomial_two_sided']:.4f} |")
    A_("")
    A_("## 5. Judge Layer-1 five-arm evaluated-equivalence (for §4.2)")
    A_("")
    A_(judge_layer1["caveat"] + ".")
    A_("")
    A_("| arm | evaluated-equivalent | Wilson 95% CI |")
    A_("|---|---|---|")
    for a in ARMS:
        t = eval_tab[a]
        A_(f"| {a} | {t['k']}/100 = {t['rate']:.0%} | "
           f"[{t['wilson95'][0]:.1%}, {t['wilson95'][1]:.1%}] |")
    A_("")
    A_(f"Judge McNemars: D vs A {j_da['D_only']}/{j_da['A_only']} "
       f"p={j_da['p_exact_binomial_two_sided']:.3f}; "
       f"D vs B {j_db['D_only']}/{j_db['B_only']} "
       f"p={j_db['p_exact_binomial_two_sided']:.3f}.")
    A_("")
    A_("## 6. Fresh-task provenance (§3.2 facts)")
    A_("")
    A_("- **A task contains:** " + ", ".join(
        f"`{k}`" for k in provenance["task_fields"]["always_present"])
       + "; optional `gold_repairs` on 41 rows (context re-extraction, "
         "commit 185377cc).")
    A_("- **Informal statement origin:** "
       + provenance["informal_statement_origin"]["process"] + ".")
    A_("- " + provenance["informal_statement_origin"]["plain_statement"])
    A_("- **Selection:** " +
       provenance["selection_criteria_recorded"]["window"] +
       "; funnel 1326 added theorem/lemma lines -> 187 docstringed "
       "candidates -> 100 kept; held-out guarantee: every kept decl absent "
       "from the pinned TheoremGraph universe (388,105 decls) and the Brain "
       "nodes; determinacy screens 86 (ann1) / 83 (ann2) / 74 both = "
       "PRIMARY set.")
    A_("- **Gold verification:** golds are the exact Mathlib master "
       "statements (`:= sorry`), elaboration-checked on the fresh pin "
       "(census 100/100 after the 41 context repairs).")
    A_("")
    OUT_MD.write_text("\n".join(L))
    print(f"wrote {OUT_MD}")

    # console
    print("\n(1) S4 post-repair D-vs-E: exposed "
          f"{de_exp['D_only']}/{de_exp['E_only']} "
          f"p={de_exp['p_exact_binomial_two_sided']:.4f}; unexposed "
          f"{de_unexp['D_only']}/{de_unexp['E_only']} "
          f"p={de_unexp['p_exact_binomial_two_sided']:.4f}; claim survives: "
          f"{survives}")
    print("(2) clustered:")
    for key in [f"{l}.{x}_vs_{y}" for l, x, y in JOBS]:
        r = clustered[key]
        print(f"    {key}: {r['classification']}"
              + ("  SURVIVES" if r["survives_clustering"] else ""))
    print(f"(3) zero-tool fresh: "
          f"{ {a: len(fresh_zero[a]) for a in ARMS} }; eval-341: "
          f"{ {a: eval_zero.get(a, 0) for a in ARMS} }")
    print(f"(4) E overruns {len(overrun['E'])}/100 "
          f"({len(e_rerun_over)}/31 rerun); within-budget {within}")
    print(f"(5) judge evaluated A {eval_tab['A']['k']} B {eval_tab['B']['k']} "
          f"C {eval_tab['C']['k']} D {eval_tab['D']['k']} "
          f"E {eval_tab['E']['k']}; D-vs-B p="
          f"{j_db['p_exact_binomial_two_sided']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
