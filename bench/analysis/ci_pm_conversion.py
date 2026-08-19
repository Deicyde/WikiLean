#!/usr/bin/env python3
"""ci_pm_conversion.py — conversion table for the v3.1 ±-format restyle of the
main Bridge paper (docs/research/BRIDGE-REPORT.md / report.tex).

Every confidence-interval presentation in the MAIN paper was reformatted as
"point ± half-width (95% confidence)", where the ± value is the WIDER
half-width of the underlying asymmetric interval (Wilson score interval for
single rates; bootstrap percentile interval for paired differences), computed
from the EXACT bounds stored in the analysis JSONs — never from the rounded
bounds previously printed in the paper. The supplement keeps the exact
[lo, hi] presentations.

For every converted value this script records: the quantity, the source file
and JSON path of the exact bounds, the exact point and bounds, the two
half-widths, the wider half-width (pm_exact), and the printed ± at the
display precision used in the paper. It asserts, for every entry:
  (1) lo <= point <= hi                      (point inside the interval)
  (2) pm_exact >= point - lo  and  pm_exact >= hi - point
Output: bench/analysis/ci_pm_conversion.json

Four entries' bounds are Wilson intervals recomputed (z=1.96, the same
convention as every stored wilson95 in these JSONs) from counts stored in the
JSONs, because the generator stored the counts but not the bounds:
the in-sample audit precision (tp=4, tp+fp=30 in halluc_validation.json) and
the three SorryDB per-arm Wilson rates (proved k / 171 in
retrieval_clustered.json). All other bounds are read verbatim.
"""
import json
import math
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

HERE = Path(__file__).resolve().parent
Z = 1.96  # matches the wilson95 convention used throughout these JSONs


def wilson(k, n, z=Z):
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def load(name):
    return json.load(open(HERE / name))


sr = load("success_repaired.json")
v3 = load("v3_gate_fixes.json")
fa = load("factorial_analysis.json")
jf = load("judge_fresh_summary.json")
hv = load("halluc_validation.json")
hh = load("halluc_holdout.json")
gr = load("grid_repaired.json")
rc = load("retrieval_clustered.json")
ua = load("union_ablation.json")


def rnd(x, decimals):
    q = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    return float(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP))


entries = []


def add(eid, section, quantity, source, point, lo, hi, unit, decimals,
        printed_point, note=None):
    """unit: 'points' (percentage points; point/lo/hi given as proportions)
    or 'metric' (decimal metric units)."""
    assert lo <= point <= hi, f"{eid}: point {point} outside [{lo}, {hi}]"
    # Decimal arithmetic so stored 4-dp bounds subtract exactly (no float ties)
    d_point, d_lo, d_hi = (Decimal(repr(v)) for v in (point, lo, hi))
    half_lo = float(d_point - d_lo)
    half_hi = float(d_hi - d_point)
    pm_exact = max(half_lo, half_hi)
    assert pm_exact >= half_lo and pm_exact >= half_hi
    scale = 100.0 if unit == "points" else 1.0
    pm_printed = rnd(pm_exact * scale, decimals)
    fmt = f"{{:.{decimals}f}}"
    entries.append({
        "id": eid,
        "section": section,
        "quantity": quantity,
        "source": source,
        "point_exact": point,
        "ci95_exact": [lo, hi],
        "half_width_lo": round(half_lo, 6),
        "half_width_hi": round(half_hi, 6),
        "pm_exact_wider_half_width": round(pm_exact, 6),
        "unit": unit,
        "printed": f"{printed_point} ± {fmt.format(pm_printed)}"
                   + (" points" if unit == "points" else ""),
        "pm_printed": pm_printed,
        **({"note": note} if note else {}),
    })
    return pm_printed


# ---------------------------------------------------------------- abstract
ccb = sr["commit_clustered_bootstrap"]["repaired_oracle"]
j = ccb["D_vs_A"]
add("abs_gtc_D_vs_A", "Abstract", "grounded typecheck RD, D − A (commit-clustered)",
    "success_repaired.json#commit_clustered_bootstrap.repaired_oracle.D_vs_A",
    j["rd"], *j["ci95_percentile"], "points", 0, "+27")
j = fa["PRIMARY_grounded_tc_repaired"]["effects"]["join"]
add("abs_join_main", "Abstract", "factorial JOIN main effect",
    "factorial_analysis.json#PRIMARY_grounded_tc_repaired.effects.join",
    j["rd"], *j["ci95_percentile"], "points", 0, "+3")
j = fa["PRIMARY_grounded_tc_repaired"]["effects"]["verifier"]
add("abs_verifier_main", "Abstract", "factorial VERIFIER main effect",
    "factorial_analysis.json#PRIMARY_grounded_tc_repaired.effects.verifier",
    j["rd"], *j["ci95_percentile"], "points", 0, "+5")

