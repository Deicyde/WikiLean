#!/usr/bin/env python3
"""Part 2 summarizer: aggregate the blind LLM-judge verdicts in
bench/analysis/judge_fresh/ into judge_fresh_summary.{json,md}.

HONESTY NOTE (carried into both outputs): the judge is an UNCALIBRATED LLM
(claude-sonnet-5, blind, no tools). The preregistered 50-item human calibration
(docs/research/BRIDGE-EXPERIMENT.md; TheoremGraph dropped their first judge
after an expert audit found 5/10 over-graded) remains UNDONE — planned as
future work — so every number here is exploratory, not confirmatory.

Computes:
  - per-arm strict / evaluated equivalence rates on fresh-100 (Wilson 95% CI);
  - conjunction (grounded-typecheck AND judge-evaluated) per arm, with the
    typecheck leg taken from bridge_summary_v2.json's patched paired_matrix;
  - exact-binomial McNemar D-vs-E, D-vs-C, D-vs-A on judge-evaluated (100
    pairs) and on the conjunction;
  - the same tables/McNemars restricted to the 69 tasks arm E completed in the
    original campaign (continuity with tier1_reanalysis.json);
  - self-consistency: strict/evaluated agreement on the fixed 50-item
    re-graded subset (judge_fresh/consistency2/);
  - judge cost / latency / error census.

Run:  python3 bench/analysis/judge_fresh_summary.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
sys.path.insert(0, str(BENCH))
from tier1_reanalysis import mcnemar_exact, wilson_ci  # noqa: E402

JUDGE_ROOT = HERE / "judge_fresh"
CONSISTENCY_ROOT = JUDGE_ROOT / "consistency2"
SUMMARY_V2 = HERE / "bridge_summary_v2.json"
TIER1 = HERE / "tier1_reanalysis.json"
OUT_JSON = HERE / "judge_fresh_summary.json"
OUT_MD = HERE / "judge_fresh_summary.md"
ARMS = ["A", "B", "C", "D", "E"]

HEADER_NOTE = (
    "UNCALIBRATED LLM JUDGE — exploratory numbers only. The judge is "
    "claude-sonnet-5 (blind: sees only informal statement, gold formal "
    "statement + its variable context, and the candidate output; no arm "
    "identity, no tools, empty cwd). The preregistered 50-item human "
    "calibration of judge-vs-human agreement remains UNDONE (planned as "
    "future work), so these rates must not be read as confirmatory. The "
    "grounded-typecheck leg (bridge_summary_v2.json) is the mechanical "
    "anchor; the judge adds an equivalence signal on top."
)


def load_verdicts(root: Path) -> dict[tuple[str, str], dict]:
    out = {}
    for arm in ARMS:
        d = root / arm
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.judge.json")):
            v = json.loads(f.read_text())
            out[(arm, f.name.replace(".judge.json", ""))] = v
    return out


def rate_row(k: int, n: int) -> dict:
    lo, hi = wilson_ci(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None,
            "wilson95": [round(lo, 4), round(hi, 4)]}


def mcn(outcomes: dict[tuple[str, str], bool], ids: list[str], x: str, y: str) -> dict:
    both = xonly = yonly = neither = 0
    for t in ids:
        sx, sy = outcomes[(x, t)], outcomes[(y, t)]
        if sx and sy:
            both += 1
        elif sx:
            xonly += 1
        elif sy:
            yonly += 1
        else:
            neither += 1
    return {"pair": f"{x}_vs_{y}", "n_paired": len(ids), "both_success": both,
            f"{x}_only": xonly, f"{y}_only": yonly, "neither": neither,
            "discordant": xonly + yonly,
            "p_exact_binomial_two_sided": round(mcnemar_exact(xonly, yonly), 6)}


def main() -> int:
    verdicts = load_verdicts(JUDGE_ROOT)
    v2 = json.loads(SUMMARY_V2.read_text())
    tier1 = json.loads(TIER1.read_text())
    fresh_ids = sorted({t for (_, t) in verdicts})
    assert len(fresh_ids) == 100, f"expected 100 fresh tasks, got {len(fresh_ids)}"
    for arm in ARMS:
        missing = [t for t in fresh_ids if (arm, t) not in verdicts]
        assert not missing, f"arm {arm} missing verdicts: {missing[:5]}"

    e31 = set(tier1["e_429_rows"]["task_ids"])
    completed_ids = sorted(set(fresh_ids) - e31)
    assert len(completed_ids) == 69

    # outcome maps
    strict = {(a, t): bool(verdicts[(a, t)].get("strict")) for a in ARMS for t in fresh_ids}
    evald = {(a, t): bool(verdicts[(a, t)].get("evaluated")) for a in ARMS for t in fresh_ids}
    tc = {(a, t): bool(v2["paired_matrix"][t][a]) for a in ARMS for t in fresh_ids}
    conj = {k: tc[k] and evald[k] for k in tc}

    def arm_table(outcomes, ids):
        return {a: rate_row(sum(outcomes[(a, t)] for t in ids), len(ids)) for a in ARMS}

    pairs = ("D_vs_E", "D_vs_C", "D_vs_A", "E_vs_A", "E_vs_B", "E_vs_C")

    def mcn_set(outcomes, ids):
        return {p: mcn(outcomes, ids, *p.split("_vs_")) for p in pairs}

    # error / no-output / cost census
    n_err = sum(1 for v in verdicts.values() if v.get("judge_error"))
    n_noout = sum(1 for v in verdicts.values() if v.get("no_output"))
    n_judged = len(verdicts) - n_noout
    cost = sum(float(v.get("judge_cost_usd") or 0) for v in verdicts.values())
    walls = [v["judge_wall_s"] for v in verdicts.values() if v.get("judge_wall_s")]

    # self-consistency
    consist = load_verdicts(CONSISTENCY_ROOT)
    cons_block = None
    if consist:
        keys = sorted(consist)
        s_agree = sum(strict[k] == bool(consist[k].get("strict")) for k in keys)
        e_agree = sum(evald[k] == bool(consist[k].get("evaluated")) for k in keys)
        cons_cost = sum(float(consist[k].get("judge_cost_usd") or 0) for k in keys)
        cost += cons_cost
        cons_block = {
            "n": len(keys), "seed": 20260727, "stratification": "10 per arm",
            "strict_agreement": round(s_agree / len(keys), 4),
            "evaluated_agreement": round(e_agree / len(keys), 4),
            "strict_disagreements": [f"{a}/{t}" for (a, t) in keys
                                     if strict[(a, t)] != bool(consist[(a, t)].get("strict"))],
            "evaluated_disagreements": [f"{a}/{t}" for (a, t) in keys
                                        if evald[(a, t)] != bool(consist[(a, t)].get("evaluated"))],
            "second_pass_cost_usd": round(cons_cost, 2),
        }

    result = {
        "generated_by": "bench/analysis/judge_fresh_summary.py",
        "header_note": HEADER_NOTE,
        "judge_model": "claude-sonnet-5",
        "subject_model": "claude-haiku-4-5-20251001",
        "typecheck_outcome_source": str(SUMMARY_V2),
        "census": {"verdicts": len(verdicts), "sent_to_judge": n_judged,
                   "no_output_predecided": n_noout, "judge_errors": n_err,
                   "total_judge_cost_usd": round(cost, 2),
                   "mean_judge_wall_s": round(sum(walls) / len(walls), 1) if walls else None},
        "fresh_100": {
            "strict_table": arm_table(strict, fresh_ids),
            "evaluated_table": arm_table(evald, fresh_ids),
            "grounded_typecheck_table_v2": arm_table(tc, fresh_ids),
            "conjunction_table": arm_table(conj, fresh_ids),
            "mcnemar_evaluated": mcn_set(evald, fresh_ids),
            "mcnemar_conjunction": mcn_set(conj, fresh_ids),
        },
        "completed_69": {
            "strict_table": arm_table(strict, completed_ids),
            "evaluated_table": arm_table(evald, completed_ids),
            "conjunction_table": arm_table(conj, completed_ids),
            "mcnemar_evaluated": mcn_set(evald, completed_ids),
            "mcnemar_conjunction": mcn_set(conj, completed_ids),
        },
        "self_consistency": cons_block,
    }
    OUT_JSON.write_text(json.dumps(result, indent=2) + "\n")

    # ------------------------------- markdown -------------------------------
    def tbl(table):
        L = ["| arm | k/n | rate | Wilson 95% CI |", "|---|---|---|---|"]
        for a in ARMS:
            r = table[a]
            L.append(f"| {a} | {r['k']}/{r['n']} | {r['rate']:.3f} "
                     f"| [{r['wilson95'][0]:.3f}, {r['wilson95'][1]:.3f}] |")
        return "\n".join(L)

    def mcns(blocks):
        L = []
        for p in pairs:
            b = blocks[p]
            x, y = p.split("_vs_")
            L.append(f"- **{x} vs {y}**: both={b['both_success']}, "
                     f"{x}-only={b[f'{x}_only']}, {y}-only={b[f'{y}_only']}, "
                     f"neither={b['neither']} -> p = "
                     f"**{b['p_exact_binomial_two_sided']:.4g}**")
        return "\n".join(L)

    md = [
        "# Bridge fresh-set blind LLM-judge equivalence grading (exploratory)\n",
        f"> **{HEADER_NOTE}**\n",
        f"Reproduce: `python3 bench/analysis/judge_fresh_run.py` then "
        f"`python3 bench/analysis/judge_fresh_summary.py`. Judge: "
        f"claude-sonnet-5 over {n_judged} candidate outputs "
        f"({n_noout} no-output rows auto-failed; {n_err} judge errors). "
        f"Total judge cost (CLI-reported): ${round(cost, 2)}.\n",
        "## Fresh-100 — strict equivalence (same proposition, same hypotheses)\n",
        tbl(result["fresh_100"]["strict_table"]),
        "\n## Fresh-100 — evaluated equivalence (mathematical equivalence, "
        "high confidence)\n",
        tbl(result["fresh_100"]["evaluated_table"]),
        "\n## Fresh-100 — conjunction: grounded-typecheck (v2) AND "
        "judge-evaluated\n",
        tbl(result["fresh_100"]["conjunction_table"]),
        "\n## McNemar (exact binomial two-sided), fresh-100 pairs\n",
        "On judge-evaluated equivalence:\n", mcns(result["fresh_100"]["mcnemar_evaluated"]),
        "\nOn the conjunction (typecheck AND evaluated):\n",
        mcns(result["fresh_100"]["mcnemar_conjunction"]),
        "\n## Continuity — completed-69 subset (tasks arm E finished in the "
        "original campaign)\n",
        "Evaluated equivalence:\n", tbl(result["completed_69"]["evaluated_table"]),
        "\nConjunction:\n", tbl(result["completed_69"]["conjunction_table"]),
        "\nMcNemar on judge-evaluated (69 pairs):\n",
        mcns(result["completed_69"]["mcnemar_evaluated"]),
        "\nMcNemar on the conjunction (69 pairs):\n",
        mcns(result["completed_69"]["mcnemar_conjunction"]),
    ]
    if cons_block:
        md += [
            "\n## Self-consistency (fixed 50-item re-grade, seed "
            f"{cons_block['seed']}, {cons_block['stratification']})\n",
            f"- strict agreement: **{cons_block['strict_agreement']:.2%}** "
            f"({len(cons_block['strict_disagreements'])} flips: "
            f"{', '.join(cons_block['strict_disagreements']) or 'none'})",
            f"- evaluated agreement: **{cons_block['evaluated_agreement']:.2%}** "
            f"({len(cons_block['evaluated_disagreements'])} flips: "
            f"{', '.join(cons_block['evaluated_disagreements']) or 'none'})",
        ]
    md.append("")
    OUT_MD.write_text("\n".join(md))
    print(f"wrote {OUT_JSON}\nwrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
