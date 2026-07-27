#!/usr/bin/env python3
"""Tier-1 corrective reanalysis for the Bridge Experiment report v2.

Every number in tier1_reanalysis.{json,md} is produced by this script.

Data policy (per the v2 correction brief):
  - Analyzes ONLY the snapshot in bench/analysis/snapshot_fresh_orig/ (copied
    from bench/data/runs/{A..E}/fresh_*.json before any concurrent E-repair job
    could rewrite rows), cross-checked against
    bench/data/runs_E_fresh_429_archive/ (the 31 originally-errored E rows).
    If a snapshot row differs from its archive copy, the ARCHIVE copy is used.
  - `success` (grounded-typecheck: produced ∧ no error ∧ zero hallucinated
    citations ∧ typecheck ok) is NOT recomputable offline — score_bridge.py
    recomputes typecheck at scoring time against the pinned Lean toolchains
    (nothing is stored in the run rows). Per the brief, per-task outcomes are
    therefore taken from bench/data/bridge_summary.json's paired_matrix, which
    was produced by score_bridge.py with typecheck folded in
    (success_metric: "produced ∧ no-halluc ∧ TYPECHECK").
  - As a consistency guard, the success_proxy leg (produced ∧ no error ∧ zero
    hallucinated citations) IS recomputed here for every fresh row using
    score_bridge.py's own Oracle + extract_cited (imported, not reimplemented),
    and we assert matrix-success ⇒ recomputed-proxy for all 500 fresh rows.

Analyses:
  1. Completed-pairs fresh analysis: the 31 E rows with 429 errors; the
     fresh-set arm table on the 69 tasks where E completed, all five arms;
     exact-binomial McNemar D-vs-E, D-vs-C, D-vs-A on those 69 pairs.
  2. Wilson 95% CIs for every per-arm rate (fresh-100, completed-69, eval-341).
  3. Turn-budget sensitivity: turns <= 30 in BOTH arms of each pair.
  4. Effect sizes: paired-Wald absolute risk differences D-vs-E (both bases).

Run:  python3 bench/analysis/tier1_reanalysis.py
"""
from __future__ import annotations

import json
import sys
from math import comb, sqrt
from pathlib import Path

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                              # bench/
sys.path.insert(0, str(BENCH))
import score_bridge  # noqa: E402  (reuse Oracle + extract_cited identically)

SNAP = HERE / "snapshot_fresh_orig"
ARCHIVE = BENCH / "data" / "runs_E_fresh_429_archive"
SUMMARY = BENCH / "data" / "bridge_summary.json"
TASKS = BENCH / "data" / "bridge_tasks.jsonl"
OUT_JSON = HERE / "tier1_reanalysis.json"
OUT_MD = HERE / "tier1_reanalysis.md"

ARMS = ["A", "B", "C", "D", "E"]
Z = 1.959963984540054  # 97.5th percentile of N(0,1)