# ---------------------------------------------------------------- §4.1
lo, hi = wilson(4, 30)
add("s41_audit_precision_insample", "4.1",
    "raw-oracle flagged-class precision, in-sample blinded audit (4/30)",
    "halluc_validation.json#blinded_validation.strict_noise_as_FP (tp=4, tp+fp=30; Wilson z=1.96 recomputed from counts)",
    4 / 30, lo, hi, "points", 1, "13.3%",
    note="bounds recomputed Wilson from stored counts; matches previously printed [5.3, 29.7]")
j = hh["supplementary"]["binary_agreement_like_for_like"]["repaired"]
add("s41_holdout_agreement", "4.1",
    "repaired-oracle binary agreement, held-out blinded sample (36/40)",
    "halluc_holdout.json#supplementary.binary_agreement_like_for_like.repaired.wilson95",
    j["agree"] / j["n"], *j["wilson95"], "points", 1, "90%")

tbl = sr["fresh_100_table_both_instruments"]["repaired_oracle"]
for arm, label in [("A", "no tools"), ("B", "informal"), ("C", "formal"),
                   ("D", "wikibrain"), ("E", "B+C unjoined")]:
    a = tbl[arm]
    add(f"s41_gtc_arm_{arm}", "4.1",
        f"grounded typecheck rate (repaired), arm {arm} {label} ({a['k']}/{a['n']})",
        f"success_repaired.json#fresh_100_table_both_instruments.repaired_oracle.{arm}.wilson95",
        a["rate"], *a["wilson95"], "points", 1, f"{a['rate']*100:.1f}%")

for pair in ["D_vs_A", "D_vs_C", "D_vs_E"]:
    j = ccb[pair]
    add(f"s41_ccb_{pair}", "4.1",
        f"grounded typecheck RD, {pair.replace('_vs_', ' − ')} (commit-clustered bootstrap)",
        f"success_repaired.json#commit_clustered_bootstrap.repaired_oracle.{pair}",
        j["rd"], *j["ci95_percentile"], "points", 1, f"{j['rd']*100:+.0f}")

j = v3["2_commit_clustered_bootstraps"]["gtc_repaired.E_vs_A"]["commit_clustered_bootstrap"]
add("s41_ccb_E_vs_A", "4.1", "grounded typecheck RD, E − A (commit-clustered bootstrap)",
    "v3_gate_fixes.json#2_commit_clustered_bootstraps.gtc_repaired.E_vs_A.commit_clustered_bootstrap",
    j["rd"], *j["ci95_percentile"], "points", 1, "+16")

# ---------------------------------------------------------------- §4.2
ev = jf["fresh_100"]["evaluated_table"]
for arm in "ABCDE":
    a = ev[arm]
    add(f"s42_judge_eval_arm_{arm}", "4.2",
        f"judge-evaluated equivalence rate, arm {arm} ({a['k']}/{a['n']})",
        f"judge_fresh_summary.json#fresh_100.evaluated_table.{arm}.wilson95",
        a["rate"], *a["wilson95"], "points", 1, f"{a['rate']*100:.1f}%")
j = v3["2_commit_clustered_bootstraps"]["judge_evaluated.D_vs_E"]["commit_clustered_bootstrap"]
add("s42_judge_D_vs_E", "4.2", "judge-evaluated RD, D − E (commit-clustered bootstrap)",
    "v3_gate_fixes.json#2_commit_clustered_bootstraps.judge_evaluated.D_vs_E.commit_clustered_bootstrap",
    j["rd"], *j["ci95_percentile"], "points", 1, "−34")

# ---------------------------------------------------------------- §4.3
hal = hv["adjusted_oracle_sensitivity"]["per_arm"]
for arm, label in [("A", "no tools"), ("B", "informal"), ("C", "formal"),
                   ("D", "wikibrain"), ("E", "B+C unjoined")]:
    a = hal[arm]
    add(f"s43_halluc_arm_{arm}", "4.3",
        f"runs with ≥1 repaired-flagged citation, arm {arm} {label} ({a['runs_any_halluc']}/100)",
        f"halluc_validation.json#adjusted_oracle_sensitivity.per_arm.{arm}.run_wilson95",
        a["run_rate"], *a["run_wilson95"], "points", 1, f"{a['run_rate']*100:.0f}%")
j = v3["2_commit_clustered_bootstraps"]["halluc_repaired.D_vs_A"]["commit_clustered_bootstrap"]
add("s43_halluc_D_vs_A", "4.3", "repaired hallucination RD, D − A (commit-clustered bootstrap)",
    "v3_gate_fixes.json#2_commit_clustered_bootstraps.halluc_repaired.D_vs_A.commit_clustered_bootstrap",
    j["rd"], *j["ci95_percentile"], "points", 1, "−31")

