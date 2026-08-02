#!/usr/bin/env python3
"""Judge conjunction under the REPAIRED typecheck instrument.

The blind-judge conjunction reported in judge_fresh_summary.{json,md}
(BRIDGE-REPORT §4.2 layer 3, SUPPLEMENT §S11) used the RAW-instrument
grounded-typecheck leg — the judge pass predates the hallucination-oracle
repair (halluc_validation.py / success_repaired.py). This script recomputes
the conjunction (grounded-typecheck AND judge-evaluated-equivalent) with the
repaired leg and reports both instruments side by side.

Inputs (all committed artifacts; nothing re-run):
  - bridge_summary_v2.json  paired_matrix         raw per-row folded success
  - success_repaired.json   affected_rows_detail  per-row repaired verdicts for
        the 129 raw-flagged/repaired-clean fresh rows (success is monotone
        under the repair, so repaired == raw everywhere else)
  - judge_fresh/<arm>/<task>.judge.json           blind judge verdicts (500)
  - tier1_reanalysis.json   e_429_rows            the completed-69 subset

Deterministic — no sampling, no seed. Exact-binomial McNemar + Wilson CIs
imported from tier1_reanalysis.py (not reimplemented).

Run:  python3 bench/analysis/conjunction_repaired.py
Out:  bench/analysis/conjunction_repaired.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tier1_reanalysis import mcnemar_exact, wilson_ci  # noqa: E402

ARMS = ["A", "B", "C", "D", "E"]
PAIRS = ("D_vs_E", "D_vs_C", "D_vs_A", "E_vs_A", "E_vs_B", "E_vs_C")


def rate_row(k: int, n: int) -> dict:
    lo, hi = wilson_ci(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 4),
            "wilson95": [round(lo, 4), round(hi, 4)]}


def mcn(outcomes: dict, ids: list[str], x: str, y: str) -> dict:
    b = sum(1 for t in ids if outcomes[(x, t)] and not outcomes[(y, t)])
    c = sum(1 for t in ids if outcomes[(y, t)] and not outcomes[(x, t)])
    both = sum(1 for t in ids if outcomes[(x, t)] and outcomes[(y, t)])
    return {"both": both, f"{x}_only": b, f"{y}_only": c,
            "discordant": b + c,
            "p_exact_binomial_two_sided": float(f"{mcnemar_exact(b, c):.3g}")}


def main() -> int:
    v2 = json.loads((HERE / "bridge_summary_v2.json").read_text())
    rep = json.loads((HERE / "success_repaired.json").read_text())
    tier1 = json.loads((HERE / "tier1_reanalysis.json").read_text())
    jfs = json.loads((HERE / "judge_fresh_summary.json").read_text())

    fresh_ids = sorted(t for t in v2["paired_matrix"] if t.startswith("fresh_"))
    assert len(fresh_ids) == 100

    # --- typecheck legs ------------------------------------------------------
    raw_tc = {(a, t): bool(v2["paired_matrix"][t][a])
              for a in ARMS for t in fresh_ids}
    rep_tc = dict(raw_tc)
    for row in rep["affected_rows_detail"]:
        k = (row["arm"], row["task_id"])
        assert raw_tc[k] == bool(row["raw_folded_success"])
        rep_tc[k] = bool(row["repaired_folded_success"])
    # repaired per-arm totals must match success_repaired.json's table
    for a in ARMS:
        want = rep["fresh_100_table_both_instruments"]["repaired_oracle"][a]["k"]
        got = sum(rep_tc[(a, t)] for t in fresh_ids)
        assert got == want, (a, got, want)

    # --- judge leg -----------------------------------------------------------
    evald = {}
    for a in ARMS:
        for t in fresh_ids:
            v = json.loads((HERE / "judge_fresh" / a / f"{t}.judge.json").read_text())
            evald[(a, t)] = bool(v.get("evaluated"))

    conj_raw = {k: raw_tc[k] and evald[k] for k in raw_tc}
    conj_rep = {k: rep_tc[k] and evald[k] for k in rep_tc}
    # raw conjunction must replicate judge_fresh_summary.json exactly
    for a in ARMS:
        want = jfs["fresh_100"]["conjunction_table"][a]["k"]
        got = sum(conj_raw[(a, t)] for t in fresh_ids)
        assert got == want, (a, got, want)

    completed = sorted(set(fresh_ids) - set(tier1["e_429_rows"]["task_ids"]))
    assert len(completed) == 69

    def block(ids):
        return {
            "raw_conjunction": {a: rate_row(sum(conj_raw[(a, t)] for t in ids),
                                            len(ids)) for a in ARMS},
            "repaired_conjunction": {a: rate_row(sum(conj_rep[(a, t)] for t in ids),
                                                 len(ids)) for a in ARMS},
            "mcnemar_raw": {p: mcn(conj_raw, ids, *p.split("_vs_")) for p in PAIRS},
            "mcnemar_repaired": {p: mcn(conj_rep, ids, *p.split("_vs_")) for p in PAIRS},
        }

    flipped = sorted(f"{a}/{t}" for a in ARMS for t in fresh_ids
                     if conj_rep[(a, t)] and not conj_raw[(a, t)])

    f100 = block(fresh_ids)

    def cls(p_):
        return "sig" if p_ < .05 else "ns"

    out = {
        "generated_by": "bench/analysis/conjunction_repaired.py",
        "what": "grounded-typecheck AND judge-evaluated, raw vs repaired "
                "typecheck instrument; judge leg identical in both",
        "deterministic": "no sampling; seed n/a",
        "sources": ["bridge_summary_v2.json", "success_repaired.json",
                    "judge_fresh/", "tier1_reanalysis.json"],
        "fresh_100": f100,
        "completed_69": block(completed),
        "rows_flipped_by_repair": flipped,
        "classification_changes_alpha05": {
            p: {"raw_p": (rp := f100["mcnemar_raw"][p]
                          ["p_exact_binomial_two_sided"]),
                "repaired_p": (qp := f100["mcnemar_repaired"][p]
                               ["p_exact_binomial_two_sided"]),
                "change": f"{cls(rp)} -> {cls(qp)}"}
            for p in PAIRS},
    }
    (HERE / "conjunction_repaired.json").write_text(
        json.dumps(out, indent=2) + "\n")

    print("fresh-100 conjunction, raw -> repaired (k/100):")
    for a in ARMS:
        print(f"  {a}: {f100['raw_conjunction'][a]['k']} -> "
              f"{f100['repaired_conjunction'][a]['k']}  "
              f"wilson95 {f100['repaired_conjunction'][a]['wilson95']}")
    print("McNemar (raw p -> repaired p):")
    for p in PAIRS:
        print(f"  {p}: {f100['mcnemar_raw'][p]['p_exact_binomial_two_sided']} -> "
              f"{f100['mcnemar_repaired'][p]['p_exact_binomial_two_sided']}  "
              f"(repaired b/c {list(f100['mcnemar_repaired'][p].values())[1]}/"
              f"{list(f100['mcnemar_repaired'][p].values())[2]})")
    print(f"rows flipped by repair: {len(flipped)}: {flipped}")
    c69 = out["completed_69"]
    print("completed-69 repaired conjunction:",
          {a: c69['repaired_conjunction'][a]['k'] for a in ARMS})
    print("wrote", HERE / "conjunction_repaired.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