# --------------------------------------------------------------------------- #
# Statistics                                                                    #
# --------------------------------------------------------------------------- #
def wilson_ci(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


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


# --------------------------------------------------------------------------- #
# Data loading                                                                  #
# --------------------------------------------------------------------------- #
def load_snapshot() -> tuple[dict[str, dict[str, dict]], list[str], list[str]]:
    """arm -> task_id -> row. E rows 069-099 are cross-checked against the
    429 archive; archive wins on any byte difference (returns list of diffs)."""
    rows: dict[str, dict[str, dict]] = {a: {} for a in ARMS}
    archive_diffs: list[str] = []
    archive_used: list[str] = []
    for arm in ARMS:
        for f in sorted((SNAP / arm).glob("fresh_*.json")):
            data = f.read_bytes()
            if arm == "E":
                af = ARCHIVE / f.name
                if af.exists():
                    adata = af.read_bytes()
                    if adata != data:
                        archive_diffs.append(f.name)
                        data = adata          # archive copy is authoritative
                        archive_used.append(f.name)
            row = json.loads(data)
            rows[arm][row.get("task_id") or f.stem] = row
    return rows, archive_diffs, archive_used


def load_matrix() -> tuple[dict[str, dict[str, bool | None]], str]:
    s = json.loads(SUMMARY.read_text())
    return s["paired_matrix"], s["success_metric"]


def load_eval_ids() -> list[str]:
    ids = []
    for line in TASKS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if "_meta" in r:
            continue
        if r.get("split") == "eval":
            ids.append(r["id"])
    return sorted(ids)


# --------------------------------------------------------------------------- #
# Table builders                                                                #
# --------------------------------------------------------------------------- #
def arm_table(matrix: dict, task_ids: list[str],
              errors_by_arm: dict[str, set[str]] | None = None) -> dict:
    out = {}
    for arm in ARMS:
        vals = [matrix[t].get(arm) for t in task_ids]
        n = sum(1 for v in vals if v is not None)
        k = sum(1 for v in vals if v is True)
        lo, hi = wilson_ci(k, n)
        row = {"k": k, "n": n, "rate": round(k / n, 4) if n else None,
               "wilson95": [round(lo, 4), round(hi, 4)]}
        if errors_by_arm is not None:
            row["errors"] = len(errors_by_arm[arm] & set(task_ids))
        out[arm] = row
    return out


def mcnemar_block(matrix: dict, task_ids: list[str], x: str, y: str) -> dict:
    both = xonly = yonly = neither = 0
    for t in task_ids:
        sx, sy = bool(matrix[t].get(x)), bool(matrix[t].get(y))
        if sx and sy:
            both += 1
        elif sx:
            xonly += 1
        elif sy:
            yonly += 1
        else:
            neither += 1
    return {"pair": f"{x}_vs_{y}", "n_paired": len(task_ids),
            "both_success": both, f"{x}_only": xonly, f"{y}_only": yonly,
            "neither": neither, "discordant": xonly + yonly,
            "p_exact_binomial_two_sided": round(mcnemar_exact(xonly, yonly), 6)}


# --------------------------------------------------------------------------- #
# main                                                                          #
# --------------------------------------------------------------------------- #
def main() -> int:
    rows, archive_diffs, archive_used = load_snapshot()
    matrix, success_metric = load_matrix()
    eval_ids = load_eval_ids()
    fresh_ids = sorted(t for t in matrix if t.startswith("fresh_"))
    assert len(fresh_ids) == 100, f"expected 100 fresh tasks, got {len(fresh_ids)}"
    assert len(eval_ids) == 341, f"expected 341 eval tasks, got {len(eval_ids)}"

    # -- error census -------------------------------------------------------- #
    errors_by_arm: dict[str, set[str]] = {}
    for arm in ARMS:
        errors_by_arm[arm] = {t for t, r in rows[arm].items() if r.get("error")}
    e_err = sorted(errors_by_arm["E"])
    assert all(not errors_by_arm[a] for a in "ABCD"), \
        f"A-D expected zero fresh errors: { {a: sorted(errors_by_arm[a]) for a in 'ABCD'} }"
    e_429 = [t for t in e_err if "429" in (rows["E"][t].get("error") or "")]
    assert len(e_err) == 31 and e_429 == e_err, (len(e_err), e_429)
    completed_ids = sorted(t for t in fresh_ids if t not in errors_by_arm["E"])
    assert len(completed_ids) == 69

    # -- consistency guard: matrix-success ⇒ recomputed proxy ---------------- #
    oracle = score_bridge.Oracle(enabled=True)
    proxy_checked = proxy_violations = 0
    for arm in ARMS:
        for t in fresh_ids:
            r = rows[arm][t]
            lean = r.get("output_lean")
            halluc = [n for n in score_bridge.extract_cited(lean)
                      if oracle.classify(n) == "hallucinated"]
            proxy = bool(lean) and not bool(r.get("error")) and not halluc
            proxy_checked += 1
            if matrix[t].get(arm) is True and not proxy:
                proxy_violations += 1
    assert proxy_violations == 0, f"{proxy_violations} matrix/snapshot mismatches"

    # -- (1) fresh tables + McNemar ------------------------------------------ #
    fresh100 = arm_table(matrix, fresh_ids, errors_by_arm)
    completed69 = arm_table(matrix, completed_ids, errors_by_arm)
    mcn100 = {p: mcnemar_block(matrix, fresh_ids, *p.split("_vs_"))
              for p in ("D_vs_E", "D_vs_C", "D_vs_A")}
    mcn69 = {p: mcnemar_block(matrix, completed_ids, *p.split("_vs_"))
             for p in ("D_vs_E", "D_vs_C", "D_vs_A")}

    # -- (2) eval-341 / Tier-1a table ---------------------------------------- #
    eval341 = arm_table(matrix, eval_ids)

    # -- (3) turn-budget sensitivity (turns <= 30 in BOTH arms of the pair) --- #
    def turns(arm: str, t: str) -> int | None:
        return (rows[arm][t].get("transcript_stats") or {}).get("turns")

    sens = {}
    for x, y in (("D", "E"), ("D", "C"), ("D", "A")):
        ids = [t for t in fresh_ids
               if (turns(x, t) or 0) <= 30 and (turns(y, t) or 0) <= 30]
        sens[f"{x}_vs_{y}"] = {
            "n": len(ids),
            "arm_rates": {a: fresh_rate(matrix, ids, a) for a in (x, y)},
            "mcnemar": mcnemar_block(matrix, ids, x, y)}
    # D-vs-E additionally restricted to E-completed pairs
    ids_de_comp = [t for t in completed_ids
                   if (turns("D", t) or 0) <= 30 and (turns("E", t) or 0) <= 30]
    sens["D_vs_E_completed_only"] = {
        "n": len(ids_de_comp),
        "arm_rates": {a: fresh_rate(matrix, ids_de_comp, a) for a in ("D", "E")},
        "mcnemar": mcnemar_block(matrix, ids_de_comp, "D", "E")}
    # per-arm own-budget table (each arm restricted to its own turns<=30 runs)
    per_arm_own = {}
    for arm in ARMS:
        ids = [t for t in fresh_ids if (turns(arm, t) or 0) <= 30]
        per_arm_own[arm] = dict(fresh_rate(matrix, ids, arm), n_within_budget=len(ids))

    # -- (4) effect sizes: D-vs-E absolute risk differences ------------------- #
    def rd_block(ids: list[str]) -> dict:
        m = mcnemar_block(matrix, ids, "D", "E")
        w = paired_rd_wald(m["D_only"], m["E_only"], m["n_paired"])
        return {"n": m["n_paired"],
                "rate_D": fresh_rate(matrix, ids, "D")["rate"],
                "rate_E": fresh_rate(matrix, ids, "E")["rate"],
                "rd_D_minus_E": round(w["rd"], 4), "se": round(w["se"], 4),
                "rd_ci95": [round(w["ci95"][0], 4), round(w["ci95"][1], 4)],
                "method": w["method"]}
    effect_sizes = {"fresh_100_errors_as_failures": rd_block(fresh_ids),
                    "completed_69": rd_block(completed_ids)}

    result = {
        "generated_by": "bench/analysis/tier1_reanalysis.py",
        "data_policy": {
            "snapshot_dir": str(SNAP),
            "outcome_source": str(SUMMARY),
            "success_metric": success_metric,
            "archive_cross_check": {
                "archive_dir": str(ARCHIVE),
                "rows_compared": 31,
                "rows_differing_archive_used": archive_used,
                "diffs_found": archive_diffs},
            "proxy_consistency_rows_checked": proxy_checked,
            "proxy_consistency_violations": proxy_violations},
        "e_429_rows": {"count": len(e_err), "task_ids": e_err,
                       "all_are_429": e_429 == e_err},
        "fresh_error_counts": {a: len(errors_by_arm[a]) for a in ARMS},
        "fresh_100_table": fresh100,
        "completed_69_table": completed69,
        "eval_341_table": eval341,
        "mcnemar_fresh_100": mcn100,
        "mcnemar_completed_69": mcn69,
        "turn_budget_sensitivity_le30": {
            "pairwise": sens, "per_arm_own_budget": per_arm_own},
        "effect_sizes_D_vs_E": effect_sizes,
    }
    OUT_JSON.write_text(json.dumps(result, indent=2) + "\n")
    OUT_MD.write_text(render_md(result))
    print(f"wrote {OUT_JSON}\nwrote {OUT_MD}")
    return 0


def fresh_rate(matrix: dict, ids: list[str], arm: str) -> dict:
    k = sum(1 for t in ids if matrix[t].get(arm) is True)
    n = len(ids)
    lo, hi = wilson_ci(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None,
            "wilson95": [round(lo, 4), round(hi, 4)]}


# --------------------------------------------------------------------------- #
# Markdown                                                                      #
# --------------------------------------------------------------------------- #
def _tbl(table: dict, extra_err: bool = False) -> str:
    hdr = "| arm | k/n | rate | Wilson 95% CI |" + (" errors |" if extra_err else "")
    sep = "|---|---|---|---|" + ("---|" if extra_err else "")
    lines = [hdr, sep]
    for a, r in table.items():
        row = (f"| {a} | {r['k']}/{r['n']} | {r['rate']:.3f} "
               f"| [{r['wilson95'][0]:.3f}, {r['wilson95'][1]:.3f}] |")
        if extra_err:
            row += f" {r.get('errors', 0)} |"
        lines.append(row)
    return "\n".join(lines)


def _mcn(block: dict) -> str:
    x, y = block["pair"].split("_vs_")
    return (f"- **{x} vs {y}** (n={block['n_paired']}): both={block['both_success']}, "
            f"{x}-only={block[f'{x}_only']}, {y}-only={block[f'{y}_only']}, "
            f"neither={block['neither']} → exact two-sided p = "
            f"**{block['p_exact_binomial_two_sided']:.4f}**")


def render_md(r: dict) -> str:
    L: list[str] = []
    L.append("# Bridge Experiment Tier-1 — corrective reanalysis (report v2)\n")
    L.append(f"Reproduce: `python3 bench/analysis/tier1_reanalysis.py` "
             f"(outcomes from `bridge_summary.json` paired_matrix; metric: "
             f"{r['data_policy']['success_metric']}).\n")
    dp = r["data_policy"]
    L.append("## Data provenance\n")
    L.append(f"- Snapshot of all 500 fresh rows analyzed from `{dp['snapshot_dir']}` "
             "(taken before the concurrent arm-E repair job could rewrite rows).")
    ac = dp["archive_cross_check"]
    L.append(f"- The 31 E rows were cross-checked byte-for-byte against the 429 "
             f"archive: {len(ac['diffs_found'])} differed"
             + (f" (archive copies used: {ac['rows_differing_archive_used']})."
                if ac["diffs_found"] else " — snapshot ≡ archive."))
    L.append(f"- Consistency guard: success_proxy recomputed for all "
             f"{dp['proxy_consistency_rows_checked']} fresh rows with "
             f"score_bridge.py's Oracle/extract_cited; "
             f"{dp['proxy_consistency_violations']} matrix contradictions.")
    L.append(f"- Typecheck is recomputed at scoring time by score_bridge.py (not "
             "stored per-row), so per-task success outcomes are reused from the "
             "typecheck-folded paired_matrix rather than re-typechecked.\n")

    L.append("## 1. The 31 arm-E 429-error rows\n")
    e = r["e_429_rows"]
    L.append(f"Arm E fresh rows errored: **{e['count']}** "
             f"({e['task_ids'][0]}…{e['task_ids'][-1]}, contiguous), all with "
             f"session-limit **429** errors (`all_are_429={e['all_are_429']}`). "
             f"Fresh error counts by arm: "
             + ", ".join(f"{a}={r['fresh_error_counts'][a]}" for a in ARMS)
             + " — A–D verified zero.\n")

    L.append("## 2. Fresh-set grounded-typecheck rate — full 100 "
             "(E errors count as failures)\n")
    L.append(_tbl(r["fresh_100_table"], extra_err=True) + "\n")
    L.append("McNemar (exact binomial, two-sided), all 100 pairs:\n")
    for p in ("D_vs_E", "D_vs_C", "D_vs_A"):
        L.append(_mcn(r["mcnemar_fresh_100"][p]))
    L.append("")

    L.append("## 3. Completed-pairs analysis — the 69 tasks arm E completed\n")
    L.append(_tbl(r["completed_69_table"]) + "\n")
    L.append("McNemar (exact binomial, two-sided) on the 69 completed pairs:\n")
    for p in ("D_vs_E", "D_vs_C", "D_vs_A"):
        L.append(_mcn(r["mcnemar_completed_69"][p]))
    L.append("")

    L.append("## 4. Eval-341 (Tier-1a) grounded-typecheck rate\n")
    L.append(_tbl(r["eval_341_table"]) + "\n")

    L.append("## 5. Turn-budget sensitivity (turns ≤ 30 in BOTH arms of the pair)\n")
    for key in ("D_vs_E", "D_vs_E_completed_only", "D_vs_C", "D_vs_A"):
        s = r["turn_budget_sensitivity_le30"]["pairwise"][key]
        rates = ", ".join(f"{a}: {v['k']}/{v['n']} ({v['rate']:.3f} "
                          f"[{v['wilson95'][0]:.3f}, {v['wilson95'][1]:.3f}])"
                          for a, v in s["arm_rates"].items())
        L.append(f"- **{key}** — n={s['n']} pairs; {rates}; "
                 f"McNemar {s['mcnemar']['discordant']} discordant "
                 f"→ p = {s['mcnemar']['p_exact_binomial_two_sided']:.4f}")
    L.append("\nPer-arm rates on each arm's own turns ≤ 30 runs:\n")
    pa = r["turn_budget_sensitivity_le30"]["per_arm_own_budget"]
    L.append("| arm | within-budget n | k | rate | Wilson 95% CI |")
    L.append("|---|---|---|---|---|")
    for a, v in pa.items():
        L.append(f"| {a} | {v['n_within_budget']} | {v['k']} | {v['rate']:.3f} "
                 f"| [{v['wilson95'][0]:.3f}, {v['wilson95'][1]:.3f}] |")
    L.append("")

    L.append("## 6. Effect sizes — D vs E absolute risk difference\n")
    for name, es in r["effect_sizes_D_vs_E"].items():
        L.append(f"- **{name}** (n={es['n']}): D {es['rate_D']:.3f} vs "
                 f"E {es['rate_E']:.3f} → RD = **{es['rd_D_minus_E']:+.3f}** "
                 f"95% CI [{es['rd_ci95'][0]:+.3f}, {es['rd_ci95'][1]:+.3f}] "
                 f"({es['method']})")
    L.append("")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