# ---------------------------------------------------------------- §4.4
ft = fa["PRIMARY_grounded_tc_repaired"]["four_arm_table_wilson95"]
for arm, label in [("Ep", "E′ (join−, verifier−)"), ("X", "X (join−, verifier+)"),
                   ("J", "J (join+, verifier−)"), ("Dp", "D′ (join+, verifier+)")]:
    a = ft[arm]
    add(f"s44_gtc_arm_{arm}", "4.4",
        f"factorial grounded typecheck rate (repaired), {label} ({a['k']}/{a['n']})",
        f"factorial_analysis.json#PRIMARY_grounded_tc_repaired.four_arm_table_wilson95.{arm}.wilson95",
        a["rate"], *a["wilson95"], "points", 1, f"{a['rate']*100:.1f}%")
for eff, printed in [("join", "+3.0"), ("verifier", "+5.0"), ("interaction", "−10.0")]:
    j = fa["PRIMARY_grounded_tc_repaired"]["effects"][eff]
    add(f"s44_effect_{eff}", "4.4", f"factorial {eff} effect (commit-clustered bootstrap)",
        f"factorial_analysis.json#PRIMARY_grounded_tc_repaired.effects.{eff}",
        j["rd"], *j["ci95_percentile"], "points", 1, printed)
j = fa["secondary_judge_evaluated"]["effects"]["join"]
add("s44_judge_join", "4.4", "factorial JOIN effect on judge-evaluated equivalence (exploratory)",
    "factorial_analysis.json#secondary_judge_evaluated.effects.join",
    j["rd"], *j["ci95_percentile"], "points", 1, "−26.5")

# ---------------------------------------------------------------- §5 QR table
qr_r = ua["qr810"]["cluster_bootstrap"]["recall@10"]["arms"]
qr_n = ua["qr810"]["cluster_bootstrap"]["ndcg@10"]["arms"]
for arm in ["N", "F", "W", "U", "WF"]:
    a = qr_r[arm]
    add(f"s5_qr_recall_arm_{arm}", "5", f"MathlibQR fair-810 R@10, agent {arm} (decl-clustered)",
        f"union_ablation.json#qr810.cluster_bootstrap.recall@10.arms.{arm}.ci95",
        a["point"], *a["ci95"], "metric", 3, f"{a['point']:.3f}")
    a = qr_n[arm]
    add(f"s5_qr_ndcg_arm_{arm}", "5", f"MathlibQR fair-810 nDCG@10, agent {arm} (decl-clustered)",
        f"union_ablation.json#qr810.cluster_bootstrap.ndcg@10.arms.{arm}.ci95",
        a["point"], *a["ci95"], "metric", 3, f"{a['point']:.3f}")

# §5 prose contrasts (3-decimal presentation)
c = gr["contrasts_repaired"]
j = c["F_vs_W"]["qr810_recall@10"]
add("s5_prose_FW_recall", "5", "F − W, QR R@10 (decl-clustered; prose)",
    "grid_repaired.json#contrasts_repaired.F_vs_W.qr810_recall@10",
    j["point"], *j["ci95"], "metric", 3, "+0.030")
j = c["WF_vs_F"]["qr810_recall@10"]
add("s5_prose_WFF_recall", "5", "WF − F, QR R@10 (decl-clustered; prose)",
    "grid_repaired.json#contrasts_repaired.WF_vs_F.qr810_recall@10",
    j["point"], *j["ci95"], "metric", 3, "+0.040")
j = ua["race_row_sensitivity"]["qr810_WF_vs_W"]
add("s5_prose_WFW_recall", "5", "WF − W, QR R@10 (decl-clustered; prose)",
    "union_ablation.json#race_row_sensitivity.qr810_WF_vs_W.diff_ci95",
    j["diff_point"], *j["diff_ci95"], "metric", 3, "+0.069")
j = c["U_vs_F"]["qr810_recall@10"]
add("s5_prose_UF_recall", "5", "U − F, QR R@10 (decl-clustered; prose)",
    "grid_repaired.json#contrasts_repaired.U_vs_F.qr810_recall@10",
    j["point"], *j["ci95"], "metric", 3, "−0.016")
j = c["WF_vs_U"]["qr810_recall@10"]
add("s5_prose_WFU_recall", "5", "WF − U, QR R@10 (decl-clustered; prose)",
    "grid_repaired.json#contrasts_repaired.WF_vs_U.qr810_recall@10",
    j["point"], *j["ci95"], "metric", 3, "+0.056")

# §5 MPR per-arm (task-bootstrap)
mpr = ua["mpr"]["arms"]
for arm in ["N", "W", "F", "U", "WF"]:
    a = mpr[arm]
    add(f"s5_mpr_arm_{arm}", "5", f"MathlibMPR group-recall@10, agent {arm} (task-bootstrap)",
        f"union_ablation.json#mpr.arms.{arm}.per_task_mean_boot_ci95",
        a["group_recall@10_per_task_mean"], *a["per_task_mean_boot_ci95"],
        "metric", 3, f"{rnd(a['group_recall@10_per_task_mean'], 3):.3f}")
j = c["F_vs_W"]["mpr_group_recall@10"]
add("s5_prose_FW_mpr", "5", "F − W, MPR gR@10 (task-paired; prose)",
    "grid_repaired.json#contrasts_repaired.F_vs_W.mpr_group_recall@10",
    j["point"], *j["ci95"], "metric", 3, "+0.275")
j = c["WF_vs_F"]["mpr_group_recall@10"]
add("s5_prose_WFF_mpr", "5", "WF − F, MPR gR@10 (task-paired; prose)",
    "grid_repaired.json#contrasts_repaired.WF_vs_F.mpr_group_recall@10",
    j["point"], *j["ci95"], "metric", 3, "+0.010")

# §5 final contrast table (4-decimal presentation)
for pair in ["WF_vs_F", "WF_vs_U", "U_vs_F", "F_vs_W"]:
    for metric, mlabel in [("qr810_recall@10", "QR R@10"),
                           ("qr810_ndcg@10", "QR nDCG@10"),
                           ("mpr_group_recall@10", "MPR gR@10")]:
        j = c[pair][metric]
        add(f"s5_tbl_{pair}_{metric}", "5",
            f"{pair.replace('_vs_', ' − ')}, {mlabel} (final contrast table)",
            f"grid_repaired.json#contrasts_repaired.{pair}.{metric}",
            j["point"], *j["ci95"], "metric", 4, f"{j['point']:+.4f}")

# ---------------------------------------------------------------- §6
sdb = rc["sorrydb"]["arms"]
for arm, printed in [("N", "1.2%"), ("F", "5.3%"), ("WF", "5.8%")]:
    a = sdb[arm]
    k, n = a["proved"], a["n_frozen_tasks"]
    lo, hi = wilson(k, n)
    add(f"s6_wilson_arm_{arm}", "6",
        f"SorryDB proved rate, arm {arm} ({k}/{n}; Wilson)",
        f"retrieval_clustered.json#sorrydb.arms.{arm} (proved k/n; Wilson z=1.96 recomputed from counts)",
        k / n, lo, hi, "points", 1, printed,
        note="bounds recomputed Wilson from stored counts")
    add(f"s6_repoclust_arm_{arm}", "6",
        f"SorryDB proved rate, arm {arm} (repo-clustered bootstrap)",
        f"retrieval_clustered.json#sorrydb.arms.{arm}.repo_boot_ci95",
        k / n, *a["repo_boot_ci95"], "points", 1, printed)
j = rc["sorrydb"]["WF_minus_F"]
add("s6_WF_minus_F", "6", "SorryDB WF − F (repo-clustered bootstrap)",
    "retrieval_clustered.json#sorrydb.WF_minus_F.repo_boot_ci95",
    j["point"], *j["repo_boot_ci95"], "points", 2, "+0.58")

# ---------------------------------------------------------------- §7
j = ccb["D_vs_E"]
add("s7_D_vs_E_span", "7", "grounded typecheck RD, D − E (limitations restatement)",
    "success_repaired.json#commit_clustered_bootstrap.repaired_oracle.D_vs_E",
    j["rd"], *j["ci95_percentile"], "points", 1, "+11")

# ---------------------------------------------------------------- output
failures = []  # populated only if an assertion above were soft; asserts are hard
out = {
    "generated_by": "bench/analysis/ci_pm_conversion.py",
    "what": ("Conversion table for the ± (95% confidence) restyle of the main "
             "Bridge paper (v3.1). pm = wider half-width of the exact "
             "asymmetric interval; supplement retains exact bounds."),
    "n_entries": len(entries),
    "assertions": {
        "point_in_interval_failures": 0,
        "pm_covers_both_half_widths_failures": 0,
        "note": "hard-asserted per entry at generation time",
    },
    "rounding": "pm printed ROUND_HALF_UP at each table's display precision",
    "entries": entries,
}
with open(HERE / "ci_pm_conversion.json", "w") as f:
    json.dump(out, f, indent=1, ensure_ascii=False)

print(f"{len(entries)} entries; all point-in-interval and half-width assertions passed")
for e in entries:
    scale = 100.0 if e["unit"] == "points" else 1.0
    print(f"{e['id']:34s} point={e['point_exact']:<9.6g} "
          f"ci=[{e['ci95_exact'][0]:.6g}, {e['ci95_exact'][1]:.6g}] "
          f"pm={e['pm_exact_wider_half_width']*scale:.4g} -> {e['printed']}")
